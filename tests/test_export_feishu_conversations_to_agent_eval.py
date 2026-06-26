from __future__ import annotations

import json

from scripts.export_feishu_conversations_to_agent_eval import (
    ExportOptions,
    rows_to_agent_eval_cases,
    write_jsonl,
)
from scripts.run_agent_eval import load_cases


def test_rows_to_agent_eval_cases_exports_user_messages_only() -> None:
    cases = rows_to_agent_eval_cases(
        [
            _row(7, "user", "帮我看下项目结构", external_message_id="om_1"),
            _row(7, "assistant", "项目结构如下", external_message_id="om_bot_1"),
            _row(7, "user", "再看看测试", external_message_id="om_2"),
        ]
    )

    assert len(cases) == 1
    assert cases[0]["id"] == "feishu_conv_7"
    assert cases[0]["category"] == "feishu_real"
    assert cases[0]["messages"] == [
        {"content": "帮我看下项目结构", "content_type": "text", "external_message_id": "om_1"},
        {"content": "再看看测试", "content_type": "text", "external_message_id": "om_2"},
    ]
    assert cases[0]["required_status"] == "completed"
    assert cases[0]["success_criteria"]


def test_rows_to_agent_eval_cases_filters_commands_and_short_conversations() -> None:
    cases = rows_to_agent_eval_cases(
        [
            _row(1, "user", "/clear"),
            _row(1, "user", "只剩一条"),
            _row(2, "user", "第一条"),
            _row(2, "user", "第二条"),
        ],
        ExportOptions(min_user_messages=2),
    )

    assert [case["id"] for case in cases] == ["feishu_conv_2"]
    assert [message["content"] for message in cases[0]["messages"]] == ["第一条", "第二条"]


def test_rows_to_agent_eval_cases_can_keep_commands() -> None:
    cases = rows_to_agent_eval_cases(
        [_row(1, "user", "/model"), _row(1, "user", "当前模型是什么？")],
        ExportOptions(include_commands=True),
    )

    assert [message["content"] for message in cases[0]["messages"]] == ["/model", "当前模型是什么？"]


def test_write_jsonl_output_loads_as_agent_eval_dataset(tmp_path) -> None:
    path = tmp_path / "feishu_real.jsonl"
    cases = rows_to_agent_eval_cases([_row(9, "user", "总结一下 Jarvis 的评估入口")])

    write_jsonl(path, cases)

    loaded = load_cases(path)
    assert [case.id for case in loaded] == ["feishu_conv_9"]
    assert loaded[0].messages[0]["content"] == "总结一下 Jarvis 的评估入口"
    assert json.loads(path.read_text(encoding="utf-8"))["metadata"]["source"] == "feishu_conversation_export"


def _row(
    conversation_id: int,
    role: str,
    content: str,
    *,
    external_message_id: str | None = None,
) -> dict:
    return {
        "conversation_id": conversation_id,
        "external_chat_id": f"oc_{conversation_id}",
        "chat_type": "group",
        "title": None,
        "message_id": conversation_id * 100,
        "turn_id": None,
        "sender_type": role,
        "role": role,
        "content": content,
        "content_type": "text",
        "external_message_id": external_message_id,
        "reply_to_message_id": None,
        "raw_payload": {},
        "created_at": "2026-06-25T00:00:00+08:00",
    }
