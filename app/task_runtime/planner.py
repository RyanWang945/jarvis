from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.agent_react.context_manager import ConversationContext
from app.agent_react.session_state import ConversationSessionState
from app.config import get_settings
from app.llm.client import LLMMessage, parse_json_content
from app.llm.model_profiles import LLMNode
from app.llm.model_router import ModelRouter
from app.prompting import PromptRegistry

logger = logging.getLogger(__name__)

NodeRuntime = Literal["llm", "react", "codex", "tool", "deepresearch"]
FinalizationMode = Literal["pass_through", "deterministic", "llm", "auto"]
DEFAULT_PLANNER_PROMPT_VERSION = "v2"

_RUNTIMES = {"llm", "react", "codex", "tool", "deepresearch"}


class PlanNode(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    runtime: NodeRuntime
    objective: str
    input_refs: list[str] = Field(default_factory=list)
    expected_output: str = ""
    tool_name: str | None = None

    @field_validator("id", "objective")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("value must not be empty")
        return text

    @field_validator("input_refs")
    @classmethod
    def _normalize_input_refs(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for item in value:
            text = str(item).strip()
            if _is_valid_input_ref(text) and text not in result:
                result.append(text)
        return result

    @field_validator("expected_output")
    @classmethod
    def _expected_output_text(cls, value: str) -> str:
        return str(value or "").strip()

    @model_validator(mode="after")
    def _validate_runtime_contract(self) -> PlanNode:
        if self.runtime == "tool":
            if not self.tool_name:
                raise ValueError("tool nodes require tool_name")
        elif self.tool_name:
            raise ValueError("tool_name is only valid when runtime is tool")
        return self


class FinalizationHint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: FinalizationMode = "auto"
    reason: str = ""
    user_facing: bool = False

    @field_validator("reason")
    @classmethod
    def _reason_text(cls, value: str) -> str:
        return str(value or "").strip()


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="ignore") # pydantic设置，如果有多余的字段，需要忽略。

    user_objective: str
    nodes: list[PlanNode]
    # 执行完任务后，最终回复如何收口，这个是ai生成的，planner LLM 的 system prompt 里设置的
    finalization_hint: FinalizationHint = Field(default_factory=FinalizationHint)

    @field_validator("user_objective")
    @classmethod
    def _objective_not_empty(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("user_objective must not be empty")
        return text

    @model_validator(mode="after")
    def _validate_graph_refs(self) -> ExecutionPlan:
        if not self.nodes:
            raise ValueError("ExecutionPlan requires at least one node")
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("ExecutionPlan node ids must be unique")
        known = set(node_ids)
        deps: dict[str, set[str]] = {}
        for node in self.nodes:
            node_refs = {ref.removeprefix("node:") for ref in node.input_refs if ref.startswith("node:")}
            if node.id in node_refs:
                raise ValueError(f"node {node.id} cannot reference itself")
            deps[node.id] = node_refs & known
        _assert_acyclic(deps)
        return self


class PlanInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    current_user_input: str
    conversation_context: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    previous_node_results: list[dict[str, Any]] = Field(default_factory=list)
    runtime_hints: dict[str, Any] = Field(default_factory=dict)
    instructions: list[str] = Field(default_factory=list)

    @field_validator("current_user_input")
    @classmethod
    def _input_not_empty(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("current_user_input must not be empty")
        return text


class TurnPlanner:
    """LLM-backed planner that compiles one user turn into a lightweight plan."""

    def __init__(self, *, prompt_registry: PromptRegistry | None = None, prompt_version: str | None = None) -> None:
        self._prompt_registry = prompt_registry or PromptRegistry()
        self._prompt_version = prompt_version or DEFAULT_PLANNER_PROMPT_VERSION

    def prompt_metadata(self) -> dict[str, Any]:
        return self._prompt_registry.load("heavy_plan", self._prompt_version).metadata()

    def plan(
        self,
        *,
        content: str,
        session_state: ConversationSessionState | None = None,
        conversation_metadata: dict[str, Any] | None = None,
        recent_artifacts: list[dict[str, Any]] | None = None,
        conversation_context: ConversationContext | None = None,
        previous_node_results: list[dict[str, Any]] | None = None,
        runtime_hints: dict[str, Any] | None = None,
        instructions: list[str] | None = None,
    ) -> ExecutionPlan:
        session = session_state or ConversationSessionState()
        plan_input = build_plan_input(
            current_user_input=content,
            conversation_context=conversation_context,
            artifacts=recent_artifacts or [],
            previous_node_results=previous_node_results or [],
            runtime_hints=runtime_hints,
            session_state=session,
            instructions=instructions or [],
        )
        resolved = ModelRouter().resolve(LLMNode.PLANNER, conversation_metadata)
        if not resolved.profile.api_key:
            logger.info("turn planner llm skipped reason=missing_api_key profile=%s", resolved.profile.id)
            return _fallback_single_node_plan(plan_input.current_user_input)

        prompt = self._prompt_registry.load("heavy_plan", self._prompt_version)
        response_format = prompt.response_format if resolved.profile.supports_json_object else None
        messages = _planner_messages(
            plan_input,
            prompt_registry=self._prompt_registry,
            prompt_version=self._prompt_version,
        )
        logger.info(
            "turn planner llm request profile=%s provider=%s response_format=%s prompt_version=%s input=%s",
            resolved.profile.id,
            resolved.profile.provider,
            response_format,
            self._prompt_version,
            _json_for_log(plan_input.model_dump(mode="json")),
        )
        response = resolved.client.chat_normalized(
            messages,
            response_format=response_format,
        )
        logger.info(
            "turn planner llm raw response model=%s finish_reason=%s usage=%s content_len=%s content=%s",
            response.model,
            response.finish_reason,
            _usage_for_log(response.usage),
            len(response.content or ""),
            response.content,
        )
        payload = parse_json_content({"content": response.content})
        logger.info("turn planner llm payload=%s", _json_for_log(payload))
        plan = _plan_from_payload(
            payload,
            fallback_objective=plan_input.current_user_input,
            known_artifact_refs=_known_artifact_refs(plan_input.artifacts),
        )
        logger.info("turn planner plan output=%s", _json_for_log(plan.model_dump(mode="json")))
        return plan


def build_plan_input(
    *,
    current_user_input: str,
    conversation_context: ConversationContext | None = None,
    artifacts: list[dict[str, Any]],
    previous_node_results: list[dict[str, Any]],
    runtime_hints: dict[str, Any] | None,
    session_state: ConversationSessionState | None = None,
    instructions: list[str] | None = None,
) -> PlanInput:
    hints = dict(runtime_hints or {})
    if session_state is not None and "active_repo" not in hints:
        hints["active_repo"] = session_state.active_repo_id
    hints = _ensure_temporal_hints(hints)
    return PlanInput(
        current_user_input=current_user_input,
        conversation_context=(
            conversation_context.planner_payload()
            if conversation_context is not None
            else {
                "has_history": False,
                "context_reference_detected": False,
                "summary_node": None,
                "messages": [],
            }
        ),
        artifacts=_normalize_artifact_context(artifacts),
        previous_node_results=[item for item in previous_node_results if isinstance(item, dict)],
        runtime_hints=hints,
        instructions=[str(item).strip() for item in (instructions or []) if str(item).strip()],
    )


def _planner_messages(
    plan_input: PlanInput,
    *,
    prompt_registry: PromptRegistry | None = None,
    prompt_version: str | None = None,
) -> list[LLMMessage]:
    registry = prompt_registry or PromptRegistry()
    prompt = registry.load("heavy_plan", prompt_version or DEFAULT_PLANNER_PROMPT_VERSION)
    return prompt.render(
        {
            "input_json": json.dumps(
                plan_input.model_dump(mode="json"),
                ensure_ascii=False,
            )
        }
    )


def _normalize_artifact_context(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, artifact in enumerate(artifacts, start=1):
        if not isinstance(artifact, dict):
            continue
        ref = _artifact_ref(artifact, index)
        item = {
            "ref": ref,
            "kind": _first_text(artifact, "kind", "type", "content_type") or "artifact",
            "name": _first_text(artifact, "name", "filename", "title") or ref,
            "description": _first_text(artifact, "description", "summary") or "",
            "availability": _first_text(artifact, "availability", default="available"),
            "recency": _first_text(artifact, "recency", default="recent"),
            "origin": _first_text(artifact, "origin", "source", default="assistant_generated"),
        }
        normalized.append(item)
    return normalized


def _first_text(payload: dict[str, Any], *keys: str, default: str | None = None) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def _artifact_ref(artifact: dict[str, Any], index: int) -> str:
    raw = _first_text(artifact, "ref", "artifact_ref", "id", "artifact_id")
    if raw:
        return raw.removeprefix("artifact:")
    return f"A{index}"


def _known_artifact_refs(artifacts: list[dict[str, Any]]) -> set[str]:
    refs: set[str] = set()
    for artifact in artifacts:
        ref = artifact.get("ref")
        if isinstance(ref, str) and ref.strip():
            refs.add(f"artifact:{ref.strip().removeprefix('artifact:')}")
    return refs


def _ensure_temporal_hints(hints: dict[str, Any]) -> dict[str, Any]:
    if hints.get("current_date") and hints.get("current_time") and hints.get("timezone"):
        return hints
    timezone_name = str(hints.get("timezone") or get_settings().default_timezone)
    tz = _resolve_timezone(timezone_name)
    current = datetime.now(tz)
    enriched = dict(hints)
    enriched.setdefault("current_date", current.date().isoformat())
    enriched.setdefault("current_time", current.isoformat(timespec="seconds"))
    enriched.setdefault("timezone", timezone_name)
    return enriched


def _resolve_timezone(timezone_name: str):
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        if timezone_name in {"Asia/Shanghai", "Asia/Chongqing"}:
            return timezone(timedelta(hours=8), name=timezone_name)
        return UTC


def _json_for_log(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def _usage_for_log(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    return {
        "prompt_tokens": int(getattr(value, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(value, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(value, "total_tokens", 0) or 0),
    }


def _plan_from_payload(
    payload: dict[str, Any],
    *,
    fallback_objective: str,
    known_artifact_refs: set[str] | None = None,
) -> ExecutionPlan:
    candidate = payload.get("plan") if isinstance(payload.get("plan"), dict) else payload
    if not isinstance(candidate, dict):
        return _fallback_single_node_plan(fallback_objective)
    normalized = _normalize_plan_payload(
        candidate,
        known_artifact_refs=known_artifact_refs,
    )
    normalized.setdefault("user_objective", fallback_objective)
    normalized.setdefault("nodes", [])
    try:
        return ExecutionPlan.model_validate(normalized)
    except ValidationError as exc:
        logger.warning("planner returned invalid ExecutionPlan; falling back error=%s payload_keys=%s", exc, sorted(normalized))
        return _fallback_single_node_plan(fallback_objective)


def _normalize_plan_payload(
    payload: dict[str, Any],
    *,
    known_artifact_refs: set[str] | None = None,
) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["finalization_hint"] = _normalize_finalization_hint(normalized.get("finalization_hint"))
    raw_nodes = normalized.get("nodes")
    if isinstance(raw_nodes, dict):
        raw_nodes = [raw_nodes]
    if isinstance(raw_nodes, list):
        normalized["nodes"] = [
            _normalize_node_payload(
                node,
                known_artifact_refs=known_artifact_refs,
            )
            for node in raw_nodes
            if isinstance(node, dict)
        ]
    normalized["finalization_hint"] = _normalize_finalization_for_nodes(
        normalized["finalization_hint"],
        normalized.get("nodes"),
    )
    return normalized


def _normalize_finalization_hint(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        hint = dict(value)
    elif isinstance(value, str):
        hint = {"mode": value}
    else:
        return {"mode": "auto"}
    mode = str(hint.get("mode") or "auto").strip().lower()
    if mode not in {"pass_through", "deterministic", "llm", "auto"}:
        mode = "auto"
    hint["mode"] = mode
    hint["reason"] = str(hint.get("reason") or "").strip()
    hint["user_facing"] = bool(hint.get("user_facing"))
    return hint


def _normalize_finalization_for_nodes(hint: dict[str, Any], nodes: Any) -> dict[str, Any]:
    if hint.get("mode") != "pass_through" or not isinstance(nodes, list):
        return hint
    if len(nodes) == 1 and nodes[0].get("runtime") == "llm":
        return hint

    adjusted = dict(hint)
    if len(nodes) == 1 and nodes[0].get("runtime") == "tool":
        adjusted["mode"] = "deterministic"
        adjusted["reason"] = _append_reason(
            adjusted.get("reason"),
            "pass_through is only valid for a single llm node; coerced tool node to deterministic finalization",
        )
    else:
        adjusted["mode"] = "llm"
        adjusted["reason"] = _append_reason(
            adjusted.get("reason"),
            "pass_through is only valid for a single llm node; coerced non-llm plan to llm aggregation",
        )
    logger.info(
        "planner finalization coerced original=pass_through new=%s node_runtimes=%s",
        adjusted["mode"],
        [node.get("runtime") for node in nodes],
    )
    return adjusted


def _append_reason(current: Any, addition: str) -> str:
    text = str(current or "").strip()
    if not text:
        return addition
    return f"{text}; {addition}"


def _normalize_node_payload(
    payload: dict[str, Any],
    *,
    known_artifact_refs: set[str] | None = None,
) -> dict[str, Any]:
    node = dict(payload)
    runtime = _normalize_runtime(node.get("runtime"))
    node["runtime"] = runtime

    tool_name = node.get("tool_name")
    if isinstance(tool_name, str):
        node["tool_name"] = tool_name.strip() or None

    expected_output = node.get("expected_output")
    if isinstance(expected_output, dict):
        node["expected_output"] = str(expected_output.get("description") or expected_output.get("kind") or "").strip()
    elif expected_output is None:
        node["expected_output"] = ""
    else:
        node["expected_output"] = str(expected_output).strip()

    node["input_refs"] = _normalize_input_refs(node.get("input_refs"), known_artifact_refs=known_artifact_refs)
    return node


def _normalize_runtime(value: Any) -> NodeRuntime:
    text = str(value or "").strip().lower()
    if text in _RUNTIMES:
        return text  # type: ignore[return-value]
    return text  # type: ignore[return-value]


def _normalize_input_refs(value: Any, *, known_artifact_refs: set[str] | None = None) -> list[str]:
    refs = value if isinstance(value, list) else []
    result: list[str] = []
    for item in refs:
        text = str(item).strip()
        if not text:
            continue
        if not text.startswith(("artifact:", "node:")):
            text = f"artifact:{text}" if known_artifact_refs and f"artifact:{text}" in known_artifact_refs else text
        if not _is_valid_input_ref(text):
            continue
        if text.startswith("artifact:") and known_artifact_refs is not None and text not in known_artifact_refs:
            continue
        if text not in result:
            result.append(text)
    return result


def _is_valid_input_ref(value: str) -> bool:
    if not value.startswith(("artifact:", "node:")):
        return False
    prefix, ref = value.split(":", 1)
    return prefix in {"artifact", "node"} and bool(ref.strip())


def _fallback_single_node_plan(objective: str) -> ExecutionPlan:
    return ExecutionPlan(
        user_objective=objective,
        finalization_hint=FinalizationHint(mode="pass_through", user_facing=True, reason="fallback single LLM node"),
        nodes=[
            PlanNode(
                id="main",
                objective=objective,
                runtime="llm",
                expected_output="User-facing result.",
            )
        ],
    )


def _assert_acyclic(deps: dict[str, set[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        if node_id in visiting:
            raise ValueError("ExecutionPlan must be acyclic")
        visiting.add(node_id)
        for dep in deps[node_id]:
            visit(dep)
        visiting.remove(node_id)
        visited.add(node_id)

    for node in deps:
        visit(node)
