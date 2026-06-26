import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from app.llm.provider_adapters import TokenUsage
from app.repositories import RepositoryRef, RepositoryRegistry
from app.tools.codex import run_codex_coder_tool
from app.tools.codex_app_server import (
    CodexAppServerSession,
    CodexAppServerRunResult,
    _approval_decision,
    _is_auto_approved_workspace_approval,
    _is_routine_repo_git_approval,
    _matches_trusted_command_prefix,
    _thread_token_usage_from_event,
    approval_command_prefix,
)
from app.tools.coder_common import build_coder_instruction, check_coder_permissions
from app.tools.common import ToolExecutionRequest
from app.tools.runtime import build_llm_tools, get_tool_definition


_TINY_PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lwot8gAAAABJRU5ErkJggg=="


def test_code_worker_tools_are_not_registered_llm_tools() -> None:
    injected_names = {tool["function"]["name"] for tool in build_llm_tools()}

    assert "delegate_to_codex" not in injected_names
    assert "delegate_to_claude_code" not in injected_names
    with pytest.raises(ValueError, match="unknown tool: delegate_to_codex"):
        get_tool_definition("delegate_to_codex")
    with pytest.raises(ValueError, match="unknown tool: delegate_to_claude_code"):
        get_tool_definition("delegate_to_claude_code")


def test_codex_coder_runs_with_clean_stdout_and_jsonl_artifact(monkeypatch, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    run_root = tmp_path / "runs"
    captured: dict[str, object] = {}

    _install_registry(monkeypatch, repo)
    monkeypatch.setattr("app.tools.codex._resolve_codex_command", lambda: ["codex"])
    monkeypatch.setattr("app.tools.codex._coder_run_dir", lambda run_id: run_root / run_id)

    def _fake_run(*, provider_command, workdir, run_dir, instruction, timeout_seconds, trusted_command_prefixes=None):
        captured["provider_command"] = provider_command
        captured["workdir"] = workdir
        captured["run_dir"] = run_dir
        captured["instruction"] = instruction
        captured["timeout_seconds"] = timeout_seconds
        events = [
            {"method": "thread/started", "params": {"thread": {"id": "thread_1"}}},
            {"type": "agent_message", "message": "Changed README and ran tests."},
        ]
        stdout = "\n".join(json.dumps(event) for event in events)
        return CodexAppServerRunResult(
            status="completed",
            raw_events=stdout,
            exit_code=0,
            final_text="Changed README and ran tests.",
            usage=TokenUsage(prompt_tokens=1200, completion_tokens=300, total_tokens=1500),
        )

    monkeypatch.setattr("app.tools.codex._run_codex_app_server", _fake_run)

    result = run_codex_coder_tool(
        ToolExecutionRequest(
            tool_name="codex_coder_provider",
            workdir=str(repo),
            args={
                "instruction": "Update README.md and run tests.",
                "repo_id": "jarvis",
                "allow_commit": False,
                "allow_push": False,
            },
            timeout_seconds=30,
        )
    )

    assert result.ok is True
    assert captured["provider_command"] == ["codex"]
    assert captured["workdir"] == repo.resolve()
    assert "Jarvis coder worker 指令" in str(captured["instruction"])
    assert "Changed README and ran tests." in result.stdout
    assert "{\"type\"" not in result.stdout
    assert "[JARVIS_" not in result.stdout
    assert result.metadata["usage_records"][0]["provider"] == "codex"
    assert result.metadata["usage_records"][0]["prompt_tokens"] == 1200
    assert result.metadata["usage_records"][0]["completion_tokens"] == 300
    assert any(str(artifact).startswith("codex_events:") for artifact in result.artifacts)
    assert any(str(artifact).startswith("jarvis_audit:") for artifact in result.artifacts)


def test_codex_thread_token_usage_updated_event_is_parsed() -> None:
    usage = _thread_token_usage_from_event(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread_1",
                "tokenUsage": {
                    "inputTokens": 42,
                    "outputTokens": 8,
                    "totalTokens": 50,
                },
            },
        }
    )

    assert usage == TokenUsage(prompt_tokens=42, completion_tokens=8, total_tokens=50)


