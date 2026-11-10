from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class AlertPayload(BaseModel):
    """Payload received from a Kubernetes watcher, webhook, or test simulator."""
    pod_name: str = Field(..., description="Name of the crashing pod")
    namespace: str = Field(default="default", description="Kubernetes namespace")
    deployment_name: Optional[str] = Field(default=None, description="Parent deployment name if known")
    container_name: Optional[str] = Field(default=None, description="Crashing container name")
    reason: str = Field(..., description="Failure reason, e.g. OOMKilled, CrashLoopBackOff, Error")
    exit_code: Optional[int] = Field(default=None, description="Container termination exit code")
    restart_count: Optional[int] = Field(default=0, description="Container restart count")
    message: Optional[str] = Field(default=None, description="Diagnostic message or event text")
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class MCPToolExecution(BaseModel):
    """Audit log entry capturing every read-only MCP tool invocation and guardrail check."""
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    output_preview: str = ""
    blocked_by_guardrail: bool = False
    status: str = Field(default="success", description="'success', 'blocked', or 'error'")
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class YAMLModification(BaseModel):
    """Structured patch change for deployment YAML remediation."""
    file_path: str = Field(..., description="Relative or absolute path to the manifest file")
    container_name: Optional[str] = Field(default=None, description="Target container name")
    parameter_path: str = Field(..., description="e.g. 'resources.limits.memory' or 'env[PORT]'")
    old_value: Any = None
    new_value: Any = None
    rationale: str = Field(..., description="Why this modification resolves the incident")


class TriageReport(BaseModel):
    """Comprehensive incident triage report combining AI diagnosis, MCP traces, and GitOps remediation."""
    incident_id: str
    status: str = Field(default="triaging", description="'triaging', 'completed', 'failed', 'blocked'")
    alert: AlertPayload
    root_cause_summary: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    mcp_traces: List[MCPToolExecution] = Field(default_factory=list)
    proposed_fix: List[YAMLModification] = Field(default_factory=list)
    yaml_diff: Optional[str] = None
    pr_url: Optional[str] = None
    local_proposal_path: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    duration_seconds: Optional[float] = None
