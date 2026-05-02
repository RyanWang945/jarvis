from __future__ import annotations

import json
from pathlib import Path

from app.config import get_settings
from app.obsidian_wiki import ObsidianWikiService
from app.tools.common import ToolExecutionRequest, ToolExecutionResult


def run_obsidian_wiki_query(request: ToolExecutionRequest) -> ToolExecutionResult:
    service = _service_from_request(request)
    query = str(request.args.get("query") or "").strip()
    if not query:
        return ToolExecutionResult(ok=False, exit_code=None, stderr="missing query", summary="Missing query.")
    query_mode = str(request.args.get("query_mode") or "wiki_then_raw")
    hits = service.query(query, query_mode=query_mode)
    payload = {
        "hits": [
            {
                "path": str(hit.path),
                "title": hit.title,
                "snippet": hit.snippet,
                "layer": hit.layer,
            }
            for hit in hits
        ]
    }
    return _json_result(payload, summary=f"Found {len(hits)} hits.")


def run_obsidian_wiki_draft(request: ToolExecutionRequest) -> ToolExecutionResult:
    service = _service_from_request(request)
    title = str(request.args.get("title") or "").strip()
    page_type = str(request.args.get("page_type") or "").strip()
    content = str(request.args.get("content") or "").strip()
    source_ids = list(request.args.get("source_ids") or [])
    target_hint = request.args.get("target_hint")
    if not title or not page_type or (not content and not source_ids):
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stderr="missing required fields",
            summary="Draft requires title, page_type, and either content or source_ids.",
        )
    draft = service.draft(
        title=title,
        page_type=page_type,
        content=content,
        source_ids=source_ids,
        target_hint=str(target_hint) if target_hint else None,
    )
    payload = {
        "draft_id": draft.draft_id,
        "path": str(draft.path),
        "page_type": draft.page_type,
        "title": draft.title,
        "target_page": draft.target_page,
        "source_ids": draft.source_ids,
    }
    return _json_result(payload, summary=f"Created draft {draft.draft_id}.")


def run_obsidian_wiki_apply(request: ToolExecutionRequest) -> ToolExecutionResult:
    service = _service_from_request(request)
    draft_id = str(request.args.get("draft_id") or "").strip()
    if not draft_id:
        return ToolExecutionResult(ok=False, exit_code=None, stderr="missing draft_id", summary="Missing draft_id.")
    target_page = request.args.get("target_page")
    result = service.apply(draft_id, target_page=str(target_page) if target_page else None)
    payload = {
        "status": result.status,
        "page_path": str(result.page_path) if result.page_path else None,
        "conflict_reason": result.conflict_reason,
    }
    ok = result.status == "applied"
    summary = "Applied draft." if ok else f"Apply {result.status}."
    return _json_result(payload, ok=ok, summary=summary)


def run_obsidian_wiki_maintain(request: ToolExecutionRequest) -> ToolExecutionResult:
    service = _service_from_request(request)
    result = service.maintain()
    payload = {
        "issues": [
            {
                "path": str(issue.path),
                "code": issue.code,
                "message": issue.message,
            }
            for issue in result.issues
        ]
    }
    return _json_result(payload, summary=f"Found {len(result.issues)} issues.")


def _service_from_request(request: ToolExecutionRequest) -> ObsidianWikiService:
    vault_arg = request.args.get("vault_path")
    if vault_arg:
        vault_path = Path(str(vault_arg))
    else:
        settings = get_settings()
        if settings.obsidian_vault_path is None:
            raise ValueError("obsidian_vault_path is not configured")
        vault_path = settings.obsidian_vault_path
    return ObsidianWikiService(vault_path)


def _json_result(payload: dict, *, ok: bool = True, summary: str = "") -> ToolExecutionResult:
    return ToolExecutionResult(
        ok=ok,
        exit_code=0 if ok else 1,
        stdout=json.dumps(payload, ensure_ascii=False, indent=2),
        summary=summary,
    )
