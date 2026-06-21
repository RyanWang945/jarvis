from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
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
from app.runtime_usage import usage_record_from_response
from app.task_runtime.runtime_context import RuntimeContext

logger = logging.getLogger(__name__)

NodeRuntime = Literal["llm", "react", "coder"]
FinalizationMode = Literal["pass_through", "llm"]
_RUNTIMES = {"llm", "react", "coder"}


class PlanNode(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    runtime: NodeRuntime
    objective: str
    input_refs: list[str] = Field(default_factory=list)
    output_hint: str = ""

    @field_validator("runtime", mode="before")
    @classmethod
    def _runtime_text(cls, value: Any) -> NodeRuntime:
        return _normalize_runtime(value)

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

    @field_validator("output_hint")
    @classmethod
    def _output_hint_text(cls, value: str) -> str:
        return str(value or "").strip()


class FinalizationHint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: FinalizationMode = "llm"
    user_facing: bool = False


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")  # pydantic设置，如果有多余的字段，需要忽略。

    user_objective: str
    nodes: list[PlanNode]
    # 执行完任务后，最终回复如何收口；mode 由 runtime 根据 nodes 推导。
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


@dataclass(frozen=True)
class TurnPlannerResult:
    plan: ExecutionPlan
    usage_records: list[dict[str, Any]] = field(default_factory=list)


class PlanInput(BaseModel):
    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    current_user_input: str
    conversation_context: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    previous_node_results: list[dict[str, Any]] = Field(default_factory=list)
    runtime_hints: dict[str, Any] = Field(default_factory=dict)
    runtime_context: RuntimeContext = Field(default_factory=lambda: RuntimeContext.from_hints({}), exclude=True)
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
        self._prompt_version = prompt_version

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
        runtime_context: RuntimeContext | None = None,
        instructions: list[str] | None = None,
    ) -> ExecutionPlan:
        return self.plan_with_usage(
            content=content,
            session_state=session_state,
            conversation_metadata=conversation_metadata,
            recent_artifacts=recent_artifacts,
            conversation_context=conversation_context,
            previous_node_results=previous_node_results,
            runtime_context=runtime_context,
            instructions=instructions,
        ).plan

    def plan_with_usage(
        self,
        *,
        content: str,
        session_state: ConversationSessionState | None = None,
        conversation_metadata: dict[str, Any] | None = None,
        recent_artifacts: list[dict[str, Any]] | None = None,
        conversation_context: ConversationContext | None = None,
        previous_node_results: list[dict[str, Any]] | None = None,
        runtime_context: RuntimeContext | None = None,
        instructions: list[str] | None = None,
    ) -> TurnPlannerResult:
        session = session_state or ConversationSessionState()
        plan_input = build_plan_input(
            current_user_input=content,
            conversation_context=conversation_context,
            artifacts=recent_artifacts or [],
            previous_node_results=previous_node_results or [],
            runtime_context=runtime_context,
            session_state=session,
            instructions=instructions or [],
        )
        resolved = ModelRouter().resolve(LLMNode.PLANNER, conversation_metadata)
        if not resolved.profile.api_key:
            logger.info("turn planner llm skipped reason=missing_api_key profile=%s", resolved.profile.id)
            return TurnPlannerResult(
                plan=_fallback_plan_for_objective(
                    plan_input.current_user_input,
                    runtime_context=plan_input.runtime_context,
                    known_artifact_refs=_known_artifact_refs(plan_input.artifacts),
                    previous_node_results=plan_input.previous_node_results,
                )
                or _fallback_single_node_plan(plan_input.current_user_input)
            )

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
            prompt.version,
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
            runtime_context=plan_input.runtime_context,
            previous_node_results=plan_input.previous_node_results,
        )
        usage_record = usage_record_from_response(response, stage="planner")
        logger.info("turn planner plan output=%s", _json_for_log(plan.model_dump(mode="json")))
        return TurnPlannerResult(plan=plan, usage_records=[usage_record] if usage_record is not None else [])


