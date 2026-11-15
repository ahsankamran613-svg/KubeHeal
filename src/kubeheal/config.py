from typing import Dict, NamedTuple, Optional, Tuple
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator


DEFAULT_LLM_PROVIDER = "auto"
SUPPORTED_LLM_PROVIDERS: Tuple[str, ...] = ("auto", "gemini", "groq")
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
DETERMINISTIC_ENGINE_NAME = "deterministic"
PROVIDER_DISPLAY_NAMES: Dict[str, str] = {
    "gemini": "Gemini",
    "groq": "Groq",
    DETERMINISTIC_ENGINE_NAME: "Deterministic Diagnostic Engine",
}


class LLMProviderConfig(NamedTuple):
    """Resolved active LLM provider configuration."""

    provider: str
    model: str
    api_key: Optional[str]
    is_configured: bool


def _clean_secret(value: Optional[str]) -> Optional[str]:
    """Return a trimmed secret string or None when unset/blank."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # AI Provider Selection & Triage Pacing
    llm_provider: str = Field(
        default=DEFAULT_LLM_PROVIDER,
        description="Active LLM provider: 'auto' (prefers Gemini if GEMINI_API_KEY is set, else Groq), 'gemini', or 'groq'",
    )
    gemini_api_key: Optional[str] = Field(default=None, description="Google Gemini API key")
    gemini_model: str = Field(default=DEFAULT_GEMINI_MODEL, description="Model name on Google Gemini")
    groq_api_key: Optional[str] = Field(default=None, description="Groq API key")
    groq_model: str = Field(default=DEFAULT_GROQ_MODEL, description="Model name on Groq")

    @field_validator("llm_provider")
    @classmethod
    def _validate_llm_provider(cls, value: str) -> str:
        normalized = (value or DEFAULT_LLM_PROVIDER).strip().lower()
        if normalized not in SUPPORTED_LLM_PROVIDERS:
            supported = ", ".join(SUPPORTED_LLM_PROVIDERS)
            raise ValueError(f"Unsupported llm_provider '{value}'. Must be one of: {supported}.")
        return normalized

    def resolve_llm_config(self, provider_override: Optional[str] = None) -> LLMProviderConfig:
        """Resolve the active LLM provider, model, and API key from settings."""
        raw_provider = provider_override if provider_override is not None else self.llm_provider
        provider_setting = (raw_provider or DEFAULT_LLM_PROVIDER).strip().lower()
        if provider_setting not in SUPPORTED_LLM_PROVIDERS:
            supported = ", ".join(SUPPORTED_LLM_PROVIDERS)
            raise ValueError(f"Unsupported llm_provider '{raw_provider}'. Must be one of: {supported}.")

        gemini_key = _clean_secret(self.gemini_api_key)
        groq_key = _clean_secret(self.groq_api_key)

        if provider_setting == "gemini":
            return LLMProviderConfig(
                provider="gemini",
                model=self.gemini_model,
                api_key=gemini_key,
                is_configured=gemini_key is not None,
            )
        if provider_setting == "groq":
            return LLMProviderConfig(
                provider="groq",
                model=self.groq_model,
                api_key=groq_key,
                is_configured=groq_key is not None,
            )

        # 'auto' mode: prefer Gemini (higher TPM headroom), then Groq, else unconfigured Gemini default
        if gemini_key is not None:
            return LLMProviderConfig(
                provider="gemini",
                model=self.gemini_model,
                api_key=gemini_key,
                is_configured=True,
            )
        if groq_key is not None:
            return LLMProviderConfig(
                provider="groq",
                model=self.groq_model,
                api_key=groq_key,
                is_configured=True,
            )
        return LLMProviderConfig(
            provider="gemini",
            model=self.gemini_model,
            api_key=None,
            is_configured=False,
        )

    def format_llm_attribution(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        is_configured: Optional[bool] = None,
    ) -> str:
        """Format a human-readable attribution string for PR bodies and UI diagnostics."""
        if provider is None:
            resolved = self.resolve_llm_config()
            provider = resolved.provider
            model = resolved.model
            is_configured = resolved.is_configured
        elif is_configured is None:
            is_configured = provider != DETERMINISTIC_ENGINE_NAME

        if not is_configured or provider == DETERMINISTIC_ENGINE_NAME:
            return PROVIDER_DISPLAY_NAMES[DETERMINISTIC_ENGINE_NAME]

        display_name = PROVIDER_DISPLAY_NAMES.get(provider, provider.capitalize())
        return f"{display_name} ({model})" if model else display_name
    llm_max_turns: int = Field(
        default=4,
        ge=1,
        description="Maximum LLM turns after baseline MCP telemetry pre-hydration",
    )
    llm_log_tail_lines: int = Field(
        default=80,
        ge=10,
        description="Maximum pod log lines requested during baseline MCP pre-hydration",
    )
    llm_max_log_chars: int = Field(
        default=4000,
        ge=100,
        description="Maximum character budget for compacted pod logs in initial LLM context",
    )
    llm_concurrency_limit: int = Field(
        default=1,
        ge=1,
        description="Maximum concurrent LLM triage loops (1 serializes crashes to prevent TPM spikes)",
    )
    llm_inter_request_delay_seconds: float = Field(
        default=0.0,
        ge=0.0,
        description="Optional pacing delay in seconds before releasing the LLM concurrency semaphore",
    )
    llm_max_retries: int = Field(
        default=1,
        ge=0,
        description="Maximum SDK retries on transient LLM API rate-limit or server errors",
    )
    llm_timeout_seconds: float = Field(
        default=30.0,
        gt=0.0,
        description="Timeout in seconds for each LLM API request",
    )
    llm_temperature: float = Field(
        default=0.1,
        ge=0.0,
        le=2.0,
        description="Sampling temperature for LLM diagnostic reasoning",
    )

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
