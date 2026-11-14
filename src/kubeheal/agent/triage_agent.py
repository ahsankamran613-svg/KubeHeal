"""AI Triage Agent orchestrating Groq LLM (openai/gpt-oss-120b) and MCP read-only tools."""

import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kubeheal.config import settings
from kubeheal.gitops.github_pr import create_remediation_proposal
from kubeheal.gitops.yaml_patcher import (
    clamp_memory_limit,
    find_manifest_for_deployment,
    format_bytes_to_k8s_memory,
    infer_workload_name_from_pod,
    parse_memory_to_bytes,
    patch_deployment_yaml,
    select_target_container,
)
from kubeheal.mcp_server.guardrails import validate_kubectl_command
from kubeheal.mcp_server.server import (
    get_deployment_spec,
    get_pod_events,
    get_pod_logs,
    get_pod_status,
    mcp,
    run_readonly_kubectl,
)
from kubeheal.models import AlertPayload, MCPToolExecution, TriageReport, YAMLModification

logger = logging.getLogger(__name__)

SUBMIT_SOLUTION_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_triage_solution",
        "description": "Conclude the investigation, supply the root cause diagnosis, confidence score, and proposed YAML manifest modifications.",
        "parameters": {
            "type": "object",
            "properties": {
                "root_cause_summary": {
                    "type": "string",
                    "description": "Clear explanation of why the pod crashed and how the fix resolves it.",
                },
                "confidence": {
                    "type": "number",
                    "description": "Confidence score between 0.0 and 1.0",
                },
                "modifications": {
                    "type": "array",
                    "description": "List of targeted YAML adjustments to apply to the manifest",
                    "items": {
                        "type": "object",
                        "properties": {
                            "container_name": {
                                "type": "string",
                                "description": "Name of the target container to modify (optional if single container)",
                            },
                            "parameter_path": {
                                "type": "string",
                                "description": "Target YAML path, e.g. 'resources.limits.memory', 'resources.requests.memory', 'image', 'command', 'args', or 'env[<VAR_NAME>]'",
                            },
                            "old_value": {"type": "string", "description": "Existing value"},
                            "new_value": {"type": "string", "description": "Recommended new value"},
                            "rationale": {"type": "string", "description": "Reason for changing this parameter"},
                        },
                        "required": ["parameter_path", "new_value", "rationale"],
                    },
                },
            },
            "required": ["root_cause_summary", "confidence", "modifications"],
        },
    },
}


async def discover_mcp_tools_for_groq() -> List[Dict[str, Any]]:
    """Dynamically discover tools registered on the MCPServer and convert them to Groq/OpenAI tool-calling schema."""
    groq_tools: List[Dict[str, Any]] = []
    try:
        mcp_tools = await mcp.list_tools()
        for tool in mcp_tools:
            schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", {})
            groq_tools.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": (tool.description or "").strip(),
                    "parameters": schema,
                },
            })
    except Exception as e:
        logger.warning(f"Dynamic MCP tool discovery fallback triggered: {e}")

    groq_tools.append(SUBMIT_SOLUTION_TOOL_SCHEMA)
    return groq_tools


async def execute_mcp_tool_call(name: str, args: Dict[str, Any]) -> Tuple[Dict[str, Any], MCPToolExecution]:
    """Execute a tool through the MCPServer protocol handler and return result + audit trace."""
    trace = MCPToolExecution(
        tool_name=name,
        arguments=args,
        status="success",
        blocked_by_guardrail=False,
    )

    result: Dict[str, Any] = {}

    try:
        if name == "run_readonly_kubectl":
            cmd = args.get("command", "")
            is_valid, reason = validate_kubectl_command(cmd)
            if not is_valid:
                trace.blocked_by_guardrail = True
                trace.status = "blocked"
                result = {"status": "blocked", "blocked_by_guardrail": True, "command": cmd, "error": reason}
                trace.output_preview = json.dumps(result)[:250]
                return result, trace

        # Invoke through MCPServer.call_tool()
        if hasattr(mcp, "call_tool"):
            mcp_res = await mcp.call_tool(name, args)
            if getattr(mcp_res, "structured_content", None) and "result" in mcp_res.structured_content:
                result = mcp_res.structured_content["result"]
            elif getattr(mcp_res, "content", None) and len(mcp_res.content) > 0:
                text_payload = getattr(mcp_res.content[0], "text", "{}")
                result = json.loads(text_payload)
            else:
                result = {"status": "unknown_mcp_response"}
        else:
            # Direct function fallback
            result = _direct_tool_fallback(name, args)

        if isinstance(result, dict) and result.get("blocked_by_guardrail"):
            trace.blocked_by_guardrail = True
            trace.status = "blocked"
        elif isinstance(result, dict) and result.get("status") == "error":
            trace.status = "error"

    except Exception as e:
        result = {"error": str(e)}
        trace.status = "error"

    output_str = json.dumps(result)
    trace.output_preview = output_str[:250]
    return result, trace


