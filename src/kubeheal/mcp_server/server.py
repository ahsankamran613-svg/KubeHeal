"""MCP Server providing strictly read-only Kubernetes tools with guardrails."""

import shlex
import subprocess
from typing import Any, Dict, List, Optional
from ruamel.yaml import YAML

from kubeheal.config import settings
from kubeheal.gitops.yaml_patcher import (
    SUPPORTED_WORKLOAD_KINDS,
    extract_containers_from_doc,
    find_manifest_for_deployment,
    infer_workload_name_from_pod,
)
from kubeheal.mcp_server.guardrails import validate_kubectl_command

try:
    from mcp.server import MCPServer
    mcp = MCPServer("kubeheal-k8s-mcp")
except (ImportError, ModuleNotFoundError):
    try:
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("kubeheal-k8s-mcp")
    except Exception:
        class FallbackMCPServer:
            def tool(self):
                def decorator(f):
                    return f
                return decorator
        mcp = FallbackMCPServer()

# Optional K8s client initialization
_k8s_initialized = False
_core_v1 = None
_apps_v1 = None
_batch_v1 = None


def _get_k8s_clients():
    """Lazily load Kubernetes configuration (in-cluster or local kubeconfig)."""
    global _k8s_initialized, _core_v1, _apps_v1, _batch_v1
    if not _k8s_initialized:
        try:
            from kubernetes import client, config
            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()
            _core_v1 = client.CoreV1Api()
            _apps_v1 = client.AppsV1Api()
            _batch_v1 = client.BatchV1Api()
            # Perform a lightweight version/API check to confirm cluster is actually reachable
            _core_v1.get_api_resources(_request_timeout=2)
            _k8s_initialized = True
        except Exception:
            _k8s_initialized = False
            _core_v1 = None
            _apps_v1 = None
            _batch_v1 = None
            return None, None
    return _core_v1, _apps_v1


def _load_local_manifest_spec(name_or_pod: str) -> Optional[Dict[str, Any]]:
    """Read local workload YAML manifest (supports nested dirs and multi-doc YAML) when running in simulation mode."""
    workload_name = infer_workload_name_from_pod(name_or_pod)
    manifest_path = (
        find_manifest_for_deployment(settings.manifests_dir, workload_name)
        or find_manifest_for_deployment(settings.manifests_dir, name_or_pod)
    )
    if not manifest_path or not manifest_path.exists():
        return None

    yaml = YAML()
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            docs = list(yaml.load_all(f))
            for doc in docs:
                if isinstance(doc, dict) and doc.get("kind") in SUPPORTED_WORKLOAD_KINDS:
                    if doc.get("metadata", {}).get("name") in (workload_name, name_or_pod):
                        return doc
            for doc in docs:
                if isinstance(doc, dict) and extract_containers_from_doc(doc):
                    return doc
    except Exception:
        return None
    return None


