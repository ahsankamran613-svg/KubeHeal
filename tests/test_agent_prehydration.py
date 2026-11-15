"""Parameterized unit & integration tests for MCP pre-hydration, log compaction, pacing, and LLM error recovery."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
import pytest

from kubeheal.agent import triage_agent
from kubeheal.config import Settings, settings
from kubeheal.models import AlertPayload


@pytest.fixture(autouse=True)
def _isolate_llm_settings(monkeypatch):
    """Isolate every test from local .env credentials and default provider state."""
    monkeypatch.setattr(settings, "llm_provider", "auto")
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "gemini_model", "gemini-3.5-flash-lite")
    monkeypatch.setattr(settings, "groq_api_key", None)
    monkeypatch.setattr(settings, "groq_model", "openai/gpt-oss-120b")
    monkeypatch.setattr(settings, "dry_run_mode", True)
    monkeypatch.setattr(settings, "github_token", None)


def _make_fake_tool_call(call_id: str, name: str, arguments: Any) -> SimpleNamespace:
    raw_args = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=raw_args),
    )


def _make_fake_response(
    tool_calls: Optional[List[SimpleNamespace]] = None,
    content: Optional[str] = None,
) -> SimpleNamespace:
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


@pytest.mark.parametrize(
    "logs_text,max_chars,expected_substrings,excluded_substrings",
    [
        # Normal: short logs preserved verbatim
        ("line1\nline2\nerror boom", 500, ["line1", "error boom"], ["truncated"]),
        # Normal: repeated consecutive spam collapsed
        ("boot ok\n" + ("retry failed\n" * 8) + "fatal exit", 500, ["boot ok", "repeated 5 more time(s)", "fatal exit"], []),
        # Boundary: exact 3 repeats NOT collapsed
        ("a\na\na\nb", 500, ["a\na\na\nb"], ["repeated"]),
        # Boundary: empty & zero budget
        ("", 200, [], ["truncated"]),
        ("some log line", 0, [], ["some log line"]),
        # Boundary: very small positive budget smaller than truncation marker
        ("head-1234567890-tail-end", 8, ["tail-end"], ["truncated"]),
        # Normal large log: preserves head startup lines and tail crash lines
        (
            "BOOT_CONFIG_LINE=ok\n" + ("noise_middle_line_" * 40) + "\nFATAL_STACK_TRACE_END",
            260,
            ["BOOT_CONFIG_LINE=ok", "middle log lines truncated", "FATAL_STACK_TRACE_END"],
            [],
        ),
    ],
)
def test_compact_log_text_parameterized(
    logs_text: str,
    max_chars: int,
    expected_substrings: List[str],
    excluded_substrings: List[str],
):
    result = triage_agent.compact_log_text(logs_text, max_chars=max_chars)
    assert len(result) <= max(0, max_chars)
    for exp in expected_substrings:
        assert exp in result
    for exc in excluded_substrings:
        assert exc not in result


@pytest.mark.parametrize(
    "raw_args,expect_ok,expected_dict",
    [
        ('{"key": "val"}', True, {"key": "val"}),
        ({"key": "val"}, True, {"key": "val"}),
        ("", True, {}),
        (None, True, {}),
        ("{bad json", False, None),
        ("[1, 2, 3]", False, None),
        ('"just-a-string"', False, None),
        (123, False, None),
    ],
)
def test_parse_tool_arguments_parameterized(
    raw_args: Any,
    expect_ok: bool,
    expected_dict: Optional[Dict[str, Any]],
):
    parsed, err = triage_agent._parse_tool_arguments(raw_args)
    if expect_ok:
        assert err is None
        assert parsed == expected_dict
    else:
        assert parsed is None
        assert err is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario_name,alert_kwargs,manifest_yaml,scripted_turns,expected_llm_calls,expected_trace_count,expected_diff_substr,expected_confidence",
    [
        # 1. Happy path: 1 LLM call solves unseen Java/Spring OOM workload via pre-hydrated telemetry
        (
            "single_turn_prehydrated_jvm_oom",
            {
                "pod_name": "billing-api-6f7b9c8d5-abcde",
                "namespace": "default",
                "container_name": "jvm",
                "reason": "OOMKilled",
                "exit_code": 137,
                "message": "Java heap exceeded 512Mi limit",
            },
            """apiVersion: apps/v1
kind: Deployment
metadata:
  name: billing-api
spec:
  template:
    spec:
      containers:
        - name: jvm
          image: eclipse-temurin:21-jre
          resources:
            limits:
              memory: 512Mi