def test_codex_thread_token_usage_updated_event_parses_nested_total_usage() -> None:
    usage = _thread_token_usage_from_event(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread_1",
                "turnId": "turn_1",
                "tokenUsage": {
                    "total": {
                        "inputTokens": 203939,
                        "outputTokens": 3970,
                        "totalTokens": 207909,
                        "cachedInputTokens": 171776,
                        "reasoningOutputTokens": 1593,
                    },
                    "last": {
                        "inputTokens": 22868,
                        "outputTokens": 164,
                        "totalTokens": 23032,
                    },
                    "modelContextWindow": 258400,
                },
            },
        }
    )

    assert usage == TokenUsage(prompt_tokens=203939, completion_tokens=3970, total_tokens=207909)


def test_codex_coder_uses_runtime_workdir_and_run_dir(monkeypatch, tmp_path: Path) -> None:
    project = _init_repo(tmp_path / "project")
    node_repo = _init_repo(tmp_path / "sessions" / "s1" / "nodes" / "n1" / "repo" / "jarvis")
    provider_run = tmp_path / "sessions" / "s1" / "nodes" / "n1" / "provider_run"
    captured: dict[str, object] = {}

    _install_registry(monkeypatch, project)
    monkeypatch.setattr("app.tools.codex._resolve_codex_command", lambda: ["codex"])

    def _fake_run(*, provider_command, workdir, run_dir, instruction, timeout_seconds, trusted_command_prefixes=None):
        del provider_command, instruction, timeout_seconds, trusted_command_prefixes
        captured["workdir"] = workdir
        captured["run_dir"] = run_dir
        return CodexAppServerRunResult(
            status="completed",
            raw_events=json.dumps({"type": "agent_message", "message": "Done."}),
            exit_code=0,
            final_text="Done.",
        )

    monkeypatch.setattr("app.tools.codex._run_codex_app_server", _fake_run)

    result = run_codex_coder_tool(
        ToolExecutionRequest(
            tool_name="codex_coder_provider",
            workdir=None,
            args={
                "instruction": "Update README.md.",
                "repo_id": "jarvis",
                "_runtime_workdir": str(node_repo),
                "_runtime_run_dir": str(provider_run),
            },
            timeout_seconds=30,
        )
    )

    assert result.ok is True
    assert captured["workdir"] == node_repo.resolve()
    assert captured["run_dir"] == provider_run.resolve()
    assert any(str(artifact) == f"codex_run:{provider_run.resolve()}" for artifact in result.artifacts)


def test_codex_instruction_contract_overrides_generated_preconfirmation() -> None:
    instruction = build_coder_instruction(
        "将当前 nltk 项目中所有未提交的更改进行 git commit，然后 push。请在执行前让我确认 commit message。",
        {"allow_commit": True, "allow_push": True},
    )

    assert "approval 权限属于 Codex approval 流程" in instruction
    assert "不要用普通聊天确认替代它" in instruction
    assert "不要停下来询问 Jarvis 或用户来确认常规执行细节" in instruction
    assert "你自行选择一个简洁 commit message" in instruction
    assert "请在执行前让我确认 commit message" in instruction


def test_codex_instruction_contract_respects_read_only_delegation() -> None:
    instruction = build_coder_instruction(
        "Review the runtime policy design.",
        {"_read_only": True, "allow_commit": True, "allow_push": True},
    )

    assert "这是只读任务：只能检查、分析、评审和报告" in instruction
    assert "不要编辑、创建、删除、重命名、暂存、提交或 push 文件" in instruction
    assert "优先直接修改文件" not in instruction
    assert "以简洁的内联报告结束" in instruction


