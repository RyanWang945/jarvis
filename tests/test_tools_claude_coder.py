from __future__ import annotations

import subprocess
from pathlib import Path

from app.tools.coder import run_coder_tool
from app.tools.common import ToolExecutionRequest


def test_claude_coder_uses_delegate_permission_mode_by_default(monkeypatch, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    captured = {}
    real_run = subprocess.run

    def _run(command, **kwargs):
        if command and command[0] == "claude":
            captured["command"] = command
            return subprocess.CompletedProcess(command, 0, stdout="Reviewed.\n", stderr="")
        return real_run(command, **kwargs)

    monkeypatch.setattr("app.tools.coder._resolve_cli_command", lambda: ["claude"])
    monkeypatch.setattr("app.tools.coder.subprocess.run", _run)

    result = run_coder_tool(
        ToolExecutionRequest(
            tool_name="claude_code_coder_provider",
            workdir=str(repo),
            args={"instruction": "Review this repo.", "repo_id": "jarvis"},
            timeout_seconds=30,
        )
    )

    assert result.ok is True
    assert "--permission-mode" in captured["command"]
    assert captured["command"][captured["command"].index("--permission-mode") + 1] == "delegate"


def test_claude_coder_uses_runtime_run_dir(monkeypatch, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    provider_run = tmp_path / "sessions" / "s1" / "nodes" / "n1" / "provider_run"
    real_run = subprocess.run

    def _run(command, **kwargs):
        if command and command[0] == "claude":
            return subprocess.CompletedProcess(command, 0, stdout="Reviewed.\n", stderr="")
        return real_run(command, **kwargs)

    monkeypatch.setattr("app.tools.coder._resolve_cli_command", lambda: ["claude"])
    monkeypatch.setattr("app.tools.coder.subprocess.run", _run)

    result = run_coder_tool(
        ToolExecutionRequest(
            tool_name="claude_code_coder_provider",
            workdir=str(repo),
            args={
                "instruction": "Review this repo.",
                "repo_id": "jarvis",
                "_runtime_run_dir": str(provider_run),
            },
            timeout_seconds=30,
        )
    )

    assert result.ok is True
    assert (provider_run / "claude-stdout.log").read_text(encoding="utf-8") == "Reviewed.\n"
    assert "[JARVIS_POSTFLIGHT]" in (provider_run / "jarvis-audit.log").read_text(encoding="utf-8")
    assert f"jarvis_audit:{provider_run / 'jarvis-audit.log'}" in result.artifacts


def test_claude_coder_fails_when_commit_created_without_permission(monkeypatch, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    real_run = subprocess.run

    def _run(command, **kwargs):
        if command and command[0] == "claude":
            (repo / "change.txt").write_text("created by claude\n", encoding="utf-8")
            _git(repo, "add", "change.txt")
            _git(repo, "commit", "-m", "claude change")
            return subprocess.CompletedProcess(command, 0, stdout="Committed a change.\n", stderr="")
        return real_run(command, **kwargs)

    monkeypatch.setattr("app.tools.coder._resolve_cli_command", lambda: ["claude"])
    monkeypatch.setattr("app.tools.coder.subprocess.run", _run)

    result = run_coder_tool(
        ToolExecutionRequest(
            tool_name="claude_code_coder_provider",
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
    assert any(str(artifact).startswith("permission_violation:") for artifact in result.artifacts)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")
    return path


def _git(workdir: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(workdir), check=True, capture_output=True, text=True)