""",
            [
                [
                    _make_fake_tool_call(
                        "call_1",
                        "submit_triage_solution",
                        {
                            "root_cause_summary": "JVM container exceeded 512Mi cgroup memory limit.",
                            "confidence": 0.94,
                            "modifications": [
                                {
                                    "container_name": "jvm",
                                    "parameter_path": "resources.limits.memory",
                                    "old_value": "512Mi",
                                    "new_value": "1Gi",
                                    "rationale": "Provide 1Gi ceiling for JVM heap + metaspace",
                                }
                            ],
                        },
                    )
                ]
            ],
            1,
            4,
            "memory: 1Gi",
            0.94,
        ),
        # 2. General multi-turn investigation: LLM uses pre-hydrated context + follow-up read-only kubectl, then submits (2 calls total)
        (
            "two_turn_followup_kubectl_general_app",
            {
                "pod_name": "orders-db-0",
                "namespace": "default",
                "container_name": "postgres",
                "reason": "CrashLoopBackOff",
                "exit_code": 2,
                "message": "Invalid CLI flag in command",
            },
            """apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: orders-db
spec:
  template:
    spec:
      containers:
        - name: istio-proxy
          image: istio/proxyv2:1.20
        - name: postgres
          image: postgres:16-alpine
          env:
            - name: PGDATA
              value: "/var/lib/postgresql/data"
""",
            [
                [
                    _make_fake_tool_call(
                        "call_followup",
                        "run_readonly_kubectl",
                        {"command": "kubectl get configmap orders-config -n default"},
                    )
                ],
                [
                    _make_fake_tool_call(
                        "call_submit",
                        "submit_triage_solution",
                        {
                            "root_cause_summary": "PostgreSQL failed due to missing PGDATA subpath configuration.",
                            "confidence": 1.5,  # boundary test: clamped to 1.0
                            "modifications": [
                                {
                                    "container_name": "postgres",
                                    "parameter_path": "env[PGDATA]",
                                    "old_value": "/var/lib/postgresql/data",
                                    "new_value": "/var/lib/postgresql/data/pgdata",
                                    "rationale": "Use dedicated subdirectory to avoid lost+found conflict",
                                }
                            ],
                        },
                    )
                ],
            ],
            2,
            5,
            "/var/lib/postgresql/data/pgdata",
            1.0,
        ),
        # 3. Error recovery: LLM emits invalid JSON args on turn 1 and blocked mutating command, recovers on turn 2
        (
            "error_recovery_malformed_json_and_blocked_cmd",
            {
                "pod_name": "worker-job-k8x9q",
                "namespace": "default",
                "container_name": "runner",
                "reason": "Error",
                "exit_code": 127,
                "message": "binary not found",
            },
            """apiVersion: batch/v1
kind: Job
metadata:
  name: worker-job
spec:
  template:
    spec:
      containers:
        - name: runner
          image: myorg/worker:broken-tag
""",
            [
                [
                    _make_fake_tool_call("call_bad_json", "run_readonly_kubectl", "{invalid-json"),
                    _make_fake_tool_call(
                        "call_blocked",
                        "run_readonly_kubectl",
                        {"command": "kubectl delete pod worker-job-k8x9q"},
                    ),
                ],
                [
                    _make_fake_tool_call(
                        "call_recover",
                        "submit_triage_solution",
                        {
                            "root_cause_summary": "Updated worker image tag after guardrail check.",
                            "confidence": -0.2,  # boundary test: clamped to 0.0
                            "modifications": [
                                {
                                    "container_name": "runner",
                                    "parameter_path": "image",
                                    "old_value": "myorg/worker:broken-tag",
                                    "new_value": "myorg/worker:latest",
                                    "rationale": "Use valid release image tag",
                                }
                            ],
                        },
                    )
                ],
            ],
            2,
            5,
            "myorg/worker:latest",
            0.0,
        ),
    ],
)
async def test_prehydrated_triage_modes_parameterized(
    scenario_name: str,
    alert_kwargs: Dict[str, Any],
    manifest_yaml: str,
    scripted_turns: List[List[SimpleNamespace]],
    expected_llm_calls: int,
    expected_trace_count: int,
    expected_diff_substr: str,
    expected_confidence: float,
    tmp_path,
    monkeypatch,
):
    workload_name = triage_agent.infer_workload_name_from_pod(alert_kwargs["pod_name"])
    manifest_file = tmp_path / f"{workload_name}.yaml"
    manifest_file.write_text(manifest_yaml, encoding="utf-8")

    monkeypatch.setattr(settings, "manifests_dir", str(tmp_path))
    monkeypatch.setattr(settings, "proposals_dir", str(tmp_path / "proposals"))
    monkeypatch.setattr(settings, "dry_run_mode", True)
    monkeypatch.setattr(settings, "github_token", None)
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test_fake_key")
    monkeypatch.setattr(settings, "llm_max_turns", 4)

    recorded_calls: List[Dict[str, Any]] = []

    class FakeCompletions:
        async def create(self, **kwargs):
            turn_idx = len(recorded_calls)
            recorded_calls.append(kwargs)
            return _make_fake_response(tool_calls=scripted_turns[turn_idx])

    class FakeAsyncGroq:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

        async def close(self):
            return None

    import groq

    monkeypatch.setattr(groq, "AsyncGroq", FakeAsyncGroq)

    alert = AlertPayload(**alert_kwargs)
    report = await triage_agent.run_triage_pipeline(alert)

    assert len(recorded_calls) == expected_llm_calls, f"Failed call count on {scenario_name}"
    # Verify pre-hydrated MCP telemetry was actually embedded in the very first LLM call
    first_user_msg = recorded_calls[0]["messages"][1]["content"]
    assert "Pre-hydrated MCP Telemetry:" in first_user_msg
    assert "pod_status" in first_user_msg
    assert "pod_logs" in first_user_msg
    assert "pod_events" in first_user_msg
    assert "workload_spec" in first_user_msg

    assert len(report.mcp_traces) == expected_trace_count
    assert expected_diff_substr in (report.yaml_diff or "")
    assert report.confidence == expected_confidence


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_mode",
    ["rate_limit_exception", "exhausted_turns_no_solution"],
)
async def test_llm_failure_fallback_does_not_duplicate_mcp_traces(
    failure_mode: str,
    tmp_path,
    monkeypatch,
):
    manifest_file = tmp_path / "cache-svc.yaml"
    manifest_file.write_text(
        """apiVersion: apps/v1
