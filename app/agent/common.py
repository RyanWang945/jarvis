from __future__ import annotations

from typing import Any

from langgraph.types import Command

from app.agent.state import AgentState, Task


def goto(node: str, update: dict[str, Any]) -> Command:
    return Command(goto=node, update=update)


def payload(state: AgentState) -> dict[str, Any]:
    value = state["event"].get("payload", {})
    return value if isinstance(value, dict) else {}


def clean_optional(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def task_status_context(state: AgentState) -> str:
    lines = ["Current task status:"]
    for task in state.get("task_list", []):
        title = task.get("title") or task.get("description") or task.get("id")
        status = task.get("status")
        result = task.get("result_summary")
        line = f"- {title}: {status}"
        if result:
            line += f" ({result})"
        lines.append(line)
    return "\n".join(lines)


def update_current_task(
    state: AgentState,
    *,
    status: str,
    result_summary: str | None,
) -> list[Task]:
    current_task_id = state.get("current_task_id")
    tasks: list[Task] = []
    for task in state["task_list"]:
        updated = task.copy()
        if updated["id"] == current_task_id:
            updated["status"] = status  # type: ignore[typeddict-item]
            updated["result_summary"] = result_summary
        tasks.append(updated)
    return tasks
