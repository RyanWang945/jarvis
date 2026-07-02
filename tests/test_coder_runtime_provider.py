from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.task_runtime.approval_types import ApprovalRequest
from app.task_runtime.coder_provider import (
    CoderApprovalContinuationResult,
    CoderRunRequest,
    CoderRunResult,
    CodexCoderProvider,
    ClaudeCodeCoderProvider,
    build_coder_provider,
    resume_coder_approval,
)
from app.task_runtime.node_execute_runtime import CoderNodeExecuteRuntime, NodeExecutionContext
from app.task_runtime.planner import PlanNode
from app.tools.common import ToolExecutionResult


class RecordingProvider:
    name = "fake"

    def __init__(self) -> None:
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return CoderRunResult(
            ok=True,
            stdout="coder done",
            artifacts=["git_worktree:clean"],
            metadata={
                "tool_artifacts": [
                    {
                        "artifact_id": "artifact_1",
                        "kind": "file",
                        "path": "report.txt",
                        "source_tool": "delegate_to_codex",
                    }
                ]
            },
        )


def _noop_git_context(**kwargs):
    del kwargs
    return {}


def test_coder_provider_factory_defaults_to_codex(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_CODER_RUNTIME_PROVIDER", raising=False)
    get_settings.cache_clear()

    try:
        provider = build_coder_provider(get_settings())
    finally:
        get_settings.cache_clear()

    assert isinstance(provider, CodexCoderProvider)


def test_coder_provider_factory_selects_claude_code(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CODER_RUNTIME_PROVIDER", "claude_code")
    get_settings.cache_clear()

    try:
        provider = build_coder_provider(get_settings())
    finally:
        get_settings.cache_clear()

    assert isinstance(provider, ClaudeCodeCoderProvider)


def test_coder_provider_factory_rejects_unknown_provider(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CODER_RUNTIME_PROVIDER", "unknown")
    get_settings.cache_clear()

    try:
        with pytest.raises(ValueError, match="Unsupported coder runtime provider"):
            build_coder_provider(get_settings())
    finally:
        get_settings.cache_clear()


def test_coder_node_runtime_builds_provider_request() -> None:
    provider = RecordingProvider()
    runtime = CoderNodeExecuteRuntime(provider=provider, git_context_resolver=_noop_git_context)

    result = runtime.run(
        NodeExecutionContext(
            user_objective="fix tests",
            node=PlanNode(
                id="fix",
                runtime="coder",
                objective="Fix failing tests",
            ),
            legacy_hints={"active_repo": "jarvis"},
        )
    )

    request = provider.requests[0]
    assert request.repo_id == "jarvis"
    assert request.workdir.name == "jarvis"
    assert "Fix failing tests" in request.instruction
    assert "用户目标" not in request.instruction
    assert result.runtime == "coder"
    assert result.status == "completed"
    assert result.debug["provider"] == "fake"
    assert result.tool_artifacts[0]["artifact_id"] == "artifact_1"
    assert "tool_artifacts" not in result.data


def test_coder_node_runtime_does_not_put_global_user_objective_in_worker_prompt() -> None:
    provider = RecordingProvider()
    runtime = CoderNodeExecuteRuntime(provider=provider, git_context_resolver=_noop_git_context)

    runtime.run(
        NodeExecutionContext(
            user_objective="review architecture, then use image gen skill to create a diagram",
            node=PlanNode(
                id="review_architecture",
                runtime="coder",
                objective="Review architecture and produce a markdown report.",
            ),
            legacy_hints={"active_repo": "jarvis"},
        )
    )

    request = provider.requests[0]
    assert "Review architecture and produce a markdown report." in request.instruction
    assert "image gen skill" not in request.instruction
    assert "review architecture, then" not in request.instruction


def test_coder_node_runtime_keeps_git_context_usage_out_of_runtime_context() -> None:
    usage = {"source": "llm", "stage": "coder_git_context", "total_tokens": 12}
    provider = RecordingProvider()
    runtime = CoderNodeExecuteRuntime(
        provider=provider,
        git_context_resolver=lambda **kwargs: {"repo_id": "jarvis", "usage_record": usage},
    )

    runtime.run(
        NodeExecutionContext(
            user_objective="review code",
            node=PlanNode(id="review", runtime="coder", objective="Review code"),
            legacy_hints={"active_repo": "jarvis"},
        )
    )

    request = provider.requests[0]
    assert request.metadata["usage_records"] == [usage]
    assert "git_context_usage" not in request.metadata


def test_coder_node_runtime_ignores_legacy_permission_hints() -> None:
    provider = RecordingProvider()
    runtime = CoderNodeExecuteRuntime(provider=provider, git_context_resolver=_noop_git_context)

    runtime.run(
        NodeExecutionContext(
            user_objective="review code",
            node=PlanNode(id="review", runtime="coder", objective="Review code"),
            legacy_hints={"active_repo": "jarvis", "allow_commit": True, "allow_push": True},
        )
    )

    request = provider.requests[0]
    assert request.repo_id == "jarvis"
    assert "Review code" in request.instruction


def test_codex_provider_surfaces_approval_request_artifact(tmp_path) -> None:
    approval_path = tmp_path / "codex-approval-requests.json"
    approval_path.write_text(
        json.dumps(
            [
                {
                    "type": "item/commandExecution/requestApproval",
                    "id": "approval_1",
                    "command": "git push origin main",
                    "reason": "Publish the completed change.",
                }
            ]
        ),
        encoding="utf-8",
    )

    def _runner(request):
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stdout="Codex requested approval.",
            summary="Codex requested approval.",
            artifacts=[f"codex_approval_requests:{approval_path}"],
        )

    provider = CodexCoderProvider(runner=_runner)
    result = provider.run(
        CoderRunRequest(
            repo_id="jarvis",
            workdir=tmp_path,
            instruction="Push the change.",
        )
    )

    assert result.approval_requests[0].approval_id == "approval_1"
    assert result.approval_requests[0].action_kind == "push"
    assert result.approval_requests[0].command == "git push origin main"
    assert result.approval_requests[0].reason == "Publish the completed change."


def test_claude_code_provider_delegates_to_claude_tool(monkeypatch, tmp_path) -> None:
    captured = {}

    def _runner(request):
        captured["request"] = request
        return ToolExecutionResult(
            ok=True,
            exit_code=0,
            stdout="Claude Code finished.",
            summary="claude done",
            artifacts=["git_worktree:clean"],
        )

    monkeypatch.setattr("app.task_runtime.coder_provider.run_coder_tool", _runner)

    provider = ClaudeCodeCoderProvider()
    result = provider.run(
        CoderRunRequest(
            repo_id="jarvis",
            workdir=tmp_path,
            instruction="Review runtime.",
            timeout_seconds=123,
        )
    )

    request = captured["request"]
    assert request.tool_name == "claude_code_coder_provider"
    assert request.workdir == str(tmp_path)
    assert request.timeout_seconds == 123
    assert request.args["repo_id"] == "jarvis"
    assert request.args["instruction"] == "Review runtime."
    assert "_read_only" not in request.args
    assert "allow_commit" not in request.args
    assert "allow_push" not in request.args
    assert result.ok is True
    assert result.stdout == "Claude Code finished."
    assert result.summary == "claude done"
    assert result.artifacts == ["git_worktree:clean"]
    assert result.metadata["provider"] == "claude_code"


def test_claude_code_provider_omits_legacy_permission_args(monkeypatch, tmp_path) -> None:
    captured = {}

    def _runner(request):
        captured["request"] = request
        return ToolExecutionResult(ok=True, exit_code=0, summary="ok")

    monkeypatch.setattr("app.task_runtime.coder_provider.run_coder_tool", _runner)

    ClaudeCodeCoderProvider().run(
        CoderRunRequest(
            repo_id="jarvis",
            workdir=tmp_path,
            instruction="Commit the change.",
        )
    )

    args = captured["request"].args
    assert "_read_only" not in args
    assert "allow_commit" not in args
    assert "allow_push" not in args


def test_claude_code_provider_passes_runtime_branch_context(monkeypatch, tmp_path) -> None:
    captured = {}

    def _runner(request):
        captured["request"] = request
        return ToolExecutionResult(ok=True, exit_code=0, summary="ok")

    monkeypatch.setattr("app.task_runtime.coder_provider.run_coder_tool", _runner)

    ClaudeCodeCoderProvider().run(
        CoderRunRequest(
            repo_id="smoke-test",
            workdir=tmp_path,
            instruction="Write quicksort.",
            metadata={
                "source_branch": "main",
                "target_branch": "feat/test",
                "node_branch": "jarvis-nodes/smoke-test/session/write_quicksort",
            },
        )
    )

    args = captured["request"].args
    assert args["source_branch"] == "main"
    assert args["target_branch"] == "feat/test"
    assert args["node_branch"] == "jarvis-nodes/smoke-test/session/write_quicksort"


def test_coder_node_runtime_blocks_when_approval_required() -> None:
    class ApprovalProvider:
        name = "fake"

        def run(self, request):
            del request
            return CoderRunResult(
                ok=False,
                summary="Approval is required.",
                approval_requests=[
                    ApprovalRequest(
                        approval_id="approval_42",
                        action_kind="commit",
                        command="git commit -m change",
                        reason="Create the requested commit.",
                        payload={"id": "approval_42"},
                    )
                ],
            )

    result = CoderNodeExecuteRuntime(provider=ApprovalProvider(), git_context_resolver=_noop_git_context).run(
        NodeExecutionContext(
            user_objective="commit changes",
            node=PlanNode(id="commit", runtime="coder", objective="Commit changes"),
            legacy_hints={"active_repo": "jarvis"},
        )
    )

    assert result.status == "blocked"
    assert result.error is not None
    assert result.error.code == "coder_approval_required"
    assert result.approval_requests[0]["approval_id"] == "approval_42"
    assert result.approval_requests[0]["action_kind"] == "commit"
    assert result.approval_requests[0]["command"] == "git commit -m change"
    assert "approval_id" not in result.data
    assert "action_kind" not in result.data
    assert "command" not in result.data
    assert result.approval_requests[0]["payload"] == {"id": "approval_42"}
    assert "approval_requests" not in result.data


def test_resume_coder_approval_delegates_to_codex(monkeypatch) -> None:
    calls = []

    class FakeCodexProvider:
        def resume_approval(self, approval_id, *, approved, timeout_seconds, trusted_command_prefixes=None):
            calls.append((approval_id, approved, timeout_seconds, trusted_command_prefixes))
            return CoderApprovalContinuationResult(status="completed", final_text="done")

    monkeypatch.setattr("app.task_runtime.coder_provider.CodexCoderProvider", lambda: FakeCodexProvider())

    result = resume_coder_approval(
        "approval_1",
        approved=True,
        timeout_seconds=30,
        provider="codex",
        trusted_command_prefixes=["git add"],
    )

    assert result.status == "completed"
    assert result.final_text == "done"
    assert calls == [("approval_1", True, 30, ["git add"])]


def test_resume_coder_approval_rejects_unknown_provider() -> None:
    result = resume_coder_approval("approval_1", approved=False, timeout_seconds=30, provider="unknown")

    assert result.status == "unsupported"
    assert "Unsupported coder runtime provider" in result.error


def test_resume_coder_approval_reports_claude_code_unsupported() -> None:
    result = resume_coder_approval("approval_1", approved=True, timeout_seconds=30, provider="claude_code")

    assert result.status == "unsupported"
    assert "Claude Code coder provider approval resume is not implemented" in result.error
    assert result.metadata["provider"] == "claude_code"
