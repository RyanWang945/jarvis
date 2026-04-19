from functools import lru_cache

from app.workers.base import WorkOrder, WorkResult
from app.workers.executor import execute_work_order


class InlineWorkerClient:
    def __init__(self) -> None:
        self._results: dict[str, WorkResult] = {}

    def dispatch(self, order: WorkOrder) -> str:
        self._results[order.order_id] = execute_work_order(order)
        return order.order_id

    def poll(self, order_id: str) -> WorkResult | None:
        return self._results.get(order_id)


@lru_cache
def get_inline_worker_client() -> InlineWorkerClient:
    return InlineWorkerClient()
