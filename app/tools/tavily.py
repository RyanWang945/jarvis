from __future__ import annotations

import json
import logging

import httpx

from app.config import get_settings
from app.tools.common import ToolExecutionRequest, ToolExecutionResult

logger = logging.getLogger(__name__)
_MAX_OUTPUT_CHARS = 4_000
_DEFAULT_TAVILY_ENDPOINT = "https://api.tavily.com/search"


def run_tavily_search(request: ToolExecutionRequest) -> ToolExecutionResult:
    settings = get_settings()
    api_key = settings.tavily_api_key
    if not api_key:
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stderr="Tavily API key not configured. Set JARVIS_TAVILY_API_KEY.",
            summary="Tavily API key missing.",
        )

    query = str(request.args.get("query", "")).strip()
    if not query:
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stderr="Missing required argument: query",
            summary="Search query is empty.",
        )

    payload: dict[str, object] = {
        "api_key": api_key,
        "query": query,
        "search_depth": request.args.get("search_depth", "basic"),
        "topic": request.args.get("topic", "general"),
        "max_results": min(int(request.args.get("max_results", 5)), 10),
        "include_answer": True,
        "include_raw_content": False,
        "include_images": False,
    }

    include_domains = request.args.get("include_domains")
    if include_domains:
        payload["include_domains"] = include_domains
    exclude_domains = request.args.get("exclude_domains")
    if exclude_domains:
        payload["exclude_domains"] = exclude_domains

    endpoint = settings.tavily_base_url or _DEFAULT_TAVILY_ENDPOINT
    if not endpoint.endswith("/search"):
        endpoint = endpoint.rstrip("/") + "/search"
    try:
        resp = httpx.post(
            endpoint,
            json=payload,
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.exception("tavily search http error")
        body = exc.response.text[:500]
        return ToolExecutionResult(
            ok=False,
            exit_code=exc.response.status_code,
            stderr=f"Tavily API HTTP error: {exc.response.status_code} - {body}",
            summary=f"Tavily API returned {exc.response.status_code}.",
        )
    except Exception as exc:
        logger.exception("tavily search request failed")
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stderr=f"Tavily request failed: {exc}",
            summary="Failed to call Tavily API.",
        )

    output = _format_results(data)
    return ToolExecutionResult(
        ok=True,
        exit_code=0,
        stdout=output,
        summary=f"Tavily search returned {len(data.get('results', []))} results.",
    )


def _format_results(data: dict) -> str:
    parts: list[str] = []

    answer = data.get("answer")
    if answer:
        parts.append(f"Answer: {answer}")
        parts.append("")

    results = data.get("results", [])
    if results:
        parts.append("Sources:")
        for idx, item in enumerate(results, start=1):
            title = item.get("title", "Untitled")
            url = item.get("url", "")
            content = item.get("content", "")
            score = item.get("score")
            score_str = f" (score: {score:.2f})" if isinstance(score, (int, float)) else ""
            parts.append(f"{idx}. {title}{score_str}")
            if url:
                parts.append(f"   URL: {url}")
            if content:
                snippet = content.replace("\n", " ")
                if len(snippet) > 300:
                    snippet = snippet[:300] + "..."
                parts.append(f"   {snippet}")
            parts.append("")

    if not parts:
        parts.append("No results found.")

    output = "\n".join(parts)
    if len(output) > _MAX_OUTPUT_CHARS:
        output = output[:_MAX_OUTPUT_CHARS] + "\n...[truncated]"
    return output
