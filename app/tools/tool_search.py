from __future__ import annotations

import json
import re
from typing import Any

from app.repositories import RepositoryRegistryError, get_repository_registry
from app.tools.common import ToolExecutionRequest, ToolExecutionResult


def run_tool_search(request: ToolExecutionRequest) -> ToolExecutionResult:
    query = str(request.args.get("query") or "").strip()
    original = str(request.args.get("original_user_request") or "").strip()
    max_results = _coerce_max_results(request.args.get("max_results"))
    if original and _looks_like_context_explanation(original.lower()):
        payload = {
            "status": "no_capable_tool",
            "reason": (
                "No hidden Jarvis tool is needed or suitable for this request. "
                "Answer from the visible conversation context, or ask the user for clarification."
            ),
            "candidates": [],
        }
        stdout = json.dumps(payload, ensure_ascii=False)
        return ToolExecutionResult(ok=True, exit_code=0, stdout=stdout, summary=payload["status"])
    text = " ".join(part for part in (original, query) if part).strip()
    candidates = _candidate_tools(text)
    if max_results > 0:
        candidates = candidates[:max_results]

    if not candidates:
        payload = {
            "status": "no_capable_tool",
            "reason": (
                "No hidden Jarvis tool is needed or suitable for this request. "
                "Answer from the visible conversation context, or ask the user for clarification."
            ),
            "candidates": [],
        }
    else:
        payload = {
            "status": "found",
            "candidates": candidates,
            "selection_instruction": (
                "Select only a candidate that directly matches the original user intent. "
                "The runtime will expose approved candidates for this turn only."
            ),
        }
    stdout = json.dumps(payload, ensure_ascii=False)
    return ToolExecutionResult(ok=True, exit_code=0, stdout=stdout, summary=payload["status"])


def _coerce_max_results(value: object) -> int:
    try:
        return max(0, min(int(value), 5))
    except (TypeError, ValueError):
        return 3


def _candidate_tools(text: str) -> list[dict[str, Any]]:
    lowered = text.lower()
    if _looks_like_context_explanation(lowered):
        return []

    candidates: list[dict[str, Any]] = []
    if _looks_like_reminder(lowered):
        candidates.append(_candidate("scheduled_task", "high", "low", "Create, list, or remove reminders from explicit reminder intent."))
    if _looks_like_repo_work(lowered):
        candidates.append(_candidate("delegate_to_codex", "high", "high", "Inspect or modify a registered local repository."))
    if _looks_like_web_search(lowered):
        candidates.append(_candidate("tavily_search", "high", "low", "Search the web for current or external facts."))
    if _looks_like_social_search(lowered):
        candidates.append(_candidate("x_search", "high", "low", "Search X/Twitter posts and public social reactions."))
    if _looks_like_wiki_write(lowered):
        candidates.append(_candidate("obsidian_wiki_draft", "medium", "medium", "Draft a wiki page or note before applying it."))
    if _looks_like_memory_query(lowered):
        candidates.append(_candidate("obsidian_wiki_query", "medium", "low", "Search Jarvis long-term project memory."))
    if _looks_like_business_knowledge(lowered):
        candidates.append(_candidate("business_knowledge_search", "medium", "low", "Search configured business or knowledge-base corpora."))
    return _dedupe(candidates)


def _candidate(tool_name: str, fit: str, risk: str, reason: str) -> dict[str, str]:
    return {
        "tool_name": tool_name,
        "fit": fit,
        "risk_level": risk,
        "reason": reason,
    }


def _dedupe(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for candidate in candidates:
        name = str(candidate.get("tool_name") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        deduped.append(candidate)
    return deduped


def _looks_like_context_explanation(text: str) -> bool:
    explanation_markers = ("什么意思", "啥意思", "what does", "what do these", "explain", "meaning")
    action_markers = (
        "提醒",
        "remind",
        "notify",
        "叫醒",
        "稍后",
        "之后",
        "分钟后",
        "小时后",
        "repo",
        "repository",
        "项目",
        "仓库",
        "代码",
        "git",
        "diff",
        "branch",
        "commit",
        "push",
        "latest",
        "最新",
        "search",
        "查一下",
        "查询",
    )
    return any(marker in text for marker in explanation_markers) and not any(marker in text for marker in action_markers)


def _looks_like_reminder(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "提醒",
            "remind",
            "notify me",
            "叫醒",
            "起床",
            "稍后通知",
            "到点",
            "定时",
            "分钟后",
            "小时后",
            "tomorrow",
            "明天",
        )
    )


def _looks_like_repo_work(text: str) -> bool:
    if any(marker in text for marker in ("repo", "repository", "仓库", "项目", "代码", "git", "diff", "branch", "commit", "push", "未提交")):
        return True
    if re.search(
        r"(?<![a-z0-9_./\\-])[\w./\\-]+\."
        r"(py|ts|tsx|js|jsx|md|rst|toml|yaml|yml|json|sql|css|html)"
        r"(?![a-z0-9_./\\-])",
        text,
    ):
        return True
    if any(
        marker in text
        for marker in (
            "app/",
            "app\\",
            "tests/",
            "tests\\",
            "scripts/",
            "scripts\\",
            "docs/",
            "docs\\",
            "utils/",
            "utils\\",
        )
    ):
        return True
    try:
        registry = get_repository_registry()
        return any(str(repo.repo_id).lower() in text or str(repo.name).lower() in text for repo in registry.list_repositories())
    except RepositoryRegistryError:
        return False


def _looks_like_web_search(text: str) -> bool:
    return any(marker in text for marker in ("latest", "current news", "recent", "today", "最新", "最近", "新闻", "当前事件", "网上", "搜索网页"))


def _looks_like_social_search(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "x/twitter",
            "twitter",
            "tweet",
            "tweets",
            "x post",
            "x posts",
            "on x",
            "社交舆情",
            "推特",
            "推文",
            "x上",
            "x 上",
            "大家怎么说",
            "网友怎么说",
        )
    )


def _looks_like_wiki_write(text: str) -> bool:
    return any(marker in text for marker in ("write this design", "write to wiki", "写入wiki", "写到wiki", "沉淀", "记录到知识库", "保存到知识库"))


def _looks_like_memory_query(text: str) -> bool:
    return any(marker in text for marker in ("wiki", "知识库", "长期记忆", "之前", "设计记录", "decision", "决策"))


def _looks_like_business_knowledge(text: str) -> bool:
    if re.search(r"\b(sec|10-k|10-q|filing|company|business knowledge)\b", text):
        return True
    return any(marker in text for marker in ("业务知识", "公司知识", "研报", "财报"))
