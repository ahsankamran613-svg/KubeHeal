"""AI Triage Agent orchestrating MCP telemetry pre-hydration, LLM diagnostics, and GitOps remediation."""

import asyncio
from dataclasses import dataclass
import json
import logging
import time
import uuid
import weakref
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple

from kubeheal.config import DETERMINISTIC_ENGINE_NAME, settings
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

# Event-loop-scoped semaphores so concurrent crashes queue instead of bursting TPM limits.
_llm_semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Tuple[int, asyncio.Semaphore]]" = (
    weakref.WeakKeyDictionary()
)

MILLISECONDS_PER_SECOND = 1000
MIN_HTTP_ATTEMPTS = 1
DEFAULT_TOOL_PARAMETERS_SCHEMA: Dict[str, Any] = {"type": "object", "properties": {}}

SUBMIT_SOLUTION_TOOL_SPEC: Dict[str, Any] = {
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
}

SUBMIT_SOLUTION_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": SUBMIT_SOLUTION_TOOL_SPEC,
}


def _get_llm_semaphore() -> asyncio.Semaphore:
    """Return an event-loop-scoped semaphore matching settings.llm_concurrency_limit."""
    loop = asyncio.get_running_loop()
    limit = max(1, int(settings.llm_concurrency_limit))
    cached = _llm_semaphores.get(loop)
    if cached is not None and cached[0] == limit:
        return cached[1]
    semaphore = asyncio.Semaphore(limit)
    _llm_semaphores[loop] = (limit, semaphore)
    return semaphore


def compact_log_text(logs_text: str, max_chars: int, max_consecutive_repeats: int = 3) -> str:
    """Collapse repeated consecutive log lines and retain head + tail within max_chars."""
    if not logs_text or max_chars <= 0:
        return ""

    lines = logs_text.splitlines()
    collapsed_lines: List[str] = []
    prev_line: Optional[str] = None
    repeat_count = 0

    for line in lines:
        if line == prev_line:
            repeat_count += 1
            if repeat_count <= max_consecutive_repeats:
                collapsed_lines.append(line)
        else:
            if repeat_count > max_consecutive_repeats:
                omitted = repeat_count - max_consecutive_repeats
                collapsed_lines.append(f"... [previous line repeated {omitted} more time(s)]")
            collapsed_lines.append(line)
            prev_line = line
            repeat_count = 1

    if repeat_count > max_consecutive_repeats:
        omitted = repeat_count - max_consecutive_repeats
        collapsed_lines.append(f"... [previous line repeated {omitted} more time(s)]")

    collapsed = "\n".join(collapsed_lines)
    if len(collapsed) <= max_chars:
        return collapsed

    marker = (
        "\n... [middle log lines truncated; use get_pod_logs or run_readonly_kubectl "
        "if earlier context is needed] ...\n"
    )
    if max_chars <= len(marker) + 20:
        return collapsed[-max_chars:]

    available = max_chars - len(marker)
    head_budget = max(1, int(available * 0.25))
    tail_budget = max(1, available - head_budget)
    return f"{collapsed[:head_budget]}{marker}{collapsed[-tail_budget:]}"


def compact_telemetry_for_llm(
    baseline_context: Dict[str, Any],
    max_log_chars: int,
) -> Dict[str, Any]:
    """Create a token-efficient copy of baseline MCP telemetry without losing diagnostic signal."""
    raw_status = baseline_context.get("pod_status") or {}
    raw_events = baseline_context.get("pod_events") or {}
    raw_logs = baseline_context.get("pod_logs") or {}
    raw_spec = baseline_context.get("workload_spec") or {}

    compact_logs_text = compact_log_text(str(raw_logs.get("logs", "")), max_chars=max_log_chars)

    raw_event_list = raw_events.get("events") or []
    compact_events = [
        {
            k: ev.get(k)
            for k in ("type", "reason", "message", "count", "last_timestamp")
            if ev.get(k) is not None
        }
        for ev in raw_event_list
        if isinstance(ev, dict)
    ]

    return {
        "pod_status": {
            k: raw_status.get(k)
            for k in ("status", "source", "phase", "container_statuses", "message")
            if raw_status.get(k) is not None
        },
        "pod_events": {
            "status": raw_events.get("status"),
            "events": compact_events,
            **({"message": raw_events["message"]} if raw_events.get("message") else {}),
        },
        "pod_logs": {
            "status": raw_logs.get("status"),
            "logs": compact_logs_text,
            **({"message": raw_logs["message"]} if raw_logs.get("message") else {}),
        },
        "workload_spec": {
            k: raw_spec.get(k)
            for k in ("status", "source", "kind", "deployment_name", "replicas", "containers", "message")
            if raw_spec.get(k) is not None
        },
    }


