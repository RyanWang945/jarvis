from __future__ import annotations

import sys
from types import SimpleNamespace

from app.observability import tracing


class _FakeSpan:
    def __init__(self) -> None:
        self.attributes = {}

    def set_attribute(self, key, value) -> None:
        self.attributes[key] = value


class _FakeSpanContext:
    def __init__(self, span: _FakeSpan) -> None:
        self._span = span

    def __enter__(self) -> _FakeSpan:
        return self._span

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def test_span_context_sets_all_non_null_attributes(monkeypatch) -> None:
    span = _FakeSpan()
    fake_trace = SimpleNamespace(
        get_tracer=lambda name: SimpleNamespace(
            start_as_current_span=lambda span_name: _FakeSpanContext(span)
        )
    )
    monkeypatch.setitem(sys.modules, "opentelemetry", SimpleNamespace(trace=fake_trace))
    monkeypatch.setattr(tracing, "is_enabled", lambda: True)

    with tracing.span_context(
        "turn.run",
        **{
            "langfuse.trace.name": "Jarvis Turn",
            "jarvis.turn_id": 1,
            "jarvis.skip": None,
        },
    ):
        pass

    assert span.attributes == {
        "langfuse.trace.name": "Jarvis Turn",
        "jarvis.turn_id": 1,
    }
