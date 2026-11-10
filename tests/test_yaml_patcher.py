"""Unit tests for ruamel.yaml manifest patcher and remediation guardrails."""

import pytest
from pathlib import Path
from kubeheal.gitops.yaml_patcher import (
    clamp_memory_limit,
    format_bytes_to_k8s_memory,
    parse_memory_to_bytes,
    patch_deployment_yaml,
)
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
