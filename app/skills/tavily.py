import json
from typing import Any

import httpx

from app.config import get_settings
from app.skills.base import SkillRequest, SkillResult

MAX_TAVILY_RESULTS = 8
MAX_TAVILY_OUTPUT_CHARS = 12_000


class TavilySearchSkill:
    name = "web_search"

    def run(self, request: SkillRequest) -> SkillResult:
        query = str(request.args.get("query", "")).strip()
        if not query:
            return SkillResult(ok=False, exit_code=None, stderr="missing query", summary="Missing search query.")

        settings = get_settings()
        if not settings.tavily_api_key:
            return SkillResult(
                ok=False,
                exit_code=None,
                stderr="JARVIS_TAVILY_API_KEY is not configured.",
                summary="Tavily API key is not configured.",
            )

        max_results = _bounded_int(request.args.get("max_results"), default=5, minimum=1, maximum=MAX_TAVILY_RESULTS)
        payload = {
            "query": query,
            "max_results": max_results,
            "search_depth": request.args.get("search_depth") or "basic",
            "include_answer": bool(request.args.get("include_answer", True)),
            "include_raw_content": bool(request.args.get("include_raw_content", False)),
        }

        try:
            response = httpx.post(
                f"{settings.tavily_base_url.rstrip('/')}/search",
                headers={
                    "Authorization": f"Bearer {settings.tavily_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=request.timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            return SkillResult(ok=False, exit_code=None, stderr=str(exc), summary=f"Tavily search failed: {exc}")

        normalized = _normalize_tavily_response(body)
        stdout = _truncate(json.dumps(normalized, ensure_ascii=False, indent=2))
        result_count = len(normalized.get("results", []))
        return SkillResult(
            ok=True,
            exit_code=0,
            stdout=stdout,
            summary=f"Found {result_count} result(s) for: {query}",
        )


def _normalize_tavily_response(body: dict[str, Any]) -> dict[str, Any]:
    results = body.get("results") if isinstance(body.get("results"), list) else []
    return {
        "query": body.get("query"),
        "answer": body.get("answer"),
        "results": [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "content": item.get("content"),
                "score": item.get("score"),
            }
            for item in results
            if isinstance(item, dict)
        ],
    }


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _truncate(value: str) -> str:
    if len(value) <= MAX_TAVILY_OUTPUT_CHARS:
        return value
    return value[:MAX_TAVILY_OUTPUT_CHARS] + "\n...[truncated]"
