# 🛡️ KubeHeal: Guardrailed Kubernetes AIOps Triage Agent (with MCP)

[![CI Validation](https://github.com/ahsankamran613-svg/KubeHeal/actions/workflows/ci-validate.yml/badge.svg)](https://github.com/ahsankamran613-svg/KubeHeal/actions)
[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ahsankamran613-svg/KubeHeal)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)
[![MCP SDK](https://img.shields.io/badge/MCP%20SDK-1.0+-orange.svg)](https://modelcontextprotocol.io/)
[![Groq](https://img.shields.io/badge/LLM-Groq%20(openai%2Fgpt--oss--120b)-purple.svg)](https://groq.com/)

**KubeHeal** is an autonomous, defense-in-depth Kubernetes AIOps SRE Agent that triages failing pods (`CrashLoopBackOff`, `OOMKilled`), performs strictly read-only diagnostics via a custom **Model Context Protocol (MCP)** server, and proposes GitOps remediations through **GitHub Pull Requests** or dry-run proposal bundles with formatted YAML diffs.

---

## 🔒 3-Layer Defense-in-Depth Guardrail Architecture

```
                                  [ Failing Pod ]
                                (OOMKilled / Crash)
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
          │ 🛡️ Guardrail 1: Code  │ ──► Blocks: delete, apply, exec, shell metachars
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
          │ 🛡️ Guardrail 3: GitOps│ ──► ruamel.yaml + safe memory caps (<=1Gi)
          └───────────┬───────────┘
                      │
           ┌──────────┴──────────┐
           ▼                     ▼
     [ GitHub PR ]       [ Local Proposals ]
   (Branch + Commit)   (output/proposals/*)
```

1. **Code-Level MCP Guardrail**: Only dedicated read-only tools and allow-listed `kubectl` verbs (`get`, `describe`, `logs`, `events`, `top`) can run. Mutating verbs (`delete`, `apply`, `patch`, `edit`, `exec`, `scale`) and shell injections (`;`, `|`, `&`, `>`, `$`) are intercepted and rejected before execution.
2. **Cluster-Level RBAC Guardrail**: The `kubeheal-reader` `ServiceAccount` and `ClusterRole` enforce `["get", "list", "watch"]` API restrictions at the Kubernetes API server level.
3. **GitOps Human-in-the-Loop**: The agent never mutates live cluster state directly. All fixes are generated as Git pull requests modifying YAML deployment manifests via `ruamel.yaml` with safe resource ceilings (memory limit capped at 1Gi).

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
# Groq AI Provider (optional: agent uses built-in heuristic rules when API key is unset)
GROQ_API_KEY=your_groq_api_key_here
GROQ_MODEL=openai/gpt-oss-120b

# Kubernetes Settings
KUBE_NAMESPACE=default
ENABLE_K8S_WATCHER=false
WATCHER_POLL_INTERVAL_SECONDS=15
ALERT_COOLDOWN_SECONDS=300
MANIFESTS_DIR=k8s/workloads

# GitOps & GitHub PRs (leave empty for local dry-run proposals)
GITHUB_TOKEN=
GITHUB_REPO=owner/repo-name
GITHUB_BASE_BRANCH=main
DRY_RUN_MODE=true
PROPOSALS_DIR=output/proposals
```

### 3. Run Automated Tests
```powershell
.venv\Scripts\python -m pytest -v
```
*(Or on Linux/macOS: `pytest -v`)* — Runs all 16 automated tests across guardrail security, triage pipelines, and YAML patching.

### 4. Start KubeHeal Live Dashboard
```bash
uvicorn kubeheal.main:app --reload --app-dir src --port 8000
```
*(On Windows: `.venv\Scripts\python -m uvicorn kubeheal.main:app --reload --app-dir src --port 8000`)*

Open [http://localhost:8000](http://localhost:8000) in your browser:
- Click **💥 Simulate OOMKilled** to triage a simulated memory failure and generate a YAML patch.
- Click **⚠️ Simulate CrashLoopBackOff** to triage an invalid port configuration crash.
- Use the **Test Guardrail** bar to test mutating commands (e.g. `kubectl delete pod --all`) and observe real-time interception.

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
| `get_pod_status` | Retrieves pod phase, container states, restart counts, exit codes, and resource limits | Read-only API call |
| `get_pod_logs` | Tails crashing/crashed container logs with previous container restart support (`previous=True`) | Read-only API call |
| `get_pod_events` | Fetches Warning/Normal events from Kubernetes event buffer | Read-only API call |
| `get_deployment_spec` | Reads deployment spec, env vars, and resource constraints | Read-only API call |
| `run_readonly_kubectl` | Runs raw diagnostics (`get`, `describe`, `logs`, `events`, `top`) | Intercepts non-allowlisted verbs & shell metachars |

---

## 🌐 API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/` | `GET` | Live AIOps Triage Web Dashboard |
| `/api/health` | `GET` | Cluster connectivity, MCP tool discovery, and guardrail status |
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
│   │   └── readonly-triage-rbac.yaml # ServiceAccount + ClusterRole (get, list, watch)
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
│   ├── agent/                 # Groq LLM orchestrator & triage pipeline
│   ├── gitops/                # ruamel.yaml patcher & PyGithub PR service
│   ├── mcp_server/            # Read-only FastMCP / MCPServer & guardrails
│   ├── watcher/               # K8s polling watcher with cooldown deduplication
│   ├── static/index.html      # Live AIOps Triage Web Dashboard
│   ├── config.py              # pydantic-settings configuration
│   ├── models.py              # Pydantic v2 data models
│   └── main.py                # FastAPI web service & webhook endpoints
├── tests/                     # 16 automated pytest tests across 3 suites
│   ├── test_guardrails.py     # MCP security & verb whitelist tests
│   ├── test_webhook_and_agent.py # FastAPI, incident triage, and simulated runs
│   └── test_yaml_patcher.py   # ruamel.yaml patching & memory guardrail tests
├── Dockerfile                 # Container image with kubectl
├── requirements.txt           # Pinned dependencies
└── pyproject.toml             # Project metadata & pytest config
```
