"""Safe Kubernetes manifest patcher using ruamel.yaml with remediation guardrails."""

import difflib
import io
import os
import re
from pathlib import Path
from typing import Any, List, Optional, Tuple
from ruamel.yaml import YAML

from kubeheal.models import YAMLModification

# Guardrail caps to prevent unbounded resource allocation by AI
MAX_SAFE_MEMORY_BYTES = 1 * 1024 * 1024 * 1024  # 1 GiB max ceiling
MIN_SAFE_MEMORY_BYTES = 16 * 1024 * 1024         # 16 MiB floor


def parse_memory_to_bytes(mem_str: str) -> int:
    """Parse a Kubernetes memory string (e.g. '32Mi', '1Gi', '500M', '256M') into integer bytes."""
    mem_str = str(mem_str).strip()
    multipliers = {
        "ki": 1024,
        "mi": 1024 ** 2,
        "gi": 1024 ** 3,
        "ti": 1024 ** 4,
        "k": 1000,
        "m": 1000 ** 2,
        "g": 1000 ** 3,
        "t": 1000 ** 4,
    }
    match = re.match(r"^([0-9.]+)\s*([a-zA-Z]*)$", mem_str)
    if not match:
        raise ValueError(f"Invalid Kubernetes memory quantity: '{mem_str}'")
    value, unit = match.groups()
    num = float(value)
    multiplier = multipliers.get(unit.lower(), 1)
    return int(num * multiplier)


def format_bytes_to_k8s_memory(bytes_val: int) -> str:
    """Format bytes into standard Kubernetes binary SI units (e.g. '128Mi', '1Gi')."""
    if bytes_val >= 1024 ** 3 and bytes_val % (1024 ** 3) == 0:
        return f"{bytes_val // (1024 ** 3)}Gi"
    mi = bytes_val // (1024 ** 2)
    return f"{mi}Mi"


def clamp_memory_limit(mem_str: str) -> Tuple[str, Optional[str]]:
    """Enforce remediation guardrails: clamp proposed memory between 16Mi and 1Gi."""
    try:
        bytes_val = parse_memory_to_bytes(mem_str)
        if bytes_val > MAX_SAFE_MEMORY_BYTES:
            clamped = format_bytes_to_k8s_memory(MAX_SAFE_MEMORY_BYTES)
            return clamped, f"Clamped proposed memory {mem_str} down to safety ceiling {clamped}"
        if bytes_val < MIN_SAFE_MEMORY_BYTES:
            clamped = format_bytes_to_k8s_memory(MIN_SAFE_MEMORY_BYTES)
            return clamped, f"Clamped proposed memory {mem_str} up to safety floor {clamped}"
        return mem_str, None
    except Exception:
        # Fallback to standard safe default if unparseable
        return "128Mi", f"Unparseable memory '{mem_str}', defaulting to safe value 128Mi"


def find_manifest_for_deployment(manifests_dir: str, deployment_name: str) -> Optional[Path]:
    """Search the manifests directory for a deployment with the matching name."""
    dir_path = Path(manifests_dir)
    if not dir_path.exists():
        return None

    yaml = YAML()
    yaml.preserve_quotes = True

    for file_path in dir_path.glob("*.y*ml"):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                docs = list(yaml.load_all(f))
                for doc in docs:
                    if (
                        doc
                        and isinstance(doc, dict)
                        and doc.get("kind") == "Deployment"
                        and doc.get("metadata", {}).get("name") == deployment_name
                    ):
                        return file_path
        except Exception:
            continue

    # Fallback: check direct filename match
    for ext in (".yaml", ".yml"):
        candidate = dir_path / f"{deployment_name}{ext}"
        if candidate.exists():
            return candidate

    return None


def patch_deployment_yaml(
    file_path: str,
    modifications: List[YAMLModification],
) -> Tuple[str, str, List[str]]:
    """Safely apply a list of modifications to a Kubernetes Deployment YAML manifest.

    Returns:
        (patched_yaml_content, unified_diff, audit_notes)
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Manifest file not found: {file_path}")

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)

    with open(path, "r", encoding="utf-8") as f:
        original_content = f.read()

    doc = yaml.load(original_content)
    audit_notes = []

    # Locate container in spec.template.spec.containers
    containers = doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    if not containers:
        raise ValueError(f"No containers found in manifest: {file_path}")

    for mod in modifications:
        target_container = None
        if mod.container_name:
            for c in containers:
                if c.get("name") == mod.container_name:
                    target_container = c
                    break
        if not target_container:
            # Default to primary / first container
            target_container = containers[0]

        param = mod.parameter_path.strip()

        # 1. Memory limit / request
        if "limits.memory" in param or param == "resources.limits.memory":
            val, note = clamp_memory_limit(str(mod.new_value))
            if note:
                audit_notes.append(note)
            target_container.setdefault("resources", {}).setdefault("limits", {})["memory"] = val
            mod.new_value = val

        elif "requests.memory" in param or param == "resources.requests.memory":
            val, note = clamp_memory_limit(str(mod.new_value))
            if note:
                audit_notes.append(note)
            target_container.setdefault("resources", {}).setdefault("requests", {})["memory"] = val
            mod.new_value = val

        # 2. CPU limit / request
        elif "limits.cpu" in param:
            target_container.setdefault("resources", {}).setdefault("limits", {})["cpu"] = str(mod.new_value)
        elif "requests.cpu" in param:
            target_container.setdefault("resources", {}).setdefault("requests", {})["cpu"] = str(mod.new_value)

        # 3. Environment variables: e.g. env[REQUIRED_PORT] or env.REQUIRED_PORT
        elif param.startswith("env[") or param.startswith("env."):
            env_key = param[4:-1] if param.startswith("env[") and param.endswith("]") else param.split(".", 1)[1]
            env_list = target_container.setdefault("env", [])
            # Search if variable already exists
            found = False
            for env_entry in env_list:
                if env_entry.get("name") == env_key:
                    env_entry["value"] = str(mod.new_value)
                    found = True
                    break
            if not found:
                env_list.append({"name": env_key, "value": str(mod.new_value)})

        # 4. Command or Args
        elif param == "command":
            target_container["command"] = list(mod.new_value)
        elif param == "args":
            target_container["args"] = list(mod.new_value)

    # Serialize patched YAML to string
    stream = io.StringIO()
    yaml.dump(doc, stream)
    patched_content = stream.getvalue()

    # Generate unified diff
    orig_lines = original_content.splitlines(keepends=True)
    patched_lines = patched_content.splitlines(keepends=True)
    diff = "".join(
        difflib.unified_diff(
            orig_lines,
            patched_lines,
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
            n=3,
        )
    )

    return patched_content, diff, audit_notes
