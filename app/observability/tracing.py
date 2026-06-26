from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Any

from app.config import get_settings


def is_enabled() -> bool:
    return bool(get_settings().otel_enabled)


def current_trace_ids() -> tuple[str, str]:
    if not is_enabled():
        return "", ""
    from opentelemetry import trace

    span = trace.get_current_span()
    if span is None:
        return "", ""
    context = span.get_span_context()
    if not context.is_valid:
        return "", ""
    return f"{context.trace_id:032x}", f"{context.span_id:016x}"


def set_attributes(**attributes: Any) -> None:
    if not is_enabled():
        return
    from opentelemetry import trace

    span = trace.get_current_span()
    if span is None:
        return
    for key, value in attributes.items():
        if value is None:
            continue
        span.set_attribute(key, _normalize_value(value))


def add_event(name: str, **attributes: Any) -> None:
    if not is_enabled():
        return
    from opentelemetry import trace

    span = trace.get_current_span()
    if span is None:
        return
    payload = {key: _normalize_value(value) for key, value in attributes.items() if value is not None}
    span.add_event(name, payload)


def record_exception(exc: BaseException, **attributes: Any) -> None:
    if not is_enabled():
        return
    from opentelemetry import trace

    span = trace.get_current_span()
    if span is None:
        return
    span.record_exception(exc)
    payload = {key: _normalize_value(value) for key, value in attributes.items() if value is not None}
    if payload:
        span.add_event("error", payload)


@contextmanager
def span_context(name: str, **attributes: Any):
    if not is_enabled():
        yield None
        return
    from opentelemetry import trace

    tracer = trace.get_tracer("jarvis")
    with tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            if value is None:
                continue
            span.set_attribute(key, _normalize_value(value))
        yield span


def trace_preview(value: Any, *, limit: int = 300) -> str:
    """Return a compact, bounded string suitable for trace attributes."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 14)] + "...[truncated]"


def content_capture_enabled() -> bool:
    return bool(get_settings().otel_capture_content)


def _normalize_value(value: Any) -> Any:
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        result: list[Any] = []
        for item in value:
            if isinstance(item, (bool, int, float, str)):
                result.append(item)
            else:
                result.append(str(item))
        return result
    return str(value)
