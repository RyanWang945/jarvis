from __future__ import annotations

from app.runtime_usage import usage_totals


def test_usage_totals_splits_direct_api_and_claude_agent_sdk_usage() -> None:
    totals = usage_totals(
        [
            {
                "source": "llm",
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
            {
                "source": "claude_agent_sdk",
                "input_tokens": 300,
                "output_tokens": 30,
                "total_tokens": 330,
            },
        ]
    )

    assert totals is not None
    assert totals["total_tokens"] == 450
    assert totals["by_source"] == {
        "direct_api": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
        },
        "claude_agent_sdk": {
            "prompt_tokens": 300,
            "completion_tokens": 30,
            "total_tokens": 330,
        },
    }
