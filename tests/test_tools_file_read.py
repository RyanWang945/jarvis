import json

from app.tools.common import ToolExecutionRequest
from app.tools.runtime import get_tool_definition


def _run_tool(name: str, **args):
    tool = get_tool_definition(name)
    result = tool.handler(ToolExecutionRequest(tool_name=name, workdir=None, args=args))
    payload = json.loads(result.stdout)
    return result, payload


def test_read_file_returns_bounded_workspace_file_content() -> None:
    result, payload = _run_tool("read_file", path="app/tools/runtime.py", start_line=1, max_lines=5)

    assert result.ok is True
    assert payload["exists"] is True
    assert payload["is_file"] is True
    assert payload["relative_path"] == "app/tools/runtime.py"
    assert payload["returned_lines"] <= 5
    assert "from __future__ import annotations" in payload["content"]


def test_file_tools_descriptions_preserve_specific_artifact_lookup_boundary() -> None:
    read_description = get_tool_definition("read_file").description
    search_description = get_tool_definition("search_files").description

    assert "具体文件或 artifact" in read_description
    assert "具体或可猜测的文件/路径/artifact" in search_description
    assert "仓库级判断" in read_description
    assert "仓库级判断" in search_description


def test_read_file_rejects_path_outside_workspace() -> None:
    result, payload = _run_tool("read_file", path=r"C:\Users\Administrator")

    assert result.ok is False
    assert "workspace" in payload["error"]


def test_search_files_reports_exact_path_and_path_matches() -> None:
    result, payload = _run_tool("search_files", query="app/tools/runtime.py", mode="path", max_results=5)

    assert result.ok is True
    assert payload["exact_path"]["exists"] is True
    assert payload["exact_path"]["is_file"] is True
    assert any(item["relative_path"] == "app/tools/runtime.py" for item in payload["results"])


def test_search_files_content_mode_returns_preview() -> None:
    result, payload = _run_tool("search_files", query="def build_llm_tools", mode="content", max_results=5)

    assert result.ok is True
    assert any(item["relative_path"] == "app/tools/runtime.py" for item in payload["results"])
    assert any("build_llm_tools" in item["preview"] for item in payload["results"])
