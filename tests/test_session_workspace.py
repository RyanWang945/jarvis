from __future__ import annotations

import subprocess
from pathlib import Path

from app.repositories import RepositoryRef, RepositoryRegistry
from app.task_runtime.node_execute_runtime import CoderNodeExecuteRuntime
from app.task_runtime.node_executor import NodeExecutor
from app.task_runtime.node_result import NodeResult
from app.task_runtime.planner import ExecutionPlan, PlanNode
from app.task_runtime.session_workspace import SessionWorkspaceManager, prepare_node_repo, prepare_node_repo_workspace, write_node_result
from app.task_runtime.coder_provider import CoderRunResult


def _noop_git_context(**kwargs):
    del kwargs
    return {}


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
    assert (workspace.root_path / "repos").is_dir()
    assert workspace.node("inspect/code").provider_run_dir.is_dir()
    assert workspace.node("../modify code").root_path.parent == workspace.nodes_dir
    assert ".." not in workspace.node("../modify code").safe_node_id
    assert workspace.node("inspect/code").repos_dir == workspace.node("inspect/code").root_path / "repo"
    assert workspace.to_legacy_hints()["session_id"] == "session-test"
    assert workspace.node("inspect/code").to_legacy_hints()["node_workspace_dir"] == str(workspace.node("inspect/code").root_path)


