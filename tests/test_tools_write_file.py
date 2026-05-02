from pathlib import Path

from app.config import get_settings
from app.tools.runtime import execute_tool, get_tool_definition


def test_write_file_writes_inside_workspace(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "jarvis"
    target_dir = workspace / "docs" / "research"
    target_dir.mkdir(parents=True)
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(workspace))
    get_settings.cache_clear()

    tool = get_tool_definition("write_file")
    result = execute_tool(
        tool,
        {
            "relative_path": "docs/research/sample.md",
            "content": "# Sample\n\nhello",
        },
    )

    assert result.ok is True
    assert (target_dir / "sample.md").read_text(encoding="utf-8") == "# Sample\n\nhello"
    assert str(target_dir / "sample.md") in result.stdout


def test_write_file_rejects_missing_directory(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "jarvis"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(workspace))
    get_settings.cache_clear()

    tool = get_tool_definition("write_file")
    result = execute_tool(
        tool,
        {
            "relative_path": "docs/research/new/sample.md",
            "content": "# Sample\n\nhello",
        },
    )

    assert result.ok is False
    assert "Target directory does not exist" in result.stderr


def test_write_file_rejects_absolute_path(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "jarvis"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(workspace))
    get_settings.cache_clear()

    tool = get_tool_definition("write_file")
    result = execute_tool(
        tool,
        {
            "relative_path": r"C:\Users\Administrator\Desktop\bad.md",
            "content": "# bad",
        },
    )

    assert result.ok is False
    assert "Absolute paths are not allowed" in result.summary