kind: Deployment
metadata:
  name: cache-svc
spec:
  template:
    spec:
      containers:
        - name: redis
          image: redis:7-alpine
          resources:
            limits:
              memory: 64Mi
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(settings, "manifests_dir", str(tmp_path))
    monkeypatch.setattr(settings, "proposals_dir", str(tmp_path / "proposals"))
    monkeypatch.setattr(settings, "dry_run_mode", True)
    monkeypatch.setattr(settings, "github_token", None)
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test_fake_key")
    monkeypatch.setattr(settings, "llm_max_turns", 2)

    class FailingCompletions:
        async def create(self, **kwargs):
            if failure_mode == "rate_limit_exception":
                raise RuntimeError("429 Too Many Requests: TPM limit exceeded")
            # Return text with no tool calls so loop exits cleanly into fallback
            return _make_fake_response(tool_calls=None, content="Unable to decide")

    class FakeAsyncGroq:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FailingCompletions())

        async def close(self):
            return None

    import groq

    monkeypatch.setattr(groq, "AsyncGroq", FakeAsyncGroq)

    alert = AlertPayload(
        pod_name="cache-svc-7598c9f566-x9z2k",
        namespace="default",
        container_name="redis",
        reason="OOMKilled",
        exit_code=137,
    )
    report = await triage_agent.run_triage_pipeline(alert)

    # Pre-hydrated traces are reused in deterministic fallback (no duplicate 4+4=8 MCP traces!)
    assert len(report.mcp_traces) == 4
    assert "memory: 256Mi" in (report.yaml_diff or "")