async def _discover_mcp_tool_specs() -> List[Dict[str, Any]]:
    """Discover registered MCP tools in a provider-neutral specification format."""
    specs: List[Dict[str, Any]] = []
    try:
        mcp_tools = await mcp.list_tools()
        for tool in mcp_tools:
            schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
            specs.append({
                "name": tool.name,
                "description": (tool.description or "").strip(),
                "parameters": schema or DEFAULT_TOOL_PARAMETERS_SCHEMA,
            })
    except Exception as e:
        logger.warning(f"Dynamic MCP tool discovery fallback triggered: {e}")

    specs.append(SUBMIT_SOLUTION_TOOL_SPEC)
    return specs


async def discover_mcp_tools_for_groq() -> List[Dict[str, Any]]:
    """Dynamically discover tools registered on the MCPServer and convert them to Groq/OpenAI tool-calling schema."""
    specs = await _discover_mcp_tool_specs()
    return [{"type": "function", "function": spec} for spec in specs]


async def discover_mcp_tools_for_gemini() -> List[Any]:
    """Dynamically discover tools registered on the MCPServer and convert them to Google GenAI Tool declarations."""
    from google.genai import types

    specs = await _discover_mcp_tool_specs()
    declarations = [
        types.FunctionDeclaration(
            name=spec["name"],
            description=spec["description"],
            parameters_json_schema=spec["parameters"],
        )
        for spec in specs
    ]
    return [types.Tool(function_declarations=declarations)]


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
            tail_lines=int(args.get("tail_lines", settings.llm_log_tail_lines)),
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


async def gather_baseline_mcp_telemetry(
    alert: AlertPayload,
    dep_name: str,
    tail_lines: Optional[int] = None,
) -> Tuple[Dict[str, Any], List[MCPToolExecution]]:
    """Pre-hydrate the 4 universal read-only MCP diagnostics through the audited MCP protocol."""
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

    log_args: Dict[str, Any] = {
        "pod_name": alert.pod_name,
        "namespace": alert.namespace,
        "tail_lines": tail_lines if tail_lines is not None else settings.llm_log_tail_lines,
        "previous": True,
    }
    if alert.container_name:
        log_args["container_name"] = alert.container_name
    logs_res, trace_logs = await execute_mcp_tool_call("get_pod_logs", log_args)
    traces.append(trace_logs)

    spec_res, trace_spec = await execute_mcp_tool_call(
        "get_deployment_spec",
        {"deployment_name": dep_name, "namespace": alert.namespace},
    )
    traces.append(trace_spec)

    baseline_context = {
        "pod_status": status_res,
        "pod_events": events_res,
        "pod_logs": logs_res,
        "workload_spec": spec_res,
    }
    return baseline_context, traces


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
    baseline_context: Optional[Dict[str, Any]] = None,
    baseline_traces: Optional[List[MCPToolExecution]] = None,
) -> Tuple[str, float, List[YAMLModification], List[MCPToolExecution]]:
    """Intelligent deterministic diagnostic engine that inspects MCP telemetry and container specs."""
    dep_name = alert.deployment_name or infer_workload_name_from_pod(alert.pod_name)
    if baseline_context is None or baseline_traces is None:
        baseline_context, traces = await gather_baseline_mcp_telemetry(alert, dep_name)
    else:
        traces = list(baseline_traces)

    logs_res = baseline_context.get("pod_logs") or {}
    spec_res = baseline_context.get("workload_spec") or {}

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

    logs_text = str(logs_res.get("logs", ""))
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


