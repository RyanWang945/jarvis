from functools import lru_cache
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.agent.events import build_user_event
from app.agent.runner import ThreadManager
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


class AgentApprovalRequest(BaseModel):
    thread_id: str = Field(min_length=1)
    approval_id: str | None = None


class AgentRunResponse(BaseModel):
    thread_id: str
    status: str
    summary: str | None
    tasks: list[dict[str, Any]]
    pending_approval_id: str | None = None
    diagnostics: dict[str, Any] | None = None


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
    result = get_thread_manager().run_event(event)
    return AgentRunResponse(
        thread_id=result.thread_id,
        status=result.status,
        summary=result.summary,
        tasks=result.tasks,
        pending_approval_id=result.pending_approval_id,
        diagnostics=result.diagnostics,
    )


@router.post("/approve", response_model=AgentRunResponse)
def approve_agent(request: AgentApprovalRequest) -> AgentRunResponse:
    manager = get_thread_manager()
    _ensure_pending_approval(manager, request)
    result = manager.resume(
        request.thread_id,
        {"approved": True, "approval_id": request.approval_id},
    )
    return AgentRunResponse(
        thread_id=result.thread_id,
        status=result.status,
        summary=result.summary,
        tasks=result.tasks,
        pending_approval_id=result.pending_approval_id,
        diagnostics=result.diagnostics,
    )


@router.post("/reject", response_model=AgentRunResponse)
def reject_agent(request: AgentApprovalRequest) -> AgentRunResponse:
    manager = get_thread_manager()
    _ensure_pending_approval(manager, request)
    result = manager.resume(
        request.thread_id,
        {"approved": False, "approval_id": request.approval_id},
    )
    return AgentRunResponse(
        thread_id=result.thread_id,
        status=result.status,
        summary=result.summary,
        tasks=result.tasks,
        pending_approval_id=result.pending_approval_id,
        diagnostics=result.diagnostics,
    )


@router.get("/runs")
def list_runs() -> dict[str, Any]:
    db = get_thread_manager().db
    runs = db.runs.list_unfinished()
    return {"runs": runs}


@router.get("/runs/{thread_id}")
def get_run(thread_id: str) -> dict[str, Any]:
    run = get_thread_manager().inspect_run(thread_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found.")
    return run


@router.post("/recover")
def recover_runs() -> dict[str, Any]:
    return get_thread_manager().recover_unfinished()


@router.post("/runs/{thread_id}/report")
def export_run_report(thread_id: str) -> dict[str, Any]:
    try:
        return {"paths": get_thread_manager().export_run_report(thread_id)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _ensure_pending_approval(manager: ThreadManager, request: AgentApprovalRequest) -> None:
    pending = manager.db.approvals.get_pending_by_thread(request.thread_id)
    if not pending:
        raise HTTPException(status_code=409, detail="No pending approval for thread.")
    if request.approval_id and all(
        approval["approval_id"] != request.approval_id for approval in pending
    ):
        raise HTTPException(status_code=404, detail="Approval not found or not pending.")


@lru_cache
def get_thread_manager() -> ThreadManager:
    settings = get_settings()
    return ThreadManager(settings.data_dir)
