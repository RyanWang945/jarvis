from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from queue import Empty, Queue

from app.workers.base import WorkResult


@dataclass(frozen=True)
class WorkerEvent:
    event_type: str
    payload: dict


class WorkerEventBus:
    def __init__(self) -> None:
        self._queue: Queue[WorkerEvent] = Queue()

    def publish_result(self, result: WorkResult) -> None:
        event_type = "worker_complete" if result.ok else "worker_failed"
        self._queue.put(WorkerEvent(event_type=event_type, payload=result.model_dump()))

    def get(self, timeout: float = 0.2) -> WorkerEvent | None:
        try:
            return self._queue.get(timeout=timeout)
        except Empty:
            return None


@lru_cache
def get_worker_event_bus() -> WorkerEventBus:
    return WorkerEventBus()
