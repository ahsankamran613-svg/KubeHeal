"""Integration tests for FastAPI endpoints, MCP tool discovery, webhook alerts, and simulations."""

import pytest
from starlette.testclient import TestClient
from kubeheal.agent.triage_agent import (
    discover_mcp_tools_for_gemini,
    discover_mcp_tools_for_groq,
)
from kubeheal.config import settings
from kubeheal.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Isolate tests from local .env credentials so pytest never calls live LLM/GitHub APIs
    monkeypatch.setattr(settings, "dry_run_mode", True)
    monkeypatch.setattr(settings, "github_token", None)
    monkeypatch.setattr(settings, "llm_provider", "auto")
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "groq_api_key", None)
    monkeypatch.setattr(settings, "proposals_dir", str(tmp_path / "proposals"))
    return TestClient(app)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["groq", "gemini"])
async def test_dynamic_mcp_tool_discovery(provider: str):
    expected_names = {
        "get_pod_status",
        "get_pod_logs",
        "get_pod_events",
        "get_deployment_spec",
        "run_readonly_kubectl",
        "submit_triage_solution",
    }

    if provider == "groq":
        tools = await discover_mcp_tools_for_groq()
        names = {t["function"]["name"] for t in tools}
    else:
        tools = await discover_mcp_tools_for_gemini()
        assert len(tools) == 1
        declarations = tools[0].function_declarations or []
        names = {fd.name for fd in declarations}
        for fd in declarations:
            assert fd.parameters_json_schema is not None
            assert fd.parameters_json_schema.get("type") == "object"

    assert expected_names.issubset(names)


@pytest.mark.parametrize(
    "llm_provider,gemini_key,groq_key,expected_provider,expected_model,expected_configured,expected_mode",
    [
        ("auto", None, None, "gemini", "gemini-3.5-flash-lite", False, "deterministic_fallback"),
        ("auto", "AIza_fake_key", None, "gemini", "gemini-3.5-flash-lite", True, "ai"),
        ("auto", None, "gsk_fake_key", "groq", "openai/gpt-oss-120b", True, "ai"),
        ("auto", "AIza_fake_key", "gsk_fake_key", "gemini", "gemini-3.5-flash-lite", True, "ai"),
        ("groq", "AIza_fake_key", "gsk_fake_key", "groq", "openai/gpt-oss-120b", True, "ai"),
        ("gemini", None, "gsk_fake_key", "gemini", "gemini-3.5-flash-lite", False, "deterministic_fallback"),
        ("invalid_provider", None, None, "invalid_provider", "", False, "deterministic_fallback"),
    ],
)
def test_health_endpoint_provider_reporting(
    client: TestClient,
    monkeypatch,
    llm_provider: str,
    gemini_key,
    groq_key,
    expected_provider: str,
    expected_model: str,
    expected_configured: bool,
    expected_mode: str,
):
    monkeypatch.setattr(settings, "llm_provider", llm_provider)
    monkeypatch.setattr(settings, "gemini_api_key", gemini_key)
    monkeypatch.setattr(settings, "groq_api_key", groq_key)

    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["mcp_tool_count"] == 5
    assert "get_pod_status" in data["mcp_tools"]
    assert "guardrails" in data
    assert data["guardrails"]["mcp_read_only"] == "enforced"
    assert data["llm_provider"] == expected_provider
    assert data["llm_model"] == expected_model
    assert data["llm_configured"] is expected_configured
    assert data["llm_mode"] == expected_mode


def test_simulate_oom_crash(client: TestClient):
    response = client.post("/api/simulate/oom-crash")
    assert response.status_code == 200
    data = response.json()
    assert "incident_id" in data
    assert data["status"] == "completed"
    assert data["alert"]["reason"] == "OOMKilled"
    assert data["confidence"] > 0.8
    assert len(data["mcp_traces"]) == 4
    assert len(data["proposed_fix"]) >= 2
    assert "+              memory: 128Mi" in data["yaml_diff"]


def test_simulate_config_crash(client: TestClient):
    response = client.post("/api/simulate/config-crash")
    assert response.status_code == 200
    data = response.json()
    assert "incident_id" in data
    assert data["status"] == "completed"
    assert data["alert"]["reason"] == "CrashLoopBackOff"
    assert data["confidence"] > 0.8
    assert len(data["mcp_traces"]) == 4
    assert 'value: "8080"' in data["yaml_diff"] or "value: 8080" in data["yaml_diff"]


