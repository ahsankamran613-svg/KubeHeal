"""Integration tests for FastAPI endpoints, MCP tool discovery, webhook alerts, and simulations."""

import pytest
from starlette.testclient import TestClient
from kubeheal.agent.triage_agent import discover_mcp_tools_for_groq
from kubeheal.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.mark.asyncio
async def test_dynamic_mcp_tool_discovery():
    tools = await discover_mcp_tools_for_groq()
    names = [t["function"]["name"] for t in tools]
    assert "get_pod_status" in names
    assert "get_pod_logs" in names
    assert "get_pod_events" in names
    assert "get_deployment_spec" in names
    assert "run_readonly_kubectl" in names
    assert "submit_triage_solution" in names


def test_health_endpoint(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["mcp_tool_count"] == 5
    assert "get_pod_status" in data["mcp_tools"]
    assert "guardrails" in data
    assert data["guardrails"]["mcp_read_only"] == "enforced"


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