def build_plan_input(
    *,
    current_user_input: str,
    conversation_context: ConversationContext | None = None,
    artifacts: list[dict[str, Any]],
    previous_node_results: list[dict[str, Any]],
    runtime_context: RuntimeContext | None = None,
    session_state: ConversationSessionState | None = None,
    instructions: list[str] | None = None,
) -> PlanInput:
    resolved_runtime_context = runtime_context or RuntimeContext.from_hints({})
    if session_state is not None and not resolved_runtime_context.repo.active_repo:
        resolved_runtime_context = resolved_runtime_context.with_hints({"active_repo": session_state.active_repo_id})
    resolved_runtime_context = RuntimeContext.from_hints(_ensure_temporal_hints(resolved_runtime_context.to_legacy_hints()))
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
        runtime_hints=resolved_runtime_context.to_legacy_hints(),
        runtime_context=resolved_runtime_context,
        instructions=[str(item).strip() for item in (instructions or []) if str(item).strip()],
    )


def _planner_messages(
    plan_input: PlanInput,
    *,
    prompt_registry: PromptRegistry | None = None,
    prompt_version: str | None = None,
) -> list[LLMMessage]:
    registry = prompt_registry or PromptRegistry()
    prompt = registry.load("heavy_plan", prompt_version)
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
    runtime_context: RuntimeContext | None = None,
    previous_node_results: list[dict[str, Any]] | None = None,
) -> ExecutionPlan:
    resolved_runtime_context = runtime_context or RuntimeContext.from_hints({})
    candidate = payload.get("plan") if isinstance(payload.get("plan"), dict) else payload
    if not isinstance(candidate, dict):
        artifact_delivery = _fallback_artifact_delivery_plan(fallback_objective, known_artifact_refs)
        if artifact_delivery is not None:
            return artifact_delivery
        intent_fallback = _fallback_plan_for_objective(
            fallback_objective,
            runtime_context=resolved_runtime_context,
            known_artifact_refs=known_artifact_refs,
            previous_node_results=previous_node_results,
        )
        if intent_fallback is not None:
            return intent_fallback
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
        artifact_delivery = _fallback_artifact_delivery_plan(fallback_objective, known_artifact_refs)
        if artifact_delivery is not None:
            return artifact_delivery
        intent_fallback = _fallback_plan_for_objective(
            fallback_objective,
            runtime_context=resolved_runtime_context,
            known_artifact_refs=known_artifact_refs,
            previous_node_results=previous_node_results,
        )
        if intent_fallback is not None:
            return intent_fallback
        return _fallback_single_node_plan(fallback_objective)


def _normalize_plan_payload(
    payload: dict[str, Any],
    *,
    known_artifact_refs: set[str] | None = None,
) -> dict[str, Any]:
    normalized = dict(payload)
    raw_finalization_hint = normalized.get("finalization_hint")
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
    normalized["finalization_hint"] = _derive_finalization_hint(
        normalized.get("nodes"),
        raw_finalization_hint,
    )
    return normalized


def _derive_finalization_hint(nodes: Any, raw_hint: Any) -> dict[str, Any]:
    user_facing = _finalization_user_facing(raw_hint)
    if isinstance(nodes, list) and len(nodes) == 1 and isinstance(nodes[0], dict):
        runtime = nodes[0].get("runtime")
        if runtime == "llm":
            return {
                "mode": "pass_through",
                "user_facing": user_facing,
            }
    return {
        "mode": "llm",
        "user_facing": user_facing,
    }


def _finalization_user_facing(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    raw = value.get("user_facing")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "y"}
    return bool(raw)


def _normalize_node_payload(
    payload: dict[str, Any],
    *,
    known_artifact_refs: set[str] | None = None,
) -> dict[str, Any]:
    node = dict(payload)
    runtime = _normalize_runtime(node.get("runtime"))
    node["runtime"] = runtime

    output_hint = node.get("output_hint")
    if isinstance(output_hint, dict):
        node["output_hint"] = str(output_hint.get("description") or output_hint.get("kind") or "").strip()
    elif output_hint is None:
        node["output_hint"] = ""
    else:
        node["output_hint"] = str(output_hint).strip()
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


