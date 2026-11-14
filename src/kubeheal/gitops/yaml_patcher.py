"""Safe Kubernetes manifest patcher using ruamel.yaml with remediation guardrails."""

import difflib
import io
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from ruamel.yaml import YAML

from kubeheal.config import settings
from kubeheal.models import YAMLModification

# Default guardrail caps (can be overridden by settings.max_memory_limit / min_memory_limit)
MAX_SAFE_MEMORY_BYTES = 1 * 1024 * 1024 * 1024  # 1 GiB default ceiling
MIN_SAFE_MEMORY_BYTES = 16 * 1024 * 1024         # 16 MiB default floor

SUPPORTED_WORKLOAD_KINDS: Set[str] = {
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "Job",
    "CronJob",
    "Pod",
    "ReplicaSet",
    "Rollout",
}

COMMON_SIDECAR_NAMES: Set[str] = {
    "istio-proxy",
    "istio-init",
    "envoy",
    "linkerd-proxy",
    "linkerd-init",
    "datadog-agent",
    "fluentbit",
    "fluentd",
    "promtail",
    "vault-agent",
    "vault-agent-init",
    "cloud-sql-proxy",
    "aws-xray-daemon",
    "otel-collector",
}


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
    mi = max(1, bytes_val // (1024 ** 2))
    return f"{mi}Mi"


def get_memory_guardrail_bounds(current_limit_str: Optional[str] = None) -> Tuple[int, int]:
    """Return (min_bytes, max_bytes) using settings and ensuring we never shrink an existing higher limit."""
    try:
        min_bytes = parse_memory_to_bytes(settings.min_memory_limit)
    except Exception:
        min_bytes = MIN_SAFE_MEMORY_BYTES

    try:
        max_bytes = parse_memory_to_bytes(settings.max_memory_limit)
    except Exception:
        max_bytes = MAX_SAFE_MEMORY_BYTES

    if current_limit_str:
        try:
            curr_bytes = parse_memory_to_bytes(current_limit_str)
            # If the workload already legitimately uses >= max_bytes, allow up to 2x current limit
            if curr_bytes >= max_bytes:
                max_bytes = curr_bytes * 2
        except Exception:
            pass

    return min_bytes, max_bytes


def clamp_memory_limit(
    mem_str: str,
    current_limit_str: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """Enforce remediation guardrails: clamp proposed memory between configured floor and ceiling."""
    min_bytes, max_bytes = get_memory_guardrail_bounds(current_limit_str)
    try:
        bytes_val = parse_memory_to_bytes(mem_str)
        if bytes_val > max_bytes:
            clamped = format_bytes_to_k8s_memory(max_bytes)
            return clamped, f"Clamped proposed memory {mem_str} down to safety ceiling {clamped}"
        if bytes_val < min_bytes:
            clamped = format_bytes_to_k8s_memory(min_bytes)
            return clamped, f"Clamped proposed memory {mem_str} up to safety floor {clamped}"
        return mem_str, None
    except Exception:
        # If unparseable, preserve current limit if valid, otherwise use 2x floor
        if current_limit_str:
            try:
                parse_memory_to_bytes(current_limit_str)
                return current_limit_str, f"Unparseable memory '{mem_str}', retaining existing value {current_limit_str}"
            except Exception:
                pass
        fallback = "128Mi"
        return fallback, f"Unparseable memory '{mem_str}', defaulting to safe value {fallback}"


def infer_workload_name_from_pod(pod_name: str, owner_references: Optional[List[Any]] = None) -> str:
    """Infer parent workload (Deployment, StatefulSet, DaemonSet, Job) name from Pod metadata or name."""
    if owner_references:
        for owner in owner_references:
            kind = getattr(owner, "kind", None) or (owner.get("kind") if isinstance(owner, dict) else None)
            name = getattr(owner, "name", None) or (owner.get("name") if isinstance(owner, dict) else None)
            if not name:
                continue
            if kind == "ReplicaSet":
                # ReplicaSet created by Deployment is named <deployment-name>-<pod-template-hash>
                if "-" in name:
                    return name.rsplit("-", 1)[0]
                return name
            if kind in ("StatefulSet", "DaemonSet", "Job", "CronJob", "Rollout", "Deployment"):
                return name

    if not pod_name or "-" not in pod_name:
        return pod_name

    # Pattern 1: Deployment / Rollout pod -> <name>-<replicaset-hash (8-10 hex chars, with at least one digit)>-<pod-hash (5 alnum)>
    dep_match = re.match(r"^(.+)-([a-f0-9]{8,10})-([a-z0-9]{5})$", pod_name)
    if dep_match and any(ch.isdigit() for ch in dep_match.group(2)):
        return dep_match.group(1)

    # Pattern 2: StatefulSet pod -> <name>-<ordinal integer>
    sts_match = re.match(r"^(.+)-[0-9]+$", pod_name)
    if sts_match:
        return sts_match.group(1)

    # Pattern 3: DaemonSet or Job pod -> <name>-<5 char alnum hash>
    ds_match = re.match(r"^(.+)-[a-z0-9]{5}$", pod_name)
    if ds_match:
        return ds_match.group(1)

    return pod_name


def extract_containers_from_doc(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract the containers list from any Kubernetes workload document (Deployment, StatefulSet, CronJob, Pod, etc.)."""
    if not isinstance(doc, dict):
        return []
    kind = doc.get("kind", "")
    spec = doc.get("spec") or {}

    if kind == "Pod":
        return spec.get("containers") or []
    if kind == "CronJob":
        return (
            spec.get("jobTemplate", {})
            .get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers")
            or []
        )

    # Standard controller: Deployment, StatefulSet, DaemonSet, Job, ReplicaSet, Rollout
    containers = spec.get("template", {}).get("spec", {}).get("containers")
    if containers is not None:
        return containers

    # Fallback if bare pod spec structure
    return spec.get("containers") or []


def select_target_container(
    containers: List[Dict[str, Any]],
    container_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Select the target container by name, or intelligently pick the primary non-sidecar application container."""
    if not containers:
        raise ValueError("No containers available to select from")

    if container_name:
        for c in containers:
            if c.get("name") == container_name:
                return c

    if len(containers) == 1:
        return containers[0]

    # Filter out known service-mesh / logging / monitoring sidecars
    non_sidecars = [
        c for c in containers
        if str(c.get("name", "")).lower() not in COMMON_SIDECAR_NAMES
    ]
    if non_sidecars:
        return non_sidecars[0]

    return containers[0]


def find_manifest_for_deployment(manifests_dir: str, deployment_name: str) -> Optional[Path]:
    """Recursively search the manifests directory for any workload manifest matching deployment_name."""
    dir_path = Path(manifests_dir)
    if not dir_path.exists() or not deployment_name:
        return None

    yaml = YAML()
    yaml.preserve_quotes = True

    # 1. Search all YAML documents recursively by metadata.name and supported workload kind
    yaml_files = sorted(list(dir_path.rglob("*.yaml")) + list(dir_path.rglob("*.yml")))
    for file_path in yaml_files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                docs = list(yaml.load_all(f))
                for doc in docs:
                    if (
                        doc
                        and isinstance(doc, dict)
                        and doc.get("kind") in SUPPORTED_WORKLOAD_KINDS
                        and doc.get("metadata", {}).get("name") == deployment_name
                    ):
                        return file_path
        except Exception:
            continue

    # 2. Fallback: check filename match (both at root and recursively)
    for file_path in yaml_files:
        if file_path.stem == deployment_name or deployment_name.startswith(file_path.stem):
            return file_path

    return None


def patch_deployment_yaml(
    file_path: str,
    modifications: List[YAMLModification],
    target_workload_name: Optional[str] = None,
) -> Tuple[str, str, List[str]]:
    """Safely apply a list of modifications to a Kubernetes YAML manifest (supports multi-document YAMLs & all workload kinds).

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

    docs = list(yaml.load_all(original_content))
    if not docs:
        raise ValueError(f"Empty YAML manifest: {file_path}")

    # Find the target workload document inside (potentially multi-document) YAML
    target_doc = None
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if doc.get("kind") in SUPPORTED_WORKLOAD_KINDS:
            if target_workload_name and doc.get("metadata", {}).get("name") == target_workload_name:
                target_doc = doc
                break
            if target_doc is None and extract_containers_from_doc(doc):
                target_doc = doc

    if target_doc is None:
        # Fallback to first dict document with containers
        for doc in docs:
            if isinstance(doc, dict) and extract_containers_from_doc(doc):
                target_doc = doc
                break

    if target_doc is None:
        raise ValueError(f"No valid workload document with containers found in manifest: {file_path}")

    containers = extract_containers_from_doc(target_doc)
    if not containers:
        raise ValueError(f"No containers found in manifest: {file_path}")

    audit_notes: List[str] = []

    for mod in modifications:
        target_container = select_target_container(containers, mod.container_name)
        param = mod.parameter_path.strip()

        # 1. Memory limit / request
        if "limits.memory" in param or param == "resources.limits.memory":
            current_mem = target_container.get("resources", {}).get("limits", {}).get("memory")
            val, note = clamp_memory_limit(str(mod.new_value), current_limit_str=str(current_mem) if current_mem else None)
            if note:
                audit_notes.append(note)
            target_container.setdefault("resources", {}).setdefault("limits", {})["memory"] = val
            mod.new_value = val

        elif "requests.memory" in param or param == "resources.requests.memory":
            current_mem = target_container.get("resources", {}).get("requests", {}).get("memory")
            val, note = clamp_memory_limit(str(mod.new_value), current_limit_str=str(current_mem) if current_mem else None)
            if note:
                audit_notes.append(note)
            target_container.setdefault("resources", {}).setdefault("requests", {})["memory"] = val
            mod.new_value = val

        # 2. CPU limit / request
        elif "limits.cpu" in param or param == "resources.limits.cpu":
            target_container.setdefault("resources", {}).setdefault("limits", {})["cpu"] = str(mod.new_value)
        elif "requests.cpu" in param or param == "resources.requests.cpu":
            target_container.setdefault("resources", {}).setdefault("requests", {})["cpu"] = str(mod.new_value)

        # 3. Environment variables: e.g. env[REQUIRED_PORT] or env.REQUIRED_PORT
        elif param.startswith("env[") or param.startswith("env."):
            env_key = param[4:-1] if param.startswith("env[") and param.endswith("]") else param.split(".", 1)[1]
            env_list = target_container.setdefault("env", [])
            found = False
            for env_entry in env_list:
                if env_entry.get("name") == env_key:
                    env_entry["value"] = str(mod.new_value)
                    if "valueFrom" in env_entry:
                        del env_entry["valueFrom"]
                    found = True
                    break
            if not found:
                env_list.append({"name": env_key, "value": str(mod.new_value)})

        # 4. Container Image
        elif param in ("image", "spec.containers.image", "container.image"):
            target_container["image"] = str(mod.new_value)

        # 5. Command or Args
        elif param == "command":
            if isinstance(mod.new_value, list):
                target_container["command"] = list(mod.new_value)
            else:
                target_container["command"] = [str(mod.new_value)]
        elif param == "args":
            if isinstance(mod.new_value, list):
                target_container["args"] = list(mod.new_value)
            else:
                target_container["args"] = [str(mod.new_value)]

        # 6. Replicas at workload spec level
        elif param in ("replicas", "spec.replicas"):
            try:
                target_doc.setdefault("spec", {})["replicas"] = int(mod.new_value)
            except ValueError:
                audit_notes.append(f"Ignored invalid non-integer replicas value '{mod.new_value}'")

        else:
            audit_notes.append(f"Unsupported parameter_path '{param}' skipped by safe YAML patcher")

    # Serialize patched YAML (single or multi-document) to string
    stream = io.StringIO()
    if len(docs) == 1:
        yaml.dump(docs[0], stream)
    else:
        yaml.dump_all(docs, stream)
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
