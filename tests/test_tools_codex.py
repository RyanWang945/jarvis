import json
import subprocess
from pathlib import Path

from app.agent_react.runtime_policy import resolve_runtime_policy
from app.repositories import RepositoryRef, RepositoryRegistry
from app.tools.codex import run_codex_coder_tool
from app.tools.coder_common import check_coder_permissions
from app.tools.common import ToolExecutionRequest
from app.tools.runtime import build_llm_tools, get_tool_definition


def test_codex_tool_is_injected_and_claude_tool_is_hidden() -> None:
    injected_names = {tool["function"]["name"] for tool in build_llm_tools()}
    codex_tool = get_tool_definition("delegate_to_codex")

    assert "delegate_to_codex" in injected_names
    assert "delegate_to_claude_code" not in injected_names
    assert codex_tool.exposed_to_llm is True
    assert codex_tool.args_schema["required"] == ["instruction"]
    assert "repo_id" in codex_tool.args_schema["properties"]
    assert get_tool_definition("delegate_to_claude_code").exposed_to_llm is False


def test_coding_policy_allows_codex_but_not_claude() -> None:
    policy = resolve_runtime_policy(session_mode="chat", turn_type="coding")

    assert "delegate_to_codex" in policy.allowed_tools
    assert "delegate_to_claude_code" not in policy.allowed_tools
    assert "shell_inspect" not in policy.allowed_tools
    assert "shell_run_command" not in policy.allowed_tools


def test_codex_coder_runs_with_clean_stdout_and_jsonl_artifact(monkeypatch, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    run_root = tmp_path / "runs"
    captured: dict[str, object] = {}

    _install_registry(monkeypatch, repo)
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
                "repo_id": "jarvis",
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
    assert "[JARVIS_" not in result.stdout
    assert any(str(artifact).startswith("codex_events:") for artifact in result.artifacts)
    assert any(str(artifact).startswith("jarvis_audit:") for artifact in result.artifacts)


def test_codex_coder_fails_when_commit_created_without_permission(monkeypatch, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    run_root = tmp_path / "runs"

    _install_registry(monkeypatch, repo)
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

    def _fake_run(command, *, workdir, instruction, timeout_seconds):
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
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("app.tools.codex._run_codex_process", _fake_run)

    result = run_codex_coder_tool(
        ToolExecutionRequest(
            tool_name="delegate_to_codex",
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
            tool_name="delegate_to_codex",
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
            tool_name="delegate_to_codex",
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
            tool_name="delegate_to_codex",
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
        "app.tools.codex._run_codex_process",
        lambda command, *, workdir, instruction, timeout_seconds: subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"type": "agent_message", "message": "Done."}),
            stderr="",
        ),
    )

    result = run_codex_coder_tool(
        ToolExecutionRequest(
            tool_name="delegate_to_codex",
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