def test_codex_logs_startup_context_and_log_paths(monkeypatch, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    run_root = tmp_path / "runs"
    log_messages: list[str] = []

    _install_registry(monkeypatch, repo)
    monkeypatch.setattr("app.tools.codex._resolve_codex_command", lambda: ["codex"])
    monkeypatch.setattr("app.tools.codex._coder_run_dir", lambda run_id: run_root / run_id)

    import app.tools.codex as codex_module

    original_info = codex_module.logger.info

    def _capture_info(message, *args, **kwargs):
        log_messages.append(message % args if args else str(message))
        return original_info(message, *args, **kwargs)

    monkeypatch.setattr(codex_module.logger, "info", _capture_info)

    def _fake_run(*, provider_command, workdir, run_dir, instruction, timeout_seconds, trusted_command_prefixes=None):
        del provider_command, workdir, run_dir, instruction, timeout_seconds, trusted_command_prefixes
        return CodexAppServerRunResult(
            status="completed",
            raw_events='{"type":"final_answer","text":"done"}\n',
            exit_code=0,
            final_text="done",
        )

    monkeypatch.setattr("app.tools.codex._run_codex_app_server", _fake_run)

    result = run_codex_coder_tool(
        ToolExecutionRequest(
            tool_name="codex_coder_provider",
            workdir=None,
            args={
                "instruction": "Inspect repo.",
                "repo_id": "jarvis",
                "allow_commit": True,
                "allow_push": False,
            },
            timeout_seconds=30,
        )
    )

    assert result.exit_code == 0
    startup = next(message for message in log_messages if "codex coder provider starting" in message)
    assert "repo_id=jarvis" in startup
    assert "permissions=" in startup
    assert '"allow_commit":true' in startup
    assert '"allow_push":false' in startup
    assert "timeout_seconds=" in startup
    paths = next(message for message in log_messages if "codex coder provider log paths" in message)
    assert "codex-events.jsonl" in paths
    assert "jarvis-audit.log" in paths
    assert "codex-stderr.log" in paths
    assert "codex-approval-requests.json" in paths


def test_codex_surfaces_approval_requests(monkeypatch, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    run_root = tmp_path / "runs"

    _install_registry(monkeypatch, repo)
    monkeypatch.setattr("app.tools.codex._resolve_codex_command", lambda: ["codex"])
    monkeypatch.setattr("app.tools.codex._coder_run_dir", lambda run_id: run_root / run_id)

    def _fake_run(*, provider_command, workdir, run_dir, instruction, timeout_seconds, trusted_command_prefixes=None):
        stdout = json.dumps(
            {
                "method": "item/commandExecution/requestApproval",
                "id": 0,
                "params": {
                    "threadId": "thread_1",
                    "turnId": "turn_1",
                    "itemId": "item_1",
                    "command": "uv add httpx",
                    "reason": "Install a dependency required by the requested implementation.",
                },
            }
        )
        return CodexAppServerRunResult(
            status="approval_requested",
            raw_events=stdout,
            exit_code=None,
            approval_requests=[
                {
                    "type": "item/commandExecution/requestApproval",
                    "id": "approval_1",
                    "command": "uv add httpx",
                    "reason": "Install a dependency required by the requested implementation.",
                }
            ],
        )

    monkeypatch.setattr("app.tools.codex._run_codex_app_server", _fake_run)

    result = run_codex_coder_tool(
        ToolExecutionRequest(
            tool_name="codex_coder_provider",
            workdir=str(repo),
            args={"instruction": "Add HTTP support.", "repo_id": "jarvis"},
            timeout_seconds=30,
        )
    )

    assert result.ok is False
    assert "Codex requested approval" in result.summary
    assert "uv add httpx" in result.summary
    assert "Install a dependency" in result.stdout
    audit_path = _artifact_path(result.artifacts, "jarvis_audit")
    assert "[JARVIS_CODEX_APPROVAL_REQUESTS]" in audit_path.read_text(encoding="utf-8")
    approval_path = _artifact_path(result.artifacts, "codex_approval_requests")
    approval_payload = json.loads(approval_path.read_text(encoding="utf-8"))
    assert approval_payload[0]["command"] == "uv add httpx"


def test_codex_failed_app_server_prefers_final_text_over_stderr(monkeypatch, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    run_root = tmp_path / "runs"

    _install_registry(monkeypatch, repo)
    monkeypatch.setattr("app.tools.codex._resolve_codex_command", lambda: ["codex"])
    monkeypatch.setattr("app.tools.codex._coder_run_dir", lambda run_id: run_root / run_id)

    def _fake_run(*, provider_command, workdir, run_dir, instruction, timeout_seconds, trusted_command_prefixes=None):
        return CodexAppServerRunResult(
            status="failed",
            raw_events="",
            raw_stderr="older low-level stderr",
            exit_code=None,
            final_text="Codex final diagnosis.",
            error="Codex app-server stdout closed.",
        )

    monkeypatch.setattr("app.tools.codex._run_codex_app_server", _fake_run)

    result = run_codex_coder_tool(
        ToolExecutionRequest(
            tool_name="codex_coder_provider",
            workdir=str(repo),
            args={"instruction": "Do the repo task.", "repo_id": "jarvis"},
            timeout_seconds=30,
        )
    )

    assert result.ok is False
    assert result.stdout == "Codex final diagnosis."
    assert result.summary == "Codex final diagnosis."
    assert result.stderr == "older low-level stderr"


def test_codex_failed_app_server_uses_last_event_text_when_no_final(monkeypatch) -> None:
    tmp_root = _sandbox_tmp("codex-failed-event-text")
    repo = _init_repo(tmp_root / "repo")
    run_root = tmp_root / "runs"

    _install_registry(monkeypatch, repo)
    monkeypatch.setattr("app.tools.codex._resolve_codex_command", lambda: ["codex"])
    monkeypatch.setattr("app.tools.codex._coder_run_dir", lambda run_id: run_root / run_id)

    events = "\n".join([
        json.dumps(
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "text": "Created docs/jarvis_architecture.png.",
                        "phase": "commentary",
                    }
                },
            }
        ),
        json.dumps(
            {
                "method": "turn/completed",
                "params": {
                    "turn": {
                        "status": "failed",
                        "error": {"message": "stream disconnected before completion"},
                    }
                },
            }
        ),
    ])

    def _fake_run(*, provider_command, workdir, run_dir, instruction, timeout_seconds, trusted_command_prefixes=None):
        return CodexAppServerRunResult(
            status="failed",
            raw_events=events,
            raw_stderr="transport error",
            exit_code=None,
            error="stream disconnected before completion",
        )

    monkeypatch.setattr("app.tools.codex._run_codex_app_server", _fake_run)

    try:
        result = run_codex_coder_tool(
            ToolExecutionRequest(
                tool_name="codex_coder_provider",
                workdir=str(repo),
                args={"instruction": "Generate diagram.", "repo_id": "jarvis"},
                timeout_seconds=30,
            )
        )

        assert result.ok is False
        assert result.stdout == "Created docs/jarvis_architecture.png."
        assert result.summary == "Created docs/jarvis_architecture.png."
        assert result.stderr == "transport error"
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def test_codex_app_server_treats_failed_turn_completed_as_failure() -> None:
    tmp_root = _sandbox_tmp("codex-failed-turn")
    session = CodexAppServerSession(provider_command=["codex"], workdir=tmp_root, run_dir=tmp_root)
    session._send = lambda message: None  # type: ignore[method-assign]
    session._stdout_queue.put(
        json.dumps(
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "text": "The PNG exists at docs/jarvis_architecture.png.",
                        "phase": "commentary",
                    }
                },
            }
        )
        + "\n"
    )
    session._stdout_queue.put(
        json.dumps(
            {
                "method": "turn/completed",
                "params": {
                    "turn": {
                        "status": "failed",
                        "error": {"message": "stream disconnected before completion"},
                    }
                },
            }
        )
        + "\n"
    )

    try:
        result = session._drain_until_waiting_or_done(timeout_seconds=1)

        assert result.status == "failed"
        assert result.final_text == "The PNG exists at docs/jarvis_architecture.png."
        assert result.error == "stream disconnected before completion"
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def test_codex_app_server_streams_events_and_stderr_to_run_files() -> None:
    tmp_root = _sandbox_tmp("codex-stream-files")
    session = CodexAppServerSession(provider_command=["codex"], workdir=tmp_root, run_dir=tmp_root)
    event = {
        "method": "item/completed",
        "params": {
            "item": {
                "type": "agentMessage",
                "text": "Streaming status is visible.",
                "phase": "commentary",
            }
        },
    }
    done = {
        "method": "turn/completed",
        "params": {
            "turn": {
                "status": "completed",
            }
        },
    }
    session._stdout_queue.put(json.dumps(event) + "\n")
    session._stdout_queue.put(json.dumps(done) + "\n")

    try:
        session._read_stderr(["first stderr line\n", "second stderr line\n"])
        result = session._drain_until_waiting_or_done(timeout_seconds=1)

        assert result.status == "completed"
        events_path = tmp_root / "codex-events.jsonl"
        stderr_path = tmp_root / "codex-stderr.log"
        assert events_path.read_text(encoding="utf-8").splitlines() == [
            json.dumps(event),
            json.dumps(done),
        ]
        assert stderr_path.read_text(encoding="utf-8").splitlines() == [
            "first stderr line",
            "second stderr line",
        ]
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def test_codex_app_server_extracts_image_generation_artifacts() -> None:
    tmp_root = _sandbox_tmp("codex-image-artifact")
    session = CodexAppServerSession(provider_command=["codex"], workdir=tmp_root, run_dir=tmp_root)
    image_event = {
        "method": "item/completed",
        "params": {
            "item": {
                "type": "imageGeneration",
                "id": "ig_test_image",
                "status": "generating",
                "revisedPrompt": "A tiny test image.",
                "result": _TINY_PNG_BASE64,
            }
        },
    }
    done = {
        "method": "turn/completed",
        "params": {
            "turn": {
                "status": "completed",
            }
        },
    }
    session._stdout_queue.put(json.dumps(image_event) + "\n")
    session._stdout_queue.put(json.dumps(done) + "\n")

    try:
        result = session._drain_until_waiting_or_done(timeout_seconds=1)

        assert result.status == "completed"
        assert len(result.tool_artifacts) == 1
        artifact = result.tool_artifacts[0]
        assert artifact.kind == "image"
        assert artifact.mime_type == "image/png"
        assert artifact.filename == "ig_test_image.png"
        assert artifact.metadata["codex_item_id"] == "ig_test_image"
        assert artifact.metadata["revised_prompt"] == "A tiny test image."
        assert Path(artifact.path or "").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def test_codex_approval_uses_one_time_accept_not_persistent_execpolicy() -> None:
    amendment = {
        "acceptWithExecpolicyAmendment": {
            "execpolicy_amendment": ["powershell.exe", "-Command", "git add README.md"]
        }
    }

    decision = _approval_decision(
        {"available_decisions": ["accept", amendment, "cancel"]},
        approved=True,
    )

    assert decision == "accept"
    assert _approval_decision({"available_decisions": ["accept", "cancel"]}, approved=False) == "cancel"


