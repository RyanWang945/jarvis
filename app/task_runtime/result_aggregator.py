from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.config import get_settings
from app.llm.client import parse_json_content
from app.llm.model_profiles import LLMNode
from app.llm.model_router import ModelRouter
from app.observability import add_event, record_exception, set_attributes
from app.prompting import PromptRegistry
from app.runtime_usage import usage_record_from_response
from app.task_runtime.approval_types import approval_request_dicts
from app.task_runtime.node_result import ExecutionReport, NodeResult
from app.task_runtime.planner import ExecutionPlan
from app.task_runtime.runtime_context import RuntimeContext

logger = logging.getLogger(__name__)

AggregationStatus = Literal["completed", "needs_user_input", "failed"]
_CLAUDE_AGGREGATOR_TIMEOUT_SECONDS = 180
_CLAUDE_AGGREGATOR_MAX_TURNS = 1
_EVIDENCE_ARTIFACT_FILENAMES = {"evidence_claims.md"}
_MAX_EVIDENCE_ARTIFACT_BYTES = 128 * 1024

_AGGREGATION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "reply", "artifact_refs", "approval_requests", "data"],
    "properties": {
        "status": {"type": "string", "enum": ["completed", "needs_user_input", "failed"]},
        "reply": {"type": "string"},
        "artifact_refs": {"type": "array", "items": {"type": "string"}},
        "approval_requests": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "data": {"type": "object", "additionalProperties": True},
    },
}


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
        backend: str | None = None,
    ) -> None:
        self._prompt_registry = prompt_registry or PromptRegistry()
        self._prompt_version = prompt_version
        self._model_resolver = model_resolver or (lambda metadata: ModelRouter().resolve(LLMNode.SUMMARY, metadata))
        self._backend = (backend or get_settings().result_aggregator_backend or "llm").strip().lower()

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
        runtime_context: RuntimeContext | None = None,
        instructions: list[str] | None = None,
        conversation_metadata: dict[str, Any] | None = None,
    ) -> AggregationResult:
        started = time.perf_counter()
        fallback = _fallback_aggregation(plan=plan, report=report)
        add_event(
            "aggregation.started",
            **{
                "jarvis.report_status": report.status,
                "jarvis.node_count": len(report.node_results),
                "jarvis.finalization_mode": plan.finalization_hint.mode,
            },
        )
        local_result = _local_aggregation_result(plan=plan, report=report, fallback=fallback)
        if local_result is not None:
            _trace_aggregation_result(
                local_result,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                mode="local",
            )
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
            add_event("aggregation.fallback", **{"jarvis.reason": "model_resolution_failed"})
            return fallback
        if not resolved.profile.api_key:
            logger.info("result aggregator llm skipped reason=missing_api_key profile=%s", getattr(resolved.profile, "id", None))
            add_event(
                "aggregation.fallback",
                **{
                    "jarvis.reason": "missing_api_key",
                    "jarvis.profile": getattr(resolved.profile, "id", None),
                },
            )
            return fallback

        resolved_runtime_context = runtime_context or RuntimeContext.from_hints({})
        payload = _aggregation_input(
            plan=plan,
            report=report,
            current_user_input=current_user_input,
            route=route,
            fast_intent=fast_intent,
            artifacts=artifacts or [],
            runtime_context=resolved_runtime_context.to_legacy_hints(),
            instructions=instructions or [],
        )

        if self._backend == "claude_agent_sdk":
            sdk_result = self._aggregate_with_claude_agent_sdk(
                plan=plan,
                report=report,
                payload=payload,
                fallback=fallback,
                resolved=resolved,
                started=started,
            )
            if sdk_result is not None:
                return sdk_result
            logger.info("result aggregator claude sdk failed or unavailable; falling back to llm backend")

        return self._aggregate_with_llm(
            plan=plan,
            report=report,
            payload=payload,
            fallback=fallback,
            resolved=resolved,
            started=started,
        )

    def _aggregate_with_llm(
        self,
        *,
        plan: ExecutionPlan,
        report: ExecutionReport,
        payload: dict[str, Any],
        fallback: AggregationResult,
        resolved,
        started: float,
    ) -> AggregationResult:
        prompt = self._prompt_registry.load("result_aggregator", self._prompt_version)
        response_format = prompt.response_format if resolved.profile.supports_json_object else None
        try:
            logger.info(
                "result aggregator llm request report_status=%s node_count=%s finalization=%s response_format=%s",
                report.status,
                len(report.node_results),
                plan.finalization_hint.mode,
                response_format,
            )
            add_event(
                "aggregation.llm.request",
                **{
                    "jarvis.report_status": report.status,
                    "jarvis.node_count": len(report.node_results),
                    "jarvis.finalization_mode": plan.finalization_hint.mode,
                    "jarvis.response_format": bool(response_format),
                },
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
            _trace_aggregation_result(
                result,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                mode="llm",
            )
            return result
        except Exception as exc:
            logger.exception("result aggregator llm failed")
            record_exception(exc, **{"jarvis.stage": "result_aggregator"})
            add_event("aggregation.fallback", **{"jarvis.reason": "llm_failed"})
            return fallback

    def _aggregate_with_claude_agent_sdk(
        self,
        *,
        plan: ExecutionPlan,
        report: ExecutionReport,
        payload: dict[str, Any],
        fallback: AggregationResult,
        resolved,
        started: float,
    ) -> AggregationResult | None:
        if not _is_claude_agent_sdk_available():
            add_event("aggregation.fallback", **{"jarvis.reason": "claude_agent_sdk_unavailable"})
            return None
        try:
            logger.info(
                "result aggregator claude sdk request report_status=%s node_count=%s finalization=%s",
                report.status,
                len(report.node_results),
                plan.finalization_hint.mode,
            )
            add_event(
                "aggregation.claude_sdk.request",
                **{
                    "jarvis.report_status": report.status,
                    "jarvis.node_count": len(report.node_results),
                    "jarvis.finalization_mode": plan.finalization_hint.mode,
                },
            )
            result_payload = asyncio.run(
                asyncio.wait_for(
                    _run_claude_agent_aggregation(payload=payload, resolved=resolved),
                    timeout=_CLAUDE_AGGREGATOR_TIMEOUT_SECONDS,
                )
            )
            parsed = result_payload.get("payload")
            result = _aggregation_from_payload(parsed, fallback=fallback, report=report)
            usage_records = result_payload.get("usage_records")
            if isinstance(usage_records, list):
                result.usage_records.extend(item for item in usage_records if isinstance(item, dict))
            result.data = {
                **result.data,
                "aggregator_backend": "claude_agent_sdk",
                **({"agent_session_id": result_payload.get("session_id")} if result_payload.get("session_id") else {}),
            }
            logger.info(
                "result aggregator claude sdk completed status=%s reply_len=%s artifact_refs=%s elapsed_ms=%s",
                result.status,
                len(result.reply),
                result.artifact_refs,
                int((time.perf_counter() - started) * 1000),
            )
            _trace_aggregation_result(
                result,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                mode="claude_agent_sdk",
            )
            return result
        except Exception as exc:
            logger.exception("result aggregator claude sdk failed")
            record_exception(exc, **{"jarvis.stage": "result_aggregator_claude_sdk"})
            add_event(
                "aggregation.fallback",
                **{
                    "jarvis.reason": "claude_agent_sdk_failed",
                    "jarvis.error": str(exc),
                },
            )
            return None


def _aggregation_input(
    *,
    plan: ExecutionPlan,
    report: ExecutionReport,
    current_user_input: str | None,
    route: str | None,
    fast_intent: dict[str, Any] | None,
    artifacts: list[dict[str, Any]],
    runtime_context: dict[str, Any],
    instructions: list[str],
) -> dict[str, Any]:
    runtime_payload = runtime_context
    return {
        "current_user_input": current_user_input or plan.user_objective,
        "route": route,
        "fast_intent": fast_intent,
        "finalization_hint": plan.finalization_hint.model_dump(mode="json"),
        "user_objective": plan.user_objective,
        "plan": plan.model_dump(mode="json"),
        "execution_report": report.model_dump(mode="json", exclude_none=True),
        "artifacts": artifacts,
        "evidence_artifacts": _load_evidence_artifacts(
            report=report,
            artifacts=artifacts,
            runtime_context=runtime_payload,
        ),
        "runtime_context": runtime_payload,
        "instructions": instructions,
    }


async def _run_claude_agent_aggregation(*, payload: dict[str, Any], resolved) -> dict[str, Any]:
    from claude_agent_sdk import ClaudeAgentOptions, query

    settings = get_settings()
    endpoint = _resolve_claude_endpoint(settings, resolved)
    api_key = getattr(resolved.profile, "api_key", "")
    model = _resolved_model_name(resolved)
    options = ClaudeAgentOptions(
        system_prompt=_claude_aggregator_system_prompt(),
        model=model,
        permission_mode="dontAsk",
        tools=[],
        allowed_tools=[],
        disallowed_tools=[
            "Bash",
            "WebFetch",
            "WebSearch",
            "Read",
            "Write",
            "Edit",
            "MultiEdit",
            "NotebookEdit",
            "Glob",
            "Grep",
            "LS",
        ],
        mcp_servers={},
        strict_mcp_config=True,
        output_format={"type": "json_schema", "schema": _AGGREGATION_OUTPUT_SCHEMA},
        max_turns=_CLAUDE_AGGREGATOR_MAX_TURNS,
        env={
            "ANTHROPIC_BASE_URL": endpoint,
            "ANTHROPIC_AUTH_TOKEN": api_key,
        },
    )
    prompt = _claude_aggregator_user_prompt(payload)
    final_text = ""
    parsed_payload: dict[str, Any] | None = None
    step_usage_records: list[dict[str, Any]] = []
    result_usage_record: dict[str, Any] | None = None
    session_id = ""

    async for msg in query(prompt=prompt, options=options):
        msg_type = type(msg).__name__
        session_id = _message_session_id(msg) or session_id
        if msg_type == "AssistantMessage":
            msg_usage = getattr(msg, "usage", None) or getattr(msg, "model_usage", None)
            if isinstance(msg_usage, dict) and msg_usage:
                step_usage_records.append(_normalize_claude_usage(msg_usage, "result_aggregator_claude_sdk_step"))
            if hasattr(msg, "content") and isinstance(msg.content, list):
                for block in msg.content:
                    if type(block).__name__ == "TextBlock":
                        final_text = getattr(block, "text", "") or final_text
        elif msg_type == "ResultMessage":
            result_usage = getattr(msg, "usage", None) or getattr(msg, "model_usage", None)
            if isinstance(result_usage, dict) and result_usage:
                result_usage_record = _normalize_claude_usage(result_usage, "result_aggregator_claude_sdk")
            structured = getattr(msg, "structured_output", None)
            if isinstance(structured, dict):
                parsed_payload = structured
            result = getattr(msg, "result", None)
            if isinstance(result, dict):
                parsed_payload = result
            elif isinstance(result, str) and result.strip():
                final_text = result.strip()

    if parsed_payload is None:
        parsed_payload = parse_json_content({"content": final_text})
    return {
        "payload": parsed_payload,
        "usage_records": _billable_claude_usage_records(step_usage_records, result_usage_record),
        "session_id": session_id,
    }


def _claude_aggregator_system_prompt() -> str:
    return """
你是 Jarvis ResultAggregator。请把执行结果转换为最终的面向用户答案。
输出必须是符合已配置 schema 的 JSON 对象。
reply 字段必须是干净的 Markdown，不要输出纯文本伪表格。
对比任务优先使用带表头的合法 Markdown 表格，例如 | 维度 | Claude Tag | YouMind |。
如果 Markdown 表格证据过长，使用简洁的 Markdown 小节和 bullet lists。
除非 artifact_refs 非空且引用了附件、报告或 artifact，否则不要声称它们存在。
不要编造 execution_report 中不存在的事实。对不确定或时间敏感的事实要明确标注。
如果 user message payload 中包含 evidence_artifacts，优先基于其中的 evidence_claims.md Markdown 证据表汇总；金融、股票、公告、行情和财务数字没有来源 URL 支撑时不要写成确定事实。
如果 execution_report 表明工作失败或被阻塞，说明具体失败原因或缺失输入，不要泛泛道歉。
不要调用工具。只使用 user message 提供的 JSON payload。
""".strip()


def _claude_aggregator_user_prompt(payload: dict[str, Any]) -> str:
    return json.dumps(
        {
            "task": "为这个 turn 生成最终 AggregationResult。",
            "requirements": [
                "reply 必须是 Markdown。",
                "对比内容使用真正的 Markdown 表格，不要使用伪表格。",
                "只提及 artifact_refs 中出现的附件或 artifacts。",
                "需要时保留 blocked node results 中的 approval_requests。",
            ],
            "input": payload,
        },
        ensure_ascii=False,
        default=str,
    )


def _is_claude_agent_sdk_available() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401
        return True
    except ImportError:
        return False


def _resolve_claude_endpoint(settings, resolved) -> str:
    provider = getattr(resolved, "provider", "") or getattr(getattr(resolved, "profile", None), "provider", "")
    provider = str(provider or getattr(settings, "llm_provider", "deepseek")).strip().lower()
    if provider == "deepseek":
        return "https://api.deepseek.com/anthropic"
    if provider in {"google", "gemini"}:
        return "https://generativelanguage.googleapis.com/v1beta/anthropic"
    base_url = getattr(resolved, "base_url", "") or getattr(getattr(resolved, "profile", None), "base_url", "")
    if base_url:
        return str(base_url).rstrip("/") + "/anthropic"
    return "https://api.deepseek.com/anthropic"


def _resolved_model_name(resolved) -> str:
    for owner in (resolved, getattr(resolved, "profile", None)):
        value = getattr(owner, "model", None)
        if value:
            return str(value)
    return "deepseek-v4-pro"


def _message_session_id(msg: Any) -> str:
    value = getattr(msg, "session_id", None)
    if value is None:
        return ""
    return str(value).strip()


def _load_evidence_artifacts(
    *,
    report: ExecutionReport,
    artifacts: list[dict[str, Any]],
    runtime_context: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates = _evidence_artifact_candidates(report=report, artifacts=artifacts, runtime_context=runtime_context)
    loaded: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for candidate in candidates:
        path = candidate.get("path")
        if not isinstance(path, Path) or path in seen:
            continue
        seen.add(path)
        payload = _read_evidence_artifact(path)
        if payload is None:
            continue
        loaded.append(
            {
                "node_id": candidate.get("node_id") or "",
                "artifact_ref": candidate.get("artifact_ref") or "",
                "filename": path.name,
                "content": payload,
            }
        )
    return loaded


def _evidence_artifact_candidates(
    *,
    report: ExecutionReport,
    artifacts: list[dict[str, Any]],
    runtime_context: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    session_root = _safe_existing_dir(runtime_context.get("session_workspace_dir"))
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        path = _candidate_path_from_payload(artifact, session_root=session_root)
        if path is None or not _is_evidence_artifact(path, artifact):
            continue
        candidates.append(
            {
                "path": path,
                "node_id": artifact.get("node_id"),
                "artifact_ref": artifact.get("artifact_id") or artifact.get("ref"),
            }
        )
    for result in report.node_results:
        for artifact in result.artifacts:
            path = _candidate_path_from_payload(
                artifact.model_dump(mode="json", exclude_none=True),
                session_root=session_root,
            )
            if path is None or not _is_evidence_artifact(path, artifact.model_dump(mode="json", exclude_none=True)):
                continue
            candidates.append(
                {
                    "path": path,
                    "node_id": result.node_id,
                    "artifact_ref": f"artifact:{artifact.ref}",
                }
            )
    return candidates


def _candidate_path_from_payload(payload: dict[str, Any], *, session_root: Path | None) -> Path | None:
    raw_path = str(payload.get("path") or "").strip()
    raw_relative = str(payload.get("session_relative_path") or "").strip()
    for raw in (raw_path, raw_relative):
        if not raw:
            continue
        path = Path(raw)
        if path.is_absolute():
            resolved = _safe_existing_file(path, session_root=session_root)
        elif session_root is not None:
            resolved = _safe_existing_file(session_root / path, session_root=session_root)
        else:
            resolved = None
        if resolved is not None:
            return resolved
    return None


def _safe_existing_dir(value: Any) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        path = Path(text).expanduser().resolve(strict=True)
    except OSError:
        return None
    return path if path.is_dir() else None


def _safe_existing_file(path: Path, *, session_root: Path | None) -> Path | None:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError:
        return None
    if session_root is not None:
        try:
            resolved.relative_to(session_root)
        except ValueError:
            return None
    return resolved if resolved.is_file() else None


def _is_evidence_artifact(path: Path, payload: dict[str, Any]) -> bool:
    filename = str(payload.get("filename") or path.name or "").strip().lower()
    if filename in _EVIDENCE_ARTIFACT_FILENAMES:
        return True
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        purpose = str(metadata.get("purpose") or metadata.get("artifact_type") or "").strip().lower()
        if purpose in {"evidence_claims", "claim_evidence", "evidence"} and path.suffix.lower() in {".md", ".markdown"}:
            return True
    return False


def _read_evidence_artifact(path: Path) -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if stat.st_size <= 0 or stat.st_size > _MAX_EVIDENCE_ARTIFACT_BYTES:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return None
    except OSError:
        return None
    return {"markdown": text[:_MAX_EVIDENCE_ARTIFACT_BYTES]}


def _normalize_claude_usage(usage: dict[str, Any], stage: str) -> dict[str, Any]:
    input_tokens = _int_value(usage.get("input_tokens"), usage.get("prompt_tokens"))
    output_tokens = _int_value(usage.get("output_tokens"), usage.get("completion_tokens"))
    total_tokens = _int_value(usage.get("total_tokens"))
    if total_tokens <= 0 and (input_tokens > 0 or output_tokens > 0):
        total_tokens = input_tokens + output_tokens
    return {
        "source": "claude_agent_sdk",
        "provider": "deepseek",
        "model": str(usage.get("model") or ""),
        "stage": stage,
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total_tokens,
        "_raw": usage,
    }


def _billable_claude_usage_records(
    step_usage_records: list[dict[str, Any]],
    result_usage_record: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if result_usage_record is not None:
        return [result_usage_record]
    return list(step_usage_records)


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
    workspace_result = _workspace_result_text(result)
    if workspace_result:
        return workspace_result
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
    elif report.status == "completed" and status == "failed":
        normalized["status"] = "completed"
    normalized.setdefault("reply", fallback.reply)
    if not isinstance(normalized.get("artifact_refs"), list):
        normalized["artifact_refs"] = _artifact_refs(report.node_results)
    if not isinstance(normalized.get("approval_requests"), list):
        normalized["approval_requests"] = fallback.approval_requests
    if not isinstance(normalized.get("data"), dict):
        normalized["data"] = {}
    if not normalized.get("artifact_refs"):
        normalized["reply"] = _strip_unbacked_artifact_claims(str(normalized.get("reply") or ""))
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
    summaries = [_result_display_text(result) for result in report.node_results]
    summaries = [summary for summary in summaries if summary]
    if not summaries:
        return f"已完成：{plan.user_objective}"
    if len(summaries) == 1:
        return summaries[0]
    lines = ["已完成，结果如下："]
    lines.extend(f"- {summary}" for summary in summaries)
    return "\n".join(lines)


def _result_display_text(result: NodeResult) -> str:
    return _workspace_result_text(result) or result.summary.strip()


def _workspace_result_text(result: NodeResult) -> str:
    workspace = result.data.get("workspace") if isinstance(result.data, dict) else None
    if not isinstance(workspace, dict):
        return ""
    path_text = str(workspace.get("result_markdown_path") or "").strip()
    if not path_text:
        return ""
    try:
        path = Path(path_text).resolve(strict=True)
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""
    if text in {"", "# Result"}:
        return ""
    return text


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


def _strip_unbacked_artifact_claims(reply: str) -> str:
    lines = []
    artifact_claim = re.compile(
        r"(查看附件|见附件|附件中|附件里|附件已|报告已生成.*附件|attached|attachment)",
        flags=re.IGNORECASE,
    )
    for line in str(reply or "").splitlines():
        if artifact_claim.search(line):
            continue
        lines.append(line)
    stripped = "\n".join(lines).strip()
    return stripped or reply


def _preview_json(value: Any, *, limit: int = 2000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _int_value(*values: Any) -> int:
    for value in values:
        if value is None:
            continue
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            continue
    return 0


def _trace_aggregation_result(result: AggregationResult, *, elapsed_ms: int, mode: str) -> None:
    set_attributes(
        **{
            "jarvis.aggregation_status": result.status,
            "jarvis.reply_len": len(result.reply),
            "jarvis.aggregation_mode": mode,
        }
    )
    add_event(
        "aggregation.completed",
        **{
            "jarvis.aggregation_status": result.status,
            "jarvis.reply_len": len(result.reply),
            "jarvis.artifact_refs": result.artifact_refs,
            "jarvis.elapsed_ms": elapsed_ms,
            "jarvis.aggregation_mode": mode,
        },
    )
