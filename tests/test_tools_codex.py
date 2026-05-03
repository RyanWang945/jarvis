import json
import subprocess
from pathlib import Path

from app.tools.codex import run_codex_coder_tool
from app.tools.coder_common import check_coder_permissions
from app.tools.common import ToolExecutionRequest
from app.tools.runtime import build_llm_tools, get_tool_definition


def test_codex_tool_is_injected_and_claude_tool_is_hidden() -> None:
    injected_names = {tool["function"]["name"] for tool in build_llm_tools()}

    assert "delegate_to_codex" in injected_names
    assert "delegate_to_claude_code" not in injected_names
    assert get_tool_definition("delegate_to_codex").exposed_to_llm is True
    assert get_tool_definition("delegate_to_claude_code").exposed_to_llm is False


def test_codex_coder_runs_with_clean_stdout_and_jsonl_artifact(monkeypatch, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    run_root = tmp_path / "runs"
    captured: dict[str, object] = {}

    monkeypatch.setattr("app.tools.codex._resolve_codex_command", lambda: ["codex"])
    monkeypatch.setattr("app.tools.codex._coder_run_dir", lambda run_id: run_root / run_id)

    def _fake_run(command, *, workdir, instruction, timeout_seconds):
        captured["command"] = command
        captured["workdir"] = workdir
        captured["instruction"] = instruction
        captured["timeout_seconds"] = timeout_seconds
        events = [
            {"type": "session_started", "id": "session_1"},
            {"type": "agent_message", "message": "Changed README and ran tests."},
        ]
        stdout = "\n".join(json.dumps(event) for event in events)
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("app.tools.codex._run_codex_process", _fake_run)

    result = run_codex_coder_tool(
        ToolExecutionRequest(
            tool_name="delegate_to_codex",
            workdir=str(repo),
            args={
                "instruction": "Update README.md and run tests.",
                "workdir": str(repo),
                "allow_commit": False,
                "allow_push": False,
            },
            timeout_seconds=30,
        )
    )

    assert result.ok is True
    assert captured["command"] == [
        "codex",
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(repo.resolve()),
        "-",
    ]
    assert "Jarvis coder worker instructions" in str(captured["instruction"])
    assert "Changed README and ran tests." in result.stdout
    assert "{\"type\"" not in result.stdout
    assert any(str(artifact).startswith("codex_events:") for artifact in result.artifacts)


def test_codex_coder_fails_when_commit_created_without_permission(monkeypatch, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    run_root = tmp_path / "runs"

    monkeypatch.setattr("app.tools.codex._resolve_codex_command", lambda: ["codex"])
    monkeypatch.setattr("app.tools.codex._coder_run_dir", lambda run_id: run_root / run_id)

    def _fake_run(command, *, workdir, instruction, timeout_seconds):
        changed = workdir / "change.txt"
        changed.write_text("created by codex\n", encoding="utf-8")
        _git(workdir, "add", "change.txt")
        _git(workdir, "commit", "-m", "codex change")
        stdout = json.dumps({"type": "agent_message", "message": "Committed a change."})
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("app.tools.codex._run_codex_process", _fake_run)

    result = run_codex_coder_tool(
        ToolExecutionRequest(
            tool_name="delegate_to_codex",
            workdir=str(repo),
            args={
                "instruction": "Create a change, but do not commit.",
                "workdir": str(repo),
                "allow_commit": False,
                "allow_push": False,
            },
            timeout_seconds=30,
        )
    )

    assert result.ok is False
    assert result.exit_code == 0
    assert "allow_commit=false but repository HEAD changed" in result.summary
    assert "result=failed" in result.stdout
    assert any(str(artifact).startswith("permission_violation:") for artifact in result.artifacts)


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
    monkeypatch.setattr("app.tools.codex._resolve_codex_command", lambda: None)

    result = run_codex_coder_tool(
        ToolExecutionRequest(
            tool_name="delegate_to_codex",
            workdir=str(repo),
            args={"instruction": "Update README.md.", "workdir": str(repo)},
        )
    )

    assert result.ok is False
    assert result.summary == "codex CLI was not found on PATH."


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