def test_codex_auto_approves_only_routine_local_git_commands() -> None:
    assert _is_routine_repo_git_approval(
        {
            "command": (
                '"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
                "-Command 'git add api pyproject.toml'"
            )
        }
    )
    assert _is_routine_repo_git_approval({"command": "git commit -m 'Add FastAPI greeting service'"})
    assert _is_routine_repo_git_approval({"command": "git restore --staged ."})

    assert not _is_routine_repo_git_approval({"command": "git push origin main"})
    assert not _is_routine_repo_git_approval({"command": "git commit --amend -m fix"})
    assert not _is_routine_repo_git_approval({"command": "git restore ."})
    assert not _is_routine_repo_git_approval({"command": "Remove-Item -Recurse data"})
    assert not _is_routine_repo_git_approval({"command": "git add .; Remove-Item -Recurse data"})


def test_codex_auto_approves_workspace_file_changes_and_manifest_writes(tmp_path: Path) -> None:
    node_workspace = tmp_path / "sessions" / "s1" / "nodes" / "n1"
    repo = node_workspace / "repo" / "smoke-test"
    repo.mkdir(parents=True)
    allowed_roots = (repo.resolve(), node_workspace.resolve())

    assert _is_auto_approved_workspace_approval(
        {
            "type": "item/fileChange/requestApproval",
            "cwd": str(repo),
        },
        allowed_roots=allowed_roots,
    )
    assert _is_auto_approved_workspace_approval(
        {
            "type": "item/commandExecution/requestApproval",
            "cwd": str(repo),
            "command": "python -m pytest -q -p no:cacheprovider",
        },
        allowed_roots=allowed_roots,
    )
    assert _is_auto_approved_workspace_approval(
        {
            "type": "item/commandExecution/requestApproval",
            "cwd": str(repo),
            "command": f"powershell.exe -Command \"Set-Content -LiteralPath '{node_workspace / 'node_manifest.json'}' -Value '{{}}'\"",
        },
        allowed_roots=allowed_roots,
    )


