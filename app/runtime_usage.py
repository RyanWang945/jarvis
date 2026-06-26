from __future__ import annotations

from typing import Any

from app.llm.provider_adapters import NormalizedLLMResponse, TokenUsage


def usage_record_from_response(response: NormalizedLLMResponse, *, stage: str) -> dict[str, Any] | None:
    if response.usage is None:
        return None
    return {
        "source": "llm",
        "provider": _provider_from_model(response.model),
        "model": response.model or "unknown",
        "stage": stage,
        "prompt_tokens": response.usage.prompt_tokens,
        "completion_tokens": response.usage.completion_tokens,
        "total_tokens": response.usage.total_tokens,
    }


def usage_record_from_token_usage(
    usage: TokenUsage | None,
    *,
    source: str,
    provider: str,
    model: str,
    stage: str,
) -> dict[str, Any] | None:
    if usage is None:
        return None
    return {
        "source": source,
        "provider": provider,
        "model": model,
        "stage": stage,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }


def collect_usage_records(*values: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for value in values:
        _collect_usage_records(value, records)
    return records


def usage_totals(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    prompt = 0
    completion = 0
    total = 0
    models: list[str] = []
    breakdown: list[dict[str, Any]] = []
    by_source: dict[str, dict[str, int]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        prompt_tokens = _int_value(record.get("prompt_tokens"), record.get("input_tokens"))
        completion_tokens = _int_value(record.get("completion_tokens"), record.get("output_tokens"))
        total_tokens = _int_value(record.get("total_tokens"))
        if total_tokens <= 0 and (prompt_tokens > 0 or completion_tokens > 0):
            total_tokens = prompt_tokens + completion_tokens
        if prompt_tokens <= 0 and completion_tokens <= 0 and total_tokens <= 0:
            continue
        prompt += prompt_tokens
        completion += completion_tokens
        total += total_tokens
        source = _usage_source_bucket(record)
        source_totals = by_source.setdefault(
            source,
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )
        source_totals["prompt_tokens"] += prompt_tokens
        source_totals["completion_tokens"] += completion_tokens
        source_totals["total_tokens"] += total_tokens
        model = str(record.get("model") or record.get("provider") or "").strip()
        if model and model not in models:
            models.append(model)
        breakdown.append(
            {
                **record,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            }
        )
    if prompt <= 0 and completion <= 0 and total <= 0:
        return None
    return {
        "model": " + ".join(models) if models else "unknown",
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "by_source": by_source,
        "records": breakdown,
    }


def _collect_usage_records(value: Any, records: list[dict[str, Any]]) -> None:
    if value is None:
        return
    if isinstance(value, dict):
        usage_records = value.get("usage_records")
        if isinstance(usage_records, list):
            for item in usage_records:
                if isinstance(item, dict):
                    records.append(dict(item))
        usage = value.get("usage")
        if isinstance(usage, dict):
            records.append(dict(usage))
        return
    if isinstance(value, list):
        for item in value:
            _collect_usage_records(item, records)
        return
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        _collect_usage_records(data, records)
    metadata = getattr(value, "metadata", None)
    if isinstance(metadata, dict):
        _collect_usage_records(metadata, records)
    usage_records = getattr(value, "usage_records", None)
    if isinstance(usage_records, list):
        for item in usage_records:
            if isinstance(item, dict):
                records.append(dict(item))


def _provider_from_model(model: str) -> str:
    text = str(model or "").strip().lower()
    if "deepseek" in text:
        return "deepseek"
    if "gpt" in text or "codex" in text or "openai" in text:
        return "openai"
    return text or "unknown"


def _usage_source_bucket(record: dict[str, Any]) -> str:
    source = str(record.get("source") or "").strip()
    if source == "claude_agent_sdk":
        return "claude_agent_sdk"
    return "direct_api"


def _int_value(*values: Any) -> int:
    for value in values:
        if value is None:
            continue
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            continue
    return 0
