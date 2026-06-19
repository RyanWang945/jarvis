from __future__ import annotations

import subprocess
from pathlib import Path

from app.repositories import RepositoryRef, RepositoryRegistry
from app.task_runtime.node_execute_runtime import CoderNodeExecuteRuntime
from app.task_runtime.node_executor import NodeExecutor
from app.task_runtime.planner import ExecutionPlan, PlanNode
from app.task_runtime.session_workspace import SessionWorkspaceManager, prepare_node_repo
from app.task_runtime.coder_provider import CoderRunResult


def test_session_workspace_creates_fixed_node_directories_after_plan(tmp_path: Path) -> None:
    plan = ExecutionPlan(
        user_objective="inspect and modify",
        nodes=[
            PlanNode(id="inspect/code", runtime="coder", objective="Inspect code"),
            PlanNode(id="../modify code", runtime="coder", objective="Modify code"),
        ],
    )
    manager = SessionWorkspaceManager(workdir=tmp_path, session_id_factory=lambda turn_id: "session-test")

    workspace = manager.create_for_plan(plan, turn_id=42, conversation_id=7)

    assert workspace.root_path == tmp_path / "sessions" / "session-test"
    assert workspace.session_path.exists()
    assert workspace.dag_path.exists()
    assert (workspace.root_path / "nodes").is_dir()
    assert not (workspace.root_path / "repos").exists()
    assert workspace.node("inspect/code").provider_run_dir.is_dir()
    assert workspace.node("../modify code").root_path.parent == workspace.nodes_dir
    assert ".." not in workspace.node("../modify code").safe_node_id
    assert workspace.node("inspect/code").repos_dir == workspace.node("inspect/code").root_path / "repo"


def test_coder_nodes_get_independent_node_repos_and_provider_run_dirs(monkeypatch, tmp_path: Path) -> None:
    project = _init_repo(tmp_path / "projects" / "jarvis")
    registry = RepositoryRegistry(
        [
            RepositoryRef(
                repo_id="jarvis",
                name="Jarvis",
                root_path=project,
                canonical_root_path=project.resolve(),
            )
        ]
    )
    monkeypatch.setattr("app.task_runtime.node_execute_runtime.get_repository_registry", lambda: registry)
    provider = _RecordingProvider()
    runtime = CoderNodeExecuteRuntime(provider=provider)
    plan = ExecutionPlan(
        user_objective="modify independently",
        nodes=[
            PlanNode(id="modify_a", runtime="coder", objective="Modify A", runtime_hints={"access_mode": "write"}),
            PlanNode(id="modify_b", runtime="coder", objective="Modify B", runtime_hints={"access_mode": "write"}),
        ],
    )
    workspace = SessionWorkspaceManager(workdir=tmp_path, session_id_factory=lambda turn_id: "session-code").create_for_plan(
        plan,
        turn_id=99,
        conversation_id=5,
    )

    report = NodeExecutor(runtimes={"coder": runtime}).execute(
        plan,
        runtime_hints={"active_repo": "jarvis"},
        session_workspace=workspace,
    )

    assert report.status == "completed"
    assert len(provider.requests) == 2
    first, second = provider.requests
    assert first.workdir == workspace.node("modify_a").repo_dir("jarvis").resolve()
    assert second.workdir == workspace.node("modify_b").repo_dir("jarvis").resolve()
    assert first.workdir != second.workdir
    assert first.run_dir == workspace.node("modify_a").provider_run_dir
    assert second.run_dir == workspace.node("modify_b").provider_run_dir
    assert (workspace.node("modify_a").input_snapshot_path).exists()
    assert (workspace.node("modify_b").result_path).exists()
    assert (first.workdir / "README.md").exists()
    assert (second.workdir / "README.md").exists()


