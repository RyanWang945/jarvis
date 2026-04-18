from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph.message import add_messages

from app.agent.events import AgentEvent

TaskStatus = Literal[
    "pending",
    "running",
    "waiting",
    "success",
    "failed",
    "blocked",
    "cancelled",
]
RiskLevel = Literal["low", "medium", "high", "critical"]
ActionKind = Literal["echo", "shell", "coder", "file", "obsidian", "github"]
ActionStatus = Literal["ready", "waiting_approval", "approved", "rejected"]
AgentRunStatus = Literal[
    "created",
    "contextualizing",
    "planning",
    "strategizing",
    "dispatching",
    "monitoring",
    "running",
    "verifying",
    "waiting_approval",
    "blocked",
    "completed",
    "failed",
    "cancelled",
]


class Task(TypedDict):
    id: str
    title: str
    description: str
    status: TaskStatus
    resource_key: str | None
    dod: str | None
    verification_cmd: str | None
    tool_name: str | None
    tool_args: dict[str, Any]
    worker_type: str | None
    order_id: str | None
    retry_count: int
    max_retries: int
    result_summary: str | None


class PendingAction(TypedDict):
    action_id: str
    kind: ActionKind
    skill: str
    action: str
    args: dict[str, Any]
    command: str | None
    workdir: str | None
    risk_level: RiskLevel
    reason: str
    status: ActionStatus


class AgentState(TypedDict):
    thread_id: str
    messages: Annotated[list[Any], add_messages]
    event: dict[str, Any]
    task_list: list[Task]
    current_task_id: str | None
    status: AgentRunStatus
    resource_key: str | None
    pending_action: PendingAction | None
    dispatch_queue: list[dict[str, Any]]
    active_workers: dict[str, str]
    worker_results: dict[str, dict[str, Any]]
    pending_approval_id: str | None
    error_count: int
    last_error: str | None
    context_summary: str | None
    final_summary: str | None
    next_node: str | None


def initial_state(event: AgentEvent, thread_id: str) -> AgentState:
    return {
        "thread_id": thread_id,
        "messages": [],
        "event": event.model_dump(),
        "task_list": [],
        "current_task_id": None,
        "status": "created",
        "resource_key": None,
        "pending_action": None,
        "dispatch_queue": [],
        "active_workers": {},
        "worker_results": {},
        "pending_approval_id": None,
        "error_count": 0,
        "last_error": None,
        "context_summary": None,
        "final_summary": None,
        "next_node": None,
    }
