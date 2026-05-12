"""ReAct subgraph: LLM reasoning + tool execution loop."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from app.agent_react.artifacts import artifact_to_payload, legacy_artifact_to_tool_artifact
from app.agent_react.context_manager import ContextManager
from app.agent_react.tool_intent_state import append_conversation_tool_intents
from app.config import get_settings
from app.llm.model_profiles import LLMNode
from app.llm.model_router import ModelRouter
from app.llm.provider_adapters import NormalizedLLMResponse
from app.persistence.models import TurnRecord
from app.repositories import RepositoryRegistryError, get_repository_registry
from app.tools.common import ToolArtifact
from app.tools.runtime import build_llm_tools, check_tool_policy, execute_tool, get_tool_definition

logger = logging.getLogger(__name__)
_CONTEXT_MANAGER = ContextManager()


class TurnStore(Protocol):
    def get_conversation(self, conversation_id: int) -> Any | None: ...

    def get_turn(self, turn_id: int) -> TurnRecord | None: ...

    def append_assistant_message(
        self,
        *,
        conversation_id: int,
        turn_id: int | None,
        content: str,
        content_type: str = "markdown",
        external_message_id: str | None = None,
        raw_payload: dict | None = None,
    ) -> Any: ...

    def append_tool_message(
        self,
        *,
        conversation_id: int,
        turn_id: int | None,
        content: str,
        content_type: str = "text",
        external_message_id: str | None = None,
        raw_payload: dict | None = None,
    ) -> Any: ...

    def create_tool_call(
        self,
        *,
        turn_id: int,
        tool_name: str,
        input: dict,
        assistant_message_id: int | None = None,
        provider_tool_call_id: str | None = None,
        step_index: int = 0,
    ) -> Any: ...

    def update_tool_call(
        self,
        tool_call_id: int,
        *,
        status: str | None = None,
        output: dict | None = None,
        error_message: str | None = None,
    ) -> Any: ...

    def list_tool_calls_by_turn(self, turn_id: int) -> list: ...

    def update_conversation_metadata(self, conversation_id: int, patch: dict[str, Any]) -> None: ...


_MAX_REACT_STEPS = 8


class ReActState(TypedDict):
    turn_id: int
    messages: list[BaseMessage]
    artifacts: list[ToolArtifact]
    cancelled: bool
    status: str
    step_count: int
    token_budget: int | None
    token_usage: dict[str, int] | None
    model: str | None
    allowed_tools: list[str]
    max_steps: int
    search_budget: int | None


_MAX_TAVILY_CALLS_PER_TURN = 10
_TOOL_SEARCH_GRANTABLE_TOOLS = {
    "scheduled_task",
    "deliver_file",
    "tavily_search",
    "obsidian_wiki_query",
    "business_knowledge_search",
    "obsidian_wiki_draft",
    "obsidian_wiki_apply",
}

_COMMIT_INTENT_PATTERN = r"(?<![A-Za-z])commit(?![A-Za-z])|提交|创建\s*commit|建立\s*commit"
_PUSH_INTENT_PATTERN = r"(?<![A-Za-z])push(?![A-Za-z])|推送|远程|origin"
_EDIT_INTENT_PATTERN = (
    r"\b(update|modify|change|edit|add|create|write|fix|repair|delete|remove)\b"
    r"|更新|修改|改一下|增加|新增|创建|写入|修复|删除|移除"
)
_READ_ONLY_INSTRUCTION_PATTERN = (
    r"\b(read|show|inspect|list|summari[sz]e|review|analy[sz]e)\b"
    r"|读取|查看|展示|列出|分析|总结|检查"
)
_DOWNGRADED_DECISION_PATTERN = (
    r"\b(show me|so i can decide|let me decide|ask the user|confirm what to update|decide what to update)\b"
    r"|让我.*决定|给我.*决定|我再决定|先.*看"
)
_EXECUTION_INSTRUCTION_PATTERN = (
    r"\b(update|modify|change|edit|add|create|write|fix|delete|commit|push)\b"
    r"|更新|修改|增加|新增|创建|写入|修复|删除|提交|推送"
)


@dataclass(frozen=True)
class ToolExecutionOutcome:
    ok: bool
    output: str
    artifacts: tuple[ToolArtifact, ...] = ()

    def __iter__(self):
        # Compatibility for older tests and call sites that unpack `(ok, output)`.
        yield self.ok
        yield self.output


def _llm_response_to_ai_message(response: NormalizedLLMResponse) -> AIMessage:
    content = response.content or ""
    tool_calls: list[dict[str, Any]] = []
    for tc in response.tool_calls:
        tool_calls.append({
            "id": tc.id,
            "name": tc.name,
            "args": tc.args,
        })
    reasoning_content = response.reasoning_content
    response_metadata = {}
    if reasoning_content is not None:
        response_metadata["reasoning_content"] = reasoning_content
    return AIMessage(content=content, tool_calls=tool_calls, response_metadata=response_metadata)


def _usage_from_normalized(response: NormalizedLLMResponse) -> dict[str, int] | None:
    if response.usage is None:
        return None
    return {
        "prompt_tokens": response.usage.prompt_tokens,
        "completion_tokens": response.usage.completion_tokens,
        "total_tokens": response.usage.total_tokens,
    }


def _usage_from_response(response: dict[str, Any]) -> dict[str, int] | None:
    usage = response.get("_usage")
    if not isinstance(usage, dict):
        usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = _coerce_usage_int(usage.get("prompt_tokens", usage.get("input_tokens")))
    completion = _coerce_usage_int(usage.get("completion_tokens", usage.get("output_tokens")))
    total = _coerce_usage_int(usage.get("total_tokens"))
    if total == 0:
        total = prompt + completion
    if prompt == 0 and completion == 0 and total == 0:
        return None
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _coerce_usage_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _merge_token_usage(current: dict[str, int] | None, addition: dict[str, int] | None) -> dict[str, int] | None:
    if addition is None:
        return current
    merged = dict(current or {})
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        merged[key] = int(merged.get(key, 0) or 0) + int(addition.get(key, 0) or 0)
    return merged


def _latest_human_message_text(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content or "")
    return ""


def _has_pattern(text: str, pattern: str) -> bool:
    return bool(re.search(pattern, text, flags=re.IGNORECASE))


def _codex_instruction_was_downgraded(instruction: str, *, user_requested_execution: bool) -> bool:
    if not user_requested_execution:
        return False
    if not instruction.strip():
        return True
    if _has_pattern(instruction, _DOWNGRADED_DECISION_PATTERN):
        return True
    if _has_pattern(instruction, _EXECUTION_INSTRUCTION_PATTERN):
        return False
    return _has_pattern(instruction, _READ_ONLY_INSTRUCTION_PATTERN)


def _strengthen_codex_contract(tool_args: dict[str, Any], messages: list[BaseMessage]) -> dict[str, Any]:
    latest_user_text = _latest_human_message_text(messages).strip()
    if not latest_user_text:
        return tool_args

    user_requested_commit = _has_pattern(latest_user_text, _COMMIT_INTENT_PATTERN)
    user_requested_push = _has_pattern(latest_user_text, _PUSH_INTENT_PATTERN)
    if _looks_like_uncommitted_status_request(latest_user_text):
        user_requested_commit = False
        user_requested_push = False
    user_requested_edit = _has_pattern(latest_user_text, _EDIT_INTENT_PATTERN)
    if _looks_like_diagnosis_or_plan_only_request(latest_user_text):
        user_requested_edit = False
    user_requested_execution = user_requested_edit or user_requested_commit or user_requested_push
    instruction = str(tool_args.get("instruction") or "").strip()
    needs_contract_rewrite = _codex_instruction_was_downgraded(
        instruction,
        user_requested_execution=user_requested_execution,
    )
    needs_permission_repair = (
        (user_requested_commit and not bool(tool_args.get("allow_commit")))
        or (user_requested_push and (not bool(tool_args.get("allow_push")) or not bool(tool_args.get("allow_commit"))))
    )
    if not needs_contract_rewrite and not needs_permission_repair:
        return tool_args

    repaired = dict(tool_args)
    if user_requested_commit or user_requested_push:
        repaired["allow_commit"] = True
    if user_requested_push:
        repaired["allow_push"] = True
    if needs_contract_rewrite:
        repaired["instruction"] = "\n".join(
            [
                "Complete the full repository task requested by the user. Do not downgrade it into a read-only inspection or ask the user to decide routine implementation details.",
                "Codex owns repository reading, planning, editing, verification, commit creation, push execution, and approval requests within the permissions below.",
                "",
                "Original user request:",
                latest_user_text,
                "",
                "Model-provided delegate instruction, for context only:",
                instruction or "(empty)",
            ]
        )
    elif needs_permission_repair:
        repaired["instruction"] = "\n".join(
            [
                instruction,
                "",
                "Jarvis contract repair: the original user request explicitly included commit/push intent. Preserve that outcome and use Codex approval flow for elevated actions.",
                "Original user request:",
                latest_user_text,
            ]
        )
    logger.info(
        "codex delegation contract repaired commit_requested=%s push_requested=%s edit_requested=%s rewrite=%s permission_repair=%s original_args=%s repaired_args=%s",
        user_requested_commit,
        user_requested_push,
        user_requested_edit,
        needs_contract_rewrite,
        needs_permission_repair,
        tool_args,
        repaired,
    )
    return repaired


def _looks_like_uncommitted_status_request(text: str) -> bool:
    lowered = text.lower()
    if not any(marker in lowered for marker in ("未提交", "uncommitted", "not committed", "not yet committed")):
        return False
    return any(marker in lowered for marker in ("多少", "几个", "哪些", "内容", "状态", "status", "changes", "diff", "有"))


def _looks_like_diagnosis_or_plan_only_request(text: str) -> bool:
    lowered = text.lower()
    if any(marker in lowered for marker in ("commit", "push")):
        return False
    execution_markers = (
        "do it",
        "apply it",
        "implement it",
        "fix it",
        "make the change",
    )
    if any(marker in lowered for marker in execution_markers):
        return False
    planning_markers = (
        "plan",
        "root cause",
        "diagnose",
        "tell me",
        "show me",
        "explain",
        "先告诉我",
        "告诉我",
        "看看具体是什么问题",
        "什么问题",
        "修改的计划",
        "修改计划",
        "修复计划",
    )
    return any(marker in lowered for marker in planning_markers)


def _is_turn_cancelled(store: TurnStore, turn_id: int) -> bool:
    turn = store.get_turn(turn_id)
    return turn is not None and getattr(turn, "status", None) == "cancelled"


def call_llm(state: ReActState, store: TurnStore) -> ReActState:
    if _is_turn_cancelled(store, state["turn_id"]):
        return {
            **state,
            "cancelled": True,
            "status": "cancelled",
        }

    settings = get_settings()
    turn = store.get_turn(state["turn_id"])
    conversation = store.get_conversation(turn.conversation_id) if turn is not None else None
    resolved_llm = ModelRouter(settings).resolve(
        LLMNode.AGENT_STEP,
        getattr(conversation, "metadata", None) if conversation is not None else None,
    )
    client = resolved_llm.client

    messages = state["messages"]
    llm_messages = _CONTEXT_MANAGER.render_for_model(messages, state.get("token_budget"))
    if not llm_messages or llm_messages[0].role != "system":
        raise ValueError("turn runtime must provide a system message before call_llm")

    # 达到最大步数前最后一次调用时，强制 LLM 生成文字总结（不传 tools）
    current_step = state.get("step_count", 0)
    max_steps = int(state.get("max_steps") or _MAX_REACT_STEPS)
    force_final = current_step >= max_steps - 1
    tools = None if force_final else build_llm_tools(allowed_tools=state.get("allowed_tools") or None)
    if tools == []:
        tools = None
    if force_final:
        logger.warning("forcing final text response turn_id=%s step=%s", state["turn_id"], current_step)
    for idx, message in enumerate(llm_messages):
        logger.info(
            "llm_input msg[%s] role=%s content=%s tool_call_id=%s has_tool_calls=%s",
            idx,
            message.role,
            repr(message.content[:120]) if message.content else "",
            message.tool_call_id,
            bool(message.tool_calls),
        )

    try:
        response = client.chat_normalized(llm_messages, tools=tools)
    except Exception:
        logger.exception("llm call failed messages_count=%s", len(llm_messages))
        return {
            **state,
            "turn_id": state["turn_id"],
            "cancelled": False,
            "status": "failed",
            "messages": messages + [AIMessage(content="抱歉，调用模型时出错了，请稍后再试。")],
            "step_count": state.get("step_count", 0) + 1,
        }

    response_usage = _usage_from_normalized(response)
    token_usage = _merge_token_usage(state.get("token_usage"), response_usage)
    model_name = response.model or resolved_llm.profile.model or state.get("model") or settings.deepseek_model
    if response_usage is not None:
        logger.info(
            "llm_usage turn_id=%s step=%s model=%s prompt=%s completion=%s total=%s turn_total=%s",
            state["turn_id"],
            state.get("step_count", 0) + 1,
            model_name,
            response_usage["prompt_tokens"],
            response_usage["completion_tokens"],
            response_usage["total_tokens"],
            token_usage["total_tokens"] if token_usage else 0,
        )

    ai_message = _llm_response_to_ai_message(response)
    if ai_message.tool_calls:
        logger.info(
            "llm proposed tool calls turn_id=%s step=%s tools=%s",
            state["turn_id"],
            state.get("step_count", 0) + 1,
            [
                {
                    "id": tool_call["id"],
                    "name": tool_call["name"],
                    "args": tool_call["args"],
                }
                for tool_call in ai_message.tool_calls
            ],
        )
    return {
        **state,
        "turn_id": state["turn_id"],
        "cancelled": False,
        "status": "running",
        "messages": messages + [ai_message],
        "step_count": state.get("step_count", 0) + 1,
        "token_usage": token_usage,
        "model": model_name,
    }
def _execute_single_tool(
    tool_name: str,
    tool_args: dict[str, Any],
    *,
    turn_id: int = 0,
    tool_call_id: str = "",
) -> ToolExecutionOutcome:
    tool = get_tool_definition(tool_name)
    result = execute_tool(tool, tool_args, timeout_seconds=30)
    artifacts = _tool_result_artifacts(
        result.tool_artifacts,
        result.artifacts,
        turn_id=turn_id,
        tool_call_id=tool_call_id,
        source_tool=tool_name,
        tool_args=tool_args,
    )
    if result.ok:
        return ToolExecutionOutcome(
            ok=True,
            output=result.stdout or result.summary or "Completed successfully.",
            artifacts=artifacts,
        )
    if tool_name == "delegate_to_codex":
        output = result.stdout or result.summary or result.stderr
        if output:
            return ToolExecutionOutcome(ok=True, output=output, artifacts=artifacts)
    return ToolExecutionOutcome(
        ok=False,
        output=f"Error (exit_code={result.exit_code}): {result.stderr or result.summary}",
        artifacts=artifacts,
    )


def _tool_result_artifacts(
    tool_artifacts: list[ToolArtifact],
    legacy_artifacts: list[str],
    *,
    turn_id: int,
    tool_call_id: str,
    source_tool: str,
    tool_args: dict[str, Any],
) -> tuple[ToolArtifact, ...]:
    artifacts: list[ToolArtifact] = [
        _bind_tool_artifact_to_turn(
            artifact,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            source_tool=source_tool,
        )
        for artifact in tool_artifacts
    ]
    base_dir = _artifact_base_dir(tool_args)
    for legacy in legacy_artifacts:
        artifact = legacy_artifact_to_tool_artifact(
            legacy,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            source_tool=source_tool,
            base_dir=base_dir,
        )
        if artifact is not None:
            artifacts.append(artifact)
    deduped: list[ToolArtifact] = []
    seen: set[str] = set()
    for artifact in artifacts:
        if artifact.artifact_id in seen:
            continue
        seen.add(artifact.artifact_id)
        deduped.append(artifact)
    return tuple(deduped)


def _bind_tool_artifact_to_turn(
    artifact: ToolArtifact,
    *,
    turn_id: int,
    tool_call_id: str,
    source_tool: str,
) -> ToolArtifact:
    updates: dict[str, Any] = {}
    if artifact.turn_id is None:
        updates["turn_id"] = turn_id
    if not artifact.tool_call_id:
        updates["tool_call_id"] = tool_call_id
    if not artifact.source_tool:
        updates["source_tool"] = source_tool
    if not updates:
        return artifact
    return replace(artifact, **updates)


def _artifact_base_dir(tool_args: dict[str, Any]) -> Path | None:
    repo_id = str(tool_args.get("repo_id") or "").strip()
    if repo_id:
        try:
            return get_repository_registry().resolve_repo(repo_id).canonical_root_path
        except RepositoryRegistryError:
            return None
    workdir = str(tool_args.get("workdir") or "").strip()
    if workdir:
        try:
            return Path(workdir).resolve(strict=True)
        except OSError:
            return None
    return get_settings().workspace_root


def _codex_trusted_approval_prefixes(store: TurnStore, conversation_id: int) -> list[str]:
    conversation = store.get_conversation(conversation_id)
    if conversation is None:
        return []
    metadata = getattr(conversation, "metadata", None) or {}
    prefixes = metadata.get("codex_approval_prefixes")
    if not isinstance(prefixes, list):
        return []
    return [str(item) for item in prefixes if str(item).strip()]


def _inject_tool_runtime_context(
    tool_name: str,
    tool_args: dict[str, Any],
    *,
    turn: TurnRecord,
    store: TurnStore,
) -> dict[str, Any]:
    if tool_name not in {"scheduled_task", "deliver_file"}:
        return tool_args

    injected = dict(tool_args)
    injected.setdefault("conversation_id", getattr(turn, "conversation_id", None))
    injected.setdefault("turn_id", getattr(turn, "id", None))
    injected.setdefault("created_by_user_id", getattr(turn, "started_by_user_id", None))
    conversation = store.get_conversation(turn.conversation_id)
    if conversation is not None:
        injected.setdefault("platform", getattr(conversation, "platform", None))
        injected.setdefault("external_chat_id", getattr(conversation, "external_chat_id", None))
    return injected


def _tools_granted_by_tool_search(
    output: str,
    messages: list[BaseMessage],
    *,
    original_user_request: str | None = None,
) -> list[str]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict) or payload.get("status") != "found":
        return []
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return []
    latest_user_text = (original_user_request or "").strip() or _latest_human_message_text(messages)
    granted: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        tool_name = str(candidate.get("tool_name") or "").strip()
        if tool_name in granted:
            continue
        if _tool_search_candidate_allowed(tool_name, latest_user_text):
            granted.append(tool_name)
        if len(granted) >= 3:
            break
    return granted


def _skill_names_loaded_by_guidance(output: str, messages: list[BaseMessage]) -> list[str]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict) or payload.get("status") != "loaded":
        return []
    skills = payload.get("skills")
    if not isinstance(skills, list):
        return []

    existing_content = "\n".join(str(message.content or "") for message in messages)
    names: list[str] = []
    for item in skills:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in names:
            continue
        if f"[Skill: {name}]" in existing_content:
            continue
        names.append(name)
        if len(names) >= 3:
            break
    return names


def _tool_search_candidate_allowed(tool_name: str, user_text: str) -> bool:
    if tool_name not in _TOOL_SEARCH_GRANTABLE_TOOLS:
        return False
    text = user_text.lower()
    if tool_name == "scheduled_task":
        return _looks_like_reminder_request(text)
    if tool_name == "deliver_file":
        return _looks_like_file_delivery_request(text)
    if tool_name == "tavily_search":
        return _looks_like_web_request(text)
    if tool_name in {"obsidian_wiki_draft", "obsidian_wiki_apply"}:
        return _looks_like_wiki_write_request(text)
    if tool_name in {"obsidian_wiki_query", "business_knowledge_search"}:
        return _looks_like_memory_or_knowledge_request(text)
    return False


def _looks_like_reminder_request(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "提醒",
            "remind",
            "notify me",
            "叫醒",
            "起床",
            "稍后通知",
            "到点",
            "定时",
            "分钟后",
            "小时后",
            "明天",
            "tomorrow",
        )
    )


def _looks_like_file_delivery_request(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "发给我",
            "发送",
            "重发",
            "重新发",
            "再发",
            "上传",
            "交付",
            "deliver",
            "send me",
            "resend",
            "upload",
        )
    ) and any(marker in text for marker in ("文件", "图片", "图", "artifact", "file", "image", ".png", ".jpg", ".svg", ".pdf"))


def _looks_like_web_request(text: str) -> bool:
    return any(marker in text for marker in ("latest", "recent", "today", "current news", "最新", "最近", "新闻", "网上", "网页搜索")) or _looks_like_social_search_request(text)


def _looks_like_social_search_request(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "x/twitter",
            "twitter",
            "tweet",
            "tweets",
            "x post",
            "x posts",
            "on x",
            "社交舆情",
            "推特",
            "推文",
            "x上",
            "x 上",
            "大家怎么说",
            "网友怎么说",
        )
    )


def _looks_like_wiki_write_request(text: str) -> bool:
    return any(marker in text for marker in ("写入wiki", "写到wiki", "write to wiki", "沉淀", "记录到知识库", "保存到知识库"))


def _looks_like_memory_or_knowledge_request(text: str) -> bool:
    return any(marker in text for marker in ("wiki", "知识库", "长期记忆", "之前", "设计记录", "业务知识", "公司知识", "研报", "财报"))


def _tool_output_payload(output: str) -> dict[str, Any]:
    trimmed = output[:2000]
    return {"result": trimmed} if trimmed else {}


def _serialize_tool_calls(message: AIMessage) -> list[dict[str, Any]]:
    return [
        {
            "id": tool_call["id"],
            "name": tool_call["name"],
            "args": tool_call["args"],
        }
        for tool_call in message.tool_calls
    ]


def _next_step_index(store: TurnStore, turn_id: int) -> int:
    records = store.list_tool_calls_by_turn(turn_id)
    if not records:
        return 1
    return max(int(getattr(record, "step_index", 0) or 0) for record in records) + 1


def execute_tools(state: ReActState, store: TurnStore) -> ReActState:
    if _is_turn_cancelled(store, state["turn_id"]):
        return {
            **state,
            "cancelled": True,
            "status": "cancelled",
        }

    last_message = state["messages"][-1]
    if not isinstance(last_message, AIMessage):
        return state

    tool_messages: list[BaseMessage] = []
    skill_reminder_messages: list[BaseMessage] = []
    collected_artifacts: list[ToolArtifact] = []
    granted_tools: list[str] = []
    turn = store.get_turn(state["turn_id"])
    if turn is None:
        raise ValueError(f"Turn not found: {state['turn_id']}")
    step_index = _next_step_index(store, state["turn_id"])
    existing_tool_calls = store.list_tool_calls_by_turn(state["turn_id"])
    tavily_calls_used = sum(1 for record in existing_tool_calls if getattr(record, "tool_name", None) == "tavily_search")
    allowed_tools = set(state.get("allowed_tools") or [])
    search_budget = state.get("search_budget")
    tavily_budget = _MAX_TAVILY_CALLS_PER_TURN if search_budget is None else max(int(search_budget), 0)
    raw_payload: dict[str, Any] = {
        "source": "agent_react.tool_call",
        "tool_calls": _serialize_tool_calls(last_message),
        "step_index": step_index,
    }
    # DeepSeek thinking mode requires reasoning_content to be passed back in subsequent requests.
    reasoning = last_message.response_metadata.get("reasoning_content") if last_message.response_metadata else None
    if reasoning:
        raw_payload["reasoning_content"] = reasoning
    assistant_message = store.append_assistant_message(
        conversation_id=turn.conversation_id,
        turn_id=state["turn_id"],
        content=str(last_message.content or ""),
        content_type="markdown",
        raw_payload=raw_payload,
    )
    for tool_call in last_message.tool_calls:
        tool_name = tool_call["name"]
        tool_args = tool_call["args"]
        if tool_name == "delegate_to_codex":
            tool_args = _strengthen_codex_contract(dict(tool_args), state["messages"])
        tool_args = _inject_tool_runtime_context(tool_name, dict(tool_args), turn=turn, store=store)
        tool_call_id = tool_call["id"]
        record = None
        outcome = ToolExecutionOutcome(ok=False, output="")
        logger.info(
            "tool execution requested turn_id=%s step=%s tool=%s tool_call_id=%s args=%s",
            state["turn_id"],
            step_index,
            tool_name,
            tool_call_id,
            tool_args,
        )

        try:
            record = store.create_tool_call(
                turn_id=state["turn_id"],
                tool_name=tool_name,
                input=tool_args,
                assistant_message_id=getattr(assistant_message, "id", None),
                provider_tool_call_id=tool_call_id,
                step_index=step_index,
            )
            if allowed_tools and tool_name not in allowed_tools:
                tool = None
                rejection = f"Rejected: tool not allowed by runtime policy: {tool_name}"
            else:
                tool = get_tool_definition(tool_name)
                rejection = check_tool_policy(tool, tool_args, state["messages"])
            if rejection is None and tool_name == "tavily_search" and tavily_calls_used >= tavily_budget:
                rejection = (
                    "Rejected: tavily_search budget exceeded for this turn. "
                    "Use the results already gathered and respond to the user."
                )
            if rejection is not None:
                output = rejection
                logger.info(
                    "tool execution rejected turn_id=%s step=%s tool=%s tool_call_id=%s reason=%s",
                    state["turn_id"],
                    step_index,
                    tool_name,
                    tool_call_id,
                    rejection,
                )
                store.update_tool_call(
                    record.id,
                    status="rejected",
                    output=_tool_output_payload(output),
                    error_message=rejection,
                )
            else:
                store.update_tool_call(record.id, status="running")
                if tool is None:
                    raise ValueError(f"unknown tool: {tool_name}")
                execution_args = dict(tool_args)
                if tool_name == "delegate_to_codex":
                    execution_args["_trusted_codex_approval_prefixes"] = _codex_trusted_approval_prefixes(
                        store,
                        turn.conversation_id,
                    )
                outcome = _execute_single_tool(
                    tool_name,
                    execution_args,
                    turn_id=state["turn_id"],
                    tool_call_id=tool_call_id,
                )
                ok = outcome.ok
                output = outcome.output
                if outcome.artifacts:
                    collected_artifacts.extend(outcome.artifacts)
                    _persist_tool_artifacts(store, turn.conversation_id, outcome.artifacts)
                    logger.info(
                        "tool artifacts collected turn_id=%s step=%s tool=%s tool_call_id=%s artifact_count=%s",
                        state["turn_id"],
                        step_index,
                        tool_name,
                        tool_call_id,
                        len(outcome.artifacts),
                    )
                if ok and tool_name == "tool_search":
                    new_grants = _tools_granted_by_tool_search(
                        output,
                        state["messages"],
                        original_user_request=str(tool_args.get("original_user_request") or ""),
                    )
                    logger.info(
                        "tool_search grant evaluation turn_id=%s step=%s tool_call_id=%s granted_tools=%s output_preview=%s",
                        state["turn_id"],
                        step_index,
                        tool_call_id,
                        new_grants,
                        repr(output[:300]),
                    )
                    granted_tools.extend(new_grants)
                if ok and tool_name == "load_skill_guidance":
                    skill_names = _skill_names_loaded_by_guidance(
                        output,
                        [*state["messages"], *tool_messages, *skill_reminder_messages],
                    )
                    skill_reminder = _CONTEXT_MANAGER.build_skill_reminder_message(skill_names)
                    if skill_reminder is not None:
                        skill_reminder_messages.append(skill_reminder)
                        logger.info(
                            "skill guidance injected turn_id=%s step=%s tool_call_id=%s skills=%s",
                            state["turn_id"],
                            step_index,
                            tool_call_id,
                            skill_names,
                        )
                if not ok:
                    logger.warning(
                        "tool execution failed turn_id=%s step=%s tool=%s tool_call_id=%s output_preview=%s",
                        state["turn_id"],
                        step_index,
                        tool_name,
                        tool_call_id,
                        repr(output[:300]),
                    )
                    store.update_tool_call(
                        record.id,
                        status="failed",
                        output=_tool_output_payload(output),
                        error_message=output,
                    )
                else:
                    if tool_name == "tavily_search":
                        tavily_calls_used += 1
                    logger.info(
                        "tool execution completed turn_id=%s step=%s tool=%s tool_call_id=%s output_preview=%s",
                        state["turn_id"],
                        step_index,
                        tool_name,
                        tool_call_id,
                        repr(output[:300]),
                    )
                    store.update_tool_call(
                        record.id,
                        status="completed",
                        output=_tool_output_payload(output),
                    )
        except Exception as exc:
            output = f"Tool execution error: {exc}"
            logger.exception("tool execution failed tool=%s", tool_name)
            if record is None:
                try:
                    record = store.create_tool_call(
                        turn_id=state["turn_id"],
                        tool_name=tool_name,
                        input=tool_args,
                        assistant_message_id=getattr(assistant_message, "id", None),
                        provider_tool_call_id=tool_call_id,
                        step_index=step_index,
                    )
                except Exception:
                    logger.exception("tool call audit create failed tool=%s", tool_name)
                    record = None
            if record is not None:
                try:
                    store.update_tool_call(
                        record.id,
                        status="failed",
                        output=_tool_output_payload(output),
                        error_message=output,
                    )
                except Exception:
                    logger.exception("tool call audit update failed tool=%s", tool_name)

        store.append_tool_message(
            conversation_id=turn.conversation_id,
            turn_id=state["turn_id"],
            content=output,
            content_type="text",
            raw_payload={
                "source": "agent_react.tool_result",
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "assistant_message_id": getattr(assistant_message, "id", None),
                "step_index": step_index,
                "artifacts": [artifact_to_payload(item) for item in outcome.artifacts],
            },
        )
        tool_messages.append(ToolMessage(content=output, tool_call_id=tool_call_id))

    allowed_tools = list(state.get("allowed_tools") or [])
    added_tools: list[str] = []
    for tool_name in granted_tools:
        if tool_name not in allowed_tools:
            allowed_tools.append(tool_name)
            added_tools.append(tool_name)
    if added_tools:
        added_conversation_tools = append_conversation_tool_intents(store, turn.conversation_id, added_tools)
        logger.info(
            "runtime allowed_tools expanded turn_id=%s granted_tools=%s allowed_tools=%s conversation_added_tools=%s",
            state["turn_id"],
            added_tools,
            allowed_tools,
            added_conversation_tools,
        )

    return {
        **state,
        "turn_id": state["turn_id"],
        "cancelled": False,
        "status": "running",
        "messages": state["messages"] + tool_messages + skill_reminder_messages,
        "artifacts": [*state.get("artifacts", []), *collected_artifacts],
        "step_count": state.get("step_count", 0),
        "allowed_tools": allowed_tools,
    }


def _persist_tool_artifacts(store: TurnStore, conversation_id: int, artifacts: tuple[ToolArtifact, ...]) -> None:
    upsert = getattr(store, "upsert_artifact", None)
    if upsert is None:
        return
    for artifact in artifacts:
        try:
            upsert(artifact, conversation_id=conversation_id)
        except Exception:
            logger.exception(
                "artifact persistence failed conversation_id=%s artifact_id=%s",
                conversation_id,
                artifact.artifact_id,
            )


def should_continue(state: ReActState) -> str:
    if state.get("cancelled"):
        return END
    max_steps = int(state.get("max_steps") or _MAX_REACT_STEPS)
    if state.get("step_count", 0) >= max_steps:
        logger.warning("react max steps reached turn_id=%s step_count=%s", state["turn_id"], state.get("step_count", 0))
        return END
    last_message = state["messages"][-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return "execute_tools"
    return END


def build_react_graph(store: TurnStore) -> StateGraph:
    builder = StateGraph(ReActState)
    builder.add_node("call_llm", lambda state: call_llm(state, store))
    builder.add_node("execute_tools", lambda state: execute_tools(state, store))
    builder.set_entry_point("call_llm")
    builder.add_conditional_edges(
        "call_llm",
        should_continue,
        {"execute_tools": "execute_tools", END: END},
    )
    builder.add_edge("execute_tools", "call_llm")
    return builder.compile()