def _fallback_plan_for_objective(
    objective: str,
    *,
    runtime_context: RuntimeContext | None = None,
    known_artifact_refs: set[str] | None = None,
    previous_node_results: list[dict[str, Any]] | None = None,
) -> ExecutionPlan | None:
    artifact_delivery = _fallback_artifact_delivery_plan(objective, known_artifact_refs)
    if artifact_delivery is not None:
        return artifact_delivery

    active_repo = (runtime_context or RuntimeContext.from_hints({})).repo.active_repo
    repo_task = bool(active_repo and _looks_like_repo_task(objective, active_repo))
    reminder_task = _looks_like_reminder_task(objective)
    previous_refs = _previous_node_refs(previous_node_results)

    if repo_task and reminder_task:
        return ExecutionPlan(
            user_objective=objective,
            finalization_hint=FinalizationHint(
                mode="llm",
                user_facing=False,
            ),
            nodes=[
                PlanNode(
                    id="repo_report",
                    runtime="coder",
                    objective=f"Review the {active_repo} repository for the user request and produce the requested markdown report / 报告: {objective}",
                    input_refs=previous_refs,
                    output_hint="Markdown report / 报告 covering the repository findings.",
                ),
                PlanNode(
                    id="set_reminder",
                    runtime="react",
                    objective=f"Create the requested reminder / 提醒 after the report node completes: {objective}",
                    input_refs=["node:repo_report"],
                    output_hint="Reminder / 提醒 created for the requested time.",
                ),
            ],
        )

    if repo_task and _looks_like_coarse_code_decomposition_task(objective):
        areas = _code_business_areas(objective)
        return _fallback_coarse_code_plan(
            objective=objective,
            active_repo=active_repo,
            areas=areas,
            previous_refs=previous_refs,
        )

    if repo_task:
        return ExecutionPlan(
            user_objective=objective,
            finalization_hint=FinalizationHint(
                mode="llm",
                user_facing=False,
            ),
            nodes=[
                PlanNode(
                    id="repo_task",
                    runtime="coder",
                    objective=f"Use the {active_repo} repository to complete the user request: {objective}",
                    input_refs=previous_refs,
                    output_hint="Repository-grounded result for the user request.",
                )
            ],
        )

    if reminder_task:
        return ExecutionPlan(
            user_objective=objective,
            finalization_hint=FinalizationHint(
                mode="llm",
                user_facing=False,
            ),
            nodes=[
                PlanNode(
                    id="set_reminder",
                    runtime="react",
                    objective=f"Create the requested reminder / 提醒: {objective}",
                    output_hint="Reminder / 提醒 created.",
                )
            ],
        )

    return None


def _fallback_single_node_plan(objective: str) -> ExecutionPlan:
    return ExecutionPlan(
        user_objective=objective,
        finalization_hint=FinalizationHint(mode="pass_through", user_facing=True),
        nodes=[
            PlanNode(
                id="main",
                objective=objective,
                runtime="llm",
                output_hint="User-facing result.",
            )
        ],
    )


def _fallback_artifact_delivery_plan(objective: str, known_artifact_refs: set[str] | None) -> ExecutionPlan | None:
    refs = sorted(ref for ref in (known_artifact_refs or set()) if ref.startswith("artifact:"))
    if not refs or not _looks_like_artifact_delivery(objective):
        return None
    return ExecutionPlan(
        user_objective=objective,
        finalization_hint=FinalizationHint(
            mode="llm",
            user_facing=False,
        ),
        nodes=[
            PlanNode(
                id="deliver_artifact",
                runtime="react",
                objective="Deliver the requested existing artifact to the user by calling the deliver_file tool.",
                input_refs=[refs[0]],
                output_hint="Artifact delivered to the user.",
            )
        ],
    )


def _looks_like_artifact_delivery(objective: str) -> bool:
    text = str(objective or "").strip().lower()
    if not text:
        return False
    delivery_terms = ("发我", "发给我", "发送", "交付", "deliver", "send")
    artifact_terms = ("刚刚", "刚才", "那个", "报告", "文件", "产物", "artifact", "file", "report")
    return any(term in text for term in delivery_terms) and any(term in text for term in artifact_terms)


def _looks_like_repo_task(objective: str, active_repo: str) -> bool:
    text = str(objective or "").strip().lower()
    if not text:
        return False
    repo_markers = (
        active_repo.lower(),
        "repo",
        "repository",
        "project",
        "项目",
        "仓库",
        "代码",
        "planner",
        "runtime",
        "review",
        "重构",
        "评估",
        "调整",
        "报告",
        "markdown",
    )
    return any(marker and marker in text for marker in repo_markers)


