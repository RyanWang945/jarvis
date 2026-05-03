from __future__ import annotations

import json
from functools import lru_cache

from app.config import get_settings
from app.knowledge_base.service import KnowledgeBaseService
from app.tools.common import ToolExecutionRequest, ToolExecutionResult

_ALLOWED_MODES = {"bm25", "vector", "hybrid", "rrf", "rrf_v2"}


def run_business_knowledge_search(request: ToolExecutionRequest) -> ToolExecutionResult:
    query = str(request.args.get("query") or "").strip()
    if not query:
        return ToolExecutionResult(ok=False, exit_code=None, stderr="missing query", summary="Missing query.")

    mode = str(request.args.get("mode") or "rrf_v2")
    if mode not in _ALLOWED_MODES:
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stderr=f"unsupported search mode: {mode}",
            summary="Unsupported search mode.",
        )

    settings = get_settings()
    source_type = _optional_str(request.args.get("source_type"))
    language = _optional_str(request.args.get("language"))
    chunk_profile_id = _optional_str(request.args.get("chunk_profile_id"))
    if source_type == "sec_filing":
        language = language or "en"
        chunk_profile_id = chunk_profile_id or "sec_filing_medium_v1"
    else:
        language = language or settings.knowledge_default_language
        chunk_profile_id = chunk_profile_id or settings.knowledge_default_chunk_profile

    top_k = _bounded_top_k(request.args.get("top_k"))
    filters = _build_filters(request.args, language=language, chunk_profile_id=chunk_profile_id)
    if source_type:
        filters["source_type"] = source_type

    service = get_business_knowledge_service()
    try:
        hits = service.search(
            query=query,
            language=language,
            chunk_profile_id=chunk_profile_id,
            mode=mode,
            top_k=top_k,
            source_type=source_type,
            filters=filters,
        )
    except Exception as exc:
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stderr=str(exc),
            summary="Business knowledge search failed.",
        )

    payload = {
        "backend": "opensearch",
        "query": query,
        "mode": mode,
        "language": language,
        "chunk_profile_id": chunk_profile_id,
        "source_type": source_type,
        "hits": [_hit_payload(hit) for hit in hits],
    }
    return ToolExecutionResult(
        ok=True,
        exit_code=0,
        stdout=json.dumps(payload, ensure_ascii=False, indent=2),
        summary=f"Found {len(hits)} business knowledge hits.",
    )


@lru_cache
def get_business_knowledge_service() -> KnowledgeBaseService:
    return KnowledgeBaseService(get_settings())


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bounded_top_k(value: object) -> int:
    try:
        top_k = int(value) if value is not None else 5
    except (TypeError, ValueError):
        return 5
    return min(max(top_k, 1), 20)


def _build_filters(args: dict, *, language: str, chunk_profile_id: str) -> dict:
    filters: dict = {
        "language": language,
        "chunk_profile_id": chunk_profile_id,
    }
    for field in (
        "source_id",
        "source_ids",
        "ticker",
        "company_name",
        "form_type",
        "fiscal_year",
        "section_title",
    ):
        value = args.get(field)
        if value not in (None, "", []):
            filters[field] = value
    return filters


def _hit_payload(hit: object) -> dict:
    source = dict(getattr(hit, "source", {}) or {})
    content = str(source.get("content") or "")
    return {
        "chunk_id": getattr(hit, "chunk_id"),
        "doc_id": getattr(hit, "doc_id"),
        "score": getattr(hit, "score"),
        "source_id": source.get("source_id"),
        "source_type": source.get("source_type"),
        "title": source.get("title"),
        "url": source.get("url"),
        "section_path": source.get("section_path"),
        "section_title": source.get("section_title"),
        "content": content[:1200],
    }
