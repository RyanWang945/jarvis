from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import get_settings
from app.task_runtime.node_result import NodeResult, ResolvedInput
from app.task_runtime.planner import ExecutionPlan, PlanNode
from app.task_runtime.runtime_context import BranchRuntimeContext, NodeWorkspaceRuntimeContext, WorkspaceRuntimeContext

logger = logging.getLogger(__name__)
_MERGE_LOCKS: dict[str, threading.RLock] = {}
_MERGE_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class NodeWorkspaceRef:
    node_id: str
    safe_node_id: str
    root_path: Path
    task_path: Path
    progress_path: Path
    result_markdown_path: Path
    state_path: Path
    artifacts_dir: Path
    input_snapshot_path: Path
    output_path: Path
    result_path: Path
    manifest_path: Path
    provider_run_dir: Path
    repo_path: Path

    def to_legacy_hints(self) -> dict[str, str]:
        return {
            "node_workspace_dir": str(self.root_path),
            "node_repo_dir": str(self.repo_path),
            "node_repos_dir": str(self.repo_path),
            "node_task_path": str(self.task_path),
            "node_progress_path": str(self.progress_path),
            "node_result_markdown_path": str(self.result_markdown_path),
            "node_state_path": str(self.state_path),
            "node_artifacts_dir": str(self.artifacts_dir),
            "node_input_snapshot_path": str(self.input_snapshot_path),
            "node_output_path": str(self.output_path),
            "node_result_path": str(self.result_path),
            "node_manifest_path": str(self.manifest_path),
            "provider_run_dir": str(self.provider_run_dir),
        }

    def repo_dir(self, repo_id: str) -> Path:
        del repo_id
        return self.repo_path


@dataclass(frozen=True)
class SessionWorkspaceRef:
    session_id: str
    root_path: Path
    session_path: Path
    dag_path: Path
    summary_path: Path
    artifacts_dir: Path
    approvals_dir: Path
    nodes_dir: Path
    nodes: dict[str, NodeWorkspaceRef]

    def node(self, node_id: str) -> NodeWorkspaceRef:
        return self.nodes[node_id]

    def to_legacy_hints(self) -> dict[str, str]:
        return {
            "session_id": self.session_id,
            "session_workspace_dir": str(self.root_path),
            "session_artifacts_dir": str(self.artifacts_dir),
            "session_approvals_dir": str(self.approvals_dir),
            "session_nodes_dir": str(self.nodes_dir),
        }

    def metadata(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "root_path": str(self.root_path),
            "session_path": str(self.session_path),
            "dag_path": str(self.dag_path),
            "summary_path": str(self.summary_path),
            "artifacts_dir": str(self.artifacts_dir),
            "approvals_dir": str(self.approvals_dir),
            "nodes_dir": str(self.nodes_dir),
            "repos_dir": str(self.root_path / "repos"),
            "nodes": {
                node_id: {
                    "safe_node_id": node.safe_node_id,
                    "root_path": str(node.root_path),
                    "repo_path": str(node.repo_path),
                    "task_path": str(node.task_path),
                    "progress_path": str(node.progress_path),
                    "result_markdown_path": str(node.result_markdown_path),
                    "state_path": str(node.state_path),
                    "artifacts_dir": str(node.artifacts_dir),
                    "provider_run_dir": str(node.provider_run_dir),
                }
                for node_id, node in self.nodes.items()
            },
        }


@dataclass(frozen=True)
class NodeRepoCommit:
    commit_hash: str
    short_hash: str
    subject: str
    files: list[str]

    def metadata(self) -> dict[str, Any]:
        return {
            "commit_hash": self.commit_hash,
            "short_hash": self.short_hash,
            "subject": self.subject,
            "files": list(self.files),
        }


@dataclass(frozen=True)
class NodeRepoWorkspace:
    repo_path: Path
    repo_id: str
    source_branch: str
    target_branch: str
    node_branch: str
    base_commit: str
    integration_path: Path | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "repo_path": str(self.repo_path),
            "repo_id": self.repo_id,
            "source_branch": self.source_branch,
            "target_branch": self.target_branch,
            "node_branch": self.node_branch,
            "base_commit": self.base_commit,
            "integration_path": str(self.integration_path) if self.integration_path is not None else None,
        }


