"""FastAPI Application for KubeHeal: Webhook Ingestion, Simulations, Guardrail Audit & Live Dashboard."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from kubeheal.agent.triage_agent import run_triage_pipeline
from kubeheal.config import settings
from kubeheal.mcp_server.guardrails import validate_kubectl_command
from kubeheal.mcp_server.server import mcp, run_readonly_kubectl
from kubeheal.models import AlertPayload, MCPToolExecution, TriageReport
from kubeheal.watcher.k8s_watcher import KubernetesWatcher

# In-memory incident store
incident_store: Dict[str, TriageReport] = {}
watcher_instance: Optional[KubernetesWatcher] = None


async def handle_alert(alert: AlertPayload):
    """Asynchronous background alert handler."""
    report = await run_triage_pipeline(alert)
    incident_store[report.incident_id] = report
    return report


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Launch K8s watcher if enabled
    global watcher_instance
    if settings.enable_k8s_watcher:
        watcher_instance = KubernetesWatcher(on_alert_callback=handle_alert)
        asyncio.create_task(watcher_instance.start())
    yield
    # Shutdown
    if watcher_instance:
        watcher_instance.stop()


app = FastAPI(
    title="KubeHeal AIOps Agent",
    description="Guardrailed Kubernetes AIOps Triage Agent with MCP and GitOps Remediation",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serve the KubeHeal Live Triage Dashboard."""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse("<h1>KubeHeal Dashboard</h1><p>Dashboard template initializing...</p>")


@app.get("/api/health")
async def health_check():
    """System health check and guardrail status."""
    from kubeheal.mcp_server.server import _get_k8s_clients
    core_v1, _ = _get_k8s_clients()

    return {
        "status": "healthy",
        "cluster_connected": core_v1 is not None,
        "k8s_watcher_enabled": settings.enable_k8s_watcher,
        "groq_configured": bool(settings.groq_api_key and settings.groq_api_key.strip()),
        "groq_model": settings.groq_model,
        "dry_run_mode": settings.dry_run_mode,
        "github_configured": bool(settings.github_token and settings.github_repo),
        "guardrails": {
            "mcp_read_only": "enforced",
            "rbac_verbs": ["get", "list", "watch"],
            "max_memory_cap": "1Gi",
        },
    }


@app.get("/api/incidents", response_model=List[TriageReport])
async def list_incidents():
    """Retrieve all triage reports ordered by most recent first."""
    return list(reversed(list(incident_store.values())))


@app.get("/api/incidents/{incident_id}", response_model=TriageReport)
async def get_incident(incident_id: str):
    """Retrieve a specific triage report by incident ID."""
    if incident_id not in incident_store:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident_store[incident_id]


@app.post("/api/webhook/alert", response_model=TriageReport)
async def receive_alert(alert: AlertPayload):
    """Receive an alert webhook from Kubernetes, Alertmanager, or monitoring systems."""
    report = await run_triage_pipeline(alert)
    incident_store[report.incident_id] = report
    return report


@app.post("/api/simulate/{scenario}", response_model=TriageReport)
async def simulate_crash_scenario(scenario: str):
    """One-click simulation of real-world Kubernetes failure scenarios."""
    if scenario == "oom-crash":
        alert = AlertPayload(
            pod_name="oom-crash-app-7598c9f566-x9z2k",
            namespace=settings.kube_namespace,
            deployment_name="oom-crash-app",
            container_name="worker",
            reason="OOMKilled",
            exit_code=137,
            restart_count=4,
            message="Container worker exceeded 32Mi cgroup limit while allocating memory buffer (~65MB). Terminated with SIGKILL by kernel OOM killer.",
        )
    elif scenario == "config-crash":
        alert = AlertPayload(
            pod_name="config-crash-app-684cbf9b4d-j7m4q",
            namespace=settings.kube_namespace,
            deployment_name="config-crash-app",
            container_name="api-server",
            reason="CrashLoopBackOff",
            exit_code=1,
            restart_count=6,
            message="FATAL ERROR: Invalid port configuration 'invalid_port': invalid literal for int() with base 10",
        )
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown scenario '{scenario}'. Available: 'oom-crash', 'config-crash'",
        )

    report = await run_triage_pipeline(alert)
    incident_store[report.incident_id] = report
    return report


class GuardrailTestRequest(BaseModel):
    command: str = "kubectl delete pod --all -n default"


@app.post("/api/guardrail-test")
async def test_guardrail(req: GuardrailTestRequest):
    """Simulate a mutating or malicious command to verify immediate blocking by the MCP guardrail."""
    is_valid, reason = validate_kubectl_command(req.command)
    result = run_readonly_kubectl(command=req.command)

    # Log to incident traces if an active incident exists
    trace = MCPToolExecution(
        tool_name="run_readonly_kubectl",
        arguments={"command": req.command},
        blocked_by_guardrail=not is_valid,
        status="blocked" if not is_valid else "success",
        output_preview=reason if not is_valid else str(result.get("stdout", ""))[:200],
    )

    return {
        "command": req.command,
        "is_allowed": is_valid,
        "guardrail_verdict": "BLOCKED" if not is_valid else "ALLOWED",
        "reason": reason,
        "execution_result": result,
        "audit_trace": trace,
    }
