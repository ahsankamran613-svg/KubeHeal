<#
.SYNOPSIS
    Sets up a local k3d cluster, provisions RBAC, and deploys broken demo workloads for KubeHeal.
#>

$ClusterName = "kubeheal-cluster"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  🚀 KubeHeal - Local k3d Cluster Setup Script" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. Check if k3d is installed
if (-not (Get-Command k3d -ErrorAction SilentlyContinue)) {
    Write-Warning "k3d CLI not found in PATH! Please install k3d (winget install k3d-io.k3d or https://k3d.io)"
    exit 1
}

# 2. Check if cluster already exists
$existing = k3d cluster list -o json | ConvertFrom-Json
$clusterExists = $existing | Where-Object { $_.name -eq $ClusterName }

if ($clusterExists) {
    Write-Host "[k3d] Cluster '$ClusterName' already exists. Reusing existing cluster." -ForegroundColor Green
} else {
    Write-Host "[k3d] Creating k3d cluster '$ClusterName'..." -ForegroundColor Yellow
    k3d cluster create $ClusterName --servers 1 --agents 1 --port "8080:80@loadbalancer"
}

# 3. Apply RBAC
Write-Host "[k8s] Applying Read-Only RBAC (ServiceAccount, ClusterRole, ClusterRoleBinding)..." -ForegroundColor Yellow
kubectl apply -f k8s/rbac/readonly-triage-rbac.yaml

# 4. Deploy Broken Workloads
Write-Host "[k8s] Deploying broken demo workloads (oom-crash-app & config-crash-app)..." -ForegroundColor Yellow
kubectl apply -f k8s/workloads/oom-crash-app.yaml
kubectl apply -f k8s/workloads/config-crash-app.yaml

Write-Host ""
Write-Host "✅ Cluster is ready!" -ForegroundColor Green
Write-Host "Pods in default namespace:" -ForegroundColor Cyan
kubectl get pods -n default
Write-Host ""
Write-Host "To start KubeHeal server with live watcher:" -ForegroundColor Green
Write-Host "  .venv\Scripts\python -m uvicorn kubeheal.main:app --reload --app-dir src --port 8000" -ForegroundColor White
Write-Host "==========================================================" -ForegroundColor Cyan
