from types import SimpleNamespace

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from app.agent_react.context_manager import (
    ContextManager,
    ContextMessage,
    ConversationContext,
    _build_batch_summary,
)
from app.agent_react.session_state import ConversationSessionState
# ---------------------------------------------------------------------------
#  Two-layer compression tests
# ---------------------------------------------------------------------------


def _make_record(msg_id: int, role: str, content: str, turn_id: int | None = None):
    return SimpleNamespace(id=msg_id, role=role, content=content, raw_payload={}, turn_id=turn_id)


def _make_turn(turn_id: int, user_text: str, assistant_text: str, base_msg_id: int):
    return [
        _make_record(base_msg_id, "user", user_text, turn_id=turn_id),
        _make_record(base_msg_id + 1, "assistant", assistant_text, turn_id=turn_id),
    ]


class TestTwoLayerCompression:
    """Verify the two-layer context compression: single-message + batch."""

    _LONG_TEXT = " ".join(
        [
            "This is a very long research report that spans many paragraphs.",
            *(f"Paragraph {i}: " + "deep research " * 50 for i in range(1, 51)),
        ]
    )  # ~4000+ chars

    def test_single_message_exceeds_budget_gets_truncated(self):
        """Layer 1: a single assistant message exceeding the token budget is truncated."""
        manager = ContextManager()
        records = [
            _make_record(1, "user", "调研 Anthropic C Compiler"),
            _make_record(2, "assistant", self._LONG_TEXT),
        ]
        ctx = manager.build_conversation_context(records, trigger_message_id=None)
        msgs = ctx.messages

        # Both messages should be present (only 2 messages, no batch compression needed)
        assert len(msgs) == 2
        assistant_msg = [m for m in msgs if m.role == "assistant"][0]
        # The content should be shorter than the original
        assert len(assistant_msg.content) < len(self._LONG_TEXT)
        # Should fit within 180 tokens (~720 chars max)
        assert len(assistant_msg.content) < 1500
        # Metadata
        assert assistant_msg.original_token_count > 0

    def test_long_history_compresses_old_turns(self):
        """Layer 2: many rounds exceed the token budget, old turns are compressed."""
        manager = ContextManager()
        # Build 8 rounds of conversation, each with moderate text
        records: list = []
        for i in range(8):
            records.extend(
                _make_turn(
                    turn_id=i + 1,
                    user_text=f"Turn {i + 1}: I want to research topic {i + 1} in detail, including methodology and findings.",
                    assistant_text=f"Turn {i + 1} response: Here is the analysis for topic {i + 1}. " * 20,
                    base_msg_id=i * 2 + 1,
                )
            )

        ctx = manager.build_conversation_context(records, trigger_message_id=None)
        msgs = ctx.messages

        # Should have a compressed batch summary + recent 2 full rounds
        assert len(msgs) >= 5  # batch_system + 4 recent messages

        system_msgs = [m for m in msgs if m.role == "system"]
        assert len(system_msgs) >= 1
        compressed = system_msgs[0]
        assert compressed.is_compressed is True
        assert compressed.compression_level == "batch"
        assert len(compressed.compressed_from_indices) > 0
        assert compressed.content.startswith("[对话历史]")

        # Last 2 rounds must be in full (user + assistant pairs)
        user_msgs = [m for m in msgs if m.role == "user"]
        assert any("Turn 7" in m.content for m in user_msgs)
        assert any("Turn 8" in m.content for m in user_msgs)

    def test_recent_messages_preserved_in_full(self):
        """The last 2 user+assistant rounds are always preserved in full text."""
        manager = ContextManager()
        records: list = []
        for i in range(10):
            records.extend(
                _make_turn(
                    turn_id=i + 1,
                    user_text=f"Round {i + 1}: investigate topic {i + 1} deeply with comprehensive analysis.",
                    assistant_text=f"Answer {i + 1}: detailed findings for topic {i + 1}. " * 10,
                    base_msg_id=i * 2 + 1,
                )
            )

        ctx = manager.build_conversation_context(records, trigger_message_id=None)
        msgs = ctx.messages

        user_msgs = [m for m in msgs if m.role == "user"]
        # Latest 2 rounds in full
        assert any("Round 9" in m.content for m in user_msgs)
        assert any("Round 10" in m.content for m in user_msgs)
        # Old rounds should NOT be in full (they're compressed)
        # Round 1-6 should be compressed away
        assert not any("Round 1: investigate" in m.content for m in user_msgs)

    def test_followup_anchors_to_recent_topic_not_old_topic(self):
        """模拟 '不够细' 场景: 长历史 + 新话题 follow-up 应该锚定到最新话题。"""
        manager = ContextManager()

        records: list = []

        # Topic A: 12 rounds of discussion, enough to exceed 1800 token budget
        for i in range(12):
            records.append(
                _make_record(
                    100 + i * 2,
                    "user",
                    f"第{i + 1}轮: 深入调研 anthropic build C compiler 第{i + 1}个模块的技术细节和实现方案",
                    turn_id=i + 1,
                )
            )
            records.append(
                _make_record(
                    100 + i * 2 + 1,
                    "assistant",
                    (
                        f"第{i + 1}轮回复: "
                        "C编译器前端解析 词法分析 AST生成 类型检查 SSA IR mem2reg 优化pass "
                        "常量折叠 死代码消除 寄存器分配 后端代码生成 ELF链接 DWARF调试信息 " * 3
                    ),
                    turn_id=i + 1,
                )
            )

        # Topic B: Ping An Bank (短讨论, 最近的话题, 应该保留全文)
        pingan_user = "平安银行是混合所有制的银行吗"
        pingan_assistant = "平安银行不是严格意义上的混合所有制银行。平安银行是股份制商业银行，隶属于中国平安保险集团。中国平安是民营控股企业，没有国有资本控股。"
        pingan_turn = 13  # after the 12 long rounds
        records.append(_make_record(200, "user", pingan_user, turn_id=pingan_turn))
        records.append(_make_record(201, "assistant", pingan_assistant, turn_id=pingan_turn))

        # Follow-up: "不够细" (now registered as a context reference marker)
        ctx = manager.build_conversation_context(
            records, trigger_message_id=None, current_user_input="不够细"
        )
        msgs = ctx.messages

        # "不够细" is now in _CONTEXT_REFERENCE_MARKERS
        assert ctx.context_reference_detected is True

        # Anthropic 长讨论应该被压缩为 system message
        system_msgs = [m for m in msgs if m.role == "system"]
        assert len(system_msgs) == 1
        assert system_msgs[0].is_compressed is True
        assert system_msgs[0].compression_level == "batch"

        # Ping An Bank 的原文必须在 messages 中完整保留
        assert any(pingan_user in m.content for m in msgs)
        assert any(pingan_assistant in m.content for m in msgs)

        # Ping An Bank 消息不在压缩消息的覆盖范围内
        compressed = system_msgs[0]
        pingan_indices = {m.original_index for m in msgs if pingan_user in m.content or pingan_assistant in m.content}
        assert not any(idx in compressed.compressed_from_indices for idx in pingan_indices if idx is not None)

    def test_planner_payload_no_summary_node(self):
        """planner_payload MUST NOT have summary_node field after migration."""
        manager = ContextManager()
        records = _make_turn(1, "hello", "world", 1)
        ctx = manager.build_conversation_context(records, trigger_message_id=None)
        payload = ctx.planner_payload()

        assert "messages" in payload
        assert "summary_node" not in payload
        assert isinstance(payload["messages"], list)
        for msg in payload["messages"]:
            assert "role" in msg
            assert "content" in msg

    def test_fast_payload_includes_working_summary_and_recent_six_rounds(self):
        manager = ContextManager()
        records: list = []
        for i in range(8):
            records.extend(
                _make_turn(
                    turn_id=i + 1,
                    user_text=f"Round {i + 1} user",
                    assistant_text=f"Round {i + 1} assistant",
                    base_msg_id=i * 2 + 1,
                )
            )
        ctx = manager.build_conversation_context(
            records,
            trigger_message_id=None,
            session_state=ConversationSessionState(working_summary="Earlier stable summary."),
        )
        payload = ctx.fast_payload()

        assert "recent_messages" in payload
        assert payload["older_summary"] == "Earlier stable summary."
        assert payload["recent_round_limit"] == 6
        assert payload["recent_messages"][0]["content"] == "Round 3 user"
        assert payload["recent_messages"][-1]["content"] == "Round 8 assistant"
        assert not any("Round 2" in item["content"] for item in payload["recent_messages"])

    def test_fast_payload_truncates_long_recent_messages(self):
        manager = ContextManager()
        long_text = "a" * 1600
        ctx = manager.build_conversation_context(
            [_make_record(1, "user", long_text)],
            trigger_message_id=None,
        )
        payload = ctx.fast_payload()

        content = payload["recent_messages"][0]["content"]
        assert len(content) < len(long_text)
        assert "[Content truncated by Jarvis fast context budget.]" in content

    def test_compression_metadata_preserved(self):
        """Compressed ContextMessage has correct metadata."""
        manager = ContextManager()
        records: list = []
        for i in range(5):
            records.extend(
                _make_turn(
                    turn_id=i + 1,
                    user_text=f"Topic {i + 1}: comprehensive research query",
                    assistant_text=f"Answer {i + 1}: " + "detailed analysis. " * 20,
                    base_msg_id=i * 2 + 1,
                )
            )

        ctx = manager.build_conversation_context(records, trigger_message_id=None)
        msgs = ctx.messages

        for msg in msgs:
            assert msg.role in {"user", "assistant", "system"}
            assert isinstance(msg.content, str)
            assert msg.compression_level in {"none", "single", "batch"}
            assert isinstance(msg.is_compressed, bool)
            if msg.compression_level == "none":
                assert msg.is_compressed is False
                assert msg.compressed_from_indices == ()
            if msg.compression_level == "batch":
                assert msg.is_compressed is True
                assert len(msg.compressed_from_indices) > 0

    def test_no_compression_for_short_context(self):
        """Short context under the budget should not trigger compression."""
        manager = ContextManager()
        records = _make_turn(1, "hello", "hi there", 1)
        ctx = manager.build_conversation_context(records, trigger_message_id=None)
        msgs = ctx.messages

        assert len(msgs) == 2
        assert all(m.compression_level == "none" for m in msgs)
        assert all(not m.is_compressed for m in msgs)

    def test_build_batch_summary_truncates_long_input(self):
        """_build_batch_summary limits output within budget."""
        long_texts = [f"topic {i}: " + "x " * 200 for i in range(20)]
        result = _build_batch_summary(long_texts, max_tokens=1800)
        # Budget is 15% of max_tokens = 270 tokens ~ 1080 chars max
        assert len(result) < 1500
        assert result.startswith("[对话历史]")

    def test_context_message_alias_backward_compat(self):
        """ConversationContextMessage is an alias for ContextMessage."""
        from app.agent_react.context_manager import ConversationContextMessage

        msg = ConversationContextMessage(role="user", content="test")
        assert isinstance(msg, ContextMessage)
        assert msg.role == "user"
        assert msg.content == "test"
        assert msg.compression_level == "none"

    def test_has_history_works_with_messages_only(self):
        """has_history should be True when there are messages, no summary needed."""
        ctx = ConversationContext(messages=())
        assert ctx.has_history is False

        ctx = ConversationContext(
            messages=(ContextMessage(role="user", content="hi"),)
        )
        assert ctx.has_history is True
