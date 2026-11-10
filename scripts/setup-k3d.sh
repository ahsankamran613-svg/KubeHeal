#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="kubeheal-cluster"

echo "=========================================================="
echo "  🚀 KubeHeal - Local k3d Cluster Setup Script"
echo "=========================================================="

if ! command -v k3d &> /dev/null; then
    echo "❌ k3d CLI not found! Please install k3d: https://k3d.io"
    exit 1
fi

if k3d cluster list | grep -q "$CLUSTER_NAME"; then
    echo "✅ Cluster '$CLUSTER_NAME' already exists."
else
    echo "Creating k3d cluster '$CLUSTER_NAME'..."
    k3d cluster create "$CLUSTER_NAME" --servers 1 --agents 1 --port "8080:80@loadbalancer"
fi

echo "Applying Read-Only RBAC..."
kubectl apply -f k8s/rbac/readonly-triage-rbac.yaml

echo "Deploying broken demo workloads..."
kubectl apply -f k8s/workloads/oom-crash-app.yaml
kubectl apply -f k8s/workloads/config-crash-app.yaml

echo ""
echo "✅ Setup complete! Pods:"
kubectl get pods -n default
echo "=========================================================="
