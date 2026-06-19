from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import get_settings
from app.task_runtime.node_result import NodeResult, ResolvedInput
from app.task_runtime.planner import ExecutionPlan, PlanNode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NodeWorkspaceRef:
    node_id: str
    safe_node_id: str
    root_path: Path
    input_snapshot_path: Path
    output_path: Path
    result_path: Path
    provider_run_dir: Path
    repos_dir: Path

    def runtime_hints(self) -> dict[str, str]:
        return {
            "node_workspace_dir": str(self.root_path),
            "node_repos_dir": str(self.repos_dir),
            "node_input_snapshot_path": str(self.input_snapshot_path),
            "node_output_path": str(self.output_path),
            "node_result_path": str(self.result_path),
            "provider_run_dir": str(self.provider_run_dir),
        }

    def repo_dir(self, repo_id: str) -> Path:
        return self.repos_dir / _safe_component(repo_id, fallback="repo")


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

    def runtime_hints(self) -> dict[str, str]:
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
            "nodes": {
                node_id: {
                    "safe_node_id": node.safe_node_id,
                    "root_path": str(node.root_path),
                    "repos_dir": str(node.repos_dir),
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
        ):
            path.mkdir(parents=True, exist_ok=True)
        for node in node_refs.values():
            node.provider_run_dir.mkdir(parents=True, exist_ok=True)

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
    runtime_hints: dict[str, Any],
    instructions: list[str],
    missing_refs: list[str] | None = None,
    blocked_refs: list[str] | None = None,
) -> None:
    payload = {
        "node_id": node.id,
        "runtime": node.runtime,
        "objective": node.objective,
        "expected_output": node.expected_output,
        "input_refs": list(node.input_refs),
        "user_objective": user_objective,
        "resolved_inputs": [item.model_dump(mode="json", exclude_none=True) for item in resolved_inputs],
        "runtime_hints": _jsonable(runtime_hints),
        "instructions": list(instructions),
        "missing_refs": list(missing_refs or []),
        "blocked_refs": list(blocked_refs or []),
    }
    node_workspace.input_snapshot_path.write_text(_markdown_snapshot("Node Input Snapshot", payload), encoding="utf-8")


def write_node_result(node_workspace: NodeWorkspaceRef, result: NodeResult) -> None:
    node_workspace.output_path.write_text(result.summary or "", encoding="utf-8")
    _write_json(node_workspace.result_path, result.model_dump(mode="json", exclude_none=True))


def prepare_node_repo(
    *,
    repo_id: str,
    project_path: Path,
    runtime_hints: dict[str, Any],
) -> Path | None:
    raw_repos_dir = runtime_hints.get("node_repos_dir")
    if not raw_repos_dir:
        return None

    repos_dir = Path(str(raw_repos_dir)).resolve()
    node_repo = (repos_dir / _safe_component(repo_id, fallback="repo")).resolve()
    _assert_child(node_repo, repos_dir)
    if node_repo.exists():
        _assert_git_worktree(node_repo)
        return node_repo

    project = project_path.resolve()
    _assert_git_worktree(project)
    repos_dir.mkdir(parents=True, exist_ok=True)
    try:
        _run_git(project, "worktree", "add", "--detach", str(node_repo), "HEAD")
        return node_repo
    except RuntimeError as exc:
        logger.info("git worktree add failed; falling back to clone --reference repo_id=%s error=%s", repo_id, exc)
    _run_git(project.parent, "clone", "--reference", str(project), str(project), str(node_repo))
    return node_repo


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
    commit_hash = _git_stdout(repo, "rev-parse", "HEAD")
    short_hash = _git_stdout(repo, "rev-parse", "--short", "HEAD")
    return NodeRepoCommit(
        commit_hash=commit_hash,
        short_hash=short_hash,
        subject=subject,
        files=files,
    )


def node_workspace_hints(session_workspace: SessionWorkspaceRef | None, node_id: str) -> dict[str, str]:
    if session_workspace is None:
        return {}
    return session_workspace.node(node_id).runtime_hints()


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
            input_snapshot_path=node_root / "input_snapshot.md",
            output_path=node_root / "output.md",
            result_path=node_root / "result.json",
            provider_run_dir=node_root / "provider_run",
            repos_dir=node_root / "repo",
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
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if completed.returncode != 0 or completed.stdout.strip() != "true":
        raise RuntimeError(f"Path is not a git worktree: {path}")


def _run_git(workdir: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", "-c", f"safe.directory={workdir.resolve()}", *args],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stdout + completed.stderr).strip() or f"git {' '.join(args)} failed")


def _git_stdout(workdir: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", f"safe.directory={workdir.resolve()}", *args],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stdout + completed.stderr).strip() or f"git {' '.join(args)} failed")
    return completed.stdout.strip()


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
