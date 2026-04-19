from concurrent.futures import Future, ThreadPoolExecutor
from functools import lru_cache

from app.workers.base import WorkOrder, WorkResult
from app.workers.executor import execute_work_order


class ThreadWorkerClient:
    def __init__(self, max_workers: int = 4) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._futures: dict[str, Future[WorkResult]] = {}
        self._results: dict[str, WorkResult] = {}

    def dispatch(self, order: WorkOrder) -> str:
        if order.order_id not in self._futures and order.order_id not in self._results:
            self._futures[order.order_id] = self._executor.submit(_execute_safely, order)
        return order.order_id

    def poll(self, order_id: str) -> WorkResult | None:
        if order_id in self._results:
            return self._results[order_id]

        future = self._futures.get(order_id)
        if future is None or not future.done():
            return None

        result = future.result()
        self._results[order_id] = result
        self._futures.pop(order_id, None)
        return result

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)


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