def test_codex_auto_approval_rejects_protected_or_outside_actions(tmp_path: Path) -> None:
    node_workspace = tmp_path / "sessions" / "s1" / "nodes" / "n1"
    repo = node_workspace / "repo" / "smoke-test"
    outside = tmp_path / "outside"
    repo.mkdir(parents=True)
    outside.mkdir()
    allowed_roots = (repo.resolve(), node_workspace.resolve())

    assert not _is_auto_approved_workspace_approval(
        {
            "type": "item/commandExecution/requestApproval",
            "cwd": str(repo),
            "command": "git push origin main",
        },
        allowed_roots=allowed_roots,
    )
    assert not _is_auto_approved_workspace_approval(
        {
            "type": "item/fileChange/requestApproval",
            "cwd": str(outside),
        },
        allowed_roots=allowed_roots,
    )


def test_codex_auto_approves_workspace_cache_cleanup(tmp_path: Path) -> None:
    node_workspace = tmp_path / "sessions" / "s1" / "nodes" / "n1"
    repo = node_workspace / "repo" / "smoke-test"
    repo.mkdir(parents=True)
    allowed_roots = (repo.resolve(), node_workspace.resolve())

    assert _is_auto_approved_workspace_approval(
        {
            "type": "item/commandExecution/requestApproval",
            "cwd": str(repo),
            "command": (
                "pwsh.exe -Command "
                "'$root = (Resolve-Path .).Path; "
                "$targets = @((Resolve-Path .\\__pycache__ -ErrorAction SilentlyContinue)); "
                "foreach ($target in $targets) { "
                "if ($target -and $target.Path.StartsWith($root)) { "
                "Remove-Item -LiteralPath $target.Path -Recurse -Force } }'"
            ),
            "raw_params": {
                "commandActions": [
                    {
                        "type": "unknown",
                        "command": (
                            "$root = (Resolve-Path .).Path; "
                            "$targets = @((Resolve-Path .\\__pycache__ -ErrorAction SilentlyContinue)); "
                            "foreach ($target in $targets) { "
                            "if ($target -and $target.Path.StartsWith($root)) { "
                            "Remove-Item -LiteralPath $target.Path -Recurse -Force } }"
                        ),
                    }
                ]
            },
        },
        allowed_roots=allowed_roots,
    )

    assert not _is_auto_approved_workspace_approval(
        {
            "type": "item/commandExecution/requestApproval",
            "cwd": str(repo),
            "command": "Remove-Item -LiteralPath data -Recurse -Force",
        },
        allowed_roots=allowed_roots,
    )


