"""FastMCP Server providing strictly read-only Kubernetes tools with guardrails."""

import subprocess
from typing import Any, Dict, List, Optional
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
            _k8s_initialized = True
        except Exception as e:
            # Not in cluster and no local kubeconfig active
            _k8s_initialized = False
            return None, None
    return _core_v1, _apps_v1


@mcp.tool()
def get_pod_status(pod_name: str, namespace: str = "default") -> Dict[str, Any]:
    """Retrieve pod status, container states, restart counts, and termination reasons (e.g. OOMKilled, Error)."""
    core_v1, _ = _get_k8s_clients()
    if not core_v1:
        return {
            "status": "unavailable",
            "message": "Kubernetes API client not connected. Pod status unavailable from live cluster.",
            "pod_name": pod_name,
            "namespace": namespace,
        }

    try:
        pod = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
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
    container_name: Optional[str] = None
) -> Dict[str, Any]:
    """Retrieve logs for a crashing or crashed container (supports reading previous terminated container logs)."""
    core_v1, _ = _get_k8s_clients()
    if not core_v1:
        return {
            "status": "unavailable",
            "message": "Kubernetes API client not connected.",
            "pod_name": pod_name,
            "logs": "",
        }

    try:
        kwargs = {"name": pod_name, "namespace": namespace, "tail_lines": tail_lines}
        if container_name:
            kwargs["container"] = container_name

        logs = ""
        # First attempt to read previous crashed container logs if requested
        if previous:
            try:
                logs = core_v1.read_namespaced_pod_log(**kwargs, previous=True)
            except Exception:
                # Fall back to current logs if previous is not available
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
        return {
            "status": "unavailable",
            "message": "Kubernetes API client not connected.",
            "events": [],
        }

    try:
        event_list = core_v1.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={pod_name}"
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
        return {
            "status": "unavailable",
            "message": "Kubernetes API client not connected.",
            "deployment_name": deployment_name,
        }

    try:
        dep = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        containers = []
        for c in dep.spec.template.spec.containers:
            # Mask secret env references for defense in depth
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

    import shlex
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