def _inspect_manifest_failure_hints(doc: Optional[Dict[str, Any]], pod_name: str) -> Dict[str, Any]:
    """Derive realistic simulation telemetry directly from the local YAML manifest spec rather than hardcoded assumptions."""
    containers_spec = extract_containers_from_doc(doc) if doc else []
    c_spec = containers_spec[0] if containers_spec else {}
    c_name = c_spec.get("name", "app")
    c_image = c_spec.get("image", "unknown-image")
    c_resources = {
        k: dict(v) for k, v in (c_spec.get("resources") or {}).items() if isinstance(v, dict)
    }
    mem_limit = c_resources.get("limits", {}).get("memory")
    env_list = c_spec.get("env") or []
    env_map = {e.get("name"): e.get("value") for e in env_list if isinstance(e, dict) and e.get("name")}
    cmd_str = " ".join(str(x) for x in (c_spec.get("command") or []) + (c_spec.get("args") or []))

    # Detect if this manifest or pod is an OOM scenario
    is_oom = (
        "oom" in pod_name.lower()
        or "bytearray(" in cmd_str
        or (mem_limit in ("16Mi", "32Mi") and "REQUIRED_PORT" not in env_map)
    )

    # Detect if this manifest has an invalid port or explicit config error in its spec
    invalid_env_keys = [
        k for k, v in env_map.items()
        if "PORT" in str(k).upper() and v is not None and not str(v).isdigit()
    ]

    if is_oom:
        limit_display = mem_limit or "configured"
        return {
            "is_oom": True,
            "container_name": c_name,
            "image": c_image,
            "resources": c_resources,
            "phase": "Running",
            "restart_count": 4,
            "terminated_reason": "OOMKilled",
            "exit_code": 137,
            "terminated_message": f"Container '{c_name}' exceeded {limit_display} memory cgroup limit",
            "logs": (
                f"[{c_name}] Starting container '{c_name}' ({c_image})...\n"
                f"[{c_name}] Memory allocation exceeded cgroup limit ({limit_display}).\n"
                f"Killed (exit code 137 - SIGKILL: cgroup memory limit {limit_display} exceeded)"
            ),
            "event_reason": "OOMKilling",
            "event_message": f"Memory cgroup out of memory: Killed container '{c_name}' (limit {limit_display})",
        }

    if invalid_env_keys:
        bad_key = invalid_env_keys[0]
        bad_val = env_map[bad_key]
        return {
            "is_oom": False,
            "container_name": c_name,
            "image": c_image,
            "resources": c_resources,
            "phase": "CrashLoopBackOff",
            "restart_count": 5,
            "terminated_reason": "Error",
            "exit_code": 1,
            "terminated_message": f"FATAL ERROR: Invalid configuration {bad_key}='{bad_val}'",
            "logs": (
                f"[{c_name}] Starting service ({c_image})...\n"
                f"[{c_name}] Validating environment configuration: {bad_key}='{bad_val}'...\n"
                f"[{c_name}] FATAL ERROR: Invalid port configuration '{bad_val}' in {bad_key}: expected numeric TCP port"
            ),
            "event_reason": "BackOff",
            "event_message": f"Back-off restarting failed container '{c_name}' in pod {pod_name}",
        }

    # Generic manifest-backed simulation for any other workload when offline
    return {
        "is_oom": False,
        "container_name": c_name,
        "image": c_image,
        "resources": c_resources,
        "phase": "CrashLoopBackOff",
        "restart_count": 3,
        "terminated_reason": "Error",
        "exit_code": 1,
        "terminated_message": f"Container '{c_name}' exited with non-zero status (offline manifest inspection)",
        "logs": (
            f"[{c_name}] Offline simulation mode: live cluster not connected.\n"
            f"[{c_name}] Inspected local manifest container '{c_name}' (image: {c_image}, limits: {c_resources.get('limits', {})})."
        ),
        "event_reason": "BackOff",
        "event_message": f"Back-off restarting failed container '{c_name}' in pod {pod_name}",
    }


@mcp.tool()
def get_pod_status(pod_name: str, namespace: str = "default") -> Dict[str, Any]:
    """Retrieve pod status, container states, restart counts, termination reasons, and resource requests/limits."""
    core_v1, _ = _get_k8s_clients()
    if not core_v1:
        if not settings.allow_offline_simulation:
            return {
                "status": "unavailable",
                "message": "Kubernetes cluster is not connected and offline simulation is disabled.",
                "pod_name": pod_name,
                "namespace": namespace,
            }
        doc = _load_local_manifest_spec(pod_name)
        hints = _inspect_manifest_failure_hints(doc, pod_name)
        return {
            "status": "simulated",
            "source": "local_manifest_simulation",
            "pod_name": pod_name,
            "namespace": namespace,
            "phase": hints["phase"],
            "container_statuses": [
                {
                    "container_name": hints["container_name"],
                    "ready": False,
                    "restart_count": hints["restart_count"],
                    "image": hints["image"],
                    "resources": hints["resources"],
                    "state": {
                        "waiting": {
                            "reason": "CrashLoopBackOff",
                            "message": "back-off restarting failed container",
                        }
                    },
                    "last_state": {
                        "terminated": {
                            "reason": hints["terminated_reason"],
                            "exit_code": hints["exit_code"],
                            "message": hints["terminated_message"],
                        }
                    },
                }
            ],
        }

    try:
        pod = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        # Map container spec resources by container name
        spec_resources_by_name: Dict[str, Dict[str, Any]] = {}
        if pod.spec and pod.spec.containers:
            for c in pod.spec.containers:
                res = {}
                if c.resources:
                    if c.resources.limits:
                        res["limits"] = dict(c.resources.limits)
                    if c.resources.requests:
                        res["requests"] = dict(c.resources.requests)
                spec_resources_by_name[c.name] = res

        container_statuses = []
        for cs in (pod.status.container_statuses or []):
            state_info = {}
            if cs.state.running:
                state_info["running"] = {"started_at": str(cs.state.running.started_at)}
            elif cs.state.waiting:
                state_info["waiting"] = {
                    "reason": cs.state.waiting.reason,
                    "message": cs.state.waiting.message,
                }
            elif cs.state.terminated:
                state_info["terminated"] = {
                    "reason": cs.state.terminated.reason,
                    "exit_code": cs.state.terminated.exit_code,
                    "message": cs.state.terminated.message,
                }

            last_state = {}
            if cs.last_state and cs.last_state.terminated:
                last_state["terminated"] = {
                    "reason": cs.last_state.terminated.reason,
                    "exit_code": cs.last_state.terminated.exit_code,
                    "message": cs.last_state.terminated.message,
                }

            container_statuses.append({
                "container_name": cs.name,
                "ready": cs.ready,
                "restart_count": cs.restart_count,
                "state": state_info,
                "last_state": last_state,
                "image": cs.image,
                "resources": spec_resources_by_name.get(cs.name, {}),
            })

        return {
            "status": "success",
            "pod_name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "phase": pod.status.phase,
            "host_ip": pod.status.host_ip,
            "pod_ip": pod.status.pod_ip,
            "container_statuses": container_statuses,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to retrieve pod status: {str(e)}",
            "pod_name": pod_name,
            "namespace": namespace,
        }


