from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.llm.client import parse_json_content
from app.llm.model_profiles import LLMNode
from app.llm.model_router import ModelRouter
from app.prompting import PromptRegistry
from app.runtime_usage import usage_record_from_response
from app.task_runtime.approval_types import approval_request_dicts
from app.task_runtime.node_result import ExecutionReport, NodeResult
from app.task_runtime.planner import ExecutionPlan
from app.task_runtime.runtime_context import RuntimeContext

logger = logging.getLogger(__name__)

AggregationStatus = Literal["completed", "needs_user_input", "failed"]

class AggregationResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: AggregationStatus
    reply: str
    artifact_refs: list[str] = Field(default_factory=list)
    approval_requests: list[dict[str, Any]] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    usage_records: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("reply")
    @classmethod
    def _reply_not_empty(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("reply must not be empty")
        return text

    @field_validator("artifact_refs")
    @classmethod
    def _dedupe_text_list(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        return result


class ResultAggregator:
    """Fixed system step that turns node results into the turn outcome."""

    def __init__(
        self,
        *,
        prompt_registry: PromptRegistry | None = None,
        prompt_version: str | None = None,
        model_resolver=None,
    ) -> None:
        self._prompt_registry = prompt_registry or PromptRegistry()
        self._prompt_version = prompt_version
        self._model_resolver = model_resolver or (lambda metadata: ModelRouter().resolve(LLMNode.SUMMARY, metadata))

    def prompt_metadata(self) -> dict[str, Any]:
        return self._prompt_registry.load("result_aggregator", self._prompt_version).metadata()

    def aggregate(
        self,
        *,
        plan: ExecutionPlan,
        report: ExecutionReport,
        current_user_input: str | None = None,
        route: str | None = None,
        fast_intent: dict[str, Any] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        runtime_hints: dict[str, Any] | None = None,
        runtime_context: RuntimeContext | None = None,
        instructions: list[str] | None = None,
        conversation_metadata: dict[str, Any] | None = None,
    ) -> AggregationResult:
        started = time.perf_counter()
        fallback = _fallback_aggregation(plan=plan, report=report)
        local_result = _local_aggregation_result(plan=plan, report=report, fallback=fallback)
        if local_result is not None:
            logger.info(
                "result aggregator llm skipped reason=local_finalization mode=%s report_status=%s result_status=%s elapsed_ms=%s",
                plan.finalization_hint.mode,
                report.status,
                local_result.status,
                int((time.perf_counter() - started) * 1000),
            )
            return local_result
        try:
            resolved = self._model_resolver(conversation_metadata)
        except Exception:
            logger.exception("result aggregator model resolution failed")
            return fallback
        if not resolved.profile.api_key:
            logger.info("result aggregator llm skipped reason=missing_api_key profile=%s", getattr(resolved.profile, "id", None))
            return fallback

        prompt = self._prompt_registry.load("result_aggregator", self._prompt_version)
        response_format = prompt.response_format if resolved.profile.supports_json_object else None
        resolved_runtime_context = runtime_context or RuntimeContext.from_hints(runtime_hints)
        payload = _aggregation_input(
            plan=plan,
            report=report,
            current_user_input=current_user_input,
            route=route,
            fast_intent=fast_intent,
            artifacts=artifacts or [],
            legacy_hints=resolved_runtime_context.to_legacy_hints(),
            instructions=instructions or [],
        )
        try:
            logger.info(
                "result aggregator llm request report_status=%s node_count=%s finalization=%s response_format=%s",
                report.status,
                len(report.node_results),
                plan.finalization_hint.mode,
                response_format,
            )
            response = resolved.client.chat_normalized(
                prompt.render({"input_json": json.dumps(payload, ensure_ascii=False)}),
                response_format=response_format,
            )
            parsed = parse_json_content({"content": response.content})
            result = _aggregation_from_payload(parsed, fallback=fallback, report=report)
            usage_record = usage_record_from_response(response, stage="result_aggregator")
            if usage_record is not None:
                result.usage_records.append(usage_record)
            logger.info(
                "result aggregator llm completed status=%s reply_len=%s artifact_refs=%s elapsed_ms=%s",
                result.status,
                len(result.reply),
                result.artifact_refs,
                int((time.perf_counter() - started) * 1000),
            )
            return result
        except Exception:
            logger.exception("result aggregator llm failed")
            return fallback


def _aggregation_input(
    *,
    plan: ExecutionPlan,
    report: ExecutionReport,
    current_user_input: str | None,
    route: str | None,
    fast_intent: dict[str, Any] | None,
    artifacts: list[dict[str, Any]],
    legacy_hints: dict[str, Any],
    instructions: list[str],
) -> dict[str, Any]:
    return {
        "current_user_input": current_user_input or plan.user_objective,
        "route": route,
        "fast_intent": fast_intent,
        "finalization_hint": plan.finalization_hint.model_dump(mode="json"),
        "user_objective": plan.user_objective,
        "plan": plan.model_dump(mode="json"),
        "execution_report": report.model_dump(mode="json", exclude_none=True),
        "artifacts": artifacts,
        "runtime_hints": legacy_hints,
        "instructions": instructions,
    }


def _local_aggregation_result(
    *,
    plan: ExecutionPlan,
    report: ExecutionReport,
    fallback: AggregationResult,
) -> AggregationResult | None:
    hint = plan.finalization_hint
    if report.status == "blocked":
        return fallback
    if report.status != "completed":
        return None
    if hint.mode != "pass_through":
        return None
    if not hint.user_facing:
        return None
    if len(report.node_results) != 1:
        return None

    result = report.node_results[0]
    if result.status != "completed":
        return None
    reply = _pass_through_reply(result)
    if not reply:
        return None
    return AggregationResult(
        status="completed",
        reply=reply,
        artifact_refs=_artifact_refs(report.node_results),
        data={
            "finalization": "pass_through",
            "fallback": False,
        },
    )


def _pass_through_reply(result: NodeResult) -> str:
    explicit = _first_text(result.data, ("reply", "final_answer", "answer", "result"))
    if explicit:
        return explicit
    structured = _structured_data_reply(result)
    if structured:
        return structured
    return result.summary.strip()


def _structured_data_reply(result: NodeResult) -> str:
    if result.runtime != "llm" or not result.data:
        return ""
    rendered_fields: list[str] = []
    for key, value in result.data.items():
        if key in {"reply", "final_answer", "answer", "result", "tool_calls"}:
            continue
        rendered = _render_data_field(key, value)
        if rendered:
            rendered_fields.append(rendered)
    if not rendered_fields:
        return ""
    parts: list[str] = []
    summary = result.summary.strip()
    if summary:
        parts.append(summary)
    parts.extend(rendered_fields)
    text = "\n\n".join(parts).strip()
    if summary and text == summary:
        return ""
    return text


def _render_data_field(key: str, value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        return f"**{_field_label(key)}**：{text}" if text else ""
    if isinstance(value, list):
        return _render_list_field(key, value)
    if isinstance(value, dict):
        lines: list[str] = []
        for item_key, item_value in value.items():
            rendered = _render_scalar(item_value)
            if rendered:
                lines.append(f"- **{_field_label(str(item_key))}**：{rendered}")
        if lines:
            return f"**{_field_label(key)}**\n" + "\n".join(lines)
    return ""


def _render_list_field(key: str, value: list[Any]) -> str:
    lines: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if text:
                lines.append(f"- {text}")
        elif isinstance(item, dict):
            label = _first_text(item, ("factor", "title", "name", "topic", "key"))
            detail = _first_text(item, ("explanation", "summary", "description", "value", "result", "answer"))
            if label and detail:
                lines.append(f"- **{label}**：{detail}")
            elif label:
                lines.append(f"- **{label}**")
            elif detail:
                lines.append(f"- {detail}")
    if not lines:
        return ""
    return f"**{_field_label(key)}**\n" + "\n".join(lines)


def _render_scalar(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _first_text(value: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
    return ""


def _field_label(key: str) -> str:
    labels = {
        "primary_factors": "主要因素",
        "typical_pattern": "典型走势",
        "current_context": "当前判断",
        "findings": "要点",
        "sources": "来源",
    }
    return labels.get(key, key.replace("_", " "))


def _aggregation_from_payload(
    payload: dict[str, Any],
    *,
    fallback: AggregationResult,
    report: ExecutionReport,
) -> AggregationResult:
    if not isinstance(payload, dict):
        return fallback
    normalized = dict(payload)
    status = normalized.get("status")
    if status not in {"completed", "needs_user_input", "failed"}:
        normalized["status"] = fallback.status
    normalized.setdefault("reply", fallback.reply)
    normalized.setdefault("artifact_refs", _artifact_refs(report.node_results))
    normalized.setdefault("data", {})
    try:
        return AggregationResult.model_validate(normalized)
    except ValidationError as exc:
        logger.warning(
            "result aggregator returned invalid payload; using fallback errors=%s payload=%s",
            exc.errors(),
            _preview_json(normalized),
        )
        return fallback


def _fallback_aggregation(*, plan: ExecutionPlan, report: ExecutionReport) -> AggregationResult:
    refs = _artifact_refs(report.node_results)
    if report.status == "completed":
        return AggregationResult(
            status="completed",
            reply=_completed_reply(plan, report),
            artifact_refs=refs,
            data={"fallback": True},
        )
    blocked = [result for result in report.node_results if result.status == "blocked"]
    if blocked and not any(result.status == "failed" for result in report.node_results):
        approval_requests = _approval_requests(blocked)
        question = _blocked_question(blocked, approval_requested=bool(approval_requests))
        return AggregationResult(
            status="needs_user_input",
            reply=question,
            artifact_refs=refs,
            approval_requests=approval_requests,
            data={"fallback": True},
        )
    failed = [result for result in report.node_results if result.status == "failed"]
    return AggregationResult(
        status="failed",
        reply=_failure_reply(failed or report.node_results),
        artifact_refs=refs,
        data={"fallback": True},
    )


def _completed_reply(plan: ExecutionPlan, report: ExecutionReport) -> str:
    summaries = [result.summary.strip() for result in report.node_results if result.summary.strip()]
    if not summaries:
        return f"已完成：{plan.user_objective}"
    if len(summaries) == 1:
        return summaries[0]
    lines = ["已完成，结果如下："]
    lines.extend(f"- {summary}" for summary in summaries)
    return "\n".join(lines)


def _blocked_question(blocked: list[NodeResult], *, approval_requested: bool = False) -> str:
    first = blocked[0]
    if approval_requested:
        return "该操作需要确认后继续。"
    message = first.summary or (first.error.message if first.error else "")
    if first.error and first.error.code == "missing_active_repo":
        return "需要先指定要操作的仓库。"
    if first.error and first.error.code == "missing_api_key":
        return "当前运行所需的 LLM API key 未配置，无法继续执行。"
    return message or "当前任务缺少必要输入，需要补充信息后继续。"


def _approval_requests(results: list[NodeResult]) -> list[dict[str, Any]]:
    raw_requests: list[Any] = []
    for result in results:
        if result.approval_requests:
            raw_requests.extend(result.approval_requests)
    return approval_request_dicts(raw_requests)


def _failure_reply(results: list[NodeResult]) -> str:
    first = results[0]
    message = first.summary or (first.error.message if first.error else "")
    return message or "任务执行失败。"


def _artifact_refs(results: list[NodeResult]) -> list[str]:
    refs: list[str] = []
    for result in results:
        for artifact in result.artifacts:
            ref = f"artifact:{artifact.ref}"
            if ref not in refs:
                refs.append(ref)
    return refs


def _preview_json(value: Any, *, limit: int = 2000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"