def _direct_tool_fallback(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if name == "get_pod_status":
        return get_pod_status(pod_name=args.get("pod_name", ""), namespace=args.get("namespace", "default"))
    if name == "get_pod_logs":
        return get_pod_logs(
            pod_name=args.get("pod_name", ""),
            namespace=args.get("namespace", "default"),
            tail_lines=int(args.get("tail_lines", 100)),
            previous=bool(args.get("previous", True)),
            container_name=args.get("container_name"),
        )
    if name == "get_pod_events":
        return get_pod_events(pod_name=args.get("pod_name", ""), namespace=args.get("namespace", "default"))
    if name == "get_deployment_spec":
        return get_deployment_spec(
            deployment_name=args.get("deployment_name", ""),
            namespace=args.get("namespace", "default"),
        )
    if name == "run_readonly_kubectl":
        return run_readonly_kubectl(command=args.get("command", ""))
    return {"error": f"Unknown tool: {name}"}


def _compute_scaled_oom_memory(current_limit_str: Optional[str], current_req_str: Optional[str]) -> Tuple[str, str, str, str]:
    """Dynamically calculate scaled memory limit and request based on the container's actual current resources."""
    old_limit = current_limit_str or "32Mi"
    old_req = current_req_str or "16Mi"

    try:
        limit_bytes = parse_memory_to_bytes(old_limit)
    except Exception:
        limit_bytes = 32 * 1024 * 1024

    # Scale 4x for small limits (<128Mi), 2x for larger limits
    if limit_bytes < 128 * 1024 * 1024:
        proposed_limit_bytes = limit_bytes * 4
    else:
        proposed_limit_bytes = limit_bytes * 2

    raw_new_limit = format_bytes_to_k8s_memory(proposed_limit_bytes)
    new_limit, _ = clamp_memory_limit(raw_new_limit, current_limit_str=old_limit)

    try:
        final_limit_bytes = parse_memory_to_bytes(new_limit)
        new_req_bytes = max(16 * 1024 * 1024, final_limit_bytes // 2)
        new_req = format_bytes_to_k8s_memory(new_req_bytes)
    except Exception:
        new_req = "64Mi"

    return old_limit, new_limit, old_req, new_req


async def _deterministic_fallback_triage(
    alert: AlertPayload,
    manifest_path: Optional[Path],
) -> Tuple[str, float, List[YAMLModification], List[MCPToolExecution]]:
    """Intelligent deterministic diagnostic engine that inspects actual MCP tool outputs and container specs."""
    traces: List[MCPToolExecution] = []

    status_res, trace_status = await execute_mcp_tool_call(
        "get_pod_status",
        {"pod_name": alert.pod_name, "namespace": alert.namespace},
    )
    traces.append(trace_status)

    events_res, trace_events = await execute_mcp_tool_call(
        "get_pod_events",
        {"pod_name": alert.pod_name, "namespace": alert.namespace},
    )
    traces.append(trace_events)

    log_args: Dict[str, Any] = {"pod_name": alert.pod_name, "namespace": alert.namespace, "previous": True}
    if alert.container_name:
        log_args["container_name"] = alert.container_name
    logs_res, trace_logs = await execute_mcp_tool_call("get_pod_logs", log_args)
    traces.append(trace_logs)

    dep_name = alert.deployment_name or infer_workload_name_from_pod(alert.pod_name)
    spec_res, trace_spec = await execute_mcp_tool_call(
        "get_deployment_spec",
        {"deployment_name": dep_name, "namespace": alert.namespace},
    )
    traces.append(trace_spec)

    # Extract actual container spec from MCP get_deployment_spec response
    spec_containers = spec_res.get("containers", []) if isinstance(spec_res, dict) else []
    target_c_spec: Dict[str, Any] = {}
    if spec_containers:
        try:
            target_c_spec = select_target_container(spec_containers, alert.container_name)
        except Exception:
            target_c_spec = spec_containers[0]

    target_container_name = alert.container_name or target_c_spec.get("name")
    target_file = str(manifest_path) if manifest_path else f"{settings.manifests_dir}/{dep_name}.yaml"

    c_resources = target_c_spec.get("resources") or {}
    curr_limit_mem = (c_resources.get("limits") or {}).get("memory")
    curr_req_mem = (c_resources.get("requests") or {}).get("memory")
    env_list = target_c_spec.get("env") or []
    env_map = {e.get("name"): e.get("value") for e in env_list if isinstance(e, dict) and e.get("name")}

    logs_text = str((logs_res or {}).get("logs", ""))
    reason_lower = alert.reason.lower()
    msg_lower = (alert.message or "").lower()
    combined_text = f"{reason_lower} {msg_lower} {logs_text.lower()}"

    modifications: List[YAMLModification] = []

    # Case 1: OOMKilled / exit code 137
    if "oom" in reason_lower or alert.exit_code == 137 or "oomkilled" in combined_text:
        old_limit, new_limit, old_req, new_req = _compute_scaled_oom_memory(curr_limit_mem, curr_req_mem)
        root_cause = (
            f"Pod '{alert.pod_name}' (container '{target_container_name or 'primary'}') was terminated by the Linux kernel "
            f"OOM (Out Of Memory) killer with exit code 137. Container inspection via MCP shows memory limit was set to "
            f"'{old_limit}'. Remediation scales memory limits to '{new_limit}' and requests to '{new_req}'."
        )
        modifications.append(
            YAMLModification(
                file_path=target_file,
                container_name=target_container_name,
                parameter_path="resources.limits.memory",
                old_value=old_limit,
                new_value=new_limit,
                rationale=f"Increase memory limit from {old_limit} to {new_limit} to prevent OOMKilled terminations.",
            )
        )
        modifications.append(
            YAMLModification(
                file_path=target_file,
                container_name=target_container_name,
                parameter_path="resources.requests.memory",
                old_value=old_req,
                new_value=new_req,
                rationale=f"Align memory request ({new_req}) with updated container memory ceiling.",
            )
        )
        return root_cause, 0.98, modifications, traces

    # Case 2: Inspect actual env vars and logs for invalid port / environment configuration errors
    invalid_port_keys = [
        k for k, v in env_map.items()
        if "PORT" in str(k).upper() and v is not None and not str(v).isdigit()
    ]
    if not invalid_port_keys and ("invalid_port" in combined_text or "required_port" in combined_text):
        invalid_port_keys = ["REQUIRED_PORT"]

    if invalid_port_keys:
        port_key = invalid_port_keys[0]
        old_port_val = env_map.get(port_key, "invalid_port")
        root_cause = (
            f"Pod '{alert.pod_name}' entered {alert.reason} (exit code {alert.exit_code}) due to invalid environment "
            f"configuration `{port_key}='{old_port_val}'`. Remediation sets `{port_key}` to valid TCP port `'8080'`."
        )
        modifications.append(
            YAMLModification(
                file_path=target_file,
                container_name=target_container_name,
                parameter_path=f"env[{port_key}]",
                old_value=old_port_val,
                new_value="8080",
                rationale=f"Replace invalid non-numeric port value '{old_port_val}' in {port_key} with valid TCP port '8080'.",
            )
        )
        if "MEMORY_LIMIT" in env_map:
            old_env_mem = env_map["MEMORY_LIMIT"]
            _, new_env_mem, _, _ = _compute_scaled_oom_memory(old_env_mem, None)
            modifications.append(
                YAMLModification(
                    file_path=target_file,
                    container_name=target_container_name,
                    parameter_path="env[MEMORY_LIMIT]",
                    old_value=old_env_mem,
                    new_value=new_env_mem,
                    rationale=f"Increase application MEMORY_LIMIT environment threshold from {old_env_mem} to {new_env_mem}.",
                )
            )
        return root_cause, 0.96, modifications, traces

    # Case 3: ImagePullBackOff / ErrImagePull / InvalidImageName
    if reason_lower in ("imagepullbackoff", "errimagepull", "invalidimagename") or "image" in reason_lower:
        curr_img = target_c_spec.get("image", "unknown")
        fixed_img = curr_img.split(":")[0] + ":latest" if ":" in curr_img else f"{curr_img}:latest"
        root_cause = (
            f"Container '{target_container_name or 'primary'}' in pod '{alert.pod_name}' failed to pull image '{curr_img}' "
            f"({alert.reason}). Message: {alert.message or 'Image tag not found or inaccessible'}."
        )
        modifications.append(
            YAMLModification(
                file_path=target_file,
                container_name=target_container_name,
                parameter_path="image",
                old_value=curr_img,
                new_value=fixed_img,
                rationale=f"Update unresolvable container image '{curr_img}' to valid tag '{fixed_img}'.",
            )
        )
        return root_cause, 0.88, modifications, traces

    # Case 4: Generic application crash / non-zero exit code
    old_limit, new_limit, _, _ = _compute_scaled_oom_memory(curr_limit_mem, curr_req_mem)
    log_excerpt = logs_text.strip().splitlines()[-1] if logs_text.strip() else (alert.message or "No log output captured")
    root_cause = (
        f"Container '{target_container_name or 'primary'}' in pod '{alert.pod_name}' terminated with '{alert.reason}' "
        f"(exit code {alert.exit_code}). Diagnostic output: {log_excerpt}."
    )
    modifications.append(
        YAMLModification(
            file_path=target_file,
            container_name=target_container_name,
            parameter_path="resources.limits.memory",
            old_value=curr_limit_mem or "unset",
            new_value=new_limit,
            rationale=f"Ensure container '{target_container_name or 'primary'}' has a safe memory ceiling ({new_limit}).",
        )
    )
    return root_cause, 0.85, modifications, traces


async def _run_groq_triage_loop(
    alert: AlertPayload,
    manifest_path: Optional[Path],
) -> Tuple[str, float, List[YAMLModification], List[MCPToolExecution]]:
    """Execute Groq LLM agentic tool-use loop with openai/gpt-oss-120b and dynamically discovered MCP tools."""
    from groq import AsyncGroq

    client = AsyncGroq(api_key=settings.groq_api_key)
    traces: List[MCPToolExecution] = []
    tools_schema = await discover_mcp_tools_for_groq()

    dep_name = alert.deployment_name or infer_workload_name_from_pod(alert.pod_name)
    manifest_name = manifest_path.name if manifest_path else f"{dep_name}.yaml"
    target_file = str(manifest_path) if manifest_path else f"{settings.manifests_dir}/{manifest_name}"

    system_prompt = f"""You are KubeHeal, an autonomous Kubernetes AIOps Triage Agent.
You are connected to a strictly read-only Model Context Protocol (MCP) server.
Mutating operations (delete, apply, patch, exec) are strictly blocked by 3-layer guardrails.

INSTRUCTIONS:
1. Inspect the failing pod '{alert.pod_name}' (container: '{alert.container_name or "auto-detect"}') in namespace '{alert.namespace}' using `get_pod_status`, `get_pod_logs`, `get_pod_events`, and `get_deployment_spec` (for workload '{dep_name}').
2. Diagnose the exact root cause of why the container failed (reason: {alert.reason}, exit_code: {alert.exit_code}) based solely on the actual logs, events, and workload specification returned by the MCP tools.
3. Once you have gathered evidence from the MCP tools, call `submit_triage_solution` with:
   - root_cause_summary: detailed technical diagnosis citing log/event evidence
   - confidence: float between 0.0 and 1.0
   - modifications: list of YAML changes targeting '{manifest_name}' (supported parameter_path formats: 'resources.limits.memory', 'resources.requests.memory', 'resources.limits.cpu', 'resources.requests.cpu', 'image', 'command', 'args', or 'env[<ENV_VAR_NAME>]'). Keep memory limits within safe bounds (<= {settings.max_memory_limit}).
"""

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"Incident alert: Pod '{alert.pod_name}' in namespace '{alert.namespace}' (Workload: '{dep_name}', "
                f"Container: '{alert.container_name or 'primary'}') is failing with reason '{alert.reason}' "
                f"(exit_code={alert.exit_code}). Diagnostic message: {alert.message or 'N/A'}. "
                "Investigate via MCP tools and submit a remediation patch."
            ),
        },
    ]

    max_turns = 6
    for _ in range(max_turns):
        response = await client.chat.completions.create(
            model=settings.groq_model,
            messages=messages,
            tools=tools_schema,
            tool_choice="auto",
            temperature=0.1,
        )

        choice = response.choices[0]
        msg = choice.message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            break

        solution_found = None
        for tc in msg.tool_calls:
            tool_name = tc.function.name
            args = json.loads(tc.function.arguments or "{}")

            if tool_name == "submit_triage_solution":
                solution_found = args
                break

            result, trace = await execute_mcp_tool_call(tool_name, args)
            traces.append(trace)

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result),
            })

        if solution_found:
            root_cause = solution_found.get("root_cause_summary", "Diagnosed by Groq agent.")
            confidence = float(solution_found.get("confidence", 0.95))
            raw_mods = solution_found.get("modifications", [])
            modifications = []
            for rm in raw_mods:
                modifications.append(
                    YAMLModification(
                        file_path=target_file,
                        container_name=rm.get("container_name") or alert.container_name,
                        parameter_path=rm.get("parameter_path", ""),
                        old_value=rm.get("old_value"),
                        new_value=rm.get("new_value"),
                        rationale=rm.get("rationale", ""),
                    )
                )
            return root_cause, confidence, modifications, traces

    return await _deterministic_fallback_triage(alert, manifest_path)


