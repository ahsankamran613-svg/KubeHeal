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


