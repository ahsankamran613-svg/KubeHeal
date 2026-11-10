"""Code-level guardrails for MCP Kubernetes operations.

Enforces read-only operations, preventing mutating verbs, shell metacharacters,
secret extraction, and privilege escalation.
"""

import re
import shlex
from typing import Tuple, Set

ALLOWED_KUBECTL_VERBS: Set[str] = {
    "get",
    "describe",
    "logs",
    "events",
    "top",
}

FORBIDDEN_KUBECTL_VERBS: Set[str] = {
    "delete",
    "apply",
    "patch",
    "edit",
    "exec",
    "run",
    "create",
    "replace",
    "scale",
    "rollout",
    "cordon",
    "drain",
    "taint",
    "label",
    "annotate",
    "port-forward",
    "cp",
    "auth",
    "attach",
    "debug",
    "proxy",
}

FORBIDDEN_SHELL_PATTERNS = re.compile(r"[;|&$`><\r\n]")

FORBIDDEN_RESOURCES: Set[str] = {
    "secret",
    "secrets",
    "serviceaccount",
    "serviceaccounts",
    "sa",
    "role",
    "roles",
    "clusterrole",
    "clusterroles",
    "rolebinding",
    "rolebindings",
    "clusterrolebinding",
    "clusterrolebindings",
}

FORBIDDEN_FLAGS: Set[str] = {
    "--token",
    "--kubeconfig",
    "--client-certificate",
    "--client-key",
    "--as",
    "--as-group",
    "--as-uid",
}


class GuardrailException(Exception):
    """Raised when an operation violates KubeHeal read-only guardrails."""
    pass


def validate_kubectl_command(cmd_str: str) -> Tuple[bool, str]:
    """Validate that a kubectl command string is strictly read-only and free of injections.

    Returns:
        (is_valid, message): Tuple indicating whether the command is allowed and explanatory message.
    """
    if not cmd_str or not cmd_str.strip():
        return False, "Command string is empty"

    cleaned = cmd_str.strip()

    # 1. Check for forbidden shell metacharacters and piping
    if FORBIDDEN_SHELL_PATTERNS.search(cleaned):
        return (
            False,
            "Security violation: Shell metacharacters (; | & $ ` > < newline) are strictly forbidden",
        )

    # 2. Tokenize command safely
    try:
        tokens = shlex.split(cleaned)
    except ValueError as e:
        return False, f"Syntax error parsing command tokens: {e}"

    if not tokens:
        return False, "No command tokens found"

    # Strip leading 'kubectl' if present
    if tokens[0].lower() in ("kubectl", "kubectl.exe"):
        tokens = tokens[1:]

    if not tokens:
        return False, "Command must specify a kubectl verb"

    # Find the first non-flag token, which represents the verb
    verb = None
    verb_idx = -1
    for i, token in enumerate(tokens):
        if not token.startswith("-"):
            verb = token.lower()
            verb_idx = i
            break

    if not verb:
        return False, "No valid kubectl verb found in command"

    # 3. Check forbidden verbs
    if verb in FORBIDDEN_KUBECTL_VERBS:
        return (
            False,
            f"Security violation: Mutating verb '{verb}' is strictly forbidden by read-only guardrail",
        )

    # 4. Check allowed verbs
    if verb not in ALLOWED_KUBECTL_VERBS:
        return (
            False,
            f"Security violation: Verb '{verb}' is not in the allowed read-only whitelist {sorted(ALLOWED_KUBECTL_VERBS)}",
        )

    # 5. Check forbidden flags
    for token in tokens:
        token_lower = token.lower()
        for forbidden_flag in FORBIDDEN_FLAGS:
            if token_lower == forbidden_flag or token_lower.startswith(f"{forbidden_flag}="):
                return (
                    False,
                    f"Security violation: Flag '{forbidden_flag}' is blocked for privilege and credential isolation",
                )

    # 6. Check forbidden resources (e.g. attempting to read secrets or RBAC credentials)
    remaining_tokens = tokens[verb_idx + 1:]
    for token in remaining_tokens:
        clean_token = token.lower().split("/")[0]  # e.g. secrets/my-secret -> secrets
        if clean_token in FORBIDDEN_RESOURCES:
            return (
                False,
                f"Security violation: Access to resource type '{clean_token}' is prohibited by credential isolation guardrail",
            )

    return True, "Command approved by read-only guardrail"
