from functools import lru_cache
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.agent.events import build_user_event
from app.agent.runner import GraphRunner
from app.config import get_settings

router = APIRouter(prefix="/agent", tags=["agent"])


class AgentRunRequest(BaseModel):
    instruction: str = Field(min_length=1)
    command: str | None = None
    verification_cmd: str | None = None
    workdir: str | None = None
    resource_key: str | None = None
    thread_id: str | None = None
    user_id: str | None = None


class AgentRunResponse(BaseModel):
    thread_id: str
    status: str
    summary: str | None
    tasks: list[dict[str, Any]]
    pending_approval_id: str | None = None


@router.post("/run", response_model=AgentRunResponse)
def run_agent(request: AgentRunRequest) -> AgentRunResponse:
    event = build_user_event(
        instruction=request.instruction,
        command=request.command,
        verification_cmd=request.verification_cmd,
        workdir=request.workdir,
        resource_key=request.resource_key,
        thread_id=request.thread_id,
        user_id=request.user_id,
    )
    result = get_graph_runner().run_event(event)
    return AgentRunResponse(
        thread_id=result.thread_id,
        status=result.status,
        summary=result.summary,
        tasks=result.tasks,
        pending_approval_id=result.pending_approval_id,
    )


@lru_cache
def get_graph_runner() -> GraphRunner:
    settings = get_settings()
    return GraphRunner(settings.data_dir)