def _looks_like_coarse_code_decomposition_task(objective: str) -> bool:
    text = str(objective or "").strip().lower()
    if not text:
        return False
    code_markers = (
        "实现",
        "开发",
        "新增",
        "修改",
        "修复",
        "重构",
        "接入",
        "改造",
        "implement",
        "build",
        "fix",
        "feature",
    )
    broad_markers = (
        "跨业务",
        "不同业务",
        "多个业务",
        "多业务",
        "多模块",
        "不同模块",
        "合并",
        "整合",
        "集成",
        "merge",
        "integrate",
        "integration",
    )
    return any(marker in text for marker in code_markers) and (
        len(_code_business_areas(objective)) >= 2 or any(marker in text for marker in broad_markers)
    )


def _code_business_areas(objective: str) -> list[str]:
    text = str(objective or "").strip().lower()
    candidates = (
        ("会员/积分业务", ("会员", "积分", "member", "points", "loyalty")),
        ("订单业务", ("订单", "order")),
        ("支付/退款业务", ("支付", "退款", "payment", "refund")),
        ("用户/账号业务", ("用户", "账号", "登录", "权限", "user", "account", "auth")),
        ("商品业务", ("商品", "sku", "product")),
        ("库存业务", ("库存", "inventory")),
        ("通知/消息业务", ("通知", "消息", "短信", "email", "notification", "message")),
        ("报表/分析业务", ("报表", "统计", "分析", "report", "analytics")),
        ("任务调度业务", ("调度", "定时", "job", "scheduler")),
        ("检索/RAG业务", ("检索", "知识库", "rag", "search", "embedding")),
        ("工具/路由业务", ("工具", "路由", "tool", "routing")),
        ("planner/runtime业务", ("planner", "runtime", "任务分解", "计划")),
    )
    areas: list[str] = []
    for label, markers in candidates:
        if any(marker in text for marker in markers):
            areas.append(label)
    return areas


def _fallback_coarse_code_plan(
    *,
    objective: str,
    active_repo: str,
    areas: list[str],
    previous_refs: list[str],
) -> ExecutionPlan:
    implementation_areas = areas[:3] or ["主要代码业务"]
    nodes: list[PlanNode] = []
    implementation_refs: list[str] = []
    for index, area in enumerate(implementation_areas, start=1):
        node_id = f"implement_area_{index}"
        implementation_refs.append(f"node:{node_id}")
        nodes.append(
            PlanNode(
                id=node_id,
                runtime="coder",
                objective=(
                    f"在 {active_repo} 仓库按粗粒度业务板块实现 {area} 相关代码改动，"
                    f"保持任务边界在业务能力层，不拆成低层文件操作或单个测试步骤：{objective}"
                ),
                input_refs=previous_refs,
                output_hint=f"{area} 的代码实现说明、关键改动和本节点内完成的必要自检结果。",
            )
        )

    integration_ref = implementation_refs[-1]
    if len(implementation_refs) > 1:
        integration_ref = "node:integrate_business_code"
        nodes.append(
            PlanNode(
                id="integrate_business_code",
                runtime="coder",
                objective=(
                    f"合并 / integrate {active_repo} 仓库中不同业务实现节点的代码结果，处理跨业务接口、数据流和冲突，"
                    f"不要拆成低层文件操作：{objective}"
                ),
                input_refs=implementation_refs,
                output_hint="不同业务代码改动已完成整合，跨业务契约、冲突处理和剩余风险说明清楚。",
            )
        )

    nodes.append(
        PlanNode(
            id="code_review",
            runtime="coder",
            objective=(
                f"Review / 代码审查 {active_repo} 仓库的粗粒度代码改动，聚焦业务正确性、集成风险、回归风险和是否满足用户要求：{objective}"
            ),
            input_refs=[integration_ref],
            output_hint="代码 review 结论、发现的问题、建议修复项和可交付状态。",
        )
    )

    return ExecutionPlan(
        user_objective=objective,
        finalization_hint=FinalizationHint(
            mode="llm",
            user_facing=False,
        ),
        nodes=nodes,
    )


def _looks_like_reminder_task(objective: str) -> bool:
    text = str(objective or "").strip().lower()
    return any(
        marker in text
        for marker in (
            "提醒",
            "remind",
            "reminder",
            "notify",
            "今晚",
            "tomorrow",
            "明天",
            "点",
            "分钟后",
            "小时后",
        )
    )


def _previous_node_refs(previous_node_results: list[dict[str, Any]] | None) -> list[str]:
    refs: list[str] = []
    for item in previous_node_results or []:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("node_id") or "").strip()
        if node_id:
            ref = f"node:{node_id}"
            if ref not in refs:
                refs.append(ref)
    return refs


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
