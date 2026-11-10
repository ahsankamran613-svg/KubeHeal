"""Integration tests for FastAPI endpoints, webhook alerts, and simulations."""

import pytest
from starlette.testclient import TestClient
from kubeheal.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_health_endpoint(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
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
    assert len(data["mcp_traces"]) > 0
    assert len(data["proposed_fix"]) > 0


def test_simulate_config_crash(client: TestClient):
    response = client.post("/api/simulate/config-crash")
    assert response.status_code == 200
    data = response.json()
    assert "incident_id" in data
    assert data["status"] == "completed"
    assert data["alert"]["reason"] == "CrashLoopBackOff"
    assert data["confidence"] > 0.8


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
    # Ensure at least one incident is generated
    client.post("/api/simulate/oom-crash")
    response = client.get("/api/incidents")
    assert response.status_code == 200
    incidents = response.json()
    assert isinstance(incidents, list)
    assert len(incidents) >= 1
