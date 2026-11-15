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

DEFAULT_KUBE_NAMESPACE = "default"
DEFAULT_WATCHER_POLL_INTERVAL_SECONDS = 15
DEFAULT_ALERT_COOLDOWN_SECONDS = 300
DEFAULT_MANIFESTS_DIR = "k8s/workloads"
DEFAULT_REPO_ROOT_DIR = "."
DEFAULT_PROPOSALS_DIR = "output/proposals"
DEFAULT_GITHUB_BASE_BRANCH = "main"
DEFAULT_PROJECT_URL = "https://github.com/KubeHeal"

DEFAULT_MAX_MEMORY_LIMIT = "1Gi"
DEFAULT_MIN_MEMORY_LIMIT = "16Mi"
DEFAULT_FALLBACK_MEMORY_LIMIT = "32Mi"
DEFAULT_FALLBACK_MEMORY_REQUEST = "16Mi"
DEFAULT_SAFE_REQUEST_FALLBACK = "64Mi"
DEFAULT_UNPARSEABLE_MEMORY_FALLBACK = "128Mi"
DEFAULT_SMALL_MEMORY_THRESHOLD = "128Mi"
DEFAULT_SMALL_MEMORY_SCALE_FACTOR = 4
DEFAULT_LARGE_MEMORY_SCALE_FACTOR = 2

DEFAULT_REMEDIATION_PORT = "8080"
DEFAULT_PORT_ENV_KEY = "REQUIRED_PORT"
DEFAULT_MEMORY_ENV_KEY = "MEMORY_LIMIT"
DEFAULT_INVALID_PORT_PLACEHOLDER = "invalid_port"
DEFAULT_IMAGE_TAG_FALLBACK = "latest"

DEFAULT_K8S_API_TIMEOUT_SECONDS = 2
DEFAULT_KUBECTL_TIMEOUT_SECONDS = 10
DEFAULT_POD_LOG_TAIL_LINES = 100
DEFAULT_GUARDRAIL_PROBE_COMMAND = "kubectl delete pod --all -n default"

OOM_SCENARIO_KEY = "oom-crash"
CONFIG_SCENARIO_KEY = "config-crash"
DEFAULT_OOM_SCENARIO_WORKLOAD = "oom-crash-app"
DEFAULT_CONFIG_SCENARIO_WORKLOAD = "config-crash-app"
DEFAULT_SIMULATED_POD_SUFFIX = "sim01"

OOM_EXIT_CODE = 137
DEFAULT_ERROR_EXIT_CODE = 1
DEFAULT_OOM_SIM_RESTART_COUNT = 4
DEFAULT_CONFIG_SIM_RESTART_COUNT = 5
DEFAULT_GENERIC_SIM_RESTART_COUNT = 3

DEFAULT_OOM_CONFIDENCE = 0.98
DEFAULT_CONFIG_CONFIDENCE = 0.96
DEFAULT_IMAGE_CONFIDENCE = 0.88
DEFAULT_GENERIC_CONFIDENCE = 0.85
DEFAULT_LLM_CONFIDENCE = 0.95
DEFAULT_LLM_FALLBACK_CONFIDENCE = 0.90

