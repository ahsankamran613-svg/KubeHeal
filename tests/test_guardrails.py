"""Unit tests verifying code-level MCP guardrail validation and protection."""

from kubeheal.mcp_server.guardrails import validate_kubectl_command


def test_allowed_readonly_commands():
    allowed_cases = [
        "kubectl get pods",
        "kubectl get pods -n kube-system",
        "kubectl describe pod oom-crash-app-xyz",
        "kubectl logs -f oom-crash-app-xyz --tail=50",
        "kubectl events -n default",
        "kubectl top pods",
        "get pods -n default -o wide",
        "describe deployment config-crash-app",
        "logs my-pod -p",
    ]
    for cmd in allowed_cases:
        is_valid, msg = validate_kubectl_command(cmd)
        assert is_valid is True, f"Expected '{cmd}' to be allowed, but failed: {msg}"


def test_blocked_mutating_verbs():
    forbidden_verbs = [
        "kubectl delete pod my-pod",
        "kubectl delete pods --all",
        "kubectl apply -f manifest.yaml",
        "kubectl patch deployment my-app -p '{}'",
        "kubectl edit deployment my-app",
        "kubectl exec -it my-pod -- sh",
        "kubectl run bad-pod --image=busybox",
        "kubectl scale deployment my-app --replicas=0",
        "kubectl rollout restart deployment my-app",
        "kubectl cordon worker-node-1",
        "kubectl drain worker-node-1",
        "kubectl taint nodes worker-node-1 key=value:NoSchedule",
    ]
    for cmd in forbidden_verbs:
        is_valid, msg = validate_kubectl_command(cmd)
        assert is_valid is False, f"Expected '{cmd}' to be blocked!"
        assert "strictly forbidden" in msg or "not in the allowed" in msg


def test_blocked_shell_injections():
    injections = [
        "kubectl get pods; rm -rf /",
        "kubectl get pods | grep evil",
        "kubectl get pods && echo hacked",
        "kubectl get pods > /tmp/out",
        "kubectl get pods < /dev/zero",
        "kubectl get pods `whoami`",
        "kubectl get pods $(id)",
        "kubectl get pods \n kubectl delete pods --all",
    ]
    for cmd in injections:
        is_valid, msg = validate_kubectl_command(cmd)
        assert is_valid is False, f"Expected injection '{cmd}' to be blocked!"
        assert "Shell metacharacters" in msg


def test_blocked_sensitive_resources_and_flags():
    sensitive_cases = [
        ("kubectl get secrets", "Access to resource type 'secrets' is prohibited"),
        ("kubectl get pods,secrets", "Access to resource type 'secrets' is prohibited"),
        ("kubectl describe secret/db-credential", "Access to resource type 'secret' is prohibited"),
        ("kubectl get serviceaccounts", "Access to resource type 'serviceaccounts' is prohibited"),
        ("kubectl get pods --token=secret-token-xyz", "Flag '--token' is blocked"),
        ("kubectl get pods --kubeconfig=/root/.kube/config", "Flag '--kubeconfig' is blocked"),
        ("kubectl get pods --as=cluster-admin", "Flag '--as' is blocked"),
        ("kubectl get pods -o jsonpath='{.items[*]}'", "Output template extraction"),
        ("kubectl get pods --output=jsonpath={.items[*].spec}", "Output template extraction"),
        ("kubectl get pods -o=go-template='{{.items}}'", "Output template extraction"),
    ]
    for cmd, expected_err in sensitive_cases:
        is_valid, msg = validate_kubectl_command(cmd)
        assert is_valid is False, f"Expected '{cmd}' to be blocked!"
        assert expected_err in msg


def test_empty_command_handling():
    is_valid, msg = validate_kubectl_command("")
    assert is_valid is False
    assert "empty" in msg
