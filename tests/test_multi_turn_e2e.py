"""Multi-turn end-to-end tests with LLM judge.

Exercises the full pipeline: Gateway → TaskAgentRuntime (real LLM) → reply,
across multiple turns in the same conversation.  Each response is evaluated by
an LLM judge against test-specific criteria.

Opt-in only (expensive and slow):
    JARVIS_RUN_E2E_TESTS=1 pytest tests/test_multi_turn_e2e.py -v

Requires:
    - DeepSeek API key (JARVIS_DEEPSEEK_API_KEY)
    - claude-agent-sdk installed (for claude_react runtime)
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.config import get_settings
from app.gateway.events import InboundEvent
from app.gateway.service import GatewayService
from app.llm.client import ChatClient, LLMMessage, parse_json_content
from app.task_runtime import TaskAgentRuntime
from tests.helpers.in_memory_store import InMemoryConversationStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Opt-in guard
# ---------------------------------------------------------------------------

RUN_E2E = os.environ.get("JARVIS_RUN_E2E_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not RUN_E2E,
    reason="real E2E tests are opt-in (set JARVIS_RUN_E2E_TESTS=1)",
)


# ---------------------------------------------------------------------------
# LLM Judge
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgeResult:
    passed: bool
    score: int  # 0-10
    reason: str
    raw_evaluation: dict[str, Any] = field(default_factory=dict)


class LLMJudge:
    """Uses DeepSeek to evaluate agent response quality."""

    _JUDGE_PROMPT = """你是一个严格的测试评判员（LLM Judge）。你需要评判助手回复的质量。

## 评判标准
{criteria}

## 用户输入
{user_input}

## 助手回复
{response}

---

