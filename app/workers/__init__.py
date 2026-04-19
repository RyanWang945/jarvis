from app.workers.base import WorkOrder, WorkResult, WorkerClient
from app.workers.events import WorkerEvent, WorkerEventBus, get_worker_event_bus
from app.workers.inline import InlineWorkerClient, get_inline_worker_client
from app.workers.threaded import ThreadWorkerClient, get_thread_worker_client


def get_worker_client() -> WorkerClient:
    from app.config import get_settings

    settings = get_settings()
    if settings.worker_mode == "thread":
        return get_thread_worker_client(settings.worker_max_workers)
    return get_inline_worker_client()

__all__ = [
    "InlineWorkerClient",
    "ThreadWorkerClient",
    "WorkOrder",
    "WorkResult",
    "WorkerEvent",
    "WorkerEventBus",
    "WorkerClient",
    "get_inline_worker_client",
    "get_thread_worker_client",
    "get_worker_event_bus",
    "get_worker_client",
]