@pytest.mark.asyncio
async def test_concurrent_crashes_are_serialized_by_semaphore(tmp_path, monkeypatch):
    for name in ("app-one", "app-two"):
        (tmp_path / f"{name}.yaml").write_text(
            f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
spec:
  template:
    spec:
      containers:
        - name: app
          image: python:3.12-alpine
          resources:
            limits:
              memory: 64Mi
""",
            encoding="utf-8",
        )

    monkeypatch.setattr(settings, "manifests_dir", str(tmp_path))
    monkeypatch.setattr(settings, "proposals_dir", str(tmp_path / "proposals"))
    monkeypatch.setattr(settings, "dry_run_mode", True)
    monkeypatch.setattr(settings, "github_token", None)
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test_fake_key")
    monkeypatch.setattr(settings, "llm_concurrency_limit", 1)

    in_flight = 0
    max_in_flight = 0

    class SlowFakeCompletions:
        async def create(self, **kwargs):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1
            return _make_fake_response(
                tool_calls=[
                    _make_fake_tool_call(
                        "call_ok",
                        "submit_triage_solution",
                        {
                            "root_cause_summary": "Scaled memory limit.",
                            "confidence": 0.95,
                            "modifications": [
                                {
                                    "container_name": "app",
                                    "parameter_path": "resources.limits.memory",
                                    "old_value": "64Mi",
                                    "new_value": "256Mi",
                                    "rationale": "Avoid OOM",
                                }
                            ],
                        },
                    )
                ]
            )

    class FakeAsyncGroq:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SlowFakeCompletions())

        async def close(self):
            return None

    import groq

    monkeypatch.setattr(groq, "AsyncGroq", FakeAsyncGroq)

    alerts = [
        AlertPayload(pod_name="app-one-7598c9f566-aaaa1", reason="OOMKilled", exit_code=137),
        AlertPayload(pod_name="app-two-7598c9f566-bbbb2", reason="OOMKilled", exit_code=137),
    ]
    reports = await asyncio.gather(*(triage_agent.run_triage_pipeline(a) for a in alerts))

    assert len(reports) == 2
    assert max_in_flight == 1
    assert all("memory: 256Mi" in (r.yaml_diff or "") for r in reports)


def test_compact_telemetry_for_llm_preserves_signal_and_source():
    raw_logs = "START=1\n" + ("X" * 1000) + "\nEND_CRASH=1"
    baseline = {
        "pod_status": {
            "status": "success",
            "phase": "Running",
            "pod_ip": "10.42.0.5",
            "host_ip": "10.0.0.1",
            "container_statuses": [{"container_name": "api", "restart_count": 3}],
        },
        "pod_events": {
            "status": "success",
            "events": [
                {"type": "Warning", "reason": "BackOff", "message": "restarting", "count": 4, "first_timestamp": "t0"}
            ],
        },
        "pod_logs": {"status": "success", "logs": raw_logs},
        "workload_spec": {
            "status": "success",
            "kind": "Deployment",
            "deployment_name": "api",
            "containers": [{"name": "api", "image": "python:3.12"}],
        },
    }

    compacted = triage_agent.compact_telemetry_for_llm(baseline, max_log_chars=250)

    # Original raw baseline logs must remain untouched for fallback engine
    assert len(baseline["pod_logs"]["logs"]) > 250
    assert len(compacted["pod_logs"]["logs"]) <= 250
    assert "START=1" in compacted["pod_logs"]["logs"]
    assert "END_CRASH=1" in compacted["pod_logs"]["logs"]
    assert "pod_ip" not in compacted["pod_status"]
    assert compacted["pod_events"]["events"][0]["reason"] == "BackOff"
    assert compacted["workload_spec"]["containers"][0]["name"] == "api"


@pytest.mark.asyncio
async def test_run_groq_triage_loop_standalone_autohydrates(tmp_path, monkeypatch):
    manifest_file = tmp_path / "payments.yaml"
    manifest_file.write_text(
        """apiVersion: apps/v1
kind: Deployment
metadata:
  name: payments
spec:
  template:
    spec:
      containers:
        - name: web
          image: nginx:1.25
          resources:
            limits:
              memory: 64Mi
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(settings, "manifests_dir", str(tmp_path))
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test_fake_key")

    seen_calls: List[Dict[str, Any]] = []

    class FakeCompletions:
        async def create(self, **kwargs):
            seen_calls.append(kwargs)
            return _make_fake_response(
                tool_calls=[
                    _make_fake_tool_call(
                        "call_1",
                        "submit_triage_solution",
                        {
                            "root_cause_summary": "Standalone call diagnosed OOM.",
                            "confidence": 0.92,
                            "modifications": [
                                {
                                    "container_name": "web",
                                    "parameter_path": "resources.limits.memory",
                                    "old_value": "64Mi",
                                    "new_value": "256Mi",
                                    "rationale": "Scale limit",
                                }
                            ],
                        },
                    )
                ]
            )

    class FakeAsyncGroq:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

        async def close(self):
            return None

    import groq

    monkeypatch.setattr(groq, "AsyncGroq", FakeAsyncGroq)

    alert = AlertPayload(pod_name="payments-7598c9f566-abcde", reason="OOMKilled", exit_code=137)
    root_cause, confidence, mods, traces = await triage_agent._run_groq_triage_loop(alert, manifest_file)

    assert len(seen_calls) == 1
    assert len(traces) == 4
    assert confidence == 0.92
    assert "Standalone call diagnosed OOM." in root_cause
    assert mods[0].new_value == "256Mi"


@pytest.mark.parametrize(
    "llm_provider,gemini_key,groq_key,expect_error,expected_provider,expected_model,expected_configured",
    [
        ("auto", "AIza_key", None, False, "gemini", "gemini-3.5-flash-lite", True),
        ("auto", None, "gsk_key", False, "groq", "openai/gpt-oss-120b", True),
        ("auto", "AIza_key", "gsk_key", False, "gemini", "gemini-3.5-flash-lite", True),
        ("auto", "   ", "   ", False, "gemini", "gemini-3.5-flash-lite", False),
        (" Gemini ", "AIza_key", "gsk_key", False, "gemini", "gemini-3.5-flash-lite", True),
        ("GROQ", "AIza_key", "gsk_key", False, "groq", "openai/gpt-oss-120b", True),
        ("gemini", None, "gsk_key", False, "gemini", "gemini-3.5-flash-lite", False),
        ("groq", "AIza_key", None, False, "groq", "openai/gpt-oss-120b", False),
        ("openai", "AIza_key", "gsk_key", True, "", "", False),
    ],
)
def test_settings_llm_provider_resolution_parameterized(
    llm_provider: str,
    gemini_key: Optional[str],
    groq_key: Optional[str],
    expect_error: bool,
    expected_provider: str,
    expected_model: str,
    expected_configured: bool,
):
    if expect_error:
        with pytest.raises(ValueError, match="Unsupported llm_provider"):
            Settings(
                _env_file=None,
                llm_provider=llm_provider,
                gemini_api_key=gemini_key,
                groq_api_key=groq_key,
            )
        return

    cfg_settings = Settings(
        _env_file=None,
        llm_provider=llm_provider,
        gemini_api_key=gemini_key,
        groq_api_key=groq_key,
    )
    resolved = cfg_settings.resolve_llm_config()
    assert resolved.provider == expected_provider
    assert resolved.model == expected_model
    assert resolved.is_configured is expected_configured


def _build_gemini_model_response(
    parts: List[Any],
    *,
    role: Optional[str] = "model",
):
    from google.genai import types

    content = types.Content(role=role, parts=parts)
    candidate = types.Candidate(content=content)
    return types.GenerateContentResponse(candidates=[candidate])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario_name,alert_kwargs,manifest_yaml,turn_builders,expected_llm_calls,expected_trace_count,expected_diff_substr,expected_confidence,expected_thought_sig",
    [
        # 1. Single-turn Gemini 3.5 Flash-Lite triage using pre-hydrated context
        (
            "gemini_single_turn_oom",
            {
                "pod_name": "billing-api-6f7b9c8d5-abcde",
                "namespace": "default",
                "container_name": "jvm",
                "reason": "OOMKilled",
                "exit_code": 137,
                "message": "Java heap exceeded 512Mi limit",
            },
            """apiVersion: apps/v1
kind: Deployment
metadata:
  name: billing-api
spec:
  template:
    spec:
      containers:
        - name: jvm
          image: eclipse-temurin:21-jre
          resources:
            limits:
              memory: 512Mi
""",
            [
                lambda types: _build_gemini_model_response([
                    types.Part(
                        thought=True,
                        text="Memory limit 512Mi exceeded; scaling to 1Gi.",
                    ),
                    types.Part(
                        thought_signature=b"sig-single-turn",
                        function_call=types.FunctionCall(
                            id="fc_1",
                            name="submit_triage_solution",
                            args={
                                "root_cause_summary": "JVM container exceeded 512Mi cgroup memory limit.",
                                "confidence": 0.96,
                                "modifications": [
                                    {
                                        "container_name": "jvm",
                                        "parameter_path": "resources.limits.memory",
                                        "old_value": "512Mi",
                                        "new_value": "1Gi",
                                        "rationale": "Provide 1Gi ceiling for JVM heap",
                                    }
                                ],
                            },
                        ),
                    ),
                ])
            ],
            1,
            4,
            "memory: 1Gi",
            0.96,
            None,
        ),
        # 2. Multi-turn Gemini 3+ preserving thought_signature across parallel tool calls + IDs
        (
            "gemini_multiturn_preserves_thought_signature_and_parallel_tools",
            {
                "pod_name": "orders-db-0",
                "namespace": "default",
                "container_name": "postgres",
                "reason": "CrashLoopBackOff",
                "exit_code": 2,
                "message": "PGDATA directory conflict",
            },
            """apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: orders-db
spec:
  template:
    spec:
      containers:
        - name: postgres
          image: postgres:16-alpine
          env:
            - name: PGDATA
              value: "/var/lib/postgresql/data"
""",
            [
                lambda types: _build_gemini_model_response([
                    types.Part(thought=True, text="Checking ConfigMap and logs before deciding."),
                    types.Part(
                        thought_signature=b"opaque-gemini3-signature-turn1",
                        function_call=types.FunctionCall(
                            id="fc_kubectl_1",
                            name="run_readonly_kubectl",
                            args={"command": "kubectl get configmap orders-config -n default"},
                        ),
                    ),
                    types.Part(
                        function_call=types.FunctionCall(
                            id="fc_logs_2",
                            name="get_pod_logs",
                            args={"pod_name": "orders-db-0", "namespace": "default", "tail_lines": 50},
                        ),
                    ),
                ]),
                lambda types: _build_gemini_model_response([
                    types.Part(
                        thought_signature=b"opaque-gemini3-signature-turn2",
                        function_call=types.FunctionCall(
                            id="fc_submit_3",
                            name="submit_triage_solution",
                            args={
                                "root_cause_summary": "PostgreSQL PGDATA points to mount root.",
                                "confidence": 1.25,
                                "modifications": [
                                    {
                                        "container_name": "postgres",
                                        "parameter_path": "env[PGDATA]",
                                        "old_value": "/var/lib/postgresql/data",
                                        "new_value": "/var/lib/postgresql/data/pgdata",
                                        "rationale": "Use dedicated PGDATA subdirectory",
                                    }
                                ],
                            },
                        ),
                    ),
                ]),
            ],
            2,
            6,
            "/var/lib/postgresql/data/pgdata",
            1.0,
            b"opaque-gemini3-signature-turn1",
        ),
        # 3. Gemini boundary/error recovery: blocked kubectl + empty args/no id, then recovers
        (
            "gemini_blocked_cmd_and_empty_args_recovery",
            {
                "pod_name": "worker-job-k8x9q",
                "namespace": "default",
                "container_name": "runner",
                "reason": "Error",
                "exit_code": 127,
                "message": "image tag pull failed",
            },
            """apiVersion: batch/v1
kind: Job
metadata:
  name: worker-job
spec:
  template:
    spec:
      containers:
        - name: runner
          image: myorg/worker:broken-tag
""",
            [
                lambda types: _build_gemini_model_response(
                    [
                        types.Part(
                            thought_signature=b"sig-recover",
                            function_call=types.FunctionCall(
                                id=None,
                                name="run_readonly_kubectl",
                                args={"command": "kubectl delete pod worker-job-k8x9q"},
                            ),
                        ),
                        types.Part(
                            function_call=types.FunctionCall(
                                id=None,
                                name="get_pod_status",
                                args=None,
                            ),
                        ),
                    ],
                    role=None,  # boundary: candidate content with role=None normalized to 'model'
                ),
                lambda types: _build_gemini_model_response([
                    types.Part(
                        function_call=types.FunctionCall(
                            name="submit_triage_solution",
                            args={
                                "root_cause_summary": "Recovered after blocked command and missing args.",
                                "confidence": -0.5,
                                "modifications": [
                                    {
                                        "container_name": "runner",
                                        "parameter_path": "image",
                                        "old_value": "myorg/worker:broken-tag",
                                        "new_value": "myorg/worker:latest",
                                        "rationale": "Switch to valid release image tag",
                                    }
                                ],
                            },
                        ),
                    ),
                ]),
            ],
            2,
            6,
            "myorg/worker:latest",
            0.0,
            b"sig-recover",
        ),
    ],
)
async def test_gemini_triage_provider_parameterized(
    scenario_name: str,
    alert_kwargs: Dict[str, Any],
    manifest_yaml: str,
    turn_builders: List[Any],
    expected_llm_calls: int,
    expected_trace_count: int,
    expected_diff_substr: str,
    expected_confidence: float,
    expected_thought_sig: Optional[bytes],
    tmp_path: Path,
    monkeypatch,
):
    from google import genai
    from google.genai import types

    workload_name = triage_agent.infer_workload_name_from_pod(alert_kwargs["pod_name"])
    (tmp_path / f"{workload_name}.yaml").write_text(manifest_yaml, encoding="utf-8")

    monkeypatch.setattr(settings, "manifests_dir", str(tmp_path))
    monkeypatch.setattr(settings, "proposals_dir", str(tmp_path / "proposals"))
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "AIza_fake_test_key")
    monkeypatch.setattr(settings, "gemini_model", "gemini-3.5-flash-lite")
    monkeypatch.setattr(settings, "llm_max_turns", 4)
    monkeypatch.setattr(settings, "llm_timeout_seconds", 25.0)
    monkeypatch.setattr(settings, "llm_max_retries", 2)

    client_init_kwargs: List[Dict[str, Any]] = []
    recorded_calls: List[Dict[str, Any]] = []
    closed_flags = {"aio": False, "sync": False}

    class FakeAsyncModels:
        async def generate_content(self, *, model, contents, config):
            turn_idx = len(recorded_calls)
            # Deep-snapshot contents list at call time so later appends don't mutate snapshot
            recorded_calls.append({
                "model": model,
                "contents": [c.model_copy(deep=True) for c in contents],
                "config": config,
            })
            return turn_builders[turn_idx](types)

    class FakeAsyncClient:
        def __init__(self):
            self.models = FakeAsyncModels()

        async def aclose(self):
            closed_flags["aio"] = True

    class FakeGenAIClient:
        def __init__(self, **kwargs):
            client_init_kwargs.append(kwargs)
            self.aio = FakeAsyncClient()

        def close(self):
            closed_flags["sync"] = True

    monkeypatch.setattr(genai, "Client", FakeGenAIClient)

    report = await triage_agent.run_triage_pipeline(AlertPayload(**alert_kwargs))

    assert len(recorded_calls) == expected_llm_calls, f"Unexpected call count for {scenario_name}"
    assert client_init_kwargs[0]["api_key"] == "AIza_fake_test_key"
    http_opts = client_init_kwargs[0]["http_options"]
    assert http_opts.timeout == 25000
    assert http_opts.retry_options.attempts == 3

    first_call = recorded_calls[0]
    assert first_call["model"] == "gemini-3.5-flash-lite"
    assert first_call["config"].automatic_function_calling.disable is True
    assert "Pre-hydrated MCP Telemetry:" in first_call["contents"][0].parts[0].text
    assert "Pre-hydrated MCP Telemetry:" not in first_call["config"].system_instruction

    if expected_thought_sig is not None:
        assert len(recorded_calls) >= 2
        turn2_contents = recorded_calls[1]["contents"]
        # [0]=initial user, [1]=model turn 1, [2]=user function_response turn 1
        assert turn2_contents[1].role == "model"
        sig_parts = [p.thought_signature for p in turn2_contents[1].parts if p.thought_signature]
        assert expected_thought_sig in sig_parts
        assert turn2_contents[2].role == "user"
        assert all(p.function_response is not None for p in turn2_contents[2].parts)
        assert len(turn2_contents[2].parts) == 2

    assert closed_flags["aio"] is True
    assert closed_flags["sync"] is True
    assert report.llm_provider == "gemini"
    assert report.llm_model == "gemini-3.5-flash-lite"
    assert len(report.mcp_traces) == expected_trace_count
    assert expected_diff_substr in (report.yaml_diff or "")
    assert report.confidence == expected_confidence

    proposal_md = (Path(report.local_proposal_path) / "PROPOSAL.md").read_text(encoding="utf-8")
    assert "Gemini (gemini-3.5-flash-lite)" in proposal_md


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_mode,llm_provider,gemini_key,groq_key",
    [
        ("api_exception", "gemini", "AIza_fake_test_key", None),
        ("empty_candidates", "gemini", "AIza_fake_test_key", None),
        ("text_only_no_tools", "gemini", "AIza_fake_test_key", None),
        ("explicit_gemini_missing_key", "gemini", None, "gsk_should_not_be_used"),
        ("invalid_provider_setting", "unknown_provider", "AIza_fake_test_key", "gsk_should_not_be_used"),
    ],
)
async def test_gemini_failure_and_misconfig_fallback_parameterized(
    failure_mode: str,
    llm_provider: str,
    gemini_key: Optional[str],
    groq_key: Optional[str],
    tmp_path: Path,
    monkeypatch,
):
    from google import genai
    from google.genai import types
    import groq

    manifest_file = tmp_path / "cache-svc.yaml"
    manifest_file.write_text(
        """apiVersion: apps/v1
kind: Deployment
metadata:
  name: cache-svc
spec:
  template:
    spec:
      containers:
        - name: redis
          image: redis:7-alpine
          resources:
            limits:
              memory: 64Mi
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(settings, "manifests_dir", str(tmp_path))
    monkeypatch.setattr(settings, "proposals_dir", str(tmp_path / "proposals"))
    monkeypatch.setattr(settings, "llm_provider", llm_provider)
    monkeypatch.setattr(settings, "gemini_api_key", gemini_key)
    monkeypatch.setattr(settings, "groq_api_key", groq_key)
    monkeypatch.setattr(settings, "llm_max_turns", 2)

    closed_aio = False
    groq_called = False

    class FakeAsyncModels:
        async def generate_content(self, **kwargs):
            if failure_mode == "api_exception":
                raise RuntimeError("429 Resource Exhausted")
            if failure_mode == "empty_candidates":
                return types.GenerateContentResponse(candidates=[])
            return _build_gemini_model_response([types.Part.from_text(text="Need more context")])

    class FakeAsyncClient:
        def __init__(self):
            self.models = FakeAsyncModels()

        async def aclose(self):
            nonlocal closed_aio
            closed_aio = True

    class FakeGenAIClient:
        def __init__(self, **kwargs):
            self.aio = FakeAsyncClient()

    class GuardAsyncGroq:
        def __init__(self, **kwargs):
            nonlocal groq_called
            groq_called = True

    monkeypatch.setattr(genai, "Client", FakeGenAIClient)
    monkeypatch.setattr(groq, "AsyncGroq", GuardAsyncGroq)

    alert = AlertPayload(
        pod_name="cache-svc-7598c9f566-x9z2k",
        namespace="default",
        container_name="redis",
        reason="OOMKilled",
        exit_code=137,
    )
    report = await triage_agent.run_triage_pipeline(alert)

    assert groq_called is False
    if gemini_key and llm_provider == "gemini":
        assert closed_aio is True
    assert len(report.mcp_traces) == 4
    assert "memory: 256Mi" in (report.yaml_diff or "")
    assert report.llm_provider == "deterministic"
    assert report.llm_model is None
    proposal_md = (Path(report.local_proposal_path) / "PROPOSAL.md").read_text(encoding="utf-8")
    assert "Deterministic Diagnostic Engine" in proposal_md


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "llm_provider,gemini_key,groq_key,expected_provider,expected_model,expected_footer",
    [
        ("auto", "AIza_key", "gsk_key", "gemini", "gemini-3.5-flash-lite", "Gemini (gemini-3.5-flash-lite)"),
        ("auto", None, "gsk_key", "groq", "openai/gpt-oss-120b", "Groq (openai/gpt-oss-120b)"),
        ("groq", "AIza_key", "gsk_key", "groq", "openai/gpt-oss-120b", "Groq (openai/gpt-oss-120b)"),
    ],
)
async def test_provider_switching_pipeline_parameterized(
    llm_provider: str,
    gemini_key: Optional[str],
    groq_key: Optional[str],
    expected_provider: str,
    expected_model: str,
    expected_footer: str,
    tmp_path: Path,
    monkeypatch,
):
    from google import genai
    from google.genai import types
    import groq

    (tmp_path / "edge-api.yaml").write_text(
        """apiVersion: apps/v1
kind: Deployment
metadata:
  name: edge-api
spec:
  template:
    spec:
      containers:
        - name: api
          image: python:3.12-alpine
          resources:
            limits:
              memory: 64Mi
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(settings, "manifests_dir", str(tmp_path))
    monkeypatch.setattr(settings, "proposals_dir", str(tmp_path / "proposals"))
    monkeypatch.setattr(settings, "llm_provider", llm_provider)
    monkeypatch.setattr(settings, "gemini_api_key", gemini_key)
    monkeypatch.setattr(settings, "groq_api_key", groq_key)

    solution_payload = {
        "root_cause_summary": "Scaled edge-api memory ceiling.",
        "confidence": 0.91,
        "modifications": [
            {
                "container_name": "api",
                "parameter_path": "resources.limits.memory",
                "old_value": "64Mi",
                "new_value": "256Mi",
                "rationale": "Avoid OOM termination",
            }
        ],
    }

    invoked: List[str] = []

    class FakeGenAIClient:
        def __init__(self, **kwargs):
            invoked.append("gemini")
            self.aio = SimpleNamespace(
                models=SimpleNamespace(generate_content=self._gen),
                aclose=self._aclose,
            )

        async def _gen(self, **kwargs):
            return _build_gemini_model_response([
                types.Part(
                    function_call=types.FunctionCall(
                        name="submit_triage_solution",
                        args=solution_payload,
                    )
                )
            ])

        async def _aclose(self):
            return None

    class FakeAsyncGroq:
        def __init__(self, **kwargs):
            invoked.append("groq")
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create),
            )

        async def _create(self, **kwargs):
            return _make_fake_response(
                tool_calls=[
                    _make_fake_tool_call("call_groq", "submit_triage_solution", solution_payload)
                ]
            )

        async def close(self):
            return None

    monkeypatch.setattr(genai, "Client", FakeGenAIClient)
    monkeypatch.setattr(groq, "AsyncGroq", FakeAsyncGroq)

    report = await triage_agent.run_triage_pipeline(
        AlertPayload(pod_name="edge-api-7598c9f566-abcde", reason="OOMKilled", exit_code=137)
    )

    assert invoked == [expected_provider]
    assert report.llm_provider == expected_provider
    assert report.llm_model == expected_model
    proposal_md = (Path(report.local_proposal_path) / "PROPOSAL.md").read_text(encoding="utf-8")
    assert expected_footer in proposal_md


