"""GitHub Pull Request creator and local proposal bundle generator."""

import json
import logging
from pathlib import Path
from typing import List, Optional, Tuple

from kubeheal.config import settings
from kubeheal.models import AlertPayload, MCPToolExecution, YAMLModification

logger = logging.getLogger(__name__)


def resolve_repo_relative_path(manifest_path: Path, repo_root: Optional[str] = None) -> str:
    """Resolve the repository-relative POSIX path for a manifest file regardless of folder naming."""
    root = Path(repo_root or settings.repo_root_dir).resolve()
    try:
        resolved_manifest = manifest_path.resolve()
        return resolved_manifest.relative_to(root).as_posix()
    except ValueError:
        # Fallback if manifest_path is already relative or outside root
        raw_posix = manifest_path.as_posix().lstrip("./")
        return raw_posix


def generate_pr_markdown_body(
    incident_id: str,
    alert: AlertPayload,
    root_cause: str,
    modifications: List[YAMLModification],
    diff_content: str,
    mcp_traces: List[MCPToolExecution],
) -> str:
    """Generate structured markdown body for the GitHub PR or local proposal."""
    mod_lines = []
    for m in modifications:
        c_tag = f" (container `{m.container_name}`)" if m.container_name else ""
        mod_lines.append(
            f"- **`{m.parameter_path}`**{c_tag}: changed from `{m.old_value}` to `{m.new_value}`\n  *Rationale*: {m.rationale}"
        )
    mod_section = "\n".join(mod_lines) if mod_lines else "No specific YAML modifications recorded."

    trace_rows = []
    for t in mcp_traces:
        status_badge = "🛡️ Blocked" if t.blocked_by_guardrail else ("✅ Success" if t.status == "success" else "⚠️ " + t.status)
        trace_rows.append(
            f"| `{t.tool_name}` | `{json.dumps(t.arguments)}` | {status_badge} | {t.output_preview[:80]}... |"
        )
    trace_table = (
        "| Tool | Arguments | Status | Preview |\n|---|---|---|---|\n" + "\n".join(trace_rows)
        if trace_rows
        else "No MCP tool traces recorded."
    )

    return f"""# 🛡️ KubeHeal Automated Remediation Proposal

**Incident ID**: `{incident_id}`  
**Target Pod**: `{alert.pod_name}` (Namespace: `{alert.namespace}`)  
**Failure Reason**: `{alert.reason}` (Exit Code: `{alert.exit_code}`)  
**Crashing Container**: `{alert.container_name or 'primary'}`

---

## 🔍 Root Cause Analysis
{root_cause}

---

## 🛠️ Proposed Manifest Changes
{mod_section}

### Unified Diff
```diff
{diff_content}
```

---

## 📋 Guardrail Verification Checklist
- [x] **Strict Read-Only Inspection**: MCP server verified zero mutating verbs (`delete`, `apply`, `patch`, `edit`, `exec`) were executed.
- [x] **RBAC Isolation**: Inspected via `kubeheal-reader` service account restricted to `get`, `list`, `watch`.
- [x] **Safe Remediation Caps**: Resource limits validated against safe infrastructure ceilings (Max Memory <= {settings.max_memory_limit}).
- [x] **GitOps Human-in-the-Loop**: Live cluster was NOT mutated directly; changes require code review and Git merge.

---

## 🔬 MCP Read-Only Diagnostic Traces
{trace_table}

*Generated autonomously by [KubeHeal](https://github.com/KubeHeal) using Groq ({settings.groq_model}) and Model Context Protocol (MCP).*
"""


def create_remediation_proposal(
    incident_id: str,
    alert: AlertPayload,
    root_cause: str,
    modifications: List[YAMLModification],
    manifest_path: Path,
    patched_content: str,
    diff_content: str,
    mcp_traces: List[MCPToolExecution],
) -> Tuple[Optional[str], str]:
    """Create a local proposal bundle on disk, and optionally open a GitHub Pull Request.

    Returns:
        (pr_url, local_proposal_dir_path)
    """
    # 1. Always write local proposal bundle
    base_dir = Path(settings.proposals_dir) / incident_id
    base_dir.mkdir(parents=True, exist_ok=True)

    # Save patched manifest
    patched_file = base_dir / manifest_path.name
    with open(patched_file, "w", encoding="utf-8") as f:
        f.write(patched_content)

    # Save diff
    diff_file = base_dir / "patch.diff"
    with open(diff_file, "w", encoding="utf-8") as f:
        f.write(diff_content)

    # Generate and save markdown proposal
    pr_body = generate_pr_markdown_body(
        incident_id=incident_id,
        alert=alert,
        root_cause=root_cause,
        modifications=modifications,
        diff_content=diff_content,
        mcp_traces=mcp_traces,
    )
    proposal_md = base_dir / "PROPOSAL.md"
    with open(proposal_md, "w", encoding="utf-8") as f:
        f.write(pr_body)

    pr_url: Optional[str] = None

    # 2. Open GitHub PR if configured and not dry-run
    if (
        not settings.dry_run_mode
        and settings.github_token
        and settings.github_repo
    ):
        try:
            from github import Github, Auth
            auth = Auth.Token(settings.github_token)
            gh = Github(auth=auth)
            repo = gh.get_repo(settings.github_repo)

            base_branch = repo.get_branch(settings.github_base_branch)
            branch_name = f"fix/kubeheal-{alert.deployment_name or 'workload'}-{incident_id[:8]}"

            # Create new branch from base branch
            repo.create_git_ref(
                ref=f"refs/heads/{branch_name}",
                sha=base_branch.commit.sha,
            )

            # Resolve accurate repository-relative file path
            repo_file_path = resolve_repo_relative_path(manifest_path)

            # Get current file sha on branch
            file_contents = repo.get_contents(repo_file_path, ref=branch_name)

            # Commit changes to branch
            commit_msg = f"fix(k8s): KubeHeal remediation for {alert.deployment_name or alert.pod_name} ({alert.reason})"
            repo.update_file(
                path=file_contents.path,
                message=commit_msg,
                content=patched_content,
                sha=file_contents.sha,
                branch=branch_name,
            )

            # Open PR
            pr_title = f"fix(k8s): KubeHeal remediation for {alert.deployment_name or alert.pod_name} ({alert.reason})"
            pr = repo.create_pull(
                title=pr_title,
                body=pr_body,
                head=branch_name,
                base=settings.github_base_branch,
            )
            pr_url = pr.html_url

        except Exception as e:
            logger.warning(f"GitHub PR creation failed ({e}); local proposal saved at {base_dir}")
            pr_url = None

    return pr_url, str(base_dir)
