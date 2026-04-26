from __future__ import annotations

import logging
from threading import Event, Thread

from app.agent.runner import ThreadManager
from app.workers.events import WorkerEvent, WorkerEventBus, get_worker_event_bus

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
        events: list[WorkerEvent] = []
        while True:
            event = self._event_bus.get(timeout=0)
            if event is None:
                self._handle_events(events)
                return len(events)
            events.append(event)

    def _run(self) -> None:
        while not self._stop.is_set():
            event = self._event_bus.get(timeout=self._poll_timeout_seconds)
            if event is None:
                continue
            events = [event]
            while True:
                next_event = self._event_bus.get(timeout=0)
                if next_event is None:
                    break
                events.append(next_event)
            self._handle_events(events)

    def _handle_event(self, event_type: str, payload: dict) -> None:
        self._handle_events([WorkerEvent(event_type=event_type, payload=payload)])

    def _handle_events(self, events: list[WorkerEvent]) -> None:
        events_by_thread: dict[str, list[WorkerEvent]] = {}
        for event in events:
            thread_id = event.payload.get("ca_thread_id")
            order_id = event.payload.get("order_id")
            if not thread_id:
                logger.warning("worker event missing ca_thread_id order_id=%s", order_id)
                continue
            events_by_thread.setdefault(thread_id, []).append(event)

        for thread_id, thread_events in events_by_thread.items():
            if len(thread_events) == 1:
                event = thread_events[0]
                self._resume_thread(
                    thread_id,
                    {"event_type": event.event_type, "payload": event.payload},
                    order_id=event.payload.get("order_id"),
                )
                continue
            self._resume_thread(
                thread_id,
                {
                    "events": [
                        {"event_type": event.event_type, "payload": event.payload}
                        for event in thread_events
                    ],
                },
                order_id=",".join(
                    str(event.payload.get("order_id"))
                    for event in thread_events
                    if event.payload.get("order_id")
                ),
            )

    def _resume_thread(self, thread_id: str, resume_value: dict, *, order_id: object) -> None:
        try:
            self._thread_manager.resume(thread_id, resume_value)
        except Exception:
            logger.exception(
                "failed to dispatch worker event thread_id=%s order_id=%s",
                thread_id,
                order_id,
            )
