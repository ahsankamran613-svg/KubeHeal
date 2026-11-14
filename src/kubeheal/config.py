from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Groq AI Provider
    groq_api_key: Optional[str] = Field(default=None, description="Groq API key")
    groq_model: str = Field(default="openai/gpt-oss-120b", description="Model name on Groq")

    # Kubernetes Settings
    kube_namespace: str = Field(default="default", description="Default namespace to monitor")
    enable_k8s_watcher: bool = Field(default=False, description="Enable automated cluster polling watcher")
    watcher_poll_interval_seconds: int = Field(default=15, description="Watcher polling interval in seconds")
    alert_cooldown_seconds: int = Field(default=300, description="Deduplication cooldown for alerts")
    manifests_dir: str = Field(default="k8s/workloads", description="Directory where workload YAML manifests live")
    repo_root_dir: str = Field(default=".", description="Repository root directory for computing relative Git paths")

    # Remediation Guardrail Caps
    max_memory_limit: str = Field(default="1Gi", description="Maximum safe memory ceiling for automated remediation")
    min_memory_limit: str = Field(default="16Mi", description="Minimum safe memory floor for automated remediation")
    allow_offline_simulation: bool = Field(
        default=True,
        description="Allow manifest-backed telemetry simulation when no live Kubernetes cluster is reachable",
    )

    # GitHub PR Automation & GitOps
    github_token: Optional[str] = Field(default=None, description="GitHub Personal Access Token")
    github_repo: Optional[str] = Field(default=None, description="GitHub repository target: owner/repo")
    github_base_branch: str = Field(default="main", description="Target base branch for PRs")
    dry_run_mode: bool = Field(default=True, description="When true, outputs local proposal instead of live PR")

    # Proposal output directory
    proposals_dir: str = Field(default="output/proposals", description="Directory for saving triage PR proposals")


settings = Settings()
