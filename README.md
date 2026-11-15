# 🛡️ KubeHeal: Guardrailed Kubernetes AIOps Triage Agent (with MCP)

[![CI Validation](https://github.com/ahsankamran613-svg/KubeHeal/actions/workflows/ci-validate.yml/badge.svg)](https://github.com/ahsankamran613-svg/KubeHeal/actions)
[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ahsankamran613-svg/KubeHeal)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)
[![MCP SDK](https://img.shields.io/badge/MCP%20SDK-1.0+-orange.svg)](https://modelcontextprotocol.io/)
[![Groq](https://img.shields.io/badge/LLM-Groq%20(openai%2Fgpt--oss--120b)-purple.svg)](https://groq.com/)

**KubeHeal** is an autonomous, defense-in-depth Kubernetes AIOps SRE Agent that triages failing pods (`CrashLoopBackOff`, `OOMKilled`, `ImagePullBackOff`, `CreateContainerConfigError`, and non-zero exits) across Kubernetes workloads (`Deployment`, `StatefulSet`, `DaemonSet`, `Job`, `CronJob`, `Pod`, `ReplicaSet`, `Rollout`), performs strictly read-only diagnostics via a custom **Model Context Protocol (MCP)** server, and proposes GitOps remediations through **GitHub Pull Requests** or dry-run proposal bundles with formatted YAML diffs.

---

## 🔒 3-Layer Defense-in-Depth Guardrail Architecture

```
                                  [ Failing Pod ]
                          (OOMKilled / CrashLoop / Err)
                                         │
                                         ▼
                            [ Kubernetes Event Watcher ]
                                         │
                                         ▼
                             [ KubeHeal FastAPI Core ]
                                         │
                      ┌──────────────────┴──────────────────┐
                      ▼                                     ▼
           [ Groq LLM Orchestrator ]              [ Live Web Dashboard ]
           (openai/gpt-oss-120b)                  - Real-time Alerts
                      │                           - MCP Audit Traces
                      │ MCP Tool Queries          - Unified YAML Diffs
                      ▼                           - PR Links
          ┌───────────────────────┐
          │   Custom MCP Server   │
          │ 🛡️ Guardrail 1: Code  │ ──► Blocks: delete, apply, exec, shell metachars, secrets
          └───────────┬───────────┘
                      │
                      ▼
          ┌───────────────────────┐
          │  Kubernetes Cluster   │
          │ 🛡️ Guardrail 2: RBAC  │ ──► ServiceAccount: kubeheal-reader (get, list, watch)
          └───────────┬───────────┘
                      │ Root Cause Diagnostics
                      ▼
          ┌───────────────────────┐
          │  GitOps Remediator    │
          │ 🛡️ Guardrail 3: GitOps│ ──► ruamel.yaml + configurable memory bounds
          └───────────┬───────────┘
                      │
           ┌──────────┴──────────┐
           ▼                     ▼
     [ GitHub PR ]       [ Local Proposals ]
   (Branch + Commit)   (output/proposals/*)
```

1. **Code-Level MCP Guardrail**: Only dedicated read-only tools and allow-listed `kubectl` verbs (`get`, `describe`, `logs`, `events`, `top`) can run. Mutating verbs (`delete`, `apply`, `patch`, `edit`, `exec`, `scale`), shell injections (`;`, `|`, `&`, `>`, `$`), sensitive resources (`secrets`, `serviceaccounts`, `roles`), privilege flags (`--token`, `--kubeconfig`, `--as`), and template extraction (`jsonpath`, `go-template`, `custom-columns`) are intercepted and rejected before execution.
2. **Cluster-Level RBAC Guardrail**: The `kubeheal-reader` `ServiceAccount` and `ClusterRole` enforce `["get", "list", "watch"]` restrictions across core, `apps` (`deployments`, `replicasets`, `statefulsets`, `daemonsets`), and `batch` (`jobs`, `cronjobs`) resources (plus `get`/`list` on `metrics.k8s.io`) at the Kubernetes API server level.
3. **GitOps Human-in-the-Loop**: The agent never mutates live cluster state directly. All fixes are generated as Git pull requests (or local proposal bundles) patching single- or multi-document YAML manifests via `ruamel.yaml` within configurable memory guardrails (`MIN_MEMORY_LIMIT` to `MAX_MEMORY_LIMIT`, defaulting to `16Mi`–`1Gi`), while ensuring workloads whose existing limits already meet or exceed `MAX_MEMORY_LIMIT` are never shrunk (allowing up to `2x` the current limit).

---

## ⚙️ Generalized Workload Discovery & Dynamic Triage

- **Controller & Owner Resolution**: Infers parent workload names from pod `ownerReferences` (`ReplicaSet` -> `Deployment`, `StatefulSet`, `DaemonSet`, `Job`, `CronJob`, `Rollout`) as well as pod naming conventions (`<name>-<rs-hash>-<pod-hash>`, `<name>-<ordinal>`, `<name>-<5char-hash>`).
- **Recursive & Multi-Document Manifest Discovery**: Recursively searches `MANIFESTS_DIR` (`*.yaml`/`*.yml`), parses multi-document (`---`) manifests, and matches `metadata.name` across `Deployment`, `StatefulSet`, `DaemonSet`, `Job`, `CronJob`, `Pod`, `ReplicaSet`, and `Rollout`.
- **Sidecar-Aware Container Selection**: Targets the failing `container_name` from alerts or selects the primary non-sidecar application container (automatically skipping common sidecars such as `istio-proxy`, `envoy`, `linkerd-proxy`, `fluentbit`, `datadog-agent`, and `otel-collector`).
- **Multi-Path YAML Patching & Repo-Relative PRs**: Patches `resources.limits.memory`, `resources.requests.memory`, `resources.limits.cpu`, `resources.requests.cpu`, `env[<VAR>]` / `env.<VAR>`, `image`, `command`, `args`, and `replicas`, and resolves repository-relative GitHub paths using `REPO_ROOT_DIR`.
- **Dynamic Deterministic Fallback**: When `GROQ_API_KEY` is unset (or unavailable), inspects MCP outputs and actual container specs to scale OOM memory dynamically (`4x` below `128Mi`, `2x` at or above `128Mi`), repair non-numeric `*PORT*` env vars, update unresolvable image tags on `ImagePullBackOff` / `ErrImagePull` / `InvalidImageName`, or apply safe baseline memory ceilings on generic crashes.

---

## ☁️ One-Click Cloud Development (GitHub Codespaces)

Run KubeHeal directly in your browser with zero local setup:

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ahsankamran613-svg/KubeHeal)

The included `.devcontainer/devcontainer.json` environment pre-configures:
- **Docker-in-Docker (DinD)** & **k3d** pre-installed to test real clusters in the cloud.
- **Python 3.12** with all pinned requirements installed upon creation.
- **FastAPI Dashboard** port `8000` auto-forwarded and opened in your browser.
- **VS Code Extensions**: Python, Docker, and Kubernetes tools pre-installed.

---

## 🚀 Quickstart

### Prerequisites
- Python 3.12+ (or use Codespaces)
- (Optional for live cluster testing) `k3d` / Docker and `kubectl`

### 1. Installation

**Linux / macOS:**
```bash
git clone https://github.com/ahsankamran613-svg/KubeHeal.git
cd KubeHeal

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell):**
```powershell
git clone https://github.com/ahsankamran613-svg/KubeHeal.git
cd KubeHeal

py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Configure Environment (`.env`)
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Key configuration settings:
```ini
# Groq AI Provider (optional: agent uses dynamic deterministic fallback when API key is unset)
GROQ_API_KEY=your_groq_api_key_here
GROQ_MODEL=openai/gpt-oss-120b

# Kubernetes Settings
KUBE_NAMESPACE=default
ENABLE_K8S_WATCHER=false
WATCHER_POLL_INTERVAL_SECONDS=15
ALERT_COOLDOWN_SECONDS=300
MANIFESTS_DIR=k8s/workloads
REPO_ROOT_DIR=.

# Remediation Guardrail Caps
MAX_MEMORY_LIMIT=1Gi
MIN_MEMORY_LIMIT=16Mi
ALLOW_OFFLINE_SIMULATION=true

# GitOps & GitHub PRs (leave empty for local dry-run proposals)
GITHUB_TOKEN=
GITHUB_REPO=owner/repo-name
GITHUB_BASE_BRANCH=main
DRY_RUN_MODE=true
PROPOSALS_DIR=output/proposals
```

- `MANIFESTS_DIR` & `REPO_ROOT_DIR`: `MANIFESTS_DIR` is scanned recursively (including nested folders and multi-document YAMLs); `REPO_ROOT_DIR` resolves accurate POSIX file paths for GitHub PR updates.
- `MIN_MEMORY_LIMIT` & `MAX_MEMORY_LIMIT`: Configurable bounds for memory remediation patches (`16Mi`–`1Gi` by default). Existing workloads already at or above `MAX_MEMORY_LIMIT` are allowed up to `2x` their current limit so large workloads are never shrunk.
- `ALLOW_OFFLINE_SIMULATION`: When `true`, `get_pod_status`, `get_pod_logs`, and `get_pod_events` synthesize telemetry from matching local manifests if no Kubernetes cluster is reachable; when `false`, they return `status: "unavailable"`.

### 3. Run Automated Tests
```powershell
.venv\Scripts\python -m pytest -v
```
*(Or on Linux/macOS: `pytest -v`)* — Runs all 21 automated tests across guardrail security, webhook/triage pipelines, workload discovery, and YAML patching.

### 4. Start KubeHeal Live Dashboard
```bash
uvicorn kubeheal.main:app --reload --app-dir src --port 8000
```
*(On Windows: `.venv\Scripts\python -m uvicorn kubeheal.main:app --reload --app-dir src --port 8000`)*

Open [http://localhost:8000](http://localhost:8000) in your browser:
- Click **Trigger OOMKilled Triage** to triage a simulated memory failure and generate a YAML patch.
- Click **Trigger CrashLoopBackOff Triage** to triage an invalid port configuration crash.
- Use the **Test Guardrail Block** probe bar to test mutating commands (e.g. `kubectl delete pod --all -n default`) and observe real-time interception.

---

## ☸️ Testing with a Live `k3d` Kubernetes Cluster

1. Provision the local development cluster and test workloads:
   ```powershell
   ./scripts/setup-k3d.ps1
   ```
   *(Or on Linux/macOS/Codespaces: `./scripts/setup-k3d.sh`)*

2. This provisions:
   - `kubeheal-cluster` in `k3d`
   - `k8s/rbac/readonly-triage-rbac.yaml` (`ServiceAccount: kubeheal-reader`)
   - Broken workload `oom-crash-app` (allocates 65MB in a 32MB limit)
   - Broken workload `config-crash-app` (invalid port string)

3. Enable the background watcher in `.env`:
   ```ini
   ENABLE_K8S_WATCHER=true
   ```

4. Restart KubeHeal to watch live crashes get automatically triaged:
   ```bash
   uvicorn kubeheal.main:app --reload --app-dir src --port 8000
   ```

---

## 🧰 Read-Only MCP Tools

KubeHeal exposes strictly read-only diagnostics via its internal Model Context Protocol (MCP) server:

| MCP Tool | Description | Guardrail Verification |
|---|---|---|
| `get_pod_status` | Retrieves pod phase, container states, restart counts, exit codes, and resource limits (with manifest-backed simulation when offline and `ALLOW_OFFLINE_SIMULATION=true`) | Read-only API call |
| `get_pod_logs` | Tails crashing/crashed container logs with `previous=True` and optional `container_name` targeting | Read-only API call |
| `get_pod_events` | Fetches Warning/Normal events from Kubernetes event buffer | Read-only API call |
| `get_deployment_spec` | Reads workload spec, images, commands/args, env vars (`valueFrom` masked), and resource constraints for `Deployment`, `StatefulSet`, and `DaemonSet` (or any supported local workload manifest) | Read-only API call |
| `run_readonly_kubectl` | Runs raw diagnostics (`get`, `describe`, `logs`, `events`, `top`) | Blocks mutating verbs, shell metachars, sensitive resources, privilege flags, & output templates |

---

## 🌐 API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/` | `GET` | Live AIOps Triage Web Dashboard |
| `/api/health` | `GET` | Cluster connectivity, MCP tool discovery, active model, and guardrail status (`max_memory_cap`, etc.) |
| `/api/incidents` | `GET` | List all triaged incidents ordered chronologically |
| `/api/incidents/{incident_id}` | `GET` | Retrieve full incident report, MCP audit traces, and YAML diff |
| `/api/webhook/alert` | `POST` | Webhook ingestion endpoint for Kubernetes/Alertmanager alerts |
| `/api/simulate/{scenario}` | `POST` | One-click simulation of `oom-crash` or `config-crash` |
| `/api/guardrail-test` | `POST` | Test MCP guardrail against mutating verbs and shell injections |

---

## 📁 Repository Structure

```
KubeHeal/
├── .devcontainer/
│   └── devcontainer.json      # Codespaces environment with DinD, k3d, & Python 3.12
├── .github/workflows/
│   └── ci-validate.yml        # CI test pipeline (Python 3.12)
├── k8s/
│   ├── rbac/
│   │   └── readonly-triage-rbac.yaml # ServiceAccount + ClusterRole (core, apps, batch, metrics)
│   ├── workloads/
│   │   ├── oom-crash-app.yaml        # Demo broken OOM workload (32Mi limit)
│   │   └── config-crash-app.yaml     # Demo broken config workload (invalid port)
│   └── agent/
│       └── agent-deployment.yaml     # In-cluster deployment manifest
├── output/proposals/          # Local GitOps proposal bundles
├── scripts/
│   ├── setup-k3d.ps1          # PowerShell k3d cluster setup
│   └── setup-k3d.sh           # Bash k3d cluster setup
├── src/kubeheal/
│   ├── agent/                 # Groq LLM orchestrator & dynamic triage pipeline
│   ├── gitops/                # Multi-kind ruamel.yaml patcher & PyGithub PR service
│   ├── mcp_server/            # Read-only FastMCP / MCPServer & guardrails
│   ├── watcher/               # K8s polling watcher (ownerRefs + cooldown deduplication)
│   ├── static/index.html      # Live AIOps Triage Web Dashboard
│   ├── config.py              # pydantic-settings configuration
│   ├── models.py              # Pydantic v2 data models
│   └── main.py                # FastAPI web service & webhook endpoints
├── tests/                     # 21 automated pytest tests across 3 suites
│   ├── test_guardrails.py     # MCP security & verb whitelist tests
│   ├── test_webhook_and_agent.py # FastAPI, dynamic triage, and simulated runs
│   └── test_yaml_patcher.py   # Multi-kind YAML patching, discovery, & guardrail tests
├── .env.example               # Environment configuration template
├── Dockerfile                 # Container image with kubectl
├── requirements.txt           # Pinned dependencies
└── pyproject.toml             # Project metadata & pytest config
```