def test_guardrail_test_endpoint_blocked(client: TestClient):
    response = client.post(
        "/api/guardrail-test",
        json={"command": "kubectl delete pod --all -n default"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["guardrail_verdict"] == "BLOCKED"
    assert data["is_allowed"] is False
    assert "strictly forbidden" in data["reason"]
    assert data["audit_trace"]["blocked_by_guardrail"] is True


def test_guardrail_test_endpoint_allowed(client: TestClient):
    response = client.post(
        "/api/guardrail-test",
        json={"command": "kubectl get pods -n default"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["guardrail_verdict"] == "ALLOWED"
    assert data["is_allowed"] is True


def test_incidents_list(client: TestClient):
    client.post("/api/simulate/oom-crash")
    response = client.get("/api/incidents")
    assert response.status_code == 200
    incidents = response.json()
    assert isinstance(incidents, list)
    assert len(incidents) >= 1


def test_custom_workload_webhook_no_hardcoded_port(client: TestClient, tmp_path, monkeypatch):
    """Verify a real user's StatefulSet or custom app crashing with exit code 127 or 256Mi OOM
    is dynamically diagnosed without injecting hardcoded REQUIRED_PORT or shrinking memory to 128Mi."""
    from kubeheal.config import settings
    from kubeheal.watcher.k8s_watcher import FAILURE_WAITING_REASONS

    assert "ImagePullBackOff" in FAILURE_WAITING_REASONS
    assert "RunContainerError" in FAILURE_WAITING_REASONS

    # Create a custom StatefulSet manifest with 256Mi memory limit
    custom_manifest = tmp_path / "analytics-worker.yaml"
    custom_manifest.write_text(
        """apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: analytics-worker
  namespace: default
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: istio-proxy
          image: istio/proxyv2:1.20
        - name: engine
          image: python:3.12-slim
          resources:
            requests:
              memory: 128Mi
            limits:
              memory: 256Mi
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "manifests_dir", str(tmp_path))

    # Send OOM alert for StatefulSet pod 'analytics-worker-0'
    response = client.post(
        "/api/webhook/alert",
        json={
            "pod_name": "analytics-worker-0",
            "namespace": "default",
            "container_name": "engine",
            "reason": "OOMKilled",
            "exit_code": 137,
            "restart_count": 2,
            "message": "Container engine exceeded its 256Mi memory limit",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["alert"]["deployment_name"] == "analytics-worker"
    # Should scale 256Mi -> 512Mi (2x), NOT hardcode 32Mi -> 128Mi!
    assert "+              memory: 512Mi" in data["yaml_diff"] or "+            memory: 512Mi" in data["yaml_diff"]
    assert "REQUIRED_PORT" not in data["yaml_diff"]


def test_dashboard_uses_provider_agnostic_ui(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert "currentGroqModel" not in html
    assert "Groq triage agent" not in html
    assert "formatProviderName" in html
    assert "formatIncidentEngineLabel" in html


@pytest.mark.parametrize(
    "scenario,live_pods,expected_pod_name,expected_deployment,expected_container,expected_reason,expected_exit_code",
    [
        # Happy path 1: Live cluster has crashing config-crash-app pod (fixes PR #20 404 bug)
        (
            "config-crash",
            [
                {
                    "name": "config-crash-app-576d788f98-7hfbx",
                    "labels": {"app": "config-crash-app"},
                    "container_name": "api-server",
                    "restart_count": 7,
                    "waiting_reason": "CrashLoopBackOff",
                    "terminated_reason": "Error",
                    "exit_code": 1,
                    "message": "Invalid port configuration",
                }
            ],
            "config-crash-app-576d788f98-7hfbx",
            "config-crash-app",
            "api-server",
            "CrashLoopBackOff",
            1,
        ),
        # Happy path 2: Live cluster has crashing oom-crash-app pod
        (
            "oom-crash",
            [
                {
                    "name": "oom-crash-app-8cbf74b46-5x5qd",
                    "labels": {"app": "oom-crash-app"},
                    "container_name": "worker",
                    "restart_count": 3,
                    "waiting_reason": "CrashLoopBackOff",
                    "terminated_reason": "OOMKilled",
                    "exit_code": 137,
                    "message": "Container worker exceeded memory limit",
                }
            ],
            "oom-crash-app-8cbf74b46-5x5qd",
            "oom-crash-app",
            "worker",
            "OOMKilled",
            137,
        ),
        # Boundary case 1: Live pod exists in cluster but container_statuses is empty (newly scheduled)
        (
            "config-crash",
            [
                {
                    "name": "config-crash-app-9988776655-z1y2x",
                    "labels": {"app": "config-crash-app"},
                    "container_name": None,
                }
            ],
            "config-crash-app-9988776655-z1y2x",
            "config-crash-app",
            "api-server",
            "CrashLoopBackOff",
            1,
        ),
        # Boundary case 2: Live cluster has only unrelated pods -> falls back to manifest + simulated_pod_suffix
        (
            "oom-crash",
            [
                {
                    "name": "unrelated-service-1234567890-abcde",
                    "labels": {"app": "unrelated-service"},
                    "container_name": "web",
                }
            ],
            "oom-crash-app-sim01",
            "oom-crash-app",
            "worker",
            "OOMKilled",
            137,
        ),
    ],
)
def test_resolve_simulation_alert_live_and_boundary_cases(
    monkeypatch,
    scenario: str,
    live_pods,
    expected_pod_name: str,
    expected_deployment: str,
    expected_container: str,
    expected_reason: str,
    expected_exit_code: int,
):
    from types import SimpleNamespace
    from kubeheal.mcp_server import server as mcp_server

    fake_items = []
    for p in live_pods:
        statuses = []
        if p.get("container_name"):
            waiting = SimpleNamespace(reason=p.get("waiting_reason"), message=p.get("message")) if p.get("waiting_reason") else None
            terminated = (
                SimpleNamespace(
                    reason=p.get("terminated_reason"),
                    exit_code=p.get("exit_code"),
                    message=p.get("message"),
                )
                if p.get("terminated_reason") or p.get("exit_code") is not None
                else None
            )
            statuses.append(
                SimpleNamespace(
                    name=p["container_name"],
                    restart_count=p.get("restart_count", 1),
                    state=SimpleNamespace(waiting=waiting, terminated=None),
                    last_state=SimpleNamespace(terminated=terminated),
                )
            )
        fake_items.append(
            SimpleNamespace(
                metadata=SimpleNamespace(
                    name=p["name"],
                    labels=p.get("labels", {}),
                    owner_references=None,
                ),
                status=SimpleNamespace(container_statuses=statuses),
            )
        )

    fake_core_v1 = SimpleNamespace(
        list_namespaced_pod=lambda namespace: SimpleNamespace(items=fake_items)
    )
    monkeypatch.setattr(mcp_server, "_get_k8s_clients", lambda: (fake_core_v1, None))

    alert = mcp_server.resolve_simulation_alert(scenario, namespace="default")
    assert alert is not None
    assert alert.pod_name == expected_pod_name
    assert alert.deployment_name == expected_deployment
    assert alert.container_name == expected_container
    assert alert.reason == expected_reason
    assert alert.exit_code == expected_exit_code


@pytest.mark.parametrize(
    "invalid_scenario",
    [
        "nonexistent-scenario",
        "unknown-workload-xyz",
        "   ",
    ],
)
def test_simulate_endpoint_error_cases(client: TestClient, invalid_scenario: str):
    response = client.post(f"/api/simulate/{invalid_scenario}")
    assert response.status_code == 400
    assert "Unknown scenario" in response.json()["detail"]


@pytest.mark.parametrize(
    "remediation_port,small_scale_factor,unparseable_fallback,sim_suffix",
    [
        ("9090", 4, "256Mi", "test1"),
        ("3000", 2, "512Mi", "pod99"),
    ],
)
def test_configurable_remediation_settings_override(
    client: TestClient,
    monkeypatch,
    remediation_port: str,
    small_scale_factor: int,
    unparseable_fallback: str,
    sim_suffix: str,
):
    from kubeheal.gitops.yaml_patcher import clamp_memory_limit

    monkeypatch.setattr(settings, "default_remediation_port", remediation_port)
    monkeypatch.setattr(settings, "small_memory_scale_factor", small_scale_factor)
    monkeypatch.setattr(settings, "unparseable_memory_fallback", unparseable_fallback)
    monkeypatch.setattr(settings, "simulated_pod_suffix", sim_suffix)

    clamped, note = clamp_memory_limit("not-a-memory-string")
    assert clamped == unparseable_fallback
    assert note is not None

    cfg_resp = client.post("/api/simulate/config-crash")
    assert cfg_resp.status_code == 200
    cfg_data = cfg_resp.json()
    assert cfg_data["alert"]["pod_name"] == f"config-crash-app-{sim_suffix}"
    assert f'value: "{remediation_port}"' in cfg_data["yaml_diff"] or f"value: {remediation_port}" in cfg_data["yaml_diff"]