@dataclass(frozen=True)
class NodeRepoMerge:
    target_branch: str
    node_branch: str
    target_before: str
    target_after: str
    merge_commit: str
    status: str = "merged"

    def metadata(self) -> dict[str, Any]:
        return {
            "target_branch": self.target_branch,
            "node_branch": self.node_branch,
            "target_before": self.target_before,
            "target_after": self.target_after,
            "merge_commit": self.merge_commit,
            "short_hash": self.merge_commit[:12],
            "status": self.status,
        }


@dataclass(frozen=True)
class GitCommandResult:
    exit_code: int
    stdout: str
    stderr: str


class SessionWorkspaceManager:
    def __init__(self, *, workdir: Path | None = None, session_id_factory=None) -> None:
        settings = get_settings()
        self._workdir = (workdir or settings.workspace_root).resolve()
        self._session_id_factory = session_id_factory or _default_session_id

    @property
    def sessions_root(self) -> Path:
        return self._workdir / "sessions"

    def create_for_plan(
        self,
        plan: ExecutionPlan,
        *,
        turn_id: int | None,
        conversation_id: int | None,
    ) -> SessionWorkspaceRef:
        session_id = _safe_component(self._session_id_factory(turn_id), fallback="session")
        root = (self.sessions_root / session_id).resolve()
        _assert_child(root, self.sessions_root.resolve())
        if root.exists():
            raise FileExistsError(f"Session workspace already exists: {root}")

        node_refs = _node_refs(root, plan.nodes)
        for path in (
            root,
            root / "artifacts",
            root / "approvals",
            root / "nodes",
            root / "repos",
        ):
            path.mkdir(parents=True, exist_ok=True)
        for node in node_refs.values():
            node.root_path.mkdir(parents=True, exist_ok=True)
            node.artifacts_dir.mkdir(parents=True, exist_ok=True)
            node.provider_run_dir.mkdir(parents=True, exist_ok=True)
            _write_initial_node_workspace_files(node, plan=plan)

        session = SessionWorkspaceRef(
            session_id=session_id,
            root_path=root,
            session_path=root / "session.json",
            dag_path=root / "dag.json",
            summary_path=root / "summary.md",
            artifacts_dir=root / "artifacts",
            approvals_dir=root / "approvals",
            nodes_dir=root / "nodes",
            nodes=node_refs,
        )
        now = _now()
        _write_json(
            session.session_path,
            {
                "session_id": session.session_id,
                "turn_id": turn_id,
                "conversation_id": conversation_id,
                "status": "created",
                "created_at": now,
                "updated_at": now,
                "nodes": {
                    node.id: {
                        "runtime": node.runtime,
                        "safe_node_id": session.node(node.id).safe_node_id,
                        "status": "created",
                    }
                    for node in plan.nodes
                },
            },
        )
        _write_json(session.dag_path, plan.model_dump(mode="json"))
        return session

    def update_status(self, session: SessionWorkspaceRef, status: str) -> None:
        try:
            payload = json.loads(session.session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("session workspace status update skipped session_id=%s", session.session_id, exc_info=True)
            return
        payload["status"] = status
        payload["updated_at"] = _now()
        _write_json(session.session_path, payload)


def write_node_input_snapshot(
    node_workspace: NodeWorkspaceRef,
    *,
    user_objective: str,
    node: PlanNode,
    resolved_inputs: list[ResolvedInput],
    legacy_hints: dict[str, Any],
    instructions: list[str],
    missing_refs: list[str] | None = None,
    blocked_refs: list[str] | None = None,
) -> None:
    payload = {
        "node_id": node.id,
        "runtime": node.runtime,
        "objective": node.objective,
        "output_hint": node.output_hint,
        "input_refs": list(node.input_refs),
        "user_objective": user_objective,
        "resolved_inputs": [item.model_dump(mode="json", exclude_none=True) for item in resolved_inputs],
        "runtime_context": _jsonable(legacy_hints),
        "instructions": list(instructions),
        "missing_refs": list(missing_refs or []),
        "blocked_refs": list(blocked_refs or []),
    }
    node_workspace.input_snapshot_path.write_text(_markdown_snapshot("Node Input Snapshot", payload), encoding="utf-8")


def write_node_result(node_workspace: NodeWorkspaceRef, result: NodeResult) -> NodeResult:
    result = _with_workspace_data(node_workspace, result)
    output_path = node_workspace.output_path
    if output_path.exists() and output_path.read_text(encoding="utf-8").strip() != "# Result":
        output_path = output_path.with_name("node_summary.md")
    output_path.write_text(result.summary or "", encoding="utf-8")
    _write_json(node_workspace.result_path, result.model_dump(mode="json", exclude_none=True))
    _update_node_workspace_state(node_workspace, result)
    return result


def prepare_node_repo(
    *,
    repo_id: str,
    project_path: Path,
    legacy_hints: dict[str, Any],
) -> Path | None:
    node_context = NodeWorkspaceRuntimeContext.from_hints(legacy_hints)
    if node_context.repos_dir is None:
        return None

    node_repo = node_context.repos_dir.resolve()
    if node_repo.exists():
        _assert_git_worktree(node_repo)
        return node_repo

    project = project_path.resolve()
    _assert_git_worktree(project)
    node_repo.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run_git(project, "worktree", "add", "--detach", str(node_repo), "HEAD")
        return node_repo
    except RuntimeError as exc:
        logger.info("git worktree add failed; falling back to clone --reference repo_id=%s error=%s", repo_id, exc)
    _run_git(project.parent, "clone", "--reference", str(project), str(project), str(node_repo))
    return node_repo


def prepare_node_repo_workspace(
    *,
    repo_id: str,
    project_path: Path,
    legacy_hints: dict[str, Any],
    node_id: str,
) -> NodeRepoWorkspace | None:
    node_context = NodeWorkspaceRuntimeContext.from_hints(legacy_hints)
    if node_context.repos_dir is None:
        return None

    node_repo = node_context.repos_dir.resolve()

    project = project_path.resolve()
    _assert_git_worktree(project)
    workspace_context = WorkspaceRuntimeContext.from_hints(legacy_hints)
    branch_context = BranchRuntimeContext.from_hints(legacy_hints)
    session_id = _safe_component(workspace_context.session_id or "session", fallback="session")
    source_branch = _resolve_source_branch(project, branch_context)
    target_branch = _resolve_target_branch(repo_id=repo_id, session_id=session_id, branch_context=branch_context)
    node_branch = _node_branch_name(repo_id=repo_id, session_id=session_id, node_id=node_id)
    integration_path = _integration_repo_path(repo_id=repo_id, workspace_context=workspace_context)

    _ensure_target_branch(project, target_branch=target_branch, source_branch=source_branch)
    base_commit = _git_stdout(project, "rev-parse", target_branch)

    if node_repo.exists():
        _assert_git_worktree(node_repo)
    else:
        node_repo.parent.mkdir(parents=True, exist_ok=True)
        _delete_branch_if_exists(project, node_branch)
        _run_git(project, "worktree", "add", "-b", node_branch, str(node_repo), base_commit)

    if integration_path is not None:
        if _git_stdout(project, "branch", "--show-current") == target_branch:
            integration_path = project
        else:
            _ensure_integration_worktree(project, integration_path=integration_path, target_branch=target_branch)

    return NodeRepoWorkspace(
        repo_path=node_repo,
        repo_id=repo_id,
        source_branch=source_branch,
        target_branch=target_branch,
        node_branch=node_branch,
        base_commit=base_commit,
        integration_path=integration_path,
    )


def merge_node_repo_to_target(workspace: NodeRepoWorkspace, *, node_commit: NodeRepoCommit) -> NodeRepoMerge:
    if workspace.integration_path is None:
        raise RuntimeError("Cannot merge node repo without an integration worktree.")
    lock = _merge_lock(workspace.repo_id, workspace.target_branch)
    with lock:
        integration = workspace.integration_path.resolve()
        _assert_git_worktree(integration)
        current = _git_stdout(integration, "branch", "--show-current")
        if current != workspace.target_branch:
            raise RuntimeError(f"Integration worktree is on {current or 'detached HEAD'}, expected {workspace.target_branch}.")
        dirty = _git_stdout(integration, "status", "--porcelain", "--untracked-files=all")
        if dirty.strip():
            raise RuntimeError(f"Integration worktree is dirty and cannot merge: {dirty}")
        target_before = _git_stdout(integration, "rev-parse", "HEAD")
        _run_git(
            integration,
            "-c",
            "user.name=Jarvis",
            "-c",
            "user.email=jarvis@example.local",
            "merge",
            "--no-ff",
            workspace.node_branch,
            "-m",
            f"Merge {workspace.node_branch} into {workspace.target_branch}",
        )
        target_after = _git_stdout(integration, "rev-parse", "HEAD")
        return NodeRepoMerge(
            target_branch=workspace.target_branch,
            node_branch=workspace.node_branch,
            target_before=target_before,
            target_after=target_after,
            merge_commit=target_after,
        )


def commit_node_repo(
    workdir: Path,
    *,
    node_id: str,
    objective: str,
) -> NodeRepoCommit | None:
    repo = workdir.resolve()
    _assert_git_worktree(repo)
    status = _git_stdout(repo, "status", "--porcelain", "--untracked-files=all")
    files = _modified_files_from_status(status)
    if not files:
        return None

    _run_git(repo, "add", "-A")
    staged = _git_stdout(repo, "diff", "--cached", "--name-only")
    if not staged.strip():
        return None

    subject = _commit_subject(node_id, objective)
    _run_git(
        repo,
        "-c",
        "user.name=Jarvis",
        "-c",
        "user.email=jarvis@example.local",
        "commit",
        "-m",
        subject,
    )
    remaining = _git_stdout(repo, "status", "--porcelain", "--untracked-files=all")
    if remaining.strip():
        raise RuntimeError(f"Node worktree is dirty after commit: {remaining}")
    commit_hash = _git_stdout(repo, "rev-parse", "HEAD")
    short_hash = _git_stdout(repo, "rev-parse", "--short", "HEAD")
    return NodeRepoCommit(
        commit_hash=commit_hash,
        short_hash=short_hash,
        subject=subject,
        files=files,
    )


def node_workspace_legacy_hints(session_workspace: SessionWorkspaceRef | None, node_id: str) -> dict[str, str]:
    if session_workspace is None:
        return {}
    return session_workspace.node(node_id).to_legacy_hints()


def node_workspace_hints(session_workspace: SessionWorkspaceRef | None, node_id: str) -> dict[str, str]:
    return node_workspace_legacy_hints(session_workspace, node_id)


def _write_initial_node_workspace_files(node_workspace: NodeWorkspaceRef, *, plan: ExecutionPlan) -> None:
    node = next((item for item in plan.nodes if item.id == node_workspace.node_id), None)
    if node is None:
        return
    if not node_workspace.task_path.exists():
        node_workspace.task_path.write_text(
            "\n".join(
                [
                    f"# Task: {node.id}",
                    "",
                    f"- Runtime: `{node.runtime}`",
                    f"- Mode: `{node.mode}`",
                    f"- Objective: {node.objective}",
                    f"- User objective: {plan.user_objective}",
                    f"- Output hint: {node.output_hint or ''}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    if not node_workspace.progress_path.exists():
        node_workspace.progress_path.write_text("# Progress\n\n- Workspace created.\n", encoding="utf-8")
    if not node_workspace.result_markdown_path.exists():
        node_workspace.result_markdown_path.write_text("# Result\n\n", encoding="utf-8")
    if not node_workspace.state_path.exists():
        _write_json(
            node_workspace.state_path,
            {
                "schema_version": 1,
                "workspace_id": node_workspace.safe_node_id,
                "node_id": node.id,
                "runtime": node.runtime,
                "mode": node.mode,
                "status": "created",
                "repo_path": "repo",
                "artifacts_path": "artifacts",
                "created_at": _now(),
                "updated_at": _now(),
            },
        )


def _with_workspace_data(node_workspace: NodeWorkspaceRef, result: NodeResult) -> NodeResult:
    data = dict(result.data)
    data.setdefault("workspace_path", str(node_workspace.root_path))
    data.setdefault("workspace", {})
    if isinstance(data["workspace"], dict):
        data["workspace"] = {
            **data["workspace"],
            "workspace_path": str(node_workspace.root_path),
            "task_path": str(node_workspace.task_path),
            "progress_path": str(node_workspace.progress_path),
            "result_markdown_path": str(node_workspace.result_markdown_path),
            "state_path": str(node_workspace.state_path),
            "artifacts_dir": str(node_workspace.artifacts_dir),
            "repo_path": str(node_workspace.repo_path),
            "node_result_path": str(node_workspace.result_path),
        }
    return result.model_copy(update={"data": data})


def _update_node_workspace_state(node_workspace: NodeWorkspaceRef, result: NodeResult) -> None:
    try:
        state = json.loads(node_workspace.state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {"schema_version": 1, "workspace_id": node_workspace.safe_node_id, "node_id": node_workspace.node_id}
    state["status"] = result.status
    state["summary"] = result.summary
    state["updated_at"] = _now()
    if result.git:
        state["git"] = result.git
    _write_json(node_workspace.state_path, state)


def _node_refs(root: Path, nodes: list[PlanNode]) -> dict[str, NodeWorkspaceRef]:
    used: set[str] = set()
    result: dict[str, NodeWorkspaceRef] = {}
    for node in nodes:
        safe = _unique_component(node.id, used)
        node_root = root / "nodes" / safe
        result[node.id] = NodeWorkspaceRef(
            node_id=node.id,
            safe_node_id=safe,
            root_path=node_root,
            task_path=node_root / "TASK.md",
            progress_path=node_root / "PROGRESS.md",
            result_markdown_path=node_root / "RESULT.md",
            state_path=node_root / "state.json",
            artifacts_dir=node_root / "artifacts",
            input_snapshot_path=node_root / "input_snapshot.md",
            output_path=node_root / "RESULT.md",
            result_path=node_root / "result.json",
            manifest_path=node_root / "node_manifest.json",
            provider_run_dir=node_root / "provider_run",
            repo_path=node_root / "repo",
        )
    return result


def _default_session_id(turn_id: int | None) -> str:
    prefix = f"sess_{turn_id}" if turn_id is not None else "sess"
    return f"{prefix}_{uuid4().hex[:12]}"


def _safe_component(value: Any, *, fallback: str) -> str:
    text = str(value or "").strip()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    return safe or fallback


def _unique_component(value: str, used: set[str]) -> str:
    safe = _safe_component(value, fallback="node")
    if safe not in used:
        used.add(safe)
        return safe
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    candidate = f"{safe}_{digest}"
    index = 2
    while candidate in used:
        candidate = f"{safe}_{digest}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def _assert_child(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"Path escapes workspace root: {path}") from exc


def _assert_git_worktree(path: Path) -> None:
    if not path.is_dir():
        raise RuntimeError(f"Repository path does not exist: {path}")
    result = _run_git_result(path, "rev-parse", "--is-inside-work-tree", timeout=30)
    if result.exit_code != 0 or result.stdout.strip() != "true":
        raise RuntimeError(f"Path is not a git worktree: {path}")


def _resolve_source_branch(project: Path, branch_context: BranchRuntimeContext) -> str:
    explicit = _validated_branch_hint(branch_context.source_branch)
    if explicit:
        return explicit
    for candidate in ("master", "main"):
        if _branch_exists(project, candidate):
            return candidate
    current = _git_stdout(project, "branch", "--show-current")
    return current or "HEAD"


def _resolve_target_branch(*, repo_id: str, session_id: str, branch_context: BranchRuntimeContext) -> str:
    explicit = _validated_branch_hint(branch_context.target_branch)
    if explicit:
        return explicit
    return f"jarvis/{_safe_component(repo_id, fallback='repo')}/{_safe_component(session_id, fallback='session')}"


def _validated_branch_hint(value: str) -> str | None:
    branch = str(value or "").strip()
    if not branch:
        return None
    _validate_branch_name(branch)
    return branch


def _node_branch_name(*, repo_id: str, session_id: str, node_id: str) -> str:
    branch = "/".join(
        [
            "jarvis-nodes",
            _safe_component(repo_id, fallback="repo"),
            _safe_component(session_id, fallback="session"),
            _safe_component(node_id, fallback="node"),
        ]
    )
    _validate_branch_name(branch)
    return branch


def _integration_repo_path(*, repo_id: str, workspace_context: WorkspaceRuntimeContext) -> Path | None:
    if workspace_context.session_root is None:
        return None
    session_dir = workspace_context.session_root.resolve()
    integration_root = (session_dir / "repos").resolve()
    integration_path = (integration_root / _safe_component(repo_id, fallback="repo")).resolve()
    _assert_child(integration_path, integration_root)
    return integration_path


def _ensure_target_branch(project: Path, *, target_branch: str, source_branch: str) -> None:
    _validate_branch_name(target_branch)
    if _branch_exists(project, target_branch):
        return
    _run_git(project, "branch", target_branch, source_branch)


def _ensure_integration_worktree(project: Path, *, integration_path: Path, target_branch: str) -> None:
    if integration_path.exists():
        _assert_git_worktree(integration_path)
        current = _git_stdout(integration_path, "branch", "--show-current")
        if current != target_branch:
            raise RuntimeError(f"Integration worktree is on {current or 'detached HEAD'}, expected {target_branch}.")
        return
    integration_path.parent.mkdir(parents=True, exist_ok=True)
    _run_git(project, "worktree", "add", str(integration_path), target_branch)


def _delete_branch_if_exists(project: Path, branch: str) -> None:
    if not _branch_exists(project, branch):
        return
    _run_git(project, "branch", "-D", branch)


def _branch_exists(project: Path, branch: str) -> bool:
    result = _run_git_result(project, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", timeout=30)
    return result.exit_code == 0


def _validate_branch_name(branch: str) -> None:
    if not branch:
        raise ValueError("Git branch name cannot be empty.")
    result = _run_git_result(Path.cwd(), "check-ref-format", "--branch", branch, timeout=30)
    if result.exit_code != 0:
        raise ValueError(f"Invalid git branch name: {branch}")


def _merge_lock(repo_id: str, target_branch: str) -> threading.RLock:
    key = f"{repo_id}:{target_branch}"
    with _MERGE_LOCKS_GUARD:
        lock = _MERGE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _MERGE_LOCKS[key] = lock
        return lock


def _run_git(workdir: Path, *args: str) -> None:
    result = _run_git_result(workdir, *args)
    if result.exit_code != 0:
        raise RuntimeError(_git_error_message(result, args))


def _git_stdout(workdir: Path, *args: str) -> str:
    result = _run_git_result(workdir, *args)
    if result.exit_code != 0:
        raise RuntimeError(_git_error_message(result, args))
    return result.stdout.strip()


def _run_git_result(workdir: Path, *args: str, timeout: int = 60) -> GitCommandResult:
    safe_directory = workdir.resolve().as_posix()
    completed = subprocess.run(
        ["git", "-c", f"safe.directory={safe_directory}", *args],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return GitCommandResult(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _git_error_message(result: GitCommandResult, args: tuple[str, ...]) -> str:
    return (result.stdout + result.stderr).strip() or f"git {' '.join(args)} failed"


def _modified_files_from_status(status_stdout: str) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    for raw_line in status_stdout.splitlines():
        line = raw_line.rstrip()
        if len(line) < 4:
            continue
        value = line[3:].strip()
        if not value:
            continue
        if " -> " in value:
            value = value.rsplit(" -> ", 1)[1]
        path = value.strip('"')
        if path and path not in seen:
            files.append(path)
            seen.add(path)
    return files


def _commit_subject(node_id: str, objective: str) -> str:
    safe_node_id = _safe_component(node_id, fallback="node")
    summary = " ".join(str(objective or "").split())
    if len(summary) > 72:
        summary = summary[:69].rstrip() + "..."
    return f"jarvis node {safe_node_id}: {summary or 'update worktree'}"


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _markdown_snapshot(title: str, payload: dict[str, Any]) -> str:
    return f"# {title}\n\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}\n```\n"


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, default=str)
        return value
    except TypeError:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