def _parse_tool_arguments(raw_args: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Safely parse tool call arguments (JSON string or dict) returned by an LLM."""
    if isinstance(raw_args, dict):
        return raw_args, None
    if raw_args is None or raw_args == "":
        return {}, None
    if not isinstance(raw_args, str):
        return None, f"Tool arguments must be a JSON object or string, got {type(raw_args).__name__}"
    try:
        parsed = json.loads(raw_args)
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON tool arguments: {e}"
    if not isinstance(parsed, dict):
        return None, "Tool arguments must decode to a JSON object"
    return parsed, None


def _serialize_assistant_message(msg: Any) -> Dict[str, Any]:
    """Convert a Groq/OpenAI ChatCompletionMessage (or test fake) into a message dict."""
    if hasattr(msg, "model_dump") and callable(msg.model_dump):
        return msg.model_dump(exclude_none=True)

    serialized: Dict[str, Any] = {"role": "assistant"}
    content = getattr(msg, "content", None)
    if content is not None:
        serialized["content"] = content
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        serialized["tool_calls"] = [
            {
                "id": tc.id,
                "type": getattr(tc, "type", "function"),
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments or "{}",
                },
            }
            for tc in tool_calls
        ]
    return serialized


async def _close_client_safely(client: Any) -> None:
    """Safely close an async or sync LLM client if it exposes aclose or close."""
    if client is None:
        return
    for method_name in ("aclose", "close"):
        close_fn = getattr(client, method_name, None)
        if callable(close_fn):
            res = close_fn()
            if asyncio.iscoroutine(res):
                await res
            return


@dataclass
class NormalizedToolCall:
    """Provider-neutral representation of a single LLM tool invocation."""

    id: Optional[str]
    name: str
    arguments: Any


class LLMProviderSession(Protocol):
    """Protocol implemented by provider-specific multi-turn tool-calling sessions."""

    provider_name: str
    model_name: str

    async def next_tool_calls(self) -> List[NormalizedToolCall]:
        """Request the next model turn and return any tool calls requested."""

    def append_tool_results(self, tool_results: List[Tuple[NormalizedToolCall, Dict[str, Any]]]) -> None:
        """Append tool execution outputs to the provider's conversation history."""

    async def aclose(self) -> None:
        """Release underlying HTTP client resources."""


class GroqProviderSession:
    """Multi-turn tool-calling session backed by AsyncGroq chat completions."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        tools_schema: List[Dict[str, Any]],
    ) -> None:
        from groq import AsyncGroq

        self.provider_name = "groq"
        self.model_name = model
        self._tools_schema = tools_schema
        self._client = AsyncGroq(
            api_key=api_key,
            max_retries=settings.llm_max_retries,
            timeout=settings.llm_timeout_seconds,
        )
        self._messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    async def next_tool_calls(self) -> List[NormalizedToolCall]:
        response = await self._client.chat.completions.create(
            model=self.model_name,
            messages=self._messages,
            tools=self._tools_schema,
            tool_choice="auto",
            parallel_tool_calls=True,
            temperature=settings.llm_temperature,
        )
        choices = getattr(response, "choices", None) or []
        if not choices:
            return []

        msg = choices[0].message
        self._messages.append(_serialize_assistant_message(msg))
        raw_calls = getattr(msg, "tool_calls", None) or []
        return [
            NormalizedToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=tc.function.arguments,
            )
            for tc in raw_calls
        ]

    def append_tool_results(self, tool_results: List[Tuple[NormalizedToolCall, Dict[str, Any]]]) -> None:
        for tc, result in tool_results:
            self._messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result),
            })

    async def aclose(self) -> None:
        await _close_client_safely(self._client)


class GeminiProviderSession:
    """Multi-turn tool-calling session backed by Google's native google-genai SDK.

    Appends each model candidate's ``Content`` object verbatim to conversation
    history so Gemini 3+ ``thought_signature`` and ``thought`` parts stay intact
    across multi-turn tool calls.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        gemini_tools: List[Any],
    ) -> None:
        from google import genai
        from google.genai import types

        self._types = types
        self.provider_name = "gemini"
        self.model_name = model

        timeout_ms = max(1, int(round(settings.llm_timeout_seconds * MILLISECONDS_PER_SECOND)))
        retry_attempts = max(MIN_HTTP_ATTEMPTS, int(settings.llm_max_retries) + 1)
        http_options = types.HttpOptions(
            timeout=timeout_ms,
            retry_options=types.HttpRetryOptions(attempts=retry_attempts),
        )
        self._client = genai.Client(api_key=api_key, http_options=http_options)
        self._config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=gemini_tools,
            temperature=settings.llm_temperature,
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.AUTO
                )
            ),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        self._contents: List[Any] = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=user_prompt)],
            )
        ]

    async def next_tool_calls(self) -> List[NormalizedToolCall]:
        response = await self._client.aio.models.generate_content(
            model=self.model_name,
            contents=self._contents,
            config=self._config,
        )
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return []

        candidate_content = getattr(candidates[0], "content", None)
        if candidate_content is None:
            return []

        # Preserve model Content verbatim (including thought_signature bytes on parts).
        if getattr(candidate_content, "role", None) is None and hasattr(candidate_content, "model_copy"):
            candidate_content = candidate_content.model_copy(update={"role": "model"})
        self._contents.append(candidate_content)

        parts = getattr(candidate_content, "parts", None) or []
        calls: List[NormalizedToolCall] = []
        for part in parts:
            fc = getattr(part, "function_call", None)
            if fc is None:
                continue
            calls.append(
                NormalizedToolCall(
                    id=getattr(fc, "id", None),
                    name=getattr(fc, "name", None) or "",
                    arguments=getattr(fc, "args", None),
                )
            )
        return calls

    def append_tool_results(self, tool_results: List[Tuple[NormalizedToolCall, Dict[str, Any]]]) -> None:
        if not tool_results:
            return

        types = self._types
        response_parts: List[Any] = []
        for tc, result in tool_results:
            response_payload = result if isinstance(result, dict) else {"output": result}
            if tc.id:
                part = types.Part(
                    function_response=types.FunctionResponse(
                        id=tc.id,
                        name=tc.name,
                        response=response_payload,
                    )
                )
            else:
                part = types.Part.from_function_response(
                    name=tc.name,
                    response=response_payload,
                )
            response_parts.append(part)

        self._contents.append(types.Content(role="user", parts=response_parts))

    async def aclose(self) -> None:
        await _close_client_safely(getattr(self._client, "aio", None))
        await _close_client_safely(self._client)


