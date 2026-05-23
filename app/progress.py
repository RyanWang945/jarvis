from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProgressEvent:
    event_type: str
    turn_id: int | None = None
    conversation_id: int | None = None
    stage: str | None = None
    title: str = ""
    summary: str = ""
    node_id: str | None = None
    tool_name: str | None = None
    status: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


class ProgressSink(Protocol):
    def on_progress(self, event: ProgressEvent) -> None:
        ...

    def close(self) -> None:
        ...


class ProgressReporter:
    def __init__(self, sinks: list[ProgressSink] | None = None) -> None:
        self._sinks = list(sinks or [])
        self._closed = False

    def emit(self, event_type: str, **payload: Any) -> None:
        if self._closed:
            return
        event = ProgressEvent(event_type=event_type, **payload)
        for sink in self._sinks:
            try:
                sink.on_progress(event)
            except Exception:
                logger.exception("progress sink failed event_type=%s sink=%s", event.event_type, type(sink).__name__)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for sink in self._sinks:
            close = getattr(sink, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception:
                logger.exception("progress sink close failed sink=%s", type(sink).__name__)


class NoopProgressReporter(ProgressReporter):
    def __init__(self) -> None:
        super().__init__([])

    def emit(self, event_type: str, **payload: Any) -> None:
        return

    def close(self) -> None:
        return


def ensure_progress(progress: ProgressReporter | None) -> ProgressReporter:
    return progress if progress is not None else NoopProgressReporter()
