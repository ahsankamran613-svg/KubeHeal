# 🛡️ KubeHeal: Guardrailed Kubernetes AIOps Triage Agent (with MCP)

[![CI Validation](https://github.com/KubeHeal/kubeheal/actions/workflows/ci-validate.yml/badge.svg)](https://github.com/KubeHeal/kubeheal/actions)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.162.2-009688.svg)](https://fastapi.tiangolo.com/)
[![MCP SDK](https://img.shields.io/badge/MCP%20SDK-2.3.0-orange.svg)](https://modelcontextprotocol.io/)
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

## 🚀 Quickstart

### Prerequisites
- Python 3.12+
- (Optional for live cluster) `k3d` / Docker and `kubectl`

### 1. Installation
Clone the repository and install dependencies in a virtual environment:
```powershell
# Clone and enter workspace
cd d:\KubeHeal

# Create virtual environment with Python 3.12
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1

# Install pinned dependencies
pip install -r requirements.txt
```

### 2. Configure Environment (`.env`)
Copy `.env.example` to `.env`:
```powershell
cp .env.example .env
```
Key configuration settings:
```ini
# Groq AI Provider (optional for simulation/offline mode)
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
```

### 3. Run Automated Tests
```powershell
.venv\Scripts\python -m pytest -v
```

### 4. Start KubeHeal Live Dashboard
```powershell
.venv\Scripts\python -m uvicorn kubeheal.main:app --reload --app-dir src --port 8000
```
Open [http://localhost:8000](http://localhost:8000) in your browser. From the dashboard:
- Click **💥 Simulate OOMKilled** to triage a simulated memory failure and generate a YAML patch.
- Click **⚠️ Simulate CrashLoopBackOff** to triage an invalid port configuration crash.
- Use the **Test Guardrail** bar to test mutating commands (e.g. `kubectl delete pod --all`) and observe real-time interception.

---

## ☸️ Testing with a Live `k3d` Kubernetes Cluster

1. Run the automated cluster setup script:
   ```powershell
   ./scripts/setup-k3d.ps1
   ```
   *(Or on Linux/macOS: `./scripts/setup-k3d.sh`)*

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
   ```powershell
   .venv\Scripts\python -m uvicorn kubeheal.main:app --reload --app-dir src --port 8000
   ```

---

## 📁 Repository Structure

```
d:\KubeHeal\
├── .github/workflows/
│   └── ci-validate.yml        # CI test pipeline (Python 3.12)
├── k8s/
│   ├── rbac/
│   │   └── readonly-triage-rbac.yaml # ServiceAccount + ClusterRole
│   ├── workloads/
│   │   ├── oom-crash-app.yaml        # Demo broken OOM workload
│   │   └── config-crash-app.yaml     # Demo broken config workload
│   └── agent/
│       └── agent-deployment.yaml     # In-cluster deployment
├── output/proposals/          # Local GitOps proposal bundles
├── scripts/
│   ├── setup-k3d.ps1          # PowerShell k3d cluster setup
│   └── setup-k3d.sh           # Bash k3d cluster setup
├── src/kubeheal/
│   ├── agent/                 # Groq LLM orchestrator & triage agent
│   ├── gitops/                # ruamel.yaml patcher & PyGithub PR service
│   ├── mcp_server/            # Read-only FastMCP / MCPServer & guardrails
│   ├── watcher/               # K8s polling watcher with cooldown deduplication
│   ├── static/index.html      # Live AIOps Triage Web Dashboard
│   ├── config.py              # pydantic-settings configuration
│   ├── models.py              # Pydantic v2 data models
│   └── main.py                # FastAPI web service & webhook endpoints
├── tests/                     # 15 automated pytest suites
├── Dockerfile                 # Container image with kubectl
├── requirements.txt           # Pinned dependencies
└── pyproject.toml             # Project metadata & pytest config
```
