from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.task_runtime.approval_types import ApprovalContinuationResult, ApprovalRequest, ApprovalSource
from app.task_runtime.coder_provider import CoderApprovalContinuationResult, resume_coder_approval
from app.task_runtime.session_workspace import NodeRepoCommit, NodeRepoWorkspace, merge_node_repo_to_target

logger = logging.getLogger(__name__)


def runtime_git_merge_approval(
    *,
    repo_workspace: NodeRepoWorkspace,
    node_commit: NodeRepoCommit,
    node_id: str,
) -> ApprovalRequest:
    approval_id = _runtime_git_approval_id(
        "merge",
        repo_workspace.repo_id,
        node_id,
        repo_workspace.target_branch,
        node_commit.short_hash,
    )
    command = (
        f"git merge --no-ff {repo_workspace.node_branch} "
        f"-m \"Merge {repo_workspace.node_branch} into {repo_workspace.target_branch}\""
    )
    reason = (
        f"Merge coder work from {repo_workspace.node_branch} into protected branch "
        f"{repo_workspace.target_branch}."
    )
    return ApprovalRequest(
        approval_id=approval_id,
        action_kind="merge_to_protected",
        command=command,
        reason=reason,
        payload={
            "source": "runtime_git",
            "operation": "merge_to_protected",
            "repo_workspace": repo_workspace.metadata(),
            "node_commit": node_commit.metadata(),
        },
    )


def continue_approval(
    *,
    source: str,
    approval_id: str,
    approved: bool,
    timeout_seconds: int,
    payload: dict[str, Any] | None = None,
    trusted_command_prefixes: list[str] | None = None,
) -> ApprovalContinuationResult:
    normalized_source = _approval_source(source)
    if normalized_source is None:
        return ApprovalContinuationResult(status="unsupported", error=f"Unsupported approval source: {source}", metadata={"source": source})
    if not approved:
        return ApprovalContinuationResult(status="rejected", metadata={"source": normalized_source})
    if normalized_source == "runtime_git":
        return _continue_runtime_git_approval(approval_id=approval_id, payload=payload or {})
    return _continue_codex_provider_approval(
        approval_id=approval_id,
        timeout_seconds=timeout_seconds,
        trusted_command_prefixes=trusted_command_prefixes,
    )


def _approval_source(source: str) -> ApprovalSource | None:
    normalized = str(source or "codex_provider").strip().lower()
    if normalized == "codex":
        return "codex_provider"
    if normalized == "codex_provider":
        return "codex_provider"
    if normalized == "runtime_git":
        return "runtime_git"
    return None


def _continue_codex_provider_approval(
    *,
    approval_id: str,
    timeout_seconds: int,
    trusted_command_prefixes: list[str] | None,
) -> ApprovalContinuationResult:
    result = resume_coder_approval(
        approval_id,
        approved=True,
        timeout_seconds=timeout_seconds,
        provider="codex",
        trusted_command_prefixes=trusted_command_prefixes,
    )
    return _from_coder_continuation(result, source="codex_provider")


def _continue_runtime_git_approval(*, approval_id: str, payload: dict[str, Any]) -> ApprovalContinuationResult:
    try:
        workspace, node_commit = _runtime_git_merge_context(payload)
        merge = merge_node_repo_to_target(workspace, node_commit=node_commit)
        final_text = (
            "已完成受保护分支合并："
            f"{merge.node_branch} -> {merge.target_branch} "
            f"({merge.merge_commit[:12]})"
        )
        return ApprovalContinuationResult(
            status="completed",
            final_text=final_text,
            metadata={
                "source": "runtime_git",
                "approval_id": approval_id,
                "node_merge": merge.metadata(),
            },
        )
    except Exception as exc:
        logger.exception("runtime git approval continuation failed approval_id=%s", approval_id)
        return ApprovalContinuationResult(
            status="failed",
            error=f"Runtime Git approval continuation failed: {exc}",
            metadata={"source": "runtime_git", "approval_id": approval_id},
        )


def _from_coder_continuation(result: CoderApprovalContinuationResult, *, source: str) -> ApprovalContinuationResult:
    return ApprovalContinuationResult(
        status=result.status,
        final_text=result.final_text,
        approval_requests=list(result.approval_requests),
        error=result.error,
        metadata={"source": source, **dict(result.metadata)},
    )


def _runtime_git_merge_context(payload: dict[str, Any]) -> tuple[NodeRepoWorkspace, NodeRepoCommit]:
    raw_workspace = payload.get("repo_workspace")
    raw_commit = payload.get("node_commit")
    if not isinstance(raw_workspace, dict) or not isinstance(raw_commit, dict):
        raise ValueError("Runtime Git approval payload is missing repo_workspace or node_commit.")
    integration_path = raw_workspace.get("integration_path")
    workspace = NodeRepoWorkspace(
        repo_path=Path(str(raw_workspace["repo_path"])),
        repo_id=str(raw_workspace["repo_id"]),
        source_branch=str(raw_workspace["source_branch"]),
        target_branch=str(raw_workspace["target_branch"]),
        node_branch=str(raw_workspace["node_branch"]),
        base_commit=str(raw_workspace["base_commit"]),
        integration_path=Path(str(integration_path)) if integration_path else None,
    )
    files = raw_commit.get("files")
    node_commit = NodeRepoCommit(
        commit_hash=str(raw_commit["commit_hash"]),
        short_hash=str(raw_commit["short_hash"]),
        subject=str(raw_commit["subject"]),
        files=[str(item) for item in files] if isinstance(files, list) else [],
    )
    return workspace, node_commit


def _runtime_git_approval_id(*parts: str) -> str:
    safe_parts = []
    for part in parts:
        safe = "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in str(part)).strip("._-")
        if safe:
            safe_parts.append(safe[:48])
    return "runtime_git_" + "_".join(safe_parts)
