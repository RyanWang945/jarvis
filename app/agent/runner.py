import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver

from app.agent.events import AgentEvent
from app.agent.graph import build_agent_graph
from app.agent.state import AgentState, initial_state


@dataclass(frozen=True)
class AgentRunResult:
    thread_id: str
    status: str
    summary: str | None
    tasks: list[dict[str, Any]]
    pending_approval_id: str | None


class GraphRunner:
    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self._checkpoint_path = data_dir / "langgraph_checkpoints.sqlite"
        self._conn = sqlite3.connect(str(self._checkpoint_path), check_same_thread=False)
        self._checkpointer = SqliteSaver(self._conn)
        self._checkpointer.setup()
        self._graph = build_agent_graph(checkpointer=self._checkpointer)

    def run_event(self, event: AgentEvent) -> AgentRunResult:
        thread_id = event.thread_id or str(uuid4())
        state = initial_state(event, thread_id)
        config = {"configurable": {"thread_id": thread_id}}
        result: AgentState = self._graph.invoke(state, config=config)
        return AgentRunResult(
            thread_id=thread_id,
            status=result["status"],
            summary=result.get("final_summary"),
            tasks=[dict(task) for task in result.get("task_list", [])],
            pending_approval_id=result.get("pending_approval_id"),
        )