async def _create_llm_session(
    *,
    provider: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> LLMProviderSession:
    """Instantiate the configured LLM provider session with discovered MCP tools."""
    if not api_key or not api_key.strip():
        raise ValueError(f"Missing API key for LLM provider '{provider}'.")

    if provider == "gemini":
        gemini_tools = await discover_mcp_tools_for_gemini()
        return GeminiProviderSession(
            api_key=api_key,
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            gemini_tools=gemini_tools,
        )
    if provider == "groq":
        groq_tools = await discover_mcp_tools_for_groq()
        return GroqProviderSession(
            api_key=api_key,
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools_schema=groq_tools,
        )
    raise ValueError(f"Unsupported LLM provider '{provider}'.")


def _build_triage_prompts(
    alert: AlertPayload,
    dep_name: str,
    manifest_path: Optional[Path],
    compact_context: Dict[str, Any],
) -> Tuple[str, str, str]:
    """Construct shared system/user prompts and target manifest path for any LLM provider."""
    manifest_name = manifest_path.name if manifest_path else f"{dep_name}.yaml"
    target_file = str(manifest_path) if manifest_path else f"{settings.manifests_dir}/{manifest_name}"

    system_prompt = f"""You are KubeHeal, an autonomous Kubernetes AIOps Triage Agent.
You are connected to a strictly read-only Model Context Protocol (MCP) server.
Mutating operations (delete, apply, patch, exec) are strictly blocked by 3-layer guardrails.

PRE-HYDRATED DIAGNOSTICS:
- The 4 baseline read-only MCP checks (`get_pod_status`, `get_pod_events`, `get_pod_logs`, `get_deployment_spec`) have ALREADY been executed for pod '{alert.pod_name}' and workload '{dep_name}' and are provided below.
- Do NOT re-call those 4 baseline tools for the same pod/workload unless you specifically need a different container's logs, extra log lines, or a different workload.

INSTRUCTIONS:
1. Analyze the pre-hydrated evidence below carefully.
2. If the evidence clearly identifies the root cause, call `submit_triage_solution` immediately in this turn.
3. Only if the failure depends on external cluster resources not included below (e.g., ConfigMaps, Services, Endpoints, PVCs, Nodes, or another container/pod), invoke `run_readonly_kubectl` or `get_pod_logs` (you may issue multiple read-only tool calls in parallel) before calling `submit_triage_solution`.
4. When calling `submit_triage_solution`, supply:
   - root_cause_summary: detailed technical diagnosis citing log/event evidence
   - confidence: float between 0.0 and 1.0
   - modifications: list of YAML changes targeting '{manifest_name}' (supported parameter_path formats: 'resources.limits.memory', 'resources.requests.memory', 'resources.limits.cpu', 'resources.requests.cpu', 'image', 'command', 'args', 'replicas', or 'env[<ENV_VAR_NAME>]'). Keep memory limits within safe bounds (<= {settings.max_memory_limit} unless existing workload limit is already higher)."""

    user_prompt = (
        f"Incident alert: Pod '{alert.pod_name}' in namespace '{alert.namespace}' (Workload: '{dep_name}', "
        f"Container: '{alert.container_name or 'auto-detect'}') failed with reason '{alert.reason}' "
        f"(exit_code={alert.exit_code}). Diagnostic message: {alert.message or 'N/A'}.\n\n"
        f"Pre-hydrated MCP Telemetry:\n{json.dumps(compact_context, indent=2)}"
    )
    return system_prompt, user_prompt, target_file


async def _execute_llm_tool_turn(
    tool_calls: List[NormalizedToolCall],
    traces: List[MCPToolExecution],
) -> Tuple[List[Tuple[NormalizedToolCall, Dict[str, Any]]], Optional[Dict[str, Any]]]:
    """Validate and execute all tool calls emitted in a single LLM turn."""
    tool_results: List[Tuple[NormalizedToolCall, Dict[str, Any]]] = []
    solution_found: Optional[Dict[str, Any]] = None

    for tc in tool_calls:
        parsed_args, parse_err = _parse_tool_arguments(tc.arguments)
        if parse_err is not None or parsed_args is None:
            tool_results.append((tc, {"status": "error", "error": parse_err}))
            continue

        if tc.name == "submit_triage_solution":
            solution_found = parsed_args
            tool_results.append((tc, {"status": "accepted"}))
            continue

        result, trace = await execute_mcp_tool_call(tc.name, parsed_args)
        traces.append(trace)
        tool_results.append((tc, result))

    return tool_results, solution_found


def _build_llm_triage_result(
    solution: Dict[str, Any],
    alert: AlertPayload,
    target_file: str,
    traces: List[MCPToolExecution],
) -> Tuple[str, float, List[YAMLModification], List[MCPToolExecution]]:
    """Convert a validated submit_triage_solution payload into structured triage outputs."""
    root_cause = str(solution.get("root_cause_summary") or "Diagnosed by LLM agent.")
    try:
        confidence = max(0.0, min(1.0, float(solution.get("confidence", 0.95))))
    except (TypeError, ValueError):
        confidence = 0.9

    raw_mods = solution.get("modifications") or []
    modifications: List[YAMLModification] = []
    for rm in raw_mods:
        if not isinstance(rm, dict):
            continue
        modifications.append(
            YAMLModification(
                file_path=target_file,
                container_name=rm.get("container_name") or alert.container_name,
                parameter_path=str(rm.get("parameter_path", "")),
                old_value=rm.get("old_value"),
                new_value=rm.get("new_value"),
                rationale=str(rm.get("rationale", "")),
            )
        )
    return root_cause, confidence, modifications, traces


async def _run_llm_triage_loop_with_metadata(
    alert: AlertPayload,
    manifest_path: Optional[Path],
    baseline_context: Optional[Dict[str, Any]] = None,
    baseline_traces: Optional[List[MCPToolExecution]] = None,
    provider_override: Optional[str] = None,
) -> Tuple[str, float, List[YAMLModification], List[MCPToolExecution], str, Optional[str]]:
    """Pre-hydrate MCP telemetry, then run paced multi-turn LLM reasoning using the active provider."""
    dep_name = alert.deployment_name or infer_workload_name_from_pod(alert.pod_name)
    if baseline_context is None or baseline_traces is None:
        baseline_context, gathered_traces = await gather_baseline_mcp_telemetry(alert, dep_name)
        traces: List[MCPToolExecution] = list(gathered_traces)
    else:
        traces = list(baseline_traces)

    llm_cfg = settings.resolve_llm_config(provider_override=provider_override)
    if not llm_cfg.is_configured or not llm_cfg.api_key:
        root_cause, confidence, mods, fallback_traces = await _deterministic_fallback_triage(
            alert,
            manifest_path,
            baseline_context=baseline_context,
            baseline_traces=traces,
        )
        return root_cause, confidence, mods, fallback_traces, DETERMINISTIC_ENGINE_NAME, None

    compact_context = compact_telemetry_for_llm(
        baseline_context,
        max_log_chars=settings.llm_max_log_chars,
    )
    system_prompt, user_prompt, target_file = _build_triage_prompts(
        alert,
        dep_name,
        manifest_path,
        compact_context,
    )

    session = await _create_llm_session(
        provider=llm_cfg.provider,
        api_key=llm_cfg.api_key,
        model=llm_cfg.model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    semaphore = _get_llm_semaphore()
    max_turns = max(1, int(settings.llm_max_turns))

    try:
        async with semaphore:
            try:
                for turn_idx in range(max_turns):
                    tool_calls = await session.next_tool_calls()
                    if not tool_calls:
                        break

                    tool_results, solution_found = await _execute_llm_tool_turn(tool_calls, traces)
                    session.append_tool_results(tool_results)

                    if solution_found is not None:
                        logger.info(
                            "%s triage (%s) completed for pod '%s' in %d LLM turn(s) (%d MCP trace(s)).",
                            llm_cfg.provider.capitalize(),
                            llm_cfg.model,
                            alert.pod_name,
                            turn_idx + 1,
                            len(traces),
                        )
                        root_cause, confidence, mods, final_traces = _build_llm_triage_result(
                            solution_found, alert, target_file, traces
                        )
                        return root_cause, confidence, mods, final_traces, llm_cfg.provider, llm_cfg.model
            finally:
                if settings.llm_inter_request_delay_seconds > 0:
                    await asyncio.sleep(settings.llm_inter_request_delay_seconds)
    finally:
        await session.aclose()

    root_cause, confidence, mods, fallback_traces = await _deterministic_fallback_triage(
        alert,
        manifest_path,
        baseline_context=baseline_context,
        baseline_traces=traces,
    )
    return root_cause, confidence, mods, fallback_traces, DETERMINISTIC_ENGINE_NAME, None


async def _run_llm_triage_loop(
    alert: AlertPayload,
    manifest_path: Optional[Path],
    baseline_context: Optional[Dict[str, Any]] = None,
    baseline_traces: Optional[List[MCPToolExecution]] = None,
    provider_override: Optional[str] = None,
) -> Tuple[str, float, List[YAMLModification], List[MCPToolExecution]]:
    """Run paced LLM reasoning and return the 4-tuple triage output."""
    root_cause, confidence, mods, traces, _, _ = await _run_llm_triage_loop_with_metadata(
        alert,
        manifest_path,
        baseline_context=baseline_context,
        baseline_traces=baseline_traces,
        provider_override=provider_override,
    )
    return root_cause, confidence, mods, traces


async def _run_groq_triage_loop(
    alert: AlertPayload,
    manifest_path: Optional[Path],
    baseline_context: Optional[Dict[str, Any]] = None,
    baseline_traces: Optional[List[MCPToolExecution]] = None,
) -> Tuple[str, float, List[YAMLModification], List[MCPToolExecution]]:
    """Run paced LLM reasoning explicitly using Groq."""
    return await _run_llm_triage_loop(
        alert,
        manifest_path,
        baseline_context=baseline_context,
        baseline_traces=baseline_traces,
        provider_override="groq",
    )


async def _run_gemini_triage_loop(
    alert: AlertPayload,
    manifest_path: Optional[Path],
    baseline_context: Optional[Dict[str, Any]] = None,
    baseline_traces: Optional[List[MCPToolExecution]] = None,
) -> Tuple[str, float, List[YAMLModification], List[MCPToolExecution]]:
    """Run paced LLM reasoning explicitly using Google Gemini."""
    return await _run_llm_triage_loop(
        alert,
        manifest_path,
        baseline_context=baseline_context,
        baseline_traces=baseline_traces,
        provider_override="gemini",
    )


async def run_triage_pipeline(alert: AlertPayload) -> TriageReport:
    """End-to-end triage orchestrator: MCP pre-hydration -> LLM reasoning -> YAML patching -> PR proposal."""
    start_time = time.time()
    incident_id = str(uuid.uuid4())[:8]

    dep_name = alert.deployment_name
    if not dep_name:
        dep_name = infer_workload_name_from_pod(alert.pod_name)
        alert.deployment_name = dep_name

    manifest_path = find_manifest_for_deployment(settings.manifests_dir, dep_name)
    baseline_context, baseline_traces = await gather_baseline_mcp_telemetry(alert, dep_name)

    triage_result: Optional[
        Tuple[str, float, List[YAMLModification], List[MCPToolExecution], str, Optional[str]]
    ] = None
    try:
        llm_cfg = settings.resolve_llm_config()
        if llm_cfg.is_configured:
            try:
                triage_result = await _run_llm_triage_loop_with_metadata(
                    alert,
                    manifest_path,
                    baseline_context=baseline_context,
                    baseline_traces=baseline_traces,
                )
            except Exception as e:
                logger.warning(f"{llm_cfg.provider.capitalize()} API call failed ({e}); falling back to deterministic triage.")
    except ValueError as e:
        logger.warning(f"Invalid LLM configuration ({e}); falling back to deterministic triage.")

    if triage_result is None:
        root_cause, confidence, modifications, mcp_traces = await _deterministic_fallback_triage(
            alert,
            manifest_path,
            baseline_context=baseline_context,
            baseline_traces=baseline_traces,
        )
        engine_provider = DETERMINISTIC_ENGINE_NAME
        engine_model: Optional[str] = None
    else:
        root_cause, confidence, modifications, mcp_traces, engine_provider, engine_model = triage_result

    engine_label = settings.format_llm_attribution(
        provider=engine_provider,
        model=engine_model,
        is_configured=(engine_provider != DETERMINISTIC_ENGINE_NAME),
    )

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
                engine_label=engine_label,
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
        llm_provider=engine_provider,
        llm_model=engine_model,
        duration_seconds=duration,
    )
