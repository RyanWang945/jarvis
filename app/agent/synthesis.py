from __future__ import annotations

import json
import re
from typing import Any

from app.agent.common import payload
from app.agent.state import AgentState
from app.config import get_settings
from app.llm.jarvis import get_jarvis_llm
from app.workers import WorkResult


def synthesize_final_answer(state: AgentState) -> str | None:
    settings = get_settings()
    if settings.planner_type != "llm":
        return None

    event_payload = payload(state)
    instruction = str(event_payload.get("instruction") or "")
    if not instruction:
        return None

    worker_results = final_answer_worker_results(state)
    if not worker_results:
        return None

    try:
        answer = get_jarvis_llm().synthesize_final_answer(
            instruction=instruction,
            tasks=final_answer_tasks(state),
            worker_results=worker_results,
        )
    except Exception:
        return fallback_final_answer(instruction=instruction, worker_results=worker_results)
    return answer or None


def final_answer_tasks(state: AgentState) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for task in state.get("task_list", []):
        tasks.append(
            {
                "title": task.get("title"),
                "description": task.get("description"),
                "status": task.get("status"),
                "dod": task.get("dod"),
                "tool_name": task.get("tool_name"),
                "worker_type": task.get("worker_type"),
                "tool_args": task.get("tool_args"),
                "result_summary": task.get("result_summary"),
                "order_id": task.get("order_id"),
            }
        )
    return tasks


def final_answer_worker_results(state: AgentState) -> list[dict[str, Any]]:
    worker_results = state.get("worker_results", {})
    results: list[dict[str, Any]] = []
    for task in state.get("task_list", []):
        order_id = task.get("order_id")
        if not order_id or order_id not in worker_results:
            continue
        result = WorkResult(**worker_results[order_id])
        results.append(
            {
                "order_id": result.order_id,
                "task_id": result.task_id,
                "worker_type": result.worker_type,
                "ok": result.ok,
                "summary": result.summary,
                "stdout": compact_stdout_for_final_answer(result.stdout),
                "stderr": truncate_for_final_answer(result.stderr, limit=2000),
                "artifacts": result.artifacts,
            }
        )
    return results


def compact_stdout_for_final_answer(stdout: str) -> str:
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return truncate_for_final_answer(stdout)
    if not isinstance(parsed, dict):
        return truncate_for_final_answer(stdout)

    results = parsed.get("results")
    if not isinstance(results, list):
        return truncate_for_final_answer(stdout)

    compact: dict[str, Any] = {
        "query": parsed.get("query"),
        "answer": parsed.get("answer"),
        "results": [],
    }
    for item in results[:5]:
        if not isinstance(item, dict):
            continue
        snippet = item.get("snippet") or item.get("content") or ""
        compact["results"].append(
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": truncate_for_final_answer(str(snippet), limit=700),
            }
        )
    return json.dumps(compact, ensure_ascii=False)


def fallback_final_answer(*, instruction: str, worker_results: list[dict[str, Any]]) -> str | None:
    for result in worker_results:
        answer = fallback_search_answer(instruction=instruction, stdout=str(result.get("stdout") or ""))
        if answer:
            return answer
    return None


def fallback_search_answer(*, instruction: str, stdout: str) -> str | None:
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return fallback_text_answer(instruction=instruction, stdout=stdout)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
        return None

    lines = [f"根据搜索结果，{instruction}："]
    answer = parsed.get("answer")
    if answer:
        lines.extend(["", str(answer).strip()])
    else:
        summary = summary_from_search_items(parsed["results"])
        if summary:
            lines.extend(["", "摘要：", summary])

    lines.extend(["", "来源："])
    for index, item in enumerate(parsed["results"][:5], start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "Untitled").strip()
        url = str(item.get("url") or "").strip()
        snippet = str(item.get("snippet") or item.get("content") or "").strip()
        lines.append(f"{index}. {title}")
        if url:
            lines.append(f"   {url}")
        if snippet:
            lines.append(f"   {truncate_for_final_answer(snippet, limit=280)}")
    return "\n".join(lines).strip()


def fallback_text_answer(*, instruction: str, stdout: str) -> str | None:
    items = parse_text_search_items(stdout)
    urls = [item["url"] for item in items if item.get("url")]
    if not urls:
        return None

    lines = [f"根据搜索结果，{instruction}："]
    summary = summary_from_search_items(items)
    if summary:
        lines.extend(["", "摘要：", summary])

    lines.extend(["", "来源："])
    for index, item in enumerate(items[:5], start=1):
        title = item.get("title") or "Untitled"
        url = item.get("url") or ""
        lines.append(f"{index}. {title}")
        if url:
            lines.append(f"   {url}")
    return "\n".join(lines).strip()


def summary_from_search_items(items: list[Any]) -> str | None:
    snippets: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        snippet = str(item.get("snippet") or item.get("content") or "").strip()
        if not snippet:
            continue
        snippets.append(truncate_for_final_answer(clean_search_snippet(snippet), limit=260))
        if len(snippets) >= 3:
            break
    if not snippets:
        return None
    return "\n".join(f"- {snippet}" for snippet in snippets)


def clean_search_snippet(snippet: str) -> str:
    cleaned = re.sub(r"\s+", " ", snippet).strip()
    return cleaned


def parse_text_search_items(text: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    has_url = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        title_match = re.match(r"^\d+\.\s+(.+)$", line)
        if title_match:
            if current:
                items.append(current)
            current = {"title": title_match.group(1).strip(), "url": "", "snippet": ""}
            has_url = False
            continue
        if stripped.startswith(("http://", "https://")):
            if current is None:
                current = {"title": "", "url": "", "snippet": ""}
            current["url"] = stripped
            has_url = True
            continue
        if current is not None and (stripped.startswith("- ") or has_url):
            snippet = stripped[2:].strip() if stripped.startswith("- ") else stripped
            current["snippet"] = f"{current.get('snippet', '')} {snippet}".strip()

    if current:
        items.append(current)

    if items:
        return items
    return [{"title": "", "url": url, "snippet": ""} for url in extract_urls_from_text(text)]


def extract_urls_from_text(text: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(r"https?://[^\s)>\"]+", text):
        url = match.group(0).rstrip(".,;]")
        if url not in urls:
            urls.append(url)
    return urls


def truncate_for_final_answer(value: str, *, limit: int = 12000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]"