def test_codex_approval_prefix_matching_normalizes_shell_wrappers() -> None:
    wrapped = (
        '"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
        "-Command 'git add api pyproject.toml'"
    )

    assert approval_command_prefix(wrapped) == "git add"
    assert _matches_trusted_command_prefix({"command": wrapped}, ("git add",))
    assert not _matches_trusted_command_prefix({"command": "uv add httpx"}, ("git add",))
    assert approval_command_prefix("git push -u origin main") == ""
    assert not _matches_trusted_command_prefix({"command": "git push -u origin main"}, ("git push -u origin main",))


def test_codex_ignores_jarvis_run_artifacts_inside_repo(monkeypatch, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    run_root = repo / "data" / "coder_runs"

    _install_registry(monkeypatch, repo)
    monkeypatch.setattr("app.tools.codex._resolve_codex_command", lambda: ["codex"])
    monkeypatch.setattr("app.tools.codex._coder_run_dir", lambda run_id: run_root / run_id)
    monkeypatch.setattr(
        "app.tools.codex._run_codex_app_server",
        lambda *, provider_command, workdir, run_dir, instruction, timeout_seconds, trusted_command_prefixes=None: CodexAppServerRunResult(
            status="completed",
            raw_events=json.dumps({"type": "agent_message", "message": "Reviewed only."}),
            exit_code=0,
            final_text="Reviewed only.",
        ),
    )

    result = run_codex_coder_tool(
        ToolExecutionRequest(
            tool_name="codex_coder_provider",
            workdir=str(repo),
            args={"instruction": "Review the repository.", "repo_id": "jarvis"},
            timeout_seconds=30,
        )
    )

    assert result.ok is True
    assert "Reviewed only." in result.stdout
    assert not any(str(artifact).startswith("git_file:data/coder_runs") for artifact in result.artifacts)


def test_codex_allows_worktree_changes_without_commit(monkeypatch, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    run_root = tmp_path / "runs"

    _install_registry(monkeypatch, repo)
    monkeypatch.setattr("app.tools.codex._resolve_codex_command", lambda: ["codex"])
    monkeypatch.setattr("app.tools.codex._coder_run_dir", lambda run_id: run_root / run_id)

    def _fake_run(*, provider_command, workdir, run_dir, instruction, timeout_seconds, trusted_command_prefixes=None):
        (workdir / "README.md").write_text("# Changed\n", encoding="utf-8")
        stdout = json.dumps({"type": "agent_message", "message": "Edited README."})
        return CodexAppServerRunResult(status="completed", raw_events=stdout, exit_code=0, final_text="Edited README.")

    monkeypatch.setattr("app.tools.codex._run_codex_app_server", _fake_run)

    result = run_codex_coder_tool(
        ToolExecutionRequest(
            tool_name="codex_coder_provider",
            workdir=str(repo),
            args={"instruction": "Update README.md.", "repo_id": "jarvis"},
            timeout_seconds=30,
        )
    )

    assert result.ok is True
    assert "Edited README." in result.stdout
    assert "git_file:README.md" in result.artifacts


def test_codex_artifacts_include_only_current_run_dirty_files(monkeypatch, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    run_root = tmp_path / "runs"
    (repo / "preexisting.txt").write_text("dirty before codex\n", encoding="utf-8")

    _install_registry(monkeypatch, repo)
    monkeypatch.setattr("app.tools.codex._resolve_codex_command", lambda: ["codex"])
    monkeypatch.setattr("app.tools.codex._coder_run_dir", lambda run_id: run_root / run_id)

    def _fake_run(*, provider_command, workdir, run_dir, instruction, timeout_seconds, trusted_command_prefixes=None):
        (workdir / "current-run.txt").write_text("created by codex\n", encoding="utf-8")
        stdout = json.dumps({"type": "agent_message", "message": "Created current-run.txt."})
        return CodexAppServerRunResult(status="completed", raw_events=stdout, exit_code=0, final_text="Created current-run.txt.")

    monkeypatch.setattr("app.tools.codex._run_codex_app_server", _fake_run)

    result = run_codex_coder_tool(
        ToolExecutionRequest(
            tool_name="codex_coder_provider",
            workdir=str(repo),
            args={"instruction": "Create current-run.txt.", "repo_id": "jarvis"},
            timeout_seconds=30,
        )
    )

    assert result.ok is True
    assert "git_file:current-run.txt" in result.artifacts
    assert "git_file:preexisting.txt" not in result.artifacts


def test_codex_coder_fails_when_commit_created_without_permission(monkeypatch, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    run_root = tmp_path / "runs"

    _install_registry(monkeypatch, repo)
    monkeypatch.setattr("app.tools.codex._resolve_codex_command", lambda: ["codex"])
    monkeypatch.setattr("app.tools.codex._coder_run_dir", lambda run_id: run_root / run_id)

    def _fake_run(*, provider_command, workdir, run_dir, instruction, timeout_seconds, trusted_command_prefixes=None):
        changed = workdir / "change.txt"
        changed.write_text("created by codex\n", encoding="utf-8")
        _git(workdir, "add", "change.txt")
        _git(workdir, "commit", "-m", "codex change")
        stdout = json.dumps({"type": "agent_message", "message": "Committed a change."})
        return CodexAppServerRunResult(status="completed", raw_events=stdout, exit_code=0, final_text="Committed a change.")

    monkeypatch.setattr("app.tools.codex._run_codex_app_server", _fake_run)

    result = run_codex_coder_tool(
        ToolExecutionRequest(
            tool_name="codex_coder_provider",
            workdir=str(repo),
            args={
                "instruction": "Create a change, but do not commit.",
                "repo_id": "jarvis",
                "allow_commit": False,
                "allow_push": False,
            },
            timeout_seconds=30,
        )
    )

    assert result.ok is False
    assert result.exit_code == 0
    assert "allow_commit=false but repository HEAD changed" in result.summary
    assert "Committed a change." in result.stdout
    assert "[JARVIS_" not in result.stdout
    audit_path = _artifact_path(result.artifacts, "jarvis_audit")
    assert "result=failed" in audit_path.read_text(encoding="utf-8")
    assert any(str(artifact).startswith("permission_violation:") for artifact in result.artifacts)


def test_codex_coder_parses_nested_agent_message_event(monkeypatch, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    run_root = tmp_path / "runs"

    _install_registry(monkeypatch, repo)
    monkeypatch.setattr("app.tools.codex._resolve_codex_command", lambda: ["codex"])
    monkeypatch.setattr("app.tools.codex._coder_run_dir", lambda run_id: run_root / run_id)

    def _fake_run(*, provider_command, workdir, run_dir, instruction, timeout_seconds, trusted_command_prefixes=None):
        stdout = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "agent_message",
                    "text": "Nested final review.",
                },
            }
        )
        return CodexAppServerRunResult(status="completed", raw_events=stdout, exit_code=0, final_text="Nested final review.")

    monkeypatch.setattr("app.tools.codex._run_codex_app_server", _fake_run)

    result = run_codex_coder_tool(
        ToolExecutionRequest(
            tool_name="codex_coder_provider",
            workdir=str(repo),
            args={"instruction": "Review this repo.", "repo_id": "jarvis"},
            timeout_seconds=30,
        )
    )

    assert result.ok is True
    assert "Nested final review." in result.stdout
    assert "{\"type\"" not in result.stdout
    assert "[JARVIS_" not in result.stdout


def test_coder_permission_check_fails_when_upstream_changes_without_push_permission() -> None:
    preflight = {
        "git_available": True,
        "head": "local-a",
        "branch": "main",
        "upstream_head": "remote-a",
    }
    postflight = {
        "git_available": True,
        "head": "local-a",
        "branch": "main",
        "upstream_head": "remote-b",
    }

    result = check_coder_permissions(preflight, postflight, allow_commit=False, allow_push=False)

    assert result["ok"] is False
    assert result["upstream_changed"] is True
    assert "allow_push=false but upstream head changed." in result["violations"]


def test_codex_coder_reports_missing_cli(monkeypatch, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _install_registry(monkeypatch, repo)
    monkeypatch.setattr("app.tools.codex._resolve_codex_command", lambda: None)

    result = run_codex_coder_tool(
        ToolExecutionRequest(
            tool_name="codex_coder_provider",
            workdir=None,
            args={"instruction": "Update README.md.", "repo_id": "jarvis"},
        )
    )

    assert result.ok is False
    assert result.summary == "codex CLI was not found on PATH."


def test_codex_coder_rejects_unregistered_workdir(monkeypatch, tmp_path: Path) -> None:
    registered = _init_repo(tmp_path / "registered")
    unknown = _init_repo(tmp_path / "unknown")
    _install_registry(monkeypatch, registered)

    result = run_codex_coder_tool(
        ToolExecutionRequest(
            tool_name="codex_coder_provider",
            workdir=str(unknown),
            args={"instruction": "Update README.md.", "workdir": str(unknown)},
        )
    )

    assert result.ok is False
    assert "not registered or not authorized" in result.summary


def test_codex_coder_rejects_repo_id_workdir_mismatch(monkeypatch, tmp_path: Path) -> None:
    registered = _init_repo(tmp_path / "registered")
    other = _init_repo(tmp_path / "other")
    _install_registry(monkeypatch, registered)

    result = run_codex_coder_tool(
        ToolExecutionRequest(
            tool_name="codex_coder_provider",
            workdir=str(other),
            args={"instruction": "Update README.md.", "repo_id": "jarvis", "workdir": str(other)},
        )
    )

    assert result.ok is False
    assert "repo_id and workdir" in result.summary


def test_codex_coder_allows_registered_workdir_with_warning(monkeypatch, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    run_root = tmp_path / "runs"
    _install_registry(monkeypatch, repo)
    monkeypatch.setattr("app.tools.codex._resolve_codex_command", lambda: ["codex"])
    monkeypatch.setattr("app.tools.codex._coder_run_dir", lambda run_id: run_root / run_id)
    monkeypatch.setattr(
        "app.tools.codex._run_codex_app_server",
        lambda *, provider_command, workdir, run_dir, instruction, timeout_seconds, trusted_command_prefixes=None: CodexAppServerRunResult(
            status="completed",
            raw_events=json.dumps({"type": "agent_message", "message": "Done."}),
            exit_code=0,
            final_text="Done.",
        ),
    )

    result = run_codex_coder_tool(
        ToolExecutionRequest(
            tool_name="codex_coder_provider",
            workdir=str(repo),
            args={"instruction": "Update README.md.", "workdir": str(repo)},
        )
    )

    assert result.ok is True
    assert result.stdout == "Done."
    audit_path = _artifact_path(result.artifacts, "jarvis_audit")
    assert "workdir is deprecated; use repo_id instead." in audit_path.read_text(encoding="utf-8")


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "config", "user.email", "jarvis@example.test")
    _git(path, "config", "user.name", "Jarvis Tests")
    (path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")
    return path


def _install_registry(monkeypatch, repo: Path) -> None:
    registry = RepositoryRegistry(
        [
            RepositoryRef(
                repo_id="jarvis",
                name="Jarvis",
                root_path=repo,
                canonical_root_path=repo.resolve(),
                status="active",
                permission_level="coder",
            )
        ]
    )
    monkeypatch.setattr("app.tools.codex.get_repository_registry", lambda: registry)


def _sandbox_tmp(name: str) -> Path:
    path = Path("sandbox") / f"{name}-{uuid4().hex}"
    path.mkdir(parents=True)
    return path


def _artifact_path(artifacts: list[str], name: str) -> Path:
    prefix = f"{name}:"
    for artifact in artifacts:
        if str(artifact).startswith(prefix):
            return Path(str(artifact)[len(prefix) :])
    raise AssertionError(f"Missing artifact: {name}")


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
