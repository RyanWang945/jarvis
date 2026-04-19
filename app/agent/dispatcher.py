from __future__ import annotations

import logging
from threading import Event, Thread

from app.agent.runner import ThreadManager
from app.workers.events import WorkerEventBus, get_worker_event_bus

logger = logging.getLogger(__name__)


class DispatcherService:
    def __init__(
        self,
        thread_manager: ThreadManager,
        event_bus: WorkerEventBus | None = None,
        poll_timeout_seconds: float = 0.2,
    ) -> None:
        self._thread_manager = thread_manager
        self._event_bus = event_bus or get_worker_event_bus()
        self._poll_timeout_seconds = poll_timeout_seconds
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="jarvis-dispatcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def drain_once(self) -> int:
        processed = 0
        while True:
            event = self._event_bus.get(timeout=0)
            if event is None:
                return processed
            self._handle_event(event.event_type, event.payload)
            processed += 1

    def _run(self) -> None:
        while not self._stop.is_set():
            event = self._event_bus.get(timeout=self._poll_timeout_seconds)
            if event is None:
                continue
            self._handle_event(event.event_type, event.payload)

    def _handle_event(self, event_type: str, payload: dict) -> None:
        thread_id = payload.get("ca_thread_id")
        order_id = payload.get("order_id")
        if not thread_id:
            logger.warning("worker event missing ca_thread_id order_id=%s", order_id)
            return
        try:
            self._thread_manager.resume(
                thread_id,
                {"event_type": event_type, "payload": payload},
            )
        except Exception:
            logger.exception(
                "failed to dispatch worker event thread_id=%s order_id=%s",
                thread_id,
                order_id,
            )
