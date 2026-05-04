import json

from app.tools.common import ToolExecutionRequest
from app.tools.runtime import get_tool_definition


def test_ask_user_returns_waiting_payload() -> None:
    tool = get_tool_definition("ask_user")

    result = tool.handler(
        ToolExecutionRequest(
            tool_name="ask_user",
            workdir=None,
            args={
                "question": "Which repository should I inspect?",
                "reason": "The user did not name a repository.",
                "expected_answer_type": "choice",
                "choices": ["jarvis", "nltk"],
            },
        )
    )

    assert result.ok
    payload = json.loads(result.stdout)
    assert payload == {
        "status": "waiting_for_user",
        "question": "Which repository should I inspect?",
        "reason": "The user did not name a repository.",
        "expected_answer_type": "choice",
        "choices": ["jarvis", "nltk"],
    }


def test_ask_user_rejects_empty_question() -> None:
    tool = get_tool_definition("ask_user")

    result = tool.handler(ToolExecutionRequest(tool_name="ask_user", workdir=None, args={"question": ""}))

    assert not result.ok
    assert result.exit_code == 2