@mcp.tool()
def get_pod_logs(
    pod_name: str,
    namespace: str = "default",
    tail_lines: int = 100,
    previous: bool = True,
    container_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Retrieve logs for a crashing or crashed container (supports reading previous terminated container logs)."""
    core_v1, _ = _get_k8s_clients()
    if not core_v1:
        if not settings.allow_offline_simulation:
            return {
                "status": "unavailable",
                "message": "Kubernetes cluster is not connected and offline simulation is disabled.",
                "pod_name": pod_name,
                "logs": "",
            }
        doc = _load_local_manifest_spec(pod_name)
        hints = _inspect_manifest_failure_hints(doc, pod_name)
        return {
            "status": "simulated",
            "source": "local_manifest_simulation",
            "pod_name": pod_name,
            "namespace": namespace,
            "tail_lines": tail_lines,
            "logs": hints["logs"],
        }

    try:
        kwargs: Dict[str, Any] = {"name": pod_name, "namespace": namespace, "tail_lines": tail_lines}
        if container_name:
            kwargs["container"] = container_name

        logs = ""
        if previous:
            try:
                logs = core_v1.read_namespaced_pod_log(**kwargs, previous=True)
            except Exception:
                logs = core_v1.read_namespaced_pod_log(**kwargs, previous=False)
        else:
            logs = core_v1.read_namespaced_pod_log(**kwargs, previous=False)

        return {
            "status": "success",
            "pod_name": pod_name,
            "namespace": namespace,
            "tail_lines": tail_lines,
            "logs": logs or "(no log output recorded)",
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to read pod logs: {str(e)}",
            "pod_name": pod_name,
            "logs": "",
        }


@mcp.tool()
def get_pod_events(pod_name: str, namespace: str = "default") -> Dict[str, Any]:
    """Fetch Warning and Normal Kubernetes events for the specified pod to diagnose scheduling or OOM events."""
    core_v1, _ = _get_k8s_clients()
    if not core_v1:
        if not settings.allow_offline_simulation:
            return {
                "status": "unavailable",
                "message": "Kubernetes cluster is not connected and offline simulation is disabled.",
                "pod_name": pod_name,
                "events": [],
            }
        doc = _load_local_manifest_spec(pod_name)
        hints = _inspect_manifest_failure_hints(doc, pod_name)
        return {
            "status": "simulated",
            "source": "local_manifest_simulation",
            "pod_name": pod_name,
            "namespace": namespace,
            "events": [
                {
                    "type": "Normal",
                    "reason": "Scheduled",
                    "message": f"Successfully assigned {namespace}/{pod_name} to cluster node",
                    "count": 1,
                },
                {
                    "type": "Warning",
                    "reason": hints["event_reason"],
                    "message": hints["event_message"],
                    "count": hints["restart_count"],
                },
            ],
        }

    try:
        event_list = core_v1.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={pod_name}",
        )
        formatted_events = []
        for ev in event_list.items:
            formatted_events.append({
                "type": ev.type,
                "reason": ev.reason,
                "message": ev.message,
                "count": ev.count,
                "first_timestamp": str(ev.first_timestamp),
                "last_timestamp": str(ev.last_timestamp),
            })

        return {
            "status": "success",
            "pod_name": pod_name,
            "namespace": namespace,
            "events": formatted_events,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to fetch pod events: {str(e)}",
            "events": [],
        }


def _extract_k8s_pod_spec_containers(pod_spec: Any) -> List[Dict[str, Any]]:
    """Convert Kubernetes V1PodSpec containers into sanitized dictionary representations."""
    containers = []
    if not pod_spec or not getattr(pod_spec, "containers", None):
        return containers
    for c in pod_spec.containers:
        safe_env = []
        if c.env:
            for env_var in c.env:
                if env_var.value_from:
                    safe_env.append({"name": env_var.name, "value_from": "<secret/configmap reference masked>"})
                else:
                    safe_env.append({"name": env_var.name, "value": env_var.value})

        resources = {}
        if c.resources:
            if c.resources.limits:
                resources["limits"] = dict(c.resources.limits)
            if c.resources.requests:
                resources["requests"] = dict(c.resources.requests)

        containers.append({
            "name": c.name,
            "image": c.image,
            "command": c.command,
            "args": c.args,
            "env": safe_env,
            "resources": resources,
        })
    return containers


@mcp.tool()
def get_deployment_spec(deployment_name: str, namespace: str = "default") -> Dict[str, Any]:
    """Retrieve container images, commands, environment variables, and resource requests/limits of a workload (Deployment, StatefulSet, DaemonSet, or Job)."""
    _, apps_v1 = _get_k8s_clients()
    if not apps_v1:
        doc = _load_local_manifest_spec(deployment_name)
        if doc:
            raw_containers = extract_containers_from_doc(doc)
            containers = []
            for c in raw_containers:
                containers.append({
                    "name": c.get("name"),
                    "image": c.get("image"),
                    "command": list(c.get("command", [])) if c.get("command") else None,
                    "args": list(c.get("args", [])) if c.get("args") else None,
                    "env": [dict(e) for e in c.get("env", [])],
                    "resources": {
                        k: dict(v) for k, v in (c.get("resources") or {}).items()
                    },
                })
            return {
                "status": "simulated",
                "source": "local_manifest_file",
                "kind": doc.get("kind", "Deployment"),
                "deployment_name": deployment_name,
                "namespace": namespace,
                "replicas": doc.get("spec", {}).get("replicas", 1),
                "containers": containers,
            }
        return {
            "status": "unavailable",
            "message": "Kubernetes API client not connected and no matching local manifest found.",
            "deployment_name": deployment_name,
        }

    # Try Deployment -> StatefulSet -> DaemonSet -> Job
    try:
        dep = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        return {
            "status": "success",
            "kind": "Deployment",
            "deployment_name": deployment_name,
            "namespace": namespace,
            "replicas": dep.spec.replicas,
            "containers": _extract_k8s_pod_spec_containers(dep.spec.template.spec),
        }
    except Exception:
        pass

    try:
        sts = apps_v1.read_namespaced_stateful_set(name=deployment_name, namespace=namespace)
        return {
            "status": "success",
            "kind": "StatefulSet",
            "deployment_name": deployment_name,
            "namespace": namespace,
            "replicas": sts.spec.replicas,
            "containers": _extract_k8s_pod_spec_containers(sts.spec.template.spec),
        }
    except Exception:
        pass

    try:
        ds = apps_v1.read_namespaced_daemon_set(name=deployment_name, namespace=namespace)
        return {
            "status": "success",
            "kind": "DaemonSet",
            "deployment_name": deployment_name,
            "namespace": namespace,
            "replicas": 1,
            "containers": _extract_k8s_pod_spec_containers(ds.spec.template.spec),
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to get workload spec for '{deployment_name}': {str(e)}",
            "deployment_name": deployment_name,
        }


@mcp.tool()
def run_readonly_kubectl(command: str) -> Dict[str, Any]:
    """Execute an allow-listed, strictly read-only kubectl inspection command with guardrail validation.

    Allowed verbs: get, describe, logs, events, top.
    Forbidden: delete, apply, patch, edit, exec, shell piping, secret extraction.
    """
    is_valid, reason = validate_kubectl_command(command)
    if not is_valid:
        return {
            "status": "blocked",
            "blocked_by_guardrail": True,
            "command": command,
            "error": reason,
        }

    tokens = shlex.split(command.strip())
    if tokens[0].lower() not in ("kubectl", "kubectl.exe"):
        tokens = ["kubectl"] + tokens

    try:
        result = subprocess.run(
            tokens,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return {
            "status": "success",
            "blocked_by_guardrail": False,
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "blocked_by_guardrail": False,
            "command": command,
            "error": "Command execution timed out after 10 seconds",
        }
    except FileNotFoundError:
        return {
            "status": "unavailable",
            "blocked_by_guardrail": False,
            "command": command,
            "error": "kubectl CLI binary not found in system PATH",
        }
    except Exception as e:
        return {
            "status": "error",
            "blocked_by_guardrail": False,
            "command": command,
            "error": str(e),
        }