请返回 JSON 格式的评判结果，仅包含以下字段，不要有任何其他内容：
```json
{{
  "passed": true或false,
  "score": 0到10的整数,
  "reason": "评判理由，简洁说明为什么通过或未通过"
}}
```"""

    def __init__(self, *, model: str = "deepseek-v4-flash") -> None:
        settings = get_settings()
        self._client = ChatClient(
            api_key=settings.deepseek_api_key or "",
            base_url=settings.deepseek_base_url or "https://api.deepseek.com",
            model=model,
            timeout_seconds=30.0,
        )

    def evaluate(
        self,
        user_input: str,
        response: str,
        criteria: str,
    ) -> JudgeResult:
        """Evaluate a single response against criteria."""
        prompt = self._JUDGE_PROMPT.format(
            criteria=criteria,
            user_input=user_input,
            response=response[:6000],  # truncate to avoid token overflow
        )
        messages = [LLMMessage(role="user", content=prompt)]
        resp = self._client.chat(messages, response_format={"type": "json_object"})
        content = resp.get("choices", [{}])[0].get("message", {}).get("content", "{}")
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return JudgeResult(passed=False, score=0, reason=f"Judge returned invalid JSON: {content[:200]}")
        return JudgeResult(
            passed=bool(data.get("passed", False)),
            score=min(10, max(0, int(data.get("score", 0)))),
            reason=str(data.get("reason", "")),
            raw_evaluation=data,
        )


# ---------------------------------------------------------------------------
# E2E test helper
# ---------------------------------------------------------------------------


def _send_and_judge(
    runtime: TaskAgentRuntime,
    gateway: GatewayService,
    *,
    external_chat_id: str,
    text: str,
    criteria: str,
    msg_counter: int,
) -> tuple[Any, JudgeResult]:
    """Send a message, wait for reply, and judge the response."""
    msg_id = f"e2e-msg-{msg_counter}"
    event = InboundEvent(
        platform="api",
        external_chat_id=external_chat_id,
        external_message_id=msg_id,
        chat_type="dm",
        sender_id="e2e-tester",
        sender_name="E2E Tester",
        text=text,
    )

    gateway_result = gateway.handle_inbound_event(event)
    if not gateway_result.should_run_agent:
        reply_text = gateway_result.immediate_reply or "(no agent response)"
        return None, JudgeResult(
            passed=False,
            score=0,
            reason=f"Agent skipped: status={gateway_result.status} reply={reply_text[:200]}",
        )

    started = time.perf_counter()
    turn_result = runtime.run_turn(gateway_result.turn_id)
    elapsed = int((time.perf_counter() - started) * 1000)

    reply = turn_result.reply
    # Strip markdown formatting for cleaner judge input
    clean_reply = reply[:4000]  # keep judge input manageable

    logger.info(
        "e2e turn done counter=%s turn_id=%s status=%s reply_len=%s elapsed_ms=%s",
        msg_counter,
        turn_result.turn_id,
        turn_result.status,
        len(reply),
        elapsed,
    )

    judge = LLMJudge()
    judge_result = judge.evaluate(
        user_input=text,
        response=clean_reply,
        criteria=criteria,
    )
    logger.info(
        "e2e judge result counter=%s passed=%s score=%s reason=%s",
        msg_counter,
        judge_result.passed,
        judge_result.score,
        judge_result.reason[:200],
    )

    return turn_result, judge_result


# ---------------------------------------------------------------------------
# Test: multi-turn basic
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_multi_turn_basic_conversation() -> None:
    """Two-turn conversation: ask a question, then a simple follow-up."""
    store = InMemoryConversationStore()
    gateway = GatewayService(conversation_store=store)
    chat_id = "e2e-multi-turn-basic"
    msg_cnt = 0

    # Use defaults: real LLM planner, real react runtime
    runtime = TaskAgentRuntime(store)

    # Turn 1: simple question
    msg_cnt += 1
    _, j1 = _send_and_judge(
        runtime,
        gateway,
        external_chat_id=chat_id,
        text="Python 的 GIL 是什么？简要解释。",
        criteria="回复应该解释 GIL（Global Interpreter Lock）是什么，内容准确，中文回答，长度适中。如果有事实性错误则判为不过。",
        msg_counter=msg_cnt,
    )
    assert j1.passed, f"Turn 1 failed: {j1.reason}"

    # Turn 2: follow-up
    msg_cnt += 1
    _, j2 = _send_and_judge(
        runtime,
        gateway,
        external_chat_id=chat_id,
        text="那 Python 3.13 对 GIL 有什么改动吗？",
        criteria="回复应该讨论 Python 3.13 关于 GIL 的具体改动（例如 free-threading / no-GIL 实验），内容应与 GIL 话题连贯，而非回答一个不相关的问题。",
        msg_counter=msg_cnt,
    )
    assert j2.passed, f"Turn 2 failed: {j2.reason}"


# ---------------------------------------------------------------------------
# Test: topic anchoring after context compression
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_topic_anchoring_after_compression() -> None:
    """Simulate the '不够细' scenario: long topic A → short topic B → follow-up.

    Turn 1: Ask for a detailed research report (long topic, will fill context)
    Turn 2: Ask a short question on a different topic
    Turn 3: Say "不够细" — must anchor to Turn 2 (Ping An), NOT Turn 1.
    """
    store = InMemoryConversationStore()
    gateway = GatewayService(conversation_store=store)
    chat_id = "e2e-topic-anchoring"
    msg_cnt = 0
    runtime = TaskAgentRuntime(store)

    # Turn 1: relatively detailed question (will be compressed later)
    msg_cnt += 1
    _, j1 = _send_and_judge(
        runtime,
        gateway,
        external_chat_id=chat_id,
        text="给我详细介绍一下 Rust 语言的所有权和借用系统，包括 move semantics、引用、生命周期等核心概念。",
        criteria="回复应该详细介绍 Rust 的所有权（ownership）、借用（borrowing）、move semantics、引用和生命周期。信息准确，结构清晰。",
        msg_counter=msg_cnt,
    )
    assert j1.passed, f"Turn 1 (Rust ownership) failed: {j1.reason}"

    # Turn 2: a completely different, shorter topic
    msg_cnt += 1
    _, j2 = _send_and_judge(
        runtime,
        gateway,
        external_chat_id=chat_id,
        text="TypeScript 的 interface 和 type 有什么区别？简单说说。",
        criteria="回复应回答 TypeScript 中 interface 和 type 的区别，内容准确（如 interface 可声明合并、type 支持联合类型等）。不能讲 Rust。",
        msg_counter=msg_cnt,
    )
    assert j2.passed, f"Turn 2 (TypeScript) failed: {j2.reason}"

    # Turn 3: the critical follow-up — must refer to Turn 2, not Turn 1
    msg_cnt += 1
    _, j3 = _send_and_judge(
        runtime,
        gateway,
        external_chat_id=chat_id,
        text="能再详细点吗",
        criteria=(
            "这是对上一轮 TypeScript 回答的跟进。回复应该继续讨论 TypeScript 的 interface 和 type 区别，"
            "提供更多细节（如具体代码示例、使用场景、何时选择 interface 或 type）。"
            "CRITICAL: 如果回复转而讨论 Rust 所有权系统或者完全不相关的话题，则判为 FAIL。"
        ),
        msg_counter=msg_cnt,
    )
    assert j3.passed, f"Turn 3 (TypeScript follow-up) FAILED — topic anchoring error: {j3.reason}"


# ---------------------------------------------------------------------------
# Test: context compression leads to correct follow-up routing
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_followup_after_long_research_anchors_to_recent_topic() -> None:
    """Long research (many tools, large context) + short new topic + follow-up.

    The long research turn will produce a large assistant response that fills
    the conversation context. After a short second question, a follow-up must
    correctly anchor to the most recent topic.

    This is the core regression test for the "不够细" → Anthropic problem.
    """
    store = InMemoryConversationStore()
    gateway = GatewayService(conversation_store=store)
    chat_id = "e2e-research-followup"
    msg_cnt = 0
    runtime = TaskAgentRuntime(store)

    # Turn 1: detailed research (large context producer)
    msg_cnt += 1
    _, j1 = _send_and_judge(
        runtime,
        gateway,
        external_chat_id=chat_id,
        text="帮我详细调研一下 VS Code 的架构设计，包括 Electron 的使用、Extension Host 进程模型、语言服务协议 LSP 等核心设计。",
        criteria="回复应该讨论 VS Code 的架构设计，包括 Electron、Extension Host、LSP 等关键概念。信息准确。",
        msg_counter=msg_cnt,
    )
    assert j1.passed, f"Turn 1 (VS Code architecture) failed: {j1.reason}"

    # Turn 2: short, completely unrelated
    msg_cnt += 1
    _, j2 = _send_and_judge(
        runtime,
        gateway,
        external_chat_id=chat_id,
        text="HTTP/2 相比于 HTTP/1.1 有哪些主要改进？",
        criteria="回复应列举 HTTP/2 的主要改进（多路复用、头部压缩、服务器推送、二进制分帧等），不能讨论 VS Code。",
        msg_counter=msg_cnt,
    )
    assert j2.passed, f"Turn 2 (HTTP/2) failed: {j2.reason}"

    # Turn 3: follow-up must anchor to HTTP/2, not VS Code
    msg_cnt += 1
    _, j3 = _send_and_judge(
        runtime,
        gateway,
        external_chat_id=chat_id,
        text="多路复用具体是怎么实现的？",
        criteria=(
            "这是对上一轮 HTTP/2 回答的跟进。回复应该详细解释 HTTP/2 多路复用的实现原理"
            "（如 stream、frame、stream identifier、并发传输等）。"
            "CRITICAL: 如果回复转而讨论 VS Code 架构或者完全不相关的话题，则判为 FAIL。"
        ),
        msg_counter=msg_cnt,
    )
    assert j3.passed, f"Turn 3 (HTTP/2 multiplexing) FAILED — topic anchoring error: {j3.reason}"
