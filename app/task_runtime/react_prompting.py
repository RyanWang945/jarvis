from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection

from app.task_runtime.runtime_context import RuntimeContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeSkill:
    skill_id: str
    reason: str
    content: str


_WEB_SEARCH_SKILL_ID = "runtime/web-search"
_RUNTIME_SKILL_PATHS = {
    _WEB_SEARCH_SKILL_ID: Path("skills/runtime/web-search.md"),
}

_FAST_CHANGING_TERMS = (
    "current",
    "latest",
    "recent",
    "today",
    "now",
    "live",
    "price",
    "quote",
    "market",
    "stock",
    "crypto",
    "bitcoin",
    "exchange rate",
    "weather",
    "news",
    "schedule",
    "policy",
    "regulation",
    "version",
    "availability",
    "inventory",
    "当前",
    "最新",
    "最近",
    "今天",
    "现在",
    "实时",
    "行情",
    "价格",
    "报价",
    "金价",
    "油价",
    "股价",
    "汇率",
    "天气",
    "新闻",
    "政策",
    "法规",
    "库存",
    "版本",
    "赛程",
)

_EXTERNAL_INFO_TERMS = (
    "web",
    "internet",
    "lookup",
    "web search",
    "internet search",
    "网页",
    "互联网",
    "网络搜索",
    "网页搜索",
    "外部信息",
    "外部事实",
)


def select_runtime_skills(
    context: Any,
    *,
    available_tool_names: Collection[str] | None = None,
) -> list[RuntimeSkill]:
    """Select runtime-owned skills for a React node.

    This intentionally does not depend on planner-emitted required_skills.
    The runtime chooses execution guidance from the node objective and the
    currently available tools.
    """

    node = getattr(context, "node", None)
    if str(getattr(node, "runtime", "") or "").strip() != "react":
        return []
    objective = str(getattr(node, "objective", "") or "")
    mode = str(getattr(node, "mode", "read") or "read")
    tool_names = set(available_tool_names or ())
    if tool_names and "tavily_search" not in tool_names:
        return []
    if _looks_like_web_search_objective(objective):
        content = _load_runtime_skill(_WEB_SEARCH_SKILL_ID)
        if content:
            return [
                RuntimeSkill(
                    skill_id=_WEB_SEARCH_SKILL_ID,
                    reason=(
                        "node objective appears to require external or time-sensitive web evidence"
                        if mode == "read"
                        else "node objective may require external web evidence before producing an artifact"
                    ),
                    content=content,
                )
            ]
    return []


def build_runtime_skill_system_section(skills: Collection[RuntimeSkill]) -> str:
    selected = [skill for skill in skills if skill.content.strip()]
    if not selected:
        return ""
    parts = ["## Selected Runtime Skills", "ReactRuntime selected these skills internally from the node objective."]
    for skill in selected:
        parts.extend(
            [
                "",
                f"### {skill.skill_id}",
                "",
                f"Selection reason: {skill.reason}",
                "",
                skill.content.strip(),
            ]
        )
    return "\n".join(parts).strip()


def build_react_user_prompt(
    context: Any,
    *,
    selected_runtime_skills: Collection[RuntimeSkill] = (),
) -> str:
    """Build the user prompt passed to a React node agent.

    The prompt is task-shaped instead of a raw JSON dump. Read nodes get only
    task semantics, time context, resolved inputs, and selected runtime skills.
    Workspace paths stay out of the LLM context unless the node may write.
    """

    node = getattr(context, "node", None)
    runtime_context = (
        getattr(context, "runtime_context", None)
        or RuntimeContext.from_hints(getattr(context, "legacy_hints", {}) or {})
    )
    temporal = getattr(runtime_context, "temporal", None)
    mode = str(getattr(node, "mode", "read") or "read")
    skill_ids = [skill.skill_id for skill in selected_runtime_skills]

    lines = [
        "你正在执行一个 Jarvis React 节点。只完成本节点，不生成最终用户回复。",
        "",
        "## Task",
        "",
        f"节点 ID：{_text(getattr(node, 'id', ''))}",
        f"执行模式：{mode}",
        "",
        "节点目标：",
        _text(getattr(node, "objective", "")),
        "",
        "## Time Context",
        "",
    ]
    if temporal is not None and temporal.current_date:
        lines.append(f"当前日期：{temporal.current_date}")
    if temporal is not None and temporal.current_time:
        lines.append(f"当前时间：{temporal.current_time}")
    if temporal is not None and temporal.timezone:
        lines.append(f"时区：{temporal.timezone}")
    if lines[-1] == "":
        lines.append("无。")

    lines.extend(
        [
            "",
            "## Selected Runtime Skills",
            "",
            _format_skill_ids(skill_ids),
            "",
            "## Resolved Inputs",
            "",
            _format_resolved_inputs(getattr(context, "resolved_inputs", []) or []),
        ]
    )

    instructions = [str(item).strip() for item in (getattr(context, "instructions", []) or []) if str(item).strip()]
    if instructions:
        lines.extend(["", "## Additional Instructions", "", _json_block(instructions)])

    if mode != "read":
        workspace_context = _write_mode_workspace_context(runtime_context)
        if workspace_context:
            lines.extend(["", "## Workspace Context", "", _json_block(workspace_context)])

    lines.extend(
        [
            "",
            "## Output",
            "",
            "返回符合 schema 的 JSON：status、summary、findings、sources、data、artifacts。",
        ]
    )
    return "\n".join(lines).strip()


def _looks_like_web_search_objective(objective: str) -> bool:
    text = objective.strip().lower()
    if not text:
        return False
    return any(term in text for term in _FAST_CHANGING_TERMS) or any(
        term in text for term in _EXTERNAL_INFO_TERMS
    )


def _load_runtime_skill(skill_id: str) -> str:
    relative = _RUNTIME_SKILL_PATHS.get(skill_id)
    if relative is None:
        return ""
    path = Path(__file__).resolve().parents[2] / relative
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        logger.warning("runtime skill missing skill_id=%s path=%s", skill_id, path)
        return ""


def _format_skill_ids(skill_ids: list[str]) -> str:
    if not skill_ids:
        return "无。"
    return "\n".join(f"- {skill_id}" for skill_id in skill_ids)


def _format_resolved_inputs(items: list[Any]) -> str:
    if not items:
        return "无。"
    payload = []
    for item in items:
        if hasattr(item, "model_dump"):
            payload.append(item.model_dump(mode="json", exclude_none=True))
        else:
            payload.append(item)
    return _json_block(payload)


def _write_mode_workspace_context(runtime_context: RuntimeContext) -> dict[str, str]:
    workspace = runtime_context.workspace
    data: dict[str, str] = {}
    if workspace.session_root is not None:
        data["session_workspace_dir"] = str(workspace.session_root)
    if workspace.node_workspace is not None:
        data["node_workspace_dir"] = str(workspace.node_workspace)
    if workspace.manifest_path_text:
        data["node_manifest_path"] = workspace.manifest_path_text
    return data


def _json_block(value: Any) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n```"


def _text(value: Any) -> str:
    return str(value or "").strip()
