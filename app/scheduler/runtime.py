from __future__ import annotations

import logging
import threading
import time
from typing import Protocol

from app.scheduler.service import SchedulerService, get_scheduler_service

logger = logging.getLogger(__name__)


class SchedulerDelivery(Protocol):
    def send_reminder(self, *, platform: str, external_chat_id: str, text: str) -> str | None: ...


class SchedulerWorker:
    def __init__(
        self,
        *,
        delivery: SchedulerDelivery,
        service: SchedulerService | None = None,
        interval_seconds: float = 30.0,
    ) -> None:
        self._delivery = delivery
        self._service = service or get_scheduler_service()
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="scheduler-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._service.tick(self._delivery)
            except Exception:
                logger.exception("scheduler tick failed")
            self._stop.wait(self._interval_seconds)