@pytest.mark.asyncio
async def test_gemini_session_preserves_thought_signature_in_mldev_wire_payload(monkeypatch):
    """Verify the real google-genai request serializer preserves thoughtSignature and MCP tool schemas."""
    from google import genai
    from google.genai import _common, models, types

    real_client = genai.Client(api_key="AIza_serialization_test_key")
    try:
        gemini_tools = await triage_agent.discover_mcp_tools_for_gemini()
        session = triage_agent.GeminiProviderSession(
            api_key="AIza_serialization_test_key",
            model="gemini-3.5-flash-lite",
            system_prompt="System prompt",
            user_prompt="User prompt",
            gemini_tools=gemini_tools,
        )

        # Simulate a Gemini 3+ response containing thought_signature + function_call
        sig_bytes = b"gemini-3.5-encrypted-thought-signature"
        model_response = _build_gemini_model_response([
            types.Part(
                thought=True,
                text="Inspecting cluster configmap.",
            ),
            types.Part(
                thought_signature=sig_bytes,
                function_call=types.FunctionCall(
                    id="fc_mldev_1",
                    name="run_readonly_kubectl",
                    args={"command": "kubectl get configmaps -n default"},
                ),
            ),
        ])

        async def _fake_generate_content(*, model, contents, config):
            return model_response

        monkeypatch.setattr(session._client.aio.models, "generate_content", _fake_generate_content)

        calls = await session.next_tool_calls()
        assert len(calls) == 1
        assert calls[0].name == "run_readonly_kubectl"
        session.append_tool_results([(calls[0], {"status": "success", "stdout": "cm/app"})])

        params = types._GenerateContentParameters(
            model=session.model_name,
            contents=session._contents,
            config=session._config,
        )
        wire_dict = models._GenerateContentParameters_to_mldev(
            real_client._api_client, params, None, params
        )
        encoded = _common.encode_unserializable_types(_common.convert_to_dict(wire_dict))

        assert encoded["tools"][0]["functionDeclarations"]
        model_turn = encoded["contents"][1]
        assert model_turn["role"] == "model"
        assert "thoughtSignature" in model_turn["parts"][1]
        tool_turn = encoded["contents"][2]
        assert tool_turn["role"] == "user"
        assert tool_turn["parts"][0]["functionResponse"]["id"] == "fc_mldev_1"

        await session.aclose()
    finally:
        real_client.close()



