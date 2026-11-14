"""Unit tests for ruamel.yaml manifest patcher and remediation guardrails."""

import pytest
from pathlib import Path
from kubeheal.gitops.yaml_patcher import (
    clamp_memory_limit,
    find_manifest_for_deployment,
    format_bytes_to_k8s_memory,
    infer_workload_name_from_pod,
    parse_memory_to_bytes,
    patch_deployment_yaml,
)
from kubeheal.gitops.github_pr import resolve_repo_relative_path
from kubeheal.models import YAMLModification


def test_memory_parsing_and_formatting():
    assert parse_memory_to_bytes("32Mi") == 32 * 1024 * 1024
    assert parse_memory_to_bytes("1Gi") == 1024 * 1024 * 1024
    assert parse_memory_to_bytes("500M") == 500 * 1000 * 1000
    assert format_bytes_to_k8s_memory(128 * 1024 * 1024) == "128Mi"
    assert format_bytes_to_k8s_memory(1024 * 1024 * 1024) == "1Gi"


def test_memory_guardrail_clamping():
    # Below floor (16Mi) -> clamped to 16Mi
    clamped, note = clamp_memory_limit("4Mi")
    assert clamped == "16Mi"
    assert note is not None

    # Exceeding ceiling (1Gi) -> clamped to 1Gi
    clamped, note = clamp_memory_limit("64Gi")
    assert clamped == "1Gi"
    assert note is not None

    # Normal range (128Mi) -> unchanged
    clamped, note = clamp_memory_limit("128Mi")
    assert clamped == "128Mi"
    assert note is None


def test_patch_oom_manifest(tmp_path: Path):
    # Copy oom-crash-app.yaml to tmp_path
    original_yaml = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: oom-crash-app
  namespace: default
spec:
  replicas: 1
  template:
    spec:
      containers:
        - name: worker
          image: python:3.12-alpine
          resources:
            requests:
              memory: 16Mi
            limits:
              # Comment preserved
              memory: 32Mi
"""
    test_file = tmp_path / "oom-crash-app.yaml"
    test_file.write_text(original_yaml, encoding="utf-8")

    modifications = [
        YAMLModification(
            file_path=str(test_file),
            container_name="worker",
            parameter_path="resources.limits.memory",
            old_value="32Mi",
            new_value="128Mi",
            rationale="Bump memory limit to 128Mi",
        ),
        YAMLModification(
            file_path=str(test_file),
            container_name="worker",
            parameter_path="resources.requests.memory",
            old_value="16Mi",
            new_value="64Mi",
            rationale="Bump memory request to 64Mi",
        ),
    ]

    patched_content, diff, notes = patch_deployment_yaml(str(test_file), modifications)

    assert "memory: 128Mi" in patched_content
    assert "memory: 64Mi" in patched_content
    assert "# Comment preserved" in patched_content
    assert "+              memory: 128Mi" in diff or "+            memory: 128Mi" in diff


def test_patch_config_env_manifest(tmp_path: Path):
    original_yaml = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: config-crash-app
spec:
  template:
    spec:
      containers:
        - name: api-server
          image: python:3.12-alpine
          env:
            - name: APP_ENV
              value: "production"
            - name: REQUIRED_PORT
              value: "invalid_port"
"""
    test_file = tmp_path / "config-crash-app.yaml"
    test_file.write_text(original_yaml, encoding="utf-8")

    modifications = [
        YAMLModification(
            file_path=str(test_file),
            container_name="api-server",
            parameter_path="env[REQUIRED_PORT]",
            old_value="invalid_port",
            new_value="8080",
            rationale="Fix port number",
        ),
    ]

    patched_content, diff, notes = patch_deployment_yaml(str(test_file), modifications)

    assert 'value: "8080"' in patched_content or "value: 8080" in patched_content
    assert "-              value: \"invalid_port\"" in diff or "-            value: \"invalid_port\"" in diff


def test_infer_workload_name_across_controllers():
    # Deployment pod
    assert infer_workload_name_from_pod("payment-service-7598c9f566-x9z2k") == "payment-service"
    # StatefulSet pod (single hyphen ordinal)
    assert infer_workload_name_from_pod("redis-cluster-0") == "redis-cluster"
    assert infer_workload_name_from_pod("postgres-12") == "postgres"
    # DaemonSet / Job pod (single 5-char hash)
    assert infer_workload_name_from_pod("node-exporter-k8x9q") == "node-exporter"
    # Explicit OwnerReferences override
    assert infer_workload_name_from_pod(
        "custom-pod-xyz",
        owner_references=[{"kind": "StatefulSet", "name": "my-stateful-db"}],
    ) == "my-stateful-db"
    assert infer_workload_name_from_pod(
        "web-abc-123",
        owner_references=[{"kind": "ReplicaSet", "name": "web-app-684cbf9b4d"}],
    ) == "web-app"


def test_recursive_and_multi_kind_manifest_discovery(tmp_path: Path):
    nested_dir = tmp_path / "prod" / "databases"
    nested_dir.mkdir(parents=True)
    sts_file = nested_dir / "redis.yaml"
    sts_file.write_text(
        """apiVersion: v1
kind: Service
metadata:
  name: redis-headless
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: redis-cluster
spec:
  serviceName: redis-headless
  replicas: 3
  template:
    spec:
      containers:
        - name: istio-proxy
          image: istio/proxyv2:1.20
        - name: redis
          image: redis:7.2-alpine
          resources:
            limits:
              memory: 256Mi
""",
        encoding="utf-8",
    )

    # Should find StatefulSet inside nested folder and multi-document YAML
    found = find_manifest_for_deployment(str(tmp_path), "redis-cluster")
    assert found is not None
    assert found == sts_file

    # Patch without specifying container_name -> should skip 'istio-proxy' sidecar and patch 'redis'
    mods = [
        YAMLModification(
            file_path=str(found),
            parameter_path="resources.limits.memory",
            old_value="256Mi",
            new_value="512Mi",
            rationale="Scale Redis memory limit",
        )
    ]
    patched, diff, notes = patch_deployment_yaml(str(found), mods, target_workload_name="redis-cluster")
    assert "kind: Service" in patched
    assert "memory: 512Mi" in patched
    assert "+              memory: 512Mi" in diff or "+            memory: 512Mi" in diff


def test_large_memory_workload_not_shrunk():
    # If an existing database container already has 2Gi memory limit, proposing 4Gi should NOT shrink it to 1Gi
    clamped, note = clamp_memory_limit("4Gi", current_limit_str="2Gi")
    assert clamped == "4Gi"
    assert note is None


def test_resolve_repo_relative_path(tmp_path: Path):
    repo_root = tmp_path / "my-repo"
    manifest_file = repo_root / "deployments" / "production" / "api.yaml"
    manifest_file.parent.mkdir(parents=True)
    manifest_file.write_text("kind: Deployment", encoding="utf-8")

    rel_path = resolve_repo_relative_path(manifest_file, repo_root=str(repo_root))
    assert rel_path == "deployments/production/api.yaml"

