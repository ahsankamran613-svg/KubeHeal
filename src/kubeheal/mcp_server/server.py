"""MCP Server providing strictly read-only Kubernetes tools with guardrails."""

import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from ruamel.yaml import YAML

from kubeheal.config import settings
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


def _get_k8s_clients():
    """Lazily load Kubernetes configuration (in-cluster or local kubeconfig)."""
    global _k8s_initialized, _core_v1, _apps_v1
    if not _k8s_initialized:
        try:
            from kubernetes import client, config
            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()
            _core_v1 = client.CoreV1Api()
            _apps_v1 = client.AppsV1Api()
            # Perform a lightweight version/API check to confirm cluster is actually reachable
            _core_v1.get_api_resources(_request_timeout=2)
            _k8s_initialized = True
        except Exception:
            _k8s_initialized = False
            _core_v1 = None
            _apps_v1 = None
            return None, None
    return _core_v1, _apps_v1


def _load_local_manifest_spec(name_prefix: str) -> Optional[Dict[str, Any]]:
    """Read local workload YAML manifest when running in simulation / disconnected mode."""
    manifests_dir = Path(settings.manifests_dir)
    if not manifests_dir.exists():
        return None
    yaml = YAML()
    for p in manifests_dir.glob("*.y*ml"):
        if p.stem in name_prefix or name_prefix.startswith(p.stem):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return yaml.load(f)
            except Exception:
                return None
    return None


@mcp.tool()
def get_pod_status(pod_name: str, namespace: str = "default") -> Dict[str, Any]:
    """Retrieve pod status, container states, restart counts, termination reasons, and resource requests/limits."""
    core_v1, _ = _get_k8s_clients()
    if not core_v1:
        # Provide local manifest-backed simulation telemetry when k3d is offline
        doc = _load_local_manifest_spec(pod_name)
        containers_spec = (doc or {}).get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        c_spec = containers_spec[0] if containers_spec else {}
        c_name = c_spec.get("name", "app")
        c_resources = dict(c_spec.get("resources", {}))

        is_oom = "oom" in pod_name.lower()
        return {
            "status": "simulated",
            "source": "local_manifest_simulation",
            "pod_name": pod_name,
            "namespace": namespace,
            "phase": "Running" if is_oom else "CrashLoopBackOff",
            "container_statuses": [
                {
                    "container_name": c_name,
                    "ready": False,
                    "restart_count": 4 if is_oom else 6,
                    "image": c_spec.get("image", "python:3.12-alpine"),
                    "resources": c_resources,
                    "state": {
                        "waiting": {
                            "reason": "CrashLoopBackOff",
                            "message": "back-off 5m0s restarting failed container",
                        }
                    },
                    "last_state": {
                        "terminated": {
                            "reason": "OOMKilled" if is_oom else "Error",
                            "exit_code": 137 if is_oom else 1,
                            "message": (
                                "Container exceeded 32Mi memory cgroup limit"
                                if is_oom
                                else "FATAL ERROR: Invalid port configuration 'invalid_port'"
                            ),
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
        is_oom = "oom" in pod_name.lower()
        simulated_logs = (
            "[oom-worker] Worker starting up...\n"
            "[oom-worker] Allocating 65MB memory chunk...\n"
            "Killed (exit code 137 - SIGKILL: cgroup memory limit 32Mi exceeded)"
            if is_oom
            else "[config-app] Booting API service...\n"
            "[config-app] Validating REQUIRED_PORT='invalid_port', MEMORY_LIMIT='16Mi'...\n"
            "[config-app] FATAL ERROR: Invalid port configuration 'invalid_port': invalid literal for int() with base 10: 'invalid_port'"
        )
        return {
            "status": "simulated",
            "source": "local_manifest_simulation",
            "pod_name": pod_name,
            "namespace": namespace,
            "tail_lines": tail_lines,
            "logs": simulated_logs,
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
        is_oom = "oom" in pod_name.lower()
        return {
            "status": "simulated",
            "source": "local_manifest_simulation",
            "pod_name": pod_name,
            "namespace": namespace,
            "events": [
                {
                    "type": "Normal",
                    "reason": "Scheduled",
                    "message": f"Successfully assigned {namespace}/{pod_name} to k3d-kubeheal-cluster-agent-0",
                    "count": 1,
                },
                {
                    "type": "Warning",
                    "reason": "OOMKilling" if is_oom else "BackOff",
                    "message": (
                        "Memory cgroup out of memory: Killed process (python) total-vm:72400kB, anon-rss:32768kB"
                        if is_oom
                        else f"Back-off restarting failed container in pod {pod_name}_{namespace}"
                    ),
                    "count": 4 if is_oom else 6,
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


@mcp.tool()
def get_deployment_spec(deployment_name: str, namespace: str = "default") -> Dict[str, Any]:
    """Retrieve container images, commands, environment variables, and resource requests/limits of a deployment."""
    _, apps_v1 = _get_k8s_clients()
    if not apps_v1:
        doc = _load_local_manifest_spec(deployment_name)
        if doc:
            raw_containers = doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
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

    try:
        dep = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        containers = []
        for c in dep.spec.template.spec.containers:
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
                "image": c.name and c.image,
                "command": c.command,
                "args": c.args,
                "env": safe_env,
                "resources": resources,
            })

        return {
            "status": "success",
            "deployment_name": deployment_name,
            "namespace": namespace,
            "replicas": dep.spec.replicas,
            "containers": containers,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to get deployment spec: {str(e)}",
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
