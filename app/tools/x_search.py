from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

import httpx

from app.config import get_settings
from app.tools.common import ToolExecutionRequest, ToolExecutionResult

logger = logging.getLogger(__name__)

_DEFAULT_XAI_ENDPOINT = "https://api.x.ai/v1/responses"
_DEFAULT_MODEL = "grok-4.20-reasoning"
_MAX_HANDLES = 10
_MAX_OUTPUT_CHARS = 4_000
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HANDLE_RE = re.compile(r"^[a-zA-Z0-9_]{1,15}$")


def run_x_search(request: ToolExecutionRequest) -> ToolExecutionResult:
    settings = get_settings()
    api_key = settings.xai_api_key
    if not api_key:
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stderr="xAI API key not configured. Set JARVIS_XAI_API_KEY.",
            summary="xAI API key missing.",
        )

    args = request.args
    query = str(args.get("query") or "").strip()
    validation_error = _validate_args(args, query)
    if validation_error is not None:
        return ToolExecutionResult(ok=False, exit_code=None, stderr=validation_error, summary=validation_error)

    payload = {
        "model": str(args.get("model") or _DEFAULT_MODEL),
        "input": [{"role": "user", "content": _query_with_result_limit(query, args.get("max_results"))}],
        "tools": [_build_tool_config(args)],
    }

    endpoint = settings.xai_base_url or _DEFAULT_XAI_ENDPOINT
    if not endpoint.endswith("/responses"):
        endpoint = endpoint.rstrip("/") + "/responses"
    try:
        resp = httpx.post(
            endpoint,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Jarvis/x-search",
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.exception("x_search http error")
        body = exc.response.text[:500]
        return ToolExecutionResult(
            ok=False,
            exit_code=exc.response.status_code,
            stderr=f"xAI API HTTP error: {exc.response.status_code} - {body}",
            summary=f"xAI API returned {exc.response.status_code}.",
        )
    except Exception as exc:
        logger.exception("x_search request failed")
        return ToolExecutionResult(
            ok=False,
            exit_code=None,
            stderr=f"xAI request failed: {exc}",
            summary="Failed to call xAI API.",
        )

    formatted = _format_response(data, query)
    stdout = json.dumps(formatted, ensure_ascii=False, indent=2)
    if len(stdout) > _MAX_OUTPUT_CHARS:
        stdout = stdout[:_MAX_OUTPUT_CHARS] + "\n...[truncated]"
    citations = formatted.get("citations", [])
    return ToolExecutionResult(
        ok=True,
        exit_code=0,
        stdout=stdout,
        summary=_build_summary(formatted, citations if isinstance(citations, list) else []),
    )


def _validate_args(args: dict[str, Any], query: str) -> str | None:
    if not query:
        return "Missing required argument: query"
    handles = _normalize_handles(args.get("handles"))
    exclude_handles = _normalize_handles(args.get("exclude_handles"))
    if handles and exclude_handles:
        return "handles and exclude_handles cannot both be provided."
    for label, values in (("handles", handles), ("exclude_handles", exclude_handles)):
        if len(values) > _MAX_HANDLES:
            return f"{label} accepts at most {_MAX_HANDLES} handles."
        invalid = [value for value in values if not _HANDLE_RE.match(value)]
        if invalid:
            return f"Invalid X handle in {label}: {invalid[0]}"
    for field_name in ("date_from", "date_to"):
        value = args.get(field_name)
        if value and not _valid_date(str(value)):
            return f"{field_name} must be a valid YYYY-MM-DD date."
    date_from = str(args.get("date_from") or "")
    date_to = str(args.get("date_to") or "")
    if date_from and date_to and date_from > date_to:
        return "date_from must be before or equal to date_to."
    max_results = args.get("max_results")
    if max_results is not None:
        try:
            value = int(max_results)
        except (TypeError, ValueError):
            return "max_results must be an integer."
        if value < 1 or value > 20:
            return "max_results must be between 1 and 20."
    return None


def _valid_date(value: str) -> bool:
    if not _DATE_RE.match(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _normalize_handles(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        values = [str(item).strip() for item in value]
    else:
        return []
    return [item.lstrip("@") for item in values if item.strip()]


def _build_tool_config(args: dict[str, Any]) -> dict[str, Any]:
    tool: dict[str, Any] = {"type": "x_search"}
    handles = _normalize_handles(args.get("handles"))
    exclude_handles = _normalize_handles(args.get("exclude_handles"))
    if handles:
        tool["allowed_x_handles"] = handles
    if exclude_handles:
        tool["excluded_x_handles"] = exclude_handles
    if args.get("date_from"):
        tool["from_date"] = str(args["date_from"])
    if args.get("date_to"):
        tool["to_date"] = str(args["date_to"])
    if bool(args.get("include_images")):
        tool["enable_image_understanding"] = True
    if bool(args.get("include_video")):
        tool["enable_video_understanding"] = True
    return tool


def _query_with_result_limit(query: str, max_results: Any) -> str:
    if max_results is None:
        return query
    return f"{query}\n\nReturn at most {int(max_results)} notable X posts or citations."


def _format_response(data: dict[str, Any], query: str) -> dict[str, Any]:
    outputs = data.get("output", [])
    if not isinstance(outputs, list):
        outputs = []
    usage = data.get("usage", {})
    if not isinstance(usage, dict):
        usage = {}
    tool_details = usage.get("server_side_tool_usage_details", {})
    if not isinstance(tool_details, dict):
        tool_details = {}

    message = next((item for item in outputs if isinstance(item, dict) and item.get("type") == "message"), None)
    content_blocks = message.get("content", []) if isinstance(message, dict) else []
    if not isinstance(content_blocks, list):
        content_blocks = []

    text = "\n\n".join(
        str(block.get("text"))
        for block in content_blocks
        if isinstance(block, dict) and block.get("text")
    )
    annotations = [
        annotation
        for block in content_blocks
        if isinstance(block, dict)
        for annotation in (block.get("annotations") or [])
        if isinstance(annotation, dict)
    ]
    citations = [
        {"title": str(annotation.get("title") or ""), "url": str(annotation["url"])}
        for annotation in annotations
        if annotation.get("type") == "url_citation" and annotation.get("url")
    ]

    status = str(data.get("status") or "unknown")
    if status not in {"completed", "unknown"}:
        error = data.get("error", {})
        error_msg = error.get("message", "") if isinstance(error, dict) else str(error)
        text = f"Search {status}" + (f": {error_msg}" if error_msg else "") + ("\n\n" + text if text else "")

    return {
        "status": status,
        "query": query,
        "text": text,
        "citations": citations,
        "searches": tool_details.get("x_search_calls", 0),
        "tokens": {
            "input": usage.get("input_tokens", 0),
            "output": usage.get("output_tokens", 0),
        },
    }


def _build_summary(formatted: dict[str, Any], citations: list[dict[str, Any]]) -> str:
    searches = formatted.get("searches", 0)
    if not citations:
        return f"x_search completed with {searches} server-side searches."
    urls = [str(item.get("url")) for item in citations if item.get("url")]
    joined = ", ".join(urls[:3])
    extra = "" if len(urls) <= 3 else f" (+{len(urls) - 3} more)"
    return f"x_search completed with {searches} server-side searches. Citation URLs: {joined}{extra}"
