# 🛡️ KubeHeal: Safe AI Helper for Fixing Crashing Kubernetes Pods

[![CI Validation](https://github.com/ahsankamran613-svg/KubeHeal/actions/workflows/ci-validate.yml/badge.svg)](https://github.com/ahsankamran613-svg/KubeHeal/actions)
[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ahsankamran613-svg/KubeHeal)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)
[![MCP SDK](https://img.shields.io/badge/MCP%20SDK-1.0+-orange.svg)](https://modelcontextprotocol.io/)
[![LLM](https://img.shields.io/badge/LLM-Gemini%203.5%20Flash--Lite%20%7C%20Groq-purple.svg)](https://ai.google.dev/)

**KubeHeal** is an AI helper for Kubernetes that watches for crashing pods (`CrashLoopBackOff`, `OOMKilled`, `ImagePullBackOff`, `CreateContainerConfigError`, and non-zero exits) across Kubernetes workloads (`Deployment`, `StatefulSet`, `DaemonSet`, `Job`, `CronJob`, `Pod`, `ReplicaSet`, `Rollout`). It checks what went wrong using strictly **read-only** tools (via the Model Context Protocol, or MCP), and then creates a **GitHub Pull Request**—or saves a local fix preview—with the suggested YAML change for a human to review.

---

## 🔒 How KubeHeal Stays Safe (3 Safety Layers)

```
                                  [ Crashing Pod ]
                          (OOMKilled / CrashLoop / Err)
                                         │
                                         ▼
                            [ Kubernetes Pod Watcher ]
                                         │
                                         ▼
                             [ KubeHeal Web Backend ]
                                         │
                      ┌──────────────────┴──────────────────┐
                      ▼                                     ▼
               [ AI Triage Agent ]                [ Live Web Dashboard ]
           (Gemini 3.5 Flash-Lite / Groq)         - Real-time Alerts
                      │                           - Safety Check Logs
                      │ Read-Only Checks          - YAML Diff Preview
                      ▼                           - Pull Request Links
          ┌───────────────────────┐
          │   Custom MCP Server   │
          │ 🛡️ Layer 1: Code Rule │ ──► Blocks: delete, apply, exec, shell tricks, secrets
          └───────────┬───────────┘
                      │
                      ▼
          ┌───────────────────────┐
          │  Kubernetes Cluster   │
          │ 🛡️ Layer 2: K8s RBAC  │ ──► Account: kubeheal-reader (get, list, watch only)
          └───────────┬───────────┘
                      │ Root Cause Found
                      ▼
          ┌───────────────────────┐
          │   YAML Fix Builder    │
          │ 🛡️ Layer 3: GitOps PR │ ──► ruamel.yaml + safe memory limits
          └───────────┬───────────┘
                      │
           ┌──────────┴──────────┐
           ▼                     ▼
     [ GitHub PR ]       [ Local Preview ]
   (Branch + Commit)    (output/proposals/*)
```

1. **Command Safety Check (Code Level)**: The AI can only run safe, read-only tools and `kubectl` commands (`get`, `describe`, `logs`, `events`, `top`). Any command that changes things (`delete`, `apply`, `patch`, `edit`, `exec`, `scale`), uses shell tricks (`;`, `|`, `&`, `>`, `$`), tries to read secrets or roles (`secrets`, `serviceaccounts`, `roles`), uses admin flags (`--token`, `--kubeconfig`, `--as`), or dumps hidden data (`jsonpath`, `go-template`, `custom-columns`) is blocked right away.
2. **Cluster Permission Lock (Kubernetes RBAC)**: Inside Kubernetes, the `kubeheal-reader` account only has read-only permission (`get`, `list`, `watch`) on core resources, `apps` (`deployments`, `replicasets`, `statefulsets`, `daemonsets`), and `batch` (`jobs`, `cronjobs`), plus read-only metrics (`get`, `list` on `metrics.k8s.io`). Even if something tried to change the cluster, Kubernetes itself would block it.
3. **Human Review via Git (GitOps)**: KubeHeal never edits your running cluster directly. Every fix is written as a YAML file change (using `ruamel.yaml` so your comments stay intact) and sent as a GitHub Pull Request or saved locally for review. Memory fixes stay inside configurable safety bounds (`MIN_MEMORY_LIMIT` to `MAX_MEMORY_LIMIT`, default `16Mi`–`1Gi`), and if an app already uses more memory than `MAX_MEMORY_LIMIT`, KubeHeal will never shrink it (allowing up to `2x` its current limit).

---

## ⚙️ What KubeHeal Can Find & Fix

- **Finds the parent app automatically**: Works out which workload owns a failing pod using Kubernetes `ownerReferences` or pod naming patterns—supporting `Deployment`, `StatefulSet`, `DaemonSet`, `Job`, `CronJob`, `Pod`, `ReplicaSet`, and `Rollout`.
- **Searches subfolders & multi-part YAML files**: Scans inside all subfolders of `MANIFESTS_DIR` (`*.yaml` / `*.yml`), reads files that contain multiple YAML resources (`---`), and matches the right workload by `metadata.name`.
- **Skips sidecar helper containers**: Updates the crashing container named in the alert, or automatically picks your main app container while ignoring common sidecars (like `istio-proxy`, `envoy`, `linkerd-proxy`, `fluentbit`, `datadog-agent`, and `otel-collector`).
- **Fixes many YAML fields & builds clean PR paths**: Can update memory/CPU `limits` and `requests`, environment variables (`env[NAME]`), container `image`, `command`, `args`, and `replicas`. Uses `REPO_ROOT_DIR` so GitHub Pull Requests point to the exact file path anywhere in your repository.
- **Works even without an AI API key**: If `GEMINI_API_KEY` and `GROQ_API_KEY` are not set (or unreachable), KubeHeal checks the pod's logs, events, and YAML config itself to scale up OOM memory (`4x` when under `128Mi`, `2x` at `128Mi` or higher), fix non-numeric `*PORT*` environment values, update broken image tags (`ImagePullBackOff` / `ErrImagePull` / `InvalidImageName`), or set a safe memory limit on other crashes.

---

## ☁️ Run in the Cloud (GitHub Codespaces)

Try KubeHeal right in your browser with zero local setup:

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ahsankamran613-svg/KubeHeal)

The included `.devcontainer/devcontainer.json` file sets everything up for you:
- **Docker and `k3d`** already installed so you can test a real Kubernetes cluster in the cloud.
- **Python 3.12** with all required packages installed automatically.
- **Web Dashboard** on port `8000` forwarded and opened in your browser.
- **VS Code Extensions** for Python, Docker, and Kubernetes pre-installed.

---

## 🚀 Quickstart

### What You Need
- Python 3.12+ (or use GitHub Codespaces)
- *(Optional, only for testing a real cluster)* `k3d` / Docker and `kubectl`

### 1. Install

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

### 2. Set Up Your `.env` Config
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Main settings:
```ini
# AI Provider ('auto', 'gemini', or 'groq'; optional: KubeHeal uses built-in diagnostic rules if keys are empty)
LLM_PROVIDER=gemini
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-3.5-flash-lite
GROQ_API_KEY=
GROQ_MODEL=openai/gpt-oss-120b

# Kubernetes Settings
KUBE_NAMESPACE=default
ENABLE_K8S_WATCHER=false
WATCHER_POLL_INTERVAL_SECONDS=15
ALERT_COOLDOWN_SECONDS=300
MANIFESTS_DIR=k8s/workloads
REPO_ROOT_DIR=.

# Memory Safety Limits & Offline Simulation
MAX_MEMORY_LIMIT=1Gi
MIN_MEMORY_LIMIT=16Mi
ALLOW_OFFLINE_SIMULATION=true

# GitHub Pull Requests (leave GITHUB_TOKEN empty to save local previews only)
GITHUB_TOKEN=
GITHUB_REPO=owner/repo-name
GITHUB_BASE_BRANCH=main
DRY_RUN_MODE=true
PROPOSALS_DIR=output/proposals
```

- `MANIFESTS_DIR` & `REPO_ROOT_DIR`: Where your Kubernetes YAML files are stored (searched including subfolders) and your repo's root folder so GitHub PRs edit the right file path.
- `MIN_MEMORY_LIMIT` & `MAX_MEMORY_LIMIT`: Smallest and largest memory limits KubeHeal can propose (`16Mi` to `1Gi` by default). Apps that already use more than `MAX_MEMORY_LIMIT` are never shrunk.
- `ALLOW_OFFLINE_SIMULATION`: When `true`, `get_pod_status`, `get_pod_logs`, and `get_pod_events` create test telemetry from your local YAML files if no live cluster is connected; when `false`, they report `status: "unavailable"`.

### 3. Run the Automated Tests
```powershell
.venv\Scripts\python -m pytest -v
```
*(Or on Linux/macOS: `pytest -v`)* — Runs the automated test suite covering safety guardrails, MCP pre-hydration, Gemini/Groq provider switching, crash handling, workload discovery, and YAML patching.

### 4. Start the Web Dashboard
```bash
uvicorn kubeheal.main:app --reload --app-dir src --port 8000
```
*(On Windows: `.venv\Scripts\python -m uvicorn kubeheal.main:app --reload --app-dir src --port 8000`)*

Open [http://localhost:8000](http://localhost:8000) in your browser:
- Click **Trigger OOMKilled Triage** to test an out-of-memory crash and see the YAML fix.
- Click **Trigger CrashLoopBackOff Triage** to test a bad port setting crash.
- Type a dangerous command (like `kubectl delete pod --all -n default`) and click **Test Guardrail Block** to watch KubeHeal block it in real time.

---

## ☸️ Testing with a Real Local Cluster (`k3d`)

1. Create the local test cluster and deploy two broken sample apps:
   ```powershell
   ./scripts/setup-k3d.ps1
   ```
   *(Or on Linux/macOS/Codespaces: `./scripts/setup-k3d.sh`)*

2. This sets up:
   - A `k3d` cluster named `kubeheal-cluster`
   - Read-only permissions from `k8s/rbac/readonly-triage-rbac.yaml` (`ServiceAccount: kubeheal-reader`)
   - Broken sample app `oom-crash-app` (tries to use 65MB inside a 32MB limit)
   - Broken sample app `config-crash-app` (has an invalid port setting)

3. Turn on live watching in `.env`:
   ```ini
   ENABLE_K8S_WATCHER=true
   ```

4. Restart KubeHeal so it watches the cluster and fixes crashes automatically:
   ```bash
   uvicorn kubeheal.main:app --reload --app-dir src --port 8000
   ```

---

## 🧰 Read-Only Tools Used by the AI (MCP)

KubeHeal gives the AI only read-only tools through its Model Context Protocol (MCP) server:

| MCP Tool | What It Does | Safety Check |
|---|---|---|
| `get_pod_status` | Checks pod phase, container state, restart count, exit code, and memory/CPU limits (uses local YAML simulation if offline and `ALLOW_OFFLINE_SIMULATION=true`) | Read-only check |
| `get_pod_logs` | Reads logs from crashing containers (supports `previous=True` and `container_name` for multi-container pods) | Read-only check |
| `get_pod_events` | Reads Warning and Normal events for the pod | Read-only check |
| `get_deployment_spec` | Reads images, commands, args, env vars (hiding secret/configmap `valueFrom` refs), and resource limits for `Deployment`, `StatefulSet`, and `DaemonSet` (or any supported local YAML file) | Read-only check |
| `run_readonly_kubectl` | Runs safe `kubectl` checks (`get`, `describe`, `logs`, `events`, `top`) | Blocks write verbs, shell tricks, secrets/roles, admin flags, & template dumps |

---

## 🌐 Web & API Endpoints

| Endpoint | Method | What It Does |
|---|---|---|
| `/` | `GET` | Opens the Live Web Dashboard |
| `/api/health` | `GET` | Shows cluster connection, MCP tools, active AI model, and guardrail settings |
| `/api/incidents` | `GET` | Lists all investigated crashes (newest first) |
| `/api/incidents/{incident_id}` | `GET` | Shows the full crash report, MCP tool logs, and YAML diff |
| `/api/webhook/alert` | `POST` | Receives crash alerts from Kubernetes or Alertmanager |
| `/api/simulate/{scenario}` | `POST` | Runs a built-in demo (`oom-crash` or `config-crash`) |
| `/api/guardrail-test` | `POST` | Tests whether a `kubectl` command is allowed or blocked |

---

## 📁 Project Files

```
KubeHeal/
├── .devcontainer/
│   └── devcontainer.json      # GitHub Codespaces setup (Docker, k3d, Python 3.12)
├── .github/workflows/
│   └── ci-validate.yml        # GitHub Actions CI test runner (Python 3.12)
├── k8s/
│   ├── rbac/
│   │   └── readonly-triage-rbac.yaml # Read-only cluster permissions (core, apps, batch, metrics)
│   ├── workloads/
│   │   ├── oom-crash-app.yaml        # Sample broken OOM app (32Mi limit)
│   │   └── config-crash-app.yaml     # Sample broken config app (bad port)
│   └── agent/
│       └── agent-deployment.yaml     # YAML to run KubeHeal inside a cluster
├── output/proposals/          # Saved local fix previews and diffs
├── scripts/
│   ├── setup-k3d.ps1          # Windows cluster setup script
│   └── setup-k3d.sh           # Linux/macOS cluster setup script
├── src/kubeheal/
│   ├── agent/                 # AI & fallback crash investigation logic
│   ├── gitops/                # Safe YAML editor & GitHub Pull Request creator
│   ├── mcp_server/            # Read-only MCP tools & command safety rules
│   ├── watcher/               # Background Kubernetes pod crash watcher
│   ├── static/index.html      # Live Web Dashboard UI
│   ├── config.py              # App settings loader (.env)
│   ├── models.py              # Data models (alerts, traces, reports)
│   └── main.py                # FastAPI server & API endpoints
├── tests/                     # Automated test suite across 4 files
│   ├── test_agent_prehydration.py # Pre-hydration, Gemini/Groq provider switching, & fallback tests
│   ├── test_guardrails.py     # Command blocking & safety tests
│   ├── test_webhook_and_agent.py # API, crash handling, and demo tests
│   └── test_yaml_patcher.py   # YAML editing, file discovery, & memory limit tests
├── .env.example               # Example environment settings file
├── Dockerfile                 # Docker image with kubectl installed
├── requirements.txt           # Python dependencies
└── pyproject.toml             # Project settings & pytest config
```