DEFAULT_INCIDENT_ID_LENGTH = 8
DEFAULT_TRACE_PREVIEW_MAX_CHARS = 250
DEFAULT_PR_TRACE_PREVIEW_MAX_CHARS = 80
DEFAULT_RBAC_VERBS: Tuple[str, ...] = ("get", "list", "watch")
DEFAULT_MCP_TOOL_NAMES: Tuple[str, ...] = (
    "get_pod_status",
    "get_pod_logs",
    "get_pod_events",
    "get_deployment_spec",
    "run_readonly_kubectl",
)


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
    kube_namespace: str = Field(default=DEFAULT_KUBE_NAMESPACE, description="Default namespace to monitor")
    enable_k8s_watcher: bool = Field(default=False, description="Enable automated cluster polling watcher")
    watcher_poll_interval_seconds: int = Field(
        default=DEFAULT_WATCHER_POLL_INTERVAL_SECONDS,
        description="Watcher polling interval in seconds",
    )
    alert_cooldown_seconds: int = Field(
        default=DEFAULT_ALERT_COOLDOWN_SECONDS,
        description="Deduplication cooldown for alerts",
    )
    manifests_dir: str = Field(
        default=DEFAULT_MANIFESTS_DIR,
        description="Directory where workload YAML manifests live",
    )
    repo_root_dir: str = Field(
        default=DEFAULT_REPO_ROOT_DIR,
        description="Repository root directory for computing relative Git paths",
    )
    k8s_api_timeout_seconds: int = Field(
        default=DEFAULT_K8S_API_TIMEOUT_SECONDS,
        ge=1,
        description="Timeout in seconds for Kubernetes API connectivity checks",
    )
    kubectl_timeout_seconds: int = Field(
        default=DEFAULT_KUBECTL_TIMEOUT_SECONDS,
        ge=1,
        description="Timeout in seconds for read-only kubectl subprocess execution",
    )

    # Remediation Guardrail Caps & Fallback Defaults
    max_memory_limit: str = Field(
        default=DEFAULT_MAX_MEMORY_LIMIT,
        description="Maximum safe memory ceiling for automated remediation",
    )
    min_memory_limit: str = Field(
        default=DEFAULT_MIN_MEMORY_LIMIT,
        description="Minimum safe memory floor for automated remediation",
    )
    fallback_memory_limit: str = Field(
        default=DEFAULT_FALLBACK_MEMORY_LIMIT,
        description="Fallback baseline memory limit when container spec omits a memory limit",
    )
    fallback_memory_request: str = Field(
        default=DEFAULT_FALLBACK_MEMORY_REQUEST,
        description="Fallback baseline memory request when container spec omits a memory request",
    )
    safe_request_fallback: str = Field(
        default=DEFAULT_SAFE_REQUEST_FALLBACK,
        description="Fallback memory request if scaled memory limit cannot be parsed",
    )
    unparseable_memory_fallback: str = Field(
        default=DEFAULT_UNPARSEABLE_MEMORY_FALLBACK,
        description="Fallback memory limit when both proposed and existing memory strings are unparseable",
    )
    small_memory_threshold: str = Field(
        default=DEFAULT_SMALL_MEMORY_THRESHOLD,
        description="Memory threshold below which OOM scaling applies small_memory_scale_factor",
    )
    small_memory_scale_factor: int = Field(
        default=DEFAULT_SMALL_MEMORY_SCALE_FACTOR,
        ge=1,
        description="Scaling multiplier applied to OOM memory limits below small_memory_threshold",
    )
    large_memory_scale_factor: int = Field(
        default=DEFAULT_LARGE_MEMORY_SCALE_FACTOR,
        ge=1,
        description="Scaling multiplier applied to OOM memory limits at or above small_memory_threshold",
    )
    default_remediation_port: str = Field(
        default=DEFAULT_REMEDIATION_PORT,
        description="Default valid TCP port string used when remediating invalid port environment variables",
    )
    default_port_env_key: str = Field(
        default=DEFAULT_PORT_ENV_KEY,
        description="Default environment variable name inspected for port configuration errors",
    )
    default_memory_env_key: str = Field(
        default=DEFAULT_MEMORY_ENV_KEY,
        description="Environment variable name inspected for application-level memory thresholds",
    )
    default_image_tag_fallback: str = Field(
        default=DEFAULT_IMAGE_TAG_FALLBACK,
        description="Fallback container image tag proposed when an image pull fails",
    )
    default_guardrail_probe_command: str = Field(
        default=DEFAULT_GUARDRAIL_PROBE_COMMAND,
        description="Default kubectl command used in the guardrail verification probe",
    )
    allow_offline_simulation: bool = Field(
        default=True,
        description="Allow manifest-backed telemetry simulation when no live Kubernetes cluster is reachable",
    )
    oom_scenario_workload: str = Field(
        default=DEFAULT_OOM_SCENARIO_WORKLOAD,
        description="Target workload name for the 'oom-crash' simulation scenario",
    )
    config_scenario_workload: str = Field(
        default=DEFAULT_CONFIG_SCENARIO_WORKLOAD,
        description="Target workload name for the 'config-crash' simulation scenario",
    )
    simulated_pod_suffix: str = Field(
        default=DEFAULT_SIMULATED_POD_SUFFIX,
        description="Pod suffix appended when synthesizing a pod identifier in offline simulation mode",
    )

    # GitHub PR Automation & GitOps
    github_token: Optional[str] = Field(default=None, description="GitHub Personal Access Token")
    github_repo: Optional[str] = Field(default=None, description="GitHub repository target: owner/repo")
    github_base_branch: str = Field(default=DEFAULT_GITHUB_BASE_BRANCH, description="Target base branch for PRs")
    dry_run_mode: bool = Field(default=True, description="When true, outputs local proposal instead of live PR")
    project_url: str = Field(default=DEFAULT_PROJECT_URL, description="Project URL used in PR attribution footers")

    # Proposal output directory
    proposals_dir: str = Field(default=DEFAULT_PROPOSALS_DIR, description="Directory for saving triage PR proposals")

    def scenario_workload_map(self) -> Dict[str, str]:
        """Return mapping from simulation scenario keys to configured workload names."""
        return {
            OOM_SCENARIO_KEY: self.oom_scenario_workload,
            CONFIG_SCENARIO_KEY: self.config_scenario_workload,
        }


settings = Settings()