async def run_triage_pipeline(alert: AlertPayload) -> TriageReport:
    """End-to-end triage orchestrator: LLM reasoning -> MCP inspections -> YAML patching -> PR proposal."""
    start_time = time.time()
    incident_id = str(uuid.uuid4())[:8]

    dep_name = alert.deployment_name
    if not dep_name:
        dep_name = infer_workload_name_from_pod(alert.pod_name)
        alert.deployment_name = dep_name

    manifest_path = find_manifest_for_deployment(settings.manifests_dir, dep_name)

    if settings.groq_api_key and settings.groq_api_key.strip():
        try:
            root_cause, confidence, modifications, mcp_traces = await _run_groq_triage_loop(alert, manifest_path)
        except Exception as e:
            logger.warning(f"Groq API call failed ({e}); falling back to deterministic triage.")
            root_cause, confidence, modifications, mcp_traces = await _deterministic_fallback_triage(alert, manifest_path)
    else:
        root_cause, confidence, modifications, mcp_traces = await _deterministic_fallback_triage(alert, manifest_path)

    diff_content = ""
    pr_url = None
    local_proposal_path = None

    if manifest_path and manifest_path.exists():
        try:
            patched_yaml, diff_content, audit_notes = patch_deployment_yaml(
                file_path=str(manifest_path),
                modifications=modifications,
                target_workload_name=dep_name,
            )
            if audit_notes:
                root_cause += "\n\n**Remediation Guardrail Notes**:\n" + "\n".join(f"- {n}" for n in audit_notes)

            pr_url, local_proposal_path = create_remediation_proposal(
                incident_id=incident_id,
                alert=alert,
                root_cause=root_cause,
                modifications=modifications,
                manifest_path=manifest_path,
                patched_content=patched_yaml,
                diff_content=diff_content,
                mcp_traces=mcp_traces,
            )
        except Exception as e:
            logger.error(f"Failed to generate YAML patch or PR proposal: {e}")
            diff_content = f"Error generating patch: {str(e)}"
    else:
        diff_content = f"Target manifest file not found in '{settings.manifests_dir}' for workload '{dep_name}'."

    duration = round(time.time() - start_time, 2)

    return TriageReport(
        incident_id=incident_id,
        status="completed",
        alert=alert,
        root_cause_summary=root_cause,
        confidence=confidence,
        mcp_traces=mcp_traces,
        proposed_fix=modifications,
        yaml_diff=diff_content,
        pr_url=pr_url,
        local_proposal_path=local_proposal_path,
        duration_seconds=duration,
    )
