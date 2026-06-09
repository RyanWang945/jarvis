import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from app.tools.codex import run_codex_coder_tool
from app.tools.common import ToolExecutionRequest

REAL_REPO = Path(os.environ.get("JARVIS_REAL_CODER_REPO", r"G:\pycharm-project\nltk"))
RUN_REAL_TESTS = os.environ.get("JARVIS_RUN_REAL_CODER_TESTS") == "1"


pytestmark = pytest.mark.skipif(
    not RUN_REAL_TESTS or not REAL_REPO.exists(),
    reason="real coder repo tests are opt-in and require JARVIS_RUN_REAL_CODER_TESTS=1",
)


def test_real_repo_bootstrap_and_extend_greeter() -> None:
    branch_name = f"jarvis-real-{uuid4().hex[:8]}"

    bootstrap_instruction = (
        f"First create and switch to a new git branch named {branch_name}. "
        "Then turn this repository into a minimal Python project based on FEATURE.md. "
        "Create a greetings package with a Greeter class. "
        "Implement greet() and greet_by_time() with simple Chinese and English support. "
        "Add a tests/test_greetings.py file with pytest coverage for greet() and greet_by_time(). "
        "Add a minimal pyproject.toml if it is missing. "
        "Add a .gitignore that ignores .idea/, .pytest_cache/, __pycache__/, and .venv/. "
        "Update README.md so it explains how to run the example and tests. "
        "Do not create a commit. "
        "Run pytest -q before finishing."
    )
    bootstrap_result = _run_real_coder(bootstrap_instruction, verification_cmd="pytest -q")

    assert bootstrap_result.ok is True, bootstrap_result.stderr or bootstrap_result.stdout
    assert _current_branch() == branch_name
    assert (REAL_REPO / "greetings").is_dir()
    assert (REAL_REPO / "greetings" / "__init__.py").exists()
    assert (REAL_REPO / "tests" / "test_greetings.py").exists()
    assert (REAL_REPO / "pyproject.toml").exists()
    gitignore = REAL_REPO / ".gitignore"
    assert gitignore.exists()
    gitignore_text = gitignore.read_text(encoding="utf-8")
    for pattern in (".idea/", ".pytest_cache/", "__pycache__/", ".venv/"):
        assert pattern in gitignore_text
    _run_repo_command("pytest -q")

    extend_instruction = (
        "Continue on the current branch. "
        "Extend Greeter.greet() so it supports user_type='new' and user_type='vip'. "
        "Keep existing behavior compatible. "
        "Update tests/test_greetings.py to cover both user types. "
        "Update FEATURE.md and README.md so the new behavior is documented. "
        "Do not create a commit. "
        "Run pytest -q before finishing."
    )
    extend_result = _run_real_coder(extend_instruction, verification_cmd="pytest -q")

    assert extend_result.ok is True, extend_result.stderr or extend_result.stdout
    assert _current_branch() == branch_name
    _run_repo_command("pytest -q")
    status = _git_status_short()
    assert ".idea/" not in status
    assert ".pytest_cache/" not in status
    assert "__pycache__/" not in status
    test_content = (REAL_REPO / "tests" / "test_greetings.py").read_text(encoding="utf-8")
    assert "vip" in test_content
    assert "new" in test_content


def _run_real_coder(instruction: str, *, verification_cmd: str | None = None):
    request = ToolExecutionRequest(
        tool_name="codex_coder_provider",
        workdir=str(REAL_REPO),
        args={
            "instruction": instruction,
            "workdir": str(REAL_REPO),
            "verification_cmd": verification_cmd,
            "allow_commit": False,
            "allow_push": False,
        },
        timeout_seconds=1800,
    )
    return run_codex_coder_tool(request)


def _current_branch() -> str:
    completed = subprocess.run(
        ["git", "-c", f"safe.directory={REAL_REPO}", "branch", "--show-current"],
        cwd=str(REAL_REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=True,
    )
    return completed.stdout.strip()


def _run_repo_command(command: str) -> None:
    completed = subprocess.run(
        command,
        cwd=str(REAL_REPO),
        capture_output=True,
        shell=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr


def _git_status_short() -> str:
    completed = subprocess.run(
        ["git", "-c", f"safe.directory={REAL_REPO}", "status", "--short"],
        cwd=str(REAL_REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=True,
    )
    return completed.stdout
