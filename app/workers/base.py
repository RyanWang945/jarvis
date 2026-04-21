from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

RiskLevel = Literal["low", "medium", "high", "critical"]
WorkerType = str


class WorkOrder(BaseModel):
    order_id: str
    task_id: str
    ca_thread_id: str
    capability_name: str | None = None
    worker_type: WorkerType
    provider: str | None = None
    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    workdir: str | None = None
    risk_level: RiskLevel = "low"
    reason: str
    verification_cmd: str | None = None
    timeout_seconds: int = 30


class WorkResult(BaseModel):
    order_id: str
    task_id: str
    ca_thread_id: str
    worker_type: WorkerType
    ok: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    artifacts: list[str] = Field(default_factory=list)
    summary: str = ""


class WorkerClient(Protocol):
    def dispatch(self, order: WorkOrder) -> str:
        ...

    def poll(self, order_id: str) -> WorkResult | None:
        ...