def test_write_coder_node_auto_commits_node_worktree(monkeypatch, tmp_path: Path) -> None:
    project = _init_repo(tmp_path / "projects" / "jarvis")
    registry = RepositoryRegistry(
        [
            RepositoryRef(
                repo_id="jarvis",
                name="Jarvis",
                root_path=project,
                canonical_root_path=project.resolve(),
            )
        ]
    )
    monkeypatch.setattr("app.task_runtime.node_execute_runtime.get_repository_registry", lambda: registry)
    runtime = CoderNodeExecuteRuntime(provider=_WritingProvider())
    plan = ExecutionPlan(
        user_objective="write a file",
        nodes=[PlanNode(id="write_node", runtime="coder", objective="Write node file", runtime_hints={"access_mode": "write"})],
    )
    workspace = SessionWorkspaceManager(workdir=tmp_path, session_id_factory=lambda turn_id: "session-commit").create_for_plan(
        plan,
        turn_id=101,
        conversation_id=5,
    )

    report = NodeExecutor(runtimes={"coder": runtime}).execute(
        plan,
        runtime_hints={"active_repo": "jarvis"},
        session_workspace=workspace,
    )

    result = report.node_results[0]
    node_repo = workspace.node("write_node").repo_dir("jarvis").resolve()
    node_commit = result.data["node_commit"]
    assert result.status == "completed"
    assert node_commit["short_hash"] == _git_stdout(node_repo, "rev-parse", "--short", "HEAD")
    assert node_commit["files"] == ["node-output.txt"]
    assert _git_stdout(node_repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert _git_stdout(node_repo, "show", "--pretty=", "--name-only", "HEAD").strip() == "node-output.txt"
    assert any(artifact.ref == node_commit["short_hash"] and artifact.kind == "git_commit" for artifact in result.artifacts)


def test_coder_runtime_does_not_auto_commit_registered_repo_without_node_workspace(monkeypatch, tmp_path: Path) -> None:
    project = _init_repo(tmp_path / "projects" / "jarvis")
    registry = RepositoryRegistry(
        [
            RepositoryRef(
                repo_id="jarvis",
                name="Jarvis",
                root_path=project,
                canonical_root_path=project.resolve(),
            )
        ]
    )
    monkeypatch.setattr("app.task_runtime.node_execute_runtime.get_repository_registry", lambda: registry)
    runtime = CoderNodeExecuteRuntime(provider=_WritingProvider())
    initial_head = _git_stdout(project, "rev-parse", "HEAD")

    result = runtime.run(
        _node_context(
            node=PlanNode(id="write_node", runtime="coder", objective="Write node file", runtime_hints={"access_mode": "write"}),
            runtime_hints={"active_repo": "jarvis"},
        )
    )

    assert result.status == "completed"
    assert "node_commit" not in result.data
    assert _git_stdout(project, "rev-parse", "HEAD") == initial_head
    assert _git_stdout(project, "status", "--porcelain", "--untracked-files=all") == "?? node-output.txt"


def test_prepare_node_repo_works_when_project_is_jarvis_workdir(tmp_path: Path) -> None:
    project = _init_repo(tmp_path / "jarvis")
    plan = ExecutionPlan(
        user_objective="modify jarvis",
        nodes=[PlanNode(id="modify", runtime="coder", objective="Modify Jarvis")],
    )
    workspace = SessionWorkspaceManager(workdir=project, session_id_factory=lambda turn_id: "session-nested").create_for_plan(
        plan,
        turn_id=100,
        conversation_id=5,
    )

    node_repo = prepare_node_repo(
        repo_id="jarvis",
        project_path=project,
        runtime_hints=workspace.node("modify").runtime_hints(),
    )

    assert node_repo == workspace.node("modify").repo_dir("jarvis").resolve()
    assert (node_repo / "README.md").exists()
    assert _git_stdout(node_repo, "rev-parse", "--is-inside-work-tree") == "true"


class _RecordingProvider:
    name = "recording"

    def __init__(self) -> None:
        self.requests = []

    def run(self, request, *, decide_action):
        del decide_action
        self.requests.append(request)
        return CoderRunResult(ok=True, exit_code=0, stdout=f"ran in {request.workdir}", summary="done")

    def resume_approval(self, approval_id, *, approved, timeout_seconds, trusted_command_prefixes=None):
        raise NotImplementedError


class _WritingProvider:
    name = "writing"

    def run(self, request, *, decide_action):
        del decide_action
        (request.workdir / "node-output.txt").write_text("created by node\n", encoding="utf-8")
        return CoderRunResult(ok=True, exit_code=0, stdout="wrote file", summary="done")

    def resume_approval(self, approval_id, *, approved, timeout_seconds, trusted_command_prefixes=None):
        raise NotImplementedError


def _node_context(*, node: PlanNode, runtime_hints: dict[str, object]):
    from app.task_runtime.node_execute_runtime import NodeExecutionContext

    return NodeExecutionContext(
        user_objective="write a file",
        node=node,
        runtime_hints=runtime_hints,
    )


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "config", "user.email", "jarvis@example.test")
    _git(path, "config", "user.name", "Jarvis Tests")
    (path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")
    return path


def _git(path: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", "-c", f"safe.directory={path.resolve()}", *args],
        cwd=str(path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _git_stdout(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", f"safe.directory={path.resolve()}", *args],
        cwd=str(path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed.stdout.strip()