def test_write_node_result_preserves_existing_output_file(tmp_path: Path) -> None:
    plan = ExecutionPlan(
        user_objective="write report",
        nodes=[PlanNode(id="report", runtime="react", objective="Write report")],
    )
    workspace = SessionWorkspaceManager(workdir=tmp_path, session_id_factory=lambda turn_id: "session-report").create_for_plan(
        plan,
        turn_id=42,
        conversation_id=7,
    )
    node = workspace.node("report")
    node.output_path.write_text("# Detailed report\n\nbody", encoding="utf-8")

    write_node_result(
        node,
        NodeResult(node_id="report", runtime="react", status="completed", summary="short summary"),
    )

    assert node.output_path.read_text(encoding="utf-8") == "# Detailed report\n\nbody"
    assert (node.root_path / "node_summary.md").read_text(encoding="utf-8") == "short summary"
    assert node.result_path.exists()


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
    runtime = CoderNodeExecuteRuntime(provider=provider, git_context_resolver=_noop_git_context)
    plan = ExecutionPlan(
        user_objective="modify independently",
        nodes=[
            PlanNode(id="modify_a", runtime="coder", objective="Modify A"),
            PlanNode(id="modify_b", runtime="coder", objective="Modify B"),
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


def test_coder_node_uses_only_registered_repo_when_active_repo_missing(monkeypatch, tmp_path: Path) -> None:
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
    runtime = CoderNodeExecuteRuntime(provider=provider, git_context_resolver=_noop_git_context)

    result = runtime.run(
        _node_context(
            node=PlanNode(id="inspect", runtime="coder", objective="Inspect repo"),
            legacy_hints={},
        )
    )

    assert result.status == "completed"
    assert provider.requests[0].repo_id == "jarvis"
    assert provider.requests[0].workdir == project.resolve()


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
    runtime = CoderNodeExecuteRuntime(provider=_WritingProvider(), git_context_resolver=_noop_git_context)
    plan = ExecutionPlan(
        user_objective="write a file",
        nodes=[PlanNode(id="write_node", runtime="coder", objective="Write node file")],
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
    node_commit = result.git["node_commit"]
    node_merge = result.git["node_merge"]
    assert result.status == "completed"
    assert node_commit["short_hash"] == _git_stdout(node_repo, "rev-parse", "--short", "HEAD")
    assert node_commit["files"] == ["node-output.txt"]
    assert result.git["repo_workspace"]["target_branch"] == "jarvis/jarvis/session-commit"
    assert result.git["repo_workspace"]["node_branch"] == "jarvis-nodes/jarvis/session-commit/write_node"
    assert node_merge["target_branch"] == "jarvis/jarvis/session-commit"
    assert node_merge["node_branch"] == "jarvis-nodes/jarvis/session-commit/write_node"
    assert node_merge["merge_commit"] == _git_stdout(workspace.root_path / "repos" / "jarvis", "rev-parse", "HEAD")
    assert _git_stdout(node_repo, "branch", "--show-current") == "jarvis-nodes/jarvis/session-commit/write_node"
    assert _git_stdout(workspace.root_path / "repos" / "jarvis", "branch", "--show-current") == "jarvis/jarvis/session-commit"
    assert _git_stdout(node_repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert _git_stdout(node_repo, "show", "--pretty=", "--name-only", "HEAD").strip() == "node-output.txt"
    assert _git_stdout(workspace.root_path / "repos" / "jarvis", "show", "--pretty=", "HEAD:node-output.txt").strip() == "created by node"
    assert any(artifact.ref == node_commit["short_hash"] and artifact.kind == "node_git_commit" for artifact in result.artifacts)
    assert any(artifact.ref == node_merge["short_hash"] and artifact.kind == "git_commit" for artifact in result.artifacts)
    assert any(artifact.ref == "jarvis/jarvis/session-commit" and artifact.kind == "git_branch" for artifact in result.artifacts)


def test_write_coder_node_uses_resolved_target_branch_without_provider_checkout(monkeypatch, tmp_path: Path) -> None:
    project = _init_repo(tmp_path / "projects" / "smoke-test")
    default_branch = _git_stdout(project, "branch", "--show-current")
    registry = RepositoryRegistry(
        [
            RepositoryRef(
                repo_id="smoke-test",
                name="Smoke Test",
                root_path=project,
                canonical_root_path=project.resolve(),
            )
        ]
    )
    monkeypatch.setattr("app.task_runtime.node_execute_runtime.get_repository_registry", lambda: registry)
    provider = _WritingProvider()
    runtime = CoderNodeExecuteRuntime(
        provider=provider,
        git_context_resolver=lambda **kwargs: {
            "repo_id": "smoke-test",
            "operation": "develop_on_branch",
            "source_branch": default_branch,
            "target_branch": "feat/test",
            "worktree_mode": "node_branch_worktree",
        },
    )
    plan = ExecutionPlan(
        user_objective="在feat/test 里写个快排吧，用python写",
        nodes=[
            PlanNode(
                id="write_quicksort",
                runtime="coder",
                objective="Write quicksort",
            )
        ],
    )
    workspace = SessionWorkspaceManager(workdir=tmp_path, session_id_factory=lambda turn_id: "session-target").create_for_plan(
        plan,
        turn_id=105,
        conversation_id=5,
    )

    report = NodeExecutor(runtimes={"coder": runtime}).execute(
        plan,
        runtime_hints={"active_repo": "smoke-test"},
        session_workspace=workspace,
    )

    result = report.node_results[0]
    integration = workspace.root_path / "repos" / "smoke-test"
    assert report.status == "completed"
    assert result.git["repo_workspace"]["target_branch"] == "feat/test"
    assert result.git["repo_workspace"]["node_branch"] == "jarvis-nodes/smoke-test/session-target/write_quicksort"
    assert result.git["node_merge"]["target_branch"] == "feat/test"
    assert _git_stdout(integration, "branch", "--show-current") == "feat/test"
    assert _git_stdout(integration, "show", "--pretty=", "HEAD:node-output.txt").strip() == "created by node"


def test_write_coder_node_requests_approval_before_merging_to_protected_branch(monkeypatch, tmp_path: Path) -> None:
    project = _init_repo(tmp_path / "projects" / "smoke-test")
    default_branch = _git_stdout(project, "branch", "--show-current")
    registry = RepositoryRegistry(
        [
            RepositoryRef(
                repo_id="smoke-test",
                name="Smoke Test",
                root_path=project,
                canonical_root_path=project.resolve(),
            )
        ]
    )
    monkeypatch.setattr("app.task_runtime.node_execute_runtime.get_repository_registry", lambda: registry)
    provider = _WritingProvider()
    runtime = CoderNodeExecuteRuntime(
        provider=provider,
        git_context_resolver=lambda **kwargs: {
            "repo_id": "smoke-test",
            "operation": "develop_on_branch",
            "target_branch": default_branch,
        },
    )
    plan = ExecutionPlan(
        user_objective="write directly on protected branch",
        nodes=[
            PlanNode(
                id="write_protected",
                runtime="coder",
                objective="Write protected",
            )
        ],
    )
    workspace = SessionWorkspaceManager(workdir=tmp_path, session_id_factory=lambda turn_id: "session-protected").create_for_plan(
        plan,
        turn_id=106,
        conversation_id=5,
    )

    report = NodeExecutor(runtimes={"coder": runtime}).execute(
        plan,
        runtime_hints={"active_repo": "smoke-test"},
        session_workspace=workspace,
    )

    result = report.node_results[0]
    assert report.status == "blocked"
    assert result.error is not None
    assert result.error.code == "coder_approval_required"
    assert len(result.approval_requests) == 1
    approval = result.approval_requests[0]
    assert approval["action_kind"] == "merge_to_protected"
    assert approval["payload"]["source"] == "runtime_git"
    assert approval["payload"]["repo_workspace"]["target_branch"] == default_branch
    assert result.git["node_commit"]["short_hash"]
    assert "node_merge" not in result.git
    assert len(provider.requests) == 1


def test_later_coder_node_starts_from_latest_target_branch(monkeypatch, tmp_path: Path) -> None:
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
    provider = _ContinuingProvider()
    runtime = CoderNodeExecuteRuntime(provider=provider, git_context_resolver=_noop_git_context)
    plan = ExecutionPlan(
        user_objective="continue development",
        nodes=[
            PlanNode(id="write_first", runtime="coder", objective="Write first file"),
            PlanNode(id="read_second", runtime="coder", objective="Read first file"),
        ],
    )
    workspace = SessionWorkspaceManager(workdir=tmp_path, session_id_factory=lambda turn_id: "session-continue").create_for_plan(
        plan,
        turn_id=103,
        conversation_id=5,
    )

    report = NodeExecutor(runtimes={"coder": runtime}).execute(
        plan,
        runtime_hints={"active_repo": "jarvis", "target_branch": "feat-skill"},
        session_workspace=workspace,
    )

    assert report.status == "completed"
    assert provider.second_seen == "created by first node\n"
    integration = workspace.root_path / "repos" / "jarvis"
    assert _git_stdout(integration, "branch", "--show-current") == "feat-skill"
    assert _git_stdout(integration, "show", "--pretty=", "--name-only", "HEAD:second-output.txt").strip() == "created by second node"
    assert (workspace.node("write_first").repo_dir("jarvis") / "first-output.txt").exists()
    assert (workspace.node("read_second").repo_dir("jarvis") / "first-output.txt").read_text(encoding="utf-8") == "created by first node\n"


def test_prepare_node_repo_workspace_creates_node_branch_from_target_branch(tmp_path: Path) -> None:
    project = _init_repo(tmp_path / "projects" / "jarvis")
    source_branch = _git_stdout(project, "branch", "--show-current")
    _git(project, "checkout", "-b", "feat-skill")
    (project / "feature.txt").write_text("feature base\n", encoding="utf-8")
    _git(project, "add", "feature.txt")
    _git(project, "commit", "-m", "feature base")
    _git(project, "checkout", source_branch)
    plan = ExecutionPlan(
        user_objective="modify jarvis",
        nodes=[PlanNode(id="modify", runtime="coder", objective="Modify Jarvis")],
    )
    workspace = SessionWorkspaceManager(workdir=tmp_path, session_id_factory=lambda turn_id: "session-branch").create_for_plan(
        plan,
        turn_id=104,
        conversation_id=5,
    )

    node_workspace = prepare_node_repo_workspace(
        repo_id="jarvis",
        project_path=project,
        legacy_hints={**workspace.node("modify").to_legacy_hints(), **workspace.to_legacy_hints(), "target_branch": "feat-skill"},
        node_id="modify",
    )

    assert node_workspace is not None
    assert node_workspace.target_branch == "feat-skill"
    assert node_workspace.node_branch == "jarvis-nodes/jarvis/session-branch/modify"
    assert node_workspace.repo_path == workspace.node("modify").repo_dir("jarvis").resolve()
    assert _git_stdout(node_workspace.repo_path, "branch", "--show-current") == "jarvis-nodes/jarvis/session-branch/modify"
    assert (node_workspace.repo_path / "feature.txt").read_text(encoding="utf-8") == "feature base\n"
    assert _git_stdout(node_workspace.integration_path or project, "branch", "--show-current") == "feat-skill"


def test_coder_node_finalizer_loads_node_manifest_artifact(monkeypatch, tmp_path: Path) -> None:
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
    runtime = CoderNodeExecuteRuntime(provider=_ManifestWritingProvider(), git_context_resolver=_noop_git_context)
    plan = ExecutionPlan(
        user_objective="write a report",
        nodes=[PlanNode(id="write_report", runtime="coder", objective="Write report")],
    )
    workspace = SessionWorkspaceManager(workdir=tmp_path, session_id_factory=lambda turn_id: "session-manifest").create_for_plan(
        plan,
        turn_id=102,
        conversation_id=5,
    )

    report = NodeExecutor(runtimes={"coder": runtime}).execute(
        plan,
        runtime_hints={"active_repo": "jarvis"},
        session_workspace=workspace,
    )

    result = report.node_results[0]
    assert result.status == "completed"
    assert result.summary == "manifest summary"
    assert result.artifacts[0].ref == "report"
    assert result.artifacts[0].kind == "file"
    assert result.artifacts[0].session_relative_path == "nodes/write_report/report.md"
    assert result.artifacts[0].publish is True
    assert result.debug["finalizer"]["manifest_loaded"] is True


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
    runtime = CoderNodeExecuteRuntime(provider=_WritingProvider(), git_context_resolver=_noop_git_context)
    initial_head = _git_stdout(project, "rev-parse", "HEAD")

    result = runtime.run(
        _node_context(
            node=PlanNode(id="write_node", runtime="coder", objective="Write node file"),
            legacy_hints={"active_repo": "jarvis"},
        )
    )

    assert result.status == "completed"
    assert "node_commit" not in result.git
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
        legacy_hints=workspace.node("modify").to_legacy_hints(),
    )

    assert node_repo == workspace.node("modify").repo_dir("jarvis").resolve()
    assert (node_repo / "README.md").exists()
    assert _git_stdout(node_repo, "rev-parse", "--is-inside-work-tree") == "true"


class _RecordingProvider:
    name = "recording"

    def __init__(self) -> None:
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return CoderRunResult(ok=True, exit_code=0, stdout=f"ran in {request.workdir}", summary="done")

    def resume_approval(self, approval_id, *, approved, timeout_seconds, trusted_command_prefixes=None):
        raise NotImplementedError


class _WritingProvider:
    name = "writing"

    def __init__(self) -> None:
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        (request.workdir / "node-output.txt").write_text("created by node\n", encoding="utf-8")
        return CoderRunResult(ok=True, exit_code=0, stdout="wrote file", summary="done")

    def resume_approval(self, approval_id, *, approved, timeout_seconds, trusted_command_prefixes=None):
        raise NotImplementedError


class _ManifestWritingProvider:
    name = "manifest_writer"

    def run(self, request):
        manifest_path = Path(request.metadata["node_manifest_path"])
        node_dir = manifest_path.parent
        report_path = node_dir / "report.md"
        report_path.write_text("# Report\n", encoding="utf-8")
        manifest_path.write_text(
            (
                "{\n"
                '  "summary": "manifest summary",\n'
                '  "artifacts": [\n'
                "    {\n"
                '      "ref": "report",\n'
                '      "kind": "file",\n'
                '      "path": "nodes/write_report/report.md",\n'
                '      "filename": "report.md",\n'
                '      "publish": true\n'
                "    }\n"
                "  ]\n"
                "}\n"
            ),
            encoding="utf-8",
        )
        return CoderRunResult(ok=True, exit_code=0, stdout="wrote manifest", summary="done")

    def resume_approval(self, approval_id, *, approved, timeout_seconds, trusted_command_prefixes=None):
        raise NotImplementedError


class _ContinuingProvider:
    name = "continuing"

    def __init__(self) -> None:
        self.second_seen = ""

    def run(self, request):
        if request.metadata["node_id"] == "write_first":
            (request.workdir / "first-output.txt").write_text("created by first node\n", encoding="utf-8")
            return CoderRunResult(ok=True, exit_code=0, stdout="wrote first", summary="done")
        self.second_seen = (request.workdir / "first-output.txt").read_text(encoding="utf-8")
        (request.workdir / "second-output.txt").write_text("created by second node\n", encoding="utf-8")
        return CoderRunResult(ok=True, exit_code=0, stdout="wrote second", summary="done")

    def resume_approval(self, approval_id, *, approved, timeout_seconds, trusted_command_prefixes=None):
        raise NotImplementedError


def _node_context(*, node: PlanNode, legacy_hints: dict[str, object]):
    from app.task_runtime.node_execute_runtime import NodeExecutionContext

    return NodeExecutionContext(
        user_objective="write a file",
        node=node,
        legacy_hints=legacy_hints,
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
