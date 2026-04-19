from concurrent.futures import Future, ThreadPoolExecutor
from functools import lru_cache
from threading import Lock

from app.workers.base import WorkOrder, WorkResult
from app.workers.events import WorkerEventBus, get_worker_event_bus
from app.workers.executor import execute_work_order


class ThreadWorkerClient:
    def __init__(self, max_workers: int = 4, event_bus: WorkerEventBus | None = None) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._event_bus = event_bus or get_worker_event_bus()
        self._futures: dict[str, Future[WorkResult]] = {}
        self._results: dict[str, WorkResult] = {}
        self._lock = Lock()

    def dispatch(self, order: WorkOrder) -> str:
        with self._lock:
            if order.order_id in self._futures or order.order_id in self._results:
                return order.order_id
            future = self._executor.submit(_execute_safely, order)
            self._futures[order.order_id] = future
            future.add_done_callback(lambda done: self._record_completion(order.order_id, done))
        return order.order_id

    def poll(self, order_id: str) -> WorkResult | None:
        with self._lock:
            return self._results.get(order_id)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)

    def _record_completion(self, order_id: str, future: Future[WorkResult]) -> None:
        result = future.result()
        with self._lock:
            self._results[order_id] = result
            self._futures.pop(order_id, None)
        self._event_bus.publish_result(result)


def _execute_safely(order: WorkOrder) -> WorkResult:
    try:
        return execute_work_order(order)
    except Exception as exc:
        return WorkResult(
            order_id=order.order_id,
            task_id=order.task_id,
            ca_thread_id=order.ca_thread_id,
            worker_type=order.worker_type,
            ok=False,
            stderr=str(exc),
            summary=f"Worker raised {type(exc).__name__}.",
        )


@lru_cache
def get_thread_worker_client(max_workers: int = 4) -> ThreadWorkerClient:
    return ThreadWorkerClient(max_workers=max_workers)
