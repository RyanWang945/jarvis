import json

from app.agent_react.react_graph import _tools_granted_by_tool_search
from app.tools.common import ToolExecutionRequest
from app.tools.runtime import get_tool_definition


def _run_tool_search(**args):
    tool = get_tool_definition("tool_search")
    result = tool.handler(ToolExecutionRequest(tool_name="tool_search", workdir=None, args=args))
    assert result.ok
    return json.loads(result.stdout)


def test_tool_search_finds_reminder_tool() -> None:
    payload = _run_tool_search(
        query="create a reminder in 10 minutes",
        original_user_request="10分钟后提醒我喝水",
    )

    assert payload["status"] == "found"
    assert payload["candidates"][0]["tool_name"] == "scheduled_task"


def test_tool_search_can_return_no_capable_tool_for_context_explanation() -> None:
    payload = _run_tool_search(
        query="git diff stat numbers meaning",
        original_user_request="21\n14 580 22啥意思",
    )

    assert payload["status"] == "no_capable_tool"
    assert payload["candidates"] == []


def test_tool_search_finds_x_search_for_social_posts() -> None:
    payload = _run_tool_search(
        query="search X/Twitter posts about Jarvis",
        original_user_request="查一下 X 上大家怎么说 Jarvis",
    )

    assert payload["status"] == "found"
    assert any(candidate["tool_name"] == "x_search" for candidate in payload["candidates"])


def test_tool_search_finds_deliver_file_for_explicit_file_delivery() -> None:
    payload = _run_tool_search(
        query="resend generated image",
        original_user_request="刚才那张图片再发给我一下",
    )

    assert payload["status"] == "found"
    assert any(candidate["tool_name"] == "deliver_file" for candidate in payload["candidates"])


def test_tool_search_grants_deliver_file_for_geiwo_file_request() -> None:
    output = json.dumps(
        {
            "status": "found",
            "candidates": [
                {
                    "tool_name": "deliver_file",
                    "fit": "high",
                    "risk_level": "medium",
                    "reason": "Deliver an explicitly named workspace file.",
                }
            ],
        },
        ensure_ascii=False,
    )

    grants = _tools_granted_by_tool_search(
        output,
        [],
        original_user_request=r"jarvis项目的这个文件E:\pythonProject\jarvis\jarvis-architecture-interview-v2.png给我",
    )

    assert grants == ["deliver_file"]
