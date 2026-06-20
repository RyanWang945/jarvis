from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.agent_react import ChannelMessage

_MAX_MARKDOWN_BLOCK_LEN = 3500
_MAX_CARD_ELEMENTS = 12
_MAX_CARD_JSON_LEN = 28000


@dataclass(frozen=True)
class FeishuDelivery:
    msg_type: str
    content: str


@dataclass(frozen=True)
class _ParsedTable:
    headers: list[str]
    rows: list[list[str]]
    end_index: int


@dataclass(frozen=True)
class _ModelUsageFooter:
    body: str
    model: str | None
    prompt_tokens: str
    completion_tokens: str
    total_tokens: str


class FeishuRenderer:
    def __init__(self, *, title: str = "Jarvis") -> None:
        self._title = title

    def render(self, message: ChannelMessage) -> FeishuDelivery:
        if message.content_type == "markdown":
            return self.render_markdown_card(message.content)
        return self.render_text_fallback(message.content)

    def render_thinking_card(self, prompt: str | None = None) -> FeishuDelivery:
        return self._render_card_from_blocks(
            [
                "**🟡 Jarvis Thinking**",
                "正在整理问题、检索上下文并生成答案，请稍候。",
            ],
            update_multi=True,
        )

    def render_progress_card(self, snapshot: Any) -> FeishuDelivery:
        blocks = [
            "**🟡 Jarvis 正在处理**",
            f"**当前阶段**: {_snapshot_value(snapshot, 'current_stage', '准备中')}",
            f"**正在做**: {_snapshot_value(snapshot, 'current_action', '正在处理请求')}",
        ]
        node_total = getattr(snapshot, "node_total", None)
        if isinstance(node_total, int) and node_total > 0:
            node_completed = getattr(snapshot, "node_completed", 0)
            blocks.append(f"**节点进度**: {node_completed}/{node_total}")

        completed_items = list(getattr(snapshot, "completed_items", []) or [])[-4:]
        if completed_items:
            blocks.append("**已完成**\n" + "\n".join(f"- {item}" for item in completed_items))

        recent_events = list(getattr(snapshot, "recent_events", []) or [])[-5:]
        if recent_events:
            blocks.append("**最近进展**\n" + "\n".join(f"- {item}" for item in recent_events))

        return self._render_card_from_blocks(blocks, update_multi=True)

    def render_cardkit_progress_card(self, snapshot: Any, *, output_markdown: str | None = None) -> FeishuDelivery:
        steps = _cardkit_progress_steps(snapshot)
        elements = [
            {
                "tag": "markdown",
                "element_id": "progress_steps",
                "content": steps or "正在理解请求",
            },
        ]
        output = _cardkit_output_content(snapshot, output_markdown)
        if output:
            elements.extend(
                [
                    {"tag": "hr", "element_id": "progress_output_divider"},
                    {
                        "tag": "markdown",
                        "element_id": "progress_output",
                        "content": output,
                    },
                ]
            )
        card = {
            "schema": "2.0",
            "config": {
                "update_multi": True,
                "style": {
                    "text_size": {
                        "normal_v2": {"default": "normal", "pc": "normal", "mobile": "normal"},
                    }
                },
            },
            "header": {
                "title": {"tag": "plain_text", "content": self._title},
                "template": "blue",
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": elements,
            },
        }
        return FeishuDelivery(msg_type="interactive", content=json.dumps(card, ensure_ascii=False))

    def render_error_card(self, message: str) -> FeishuDelivery:
        return self._render_card_from_blocks(
            [
                "**❌ Request Failed**",
                message.strip() or "Something went wrong.",
            ],
            update_multi=True,
        )

    def render_approval_card(
        self,
        *,
        approval_id: str,
        conversation_id: int,
        turn_id: int,
        chat_id: str = "",
        command: str = "",
        reason: str = "",
        language: str = "zh",
        approval_source: str = "codex_provider",
        payload: dict[str, Any] | None = None,
    ) -> FeishuDelivery:
        blocks = ["**Jarvis 权限审批**"]
        elements = self._blocks_to_elements(blocks)
        if command:
            elements.extend(_command_elements(command))
        if reason:
            elements.extend(_reason_elements(reason, language=language))
        elements.extend(self._blocks_to_elements(["该操作需要确认后继续。"]))
        action_value = {
            "source": "jarvis_codex_approval",
            "approval_id": approval_id,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "chat_id": chat_id,
            "command": command,
            "reason": reason,
            "language": language,
            "approval_source": approval_source,
            "payload": payload or {},
        }
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "同意"},
                        "type": "primary",
                        "behaviors": [
                            {
                                "type": "callback",
                                "value": {**action_value, "decision": "approve"},
                            }
                        ],
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "type": "danger",
                        "behaviors": [
                            {
                                "type": "callback",
                                "value": {**action_value, "decision": "reject"},
                            }
                        ],
                    },
                ],
            }
        )
        return self._render_card_from_elements(elements, update_multi=True)

    def render_approval_decision_card(
        self,
        *,
        decision: str,
        command: str = "",
        reason: str = "",
        language: str = "zh",
    ) -> FeishuDelivery:
        label = {
            "approve": "已同意",
            "approved": "已同意",
            "reject": "已拒绝",
            "rejected": "已拒绝",
            "completed": "已完成",
            "failed": "已失败",
            "timeout": "已超时",
            "missing": "已失效",
        }.get(decision, "已处理")
        blocks = [f"**Jarvis 权限审批：{label}**"]
        elements = self._blocks_to_elements(blocks)
        if command:
            elements.extend(_command_elements(command))
        if reason:
            elements.extend(_reason_elements(reason, language=language))
        return self._render_card_from_elements(elements, update_multi=True)

    def render_markdown_card(self, markdown: str) -> FeishuDelivery:
        usage_footer = extract_model_usage_footer(markdown)
        markdown_body = usage_footer.body if usage_footer is not None else markdown
        normalized = normalize_markdown(markdown_body)
        if not normalized:
            if usage_footer is None:
                return self.render_text_fallback("")
            return self._render_card_from_elements(
                [
                    *_model_usage_elements(usage_footer),
                ],
                update_multi=True,
            )

        blocks = split_markdown_blocks(normalized)
        if len(blocks) > _MAX_CARD_ELEMENTS:
            return self.render_text_fallback(downgrade_markdown_to_text(normalized))
        elements = self._blocks_to_elements(["**✅ Completed**", *blocks])
        if usage_footer is not None:
            elements.extend(_model_usage_elements(usage_footer))
        return self._render_card_from_elements(elements, update_multi=True)

    def render_text_fallback(self, text: str) -> FeishuDelivery:
        return FeishuDelivery(
            msg_type="text",
            content=json.dumps({"text": text}, ensure_ascii=False),
        )

    def _render_card_from_blocks(
        self,
        blocks: list[str],
        *,
        update_multi: bool,
    ) -> FeishuDelivery:
        return self._render_card_from_elements(self._blocks_to_elements(blocks), update_multi=update_multi)

    def _blocks_to_elements(self, blocks: list[str]) -> list[dict]:
        return [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": block,
                },
            }
            for block in blocks
        ]

    def _render_card_from_elements(
        self,
        elements: list[dict],
        *,
        update_multi: bool,
    ) -> FeishuDelivery:
        card = {
            "config": {
                "wide_screen_mode": True,
                "enable_forward": True,
                "update_multi": update_multi,
            },
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": self._title,
                }
            },
            "elements": elements,
        }
        payload = json.dumps(card, ensure_ascii=False)
        if len(payload) > _MAX_CARD_JSON_LEN:
            blocks = []
            for element in elements:
                text = element.get("text") if isinstance(element, dict) else None
                if isinstance(text, dict):
                    blocks.append(str(text.get("content") or ""))
            return self.render_text_fallback(downgrade_markdown_to_text("\n\n".join(blocks)))
        return FeishuDelivery(msg_type="interactive", content=payload)


def normalize_markdown(markdown: str) -> str:
    normalized = markdown.replace("\r\n", "\n").strip()
    normalized = adapt_markdown_for_feishu(normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized


def extract_model_usage_footer(markdown: str) -> _ModelUsageFooter | None:
    pattern = re.compile(
        r"""
        (?P<body>.*?)
        (?:\n{2,}|\A)
        ---\s*
        \n(?:-\s*模型：`(?P<model>[^`]+)`\s*\n)?
        -\s*Token：输入\s*`(?P<prompt>\d+)`\s*/\s*输出\s*`(?P<completion>\d+)`\s*/\s*合计\s*`(?P<total>\d+)`\s*
        \Z
        """,
        flags=re.DOTALL | re.VERBOSE,
    )
    match = pattern.match(markdown.strip())
    if match is None:
        return None
    return _ModelUsageFooter(
        body=match.group("body").rstrip(),
        model=match.group("model").strip() if match.group("model") else None,
        prompt_tokens=match.group("prompt").strip(),
        completion_tokens=match.group("completion").strip(),
        total_tokens=match.group("total").strip(),
    )


def split_markdown_blocks(markdown: str) -> list[str]:
    if len(markdown) <= _MAX_MARKDOWN_BLOCK_LEN:
        return [markdown]

    lines = markdown.split("\n")
    blocks: list[str] = []
    current: list[str] = []
    current_len = 0
    in_fence = False

    for line in lines:
        if line.strip().startswith("```"):
            in_fence = not in_fence

        line_len = len(line) + 1
        should_flush = (
            current
            and current_len + line_len > _MAX_MARKDOWN_BLOCK_LEN
            and not in_fence
        )
        if should_flush:
            blocks.append("\n".join(current).strip())
            current = []
            current_len = 0

        if current and current_len + line_len > _MAX_MARKDOWN_BLOCK_LEN and in_fence:
            blocks.append("\n".join(current).strip())
            current = []
            current_len = 0

        if line_len > _MAX_MARKDOWN_BLOCK_LEN and not in_fence:
            if current:
                blocks.append("\n".join(current).strip())
                current = []
                current_len = 0
            blocks.extend(_split_long_line(line))
            continue

        current.append(line)
        current_len += line_len

    if current:
        blocks.append("\n".join(current).strip())

    return [block for block in blocks if block]


def downgrade_markdown_to_text(markdown: str) -> str:
    text = markdown
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"[image: \1] \2", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1: \2", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "- ", text, flags=re.MULTILINE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def adapt_markdown_for_feishu(markdown: str) -> str:
    lines = markdown.split("\n")
    rendered: list[str] = []
    in_fence = False
    index = 0

    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            in_fence = not in_fence
            rendered.append(stripped)
            index += 1
            continue

        if in_fence:
            rendered.append(line)
            index += 1
            continue

        if not stripped:
            if rendered and rendered[-1] != "":
                rendered.append("")
            index += 1
            continue

        table = _parse_table(lines, index)
        if table is not None:
            if rendered and rendered[-1] != "":
                rendered.append("")
            rendered.extend(_render_table_as_lines(table))
            rendered.append("")
            index = table.end_index
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            title = _adapt_inline_markdown(_strip_trailing_heading_marks(heading.group(2)))
            if rendered and rendered[-1] != "":
                rendered.append("")
            rendered.append(title if _is_strong_markdown(title) else f"**{title}**")
            rendered.append("")
            index += 1
            continue

        quote = re.match(r"^>\s?(.*)$", stripped)
        if quote:
            if rendered and rendered[-1] != "":
                rendered.append("")
            rendered.append("**Quote**")
            rendered.append(_adapt_inline_markdown(quote.group(1)))
            rendered.append("")
            index += 1
            continue

        ordered = re.match(r"^(\d+)\.\s+(.+)$", stripped)
        if ordered:
            rendered.append(f"{ordered.group(1)}. {_adapt_inline_markdown(ordered.group(2))}")
            index += 1
            continue

        bullet = re.match(r"^([*+-])\s+(.+)$", stripped)
        if bullet:
            rendered.append(f"- {_adapt_inline_markdown(bullet.group(2))}")
            index += 1
            continue

        rendered.append(_adapt_inline_markdown(stripped))
        index += 1

    while rendered and rendered[-1] == "":
        rendered.pop()
    return "\n".join(rendered)


def _strip_trailing_heading_marks(value: str) -> str:
    return re.sub(r"\s+#+\s*$", "", value).strip()


def _adapt_inline_markdown(text: str) -> str:
    text = text.strip()
    quad_label = re.match(r"^\*{4}\s*(?P<label>[^|:：]+?)\s*(?P<sep>[|:：])\s*(?P<value>.*)$", text)
    if quad_label:
        label = re.sub(r"\*+\s*$", "", quad_label.group("label")).strip()
        value = quad_label.group("value").strip()
        sep = quad_label.group("sep")
        if sep == "|":
            return f"**{label}** | {value}" if value else f"**{label}** |"
        return f"**{label}**: {value}" if value else f"**{label}**:"

    quad_wrapped = re.match(r"^\*{4}\s*(?P<body>.+?)\s*\*{2,4}$", text)
    if quad_wrapped:
        return f"**{quad_wrapped.group('body').strip()}**"

    quad_open = re.match(r"^\*{4}\s*(?P<body>.+)$", text)
    if quad_open:
        return f"**{quad_open.group('body').strip()}**"

    return text


def _is_strong_markdown(text: str) -> bool:
    return len(text) > 4 and text.startswith("**") and text.endswith("**")


def _parse_table(lines: list[str], start_index: int) -> _ParsedTable | None:
    if start_index + 1 >= len(lines):
        return None
    header_line = lines[start_index].strip()
    separator_line = lines[start_index + 1].strip()
    if not _looks_like_table_row(header_line) or not _looks_like_table_separator(separator_line):
        return None

    headers = _split_table_row(header_line)
    rows: list[list[str]] = []
    index = start_index + 2
    while index < len(lines):
        candidate = lines[index].strip()
        if not _looks_like_table_row(candidate):
            break
        values = _split_table_row(candidate)
        if len(values) < len(headers):
            values.extend([""] * (len(headers) - len(values)))
        rows.append(values[: len(headers)])
        index += 1

    if not rows:
        return None
    return _ParsedTable(headers=headers, rows=rows, end_index=index)


def _render_table_as_lines(table: _ParsedTable) -> list[str]:
    rendered: list[str] = []
    for row in table.rows:
        rendered.extend(_render_table_row(table.headers, row))
        rendered.append("")
    while rendered and rendered[-1] == "":
        rendered.pop()
    return rendered


def _render_table_row(headers: list[str], row: list[str]) -> list[str]:
    pairs = list(zip(headers, row, strict=False))
    if not pairs:
        return []

    primary: list[str] = []
    details: list[str] = []
    for index, (header, value) in enumerate(pairs):
        safe_value = value or "-"
        if index < 2:
            primary.append(safe_value)
        else:
            details.append(f"**{header}**: {safe_value}")

    title = " | ".join(primary) if primary else "Record"
    rendered = [f"**{title}**"]
    if details:
        rendered.extend(details)
    return rendered


def _looks_like_table_row(line: str) -> bool:
    return line.count("|") >= 2 and not line.startswith("```")


def _looks_like_table_separator(line: str) -> bool:
    if "|" not in line:
        return False
    parts = _split_table_row(line)
    if not parts:
        return False
    return all(re.fullmatch(r":?-{3,}:?", part) for part in parts)


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    if not stripped:
        return []
    return [cell.strip() for cell in stripped.split("|")]


def _split_long_line(line: str) -> list[str]:
    parts: list[str] = []
    remaining = line
    while remaining:
        parts.append(remaining[:_MAX_MARKDOWN_BLOCK_LEN].strip())
        remaining = remaining[_MAX_MARKDOWN_BLOCK_LEN:]
    return [part for part in parts if part]


def _command_elements(command: str) -> list[dict]:
    return [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**命令**",
            },
        },
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": _truncate_card_text(command, 1600),
            },
        },
    ]


def _model_usage_elements(footer: _ModelUsageFooter) -> list[dict]:
    return [
        {"tag": "hr"},
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": f"Token：输入 {footer.prompt_tokens} / 输出 {footer.completion_tokens} / 合计 {footer.total_tokens}",
                }
            ],
        },
    ]


def _reason_elements(reason: str, *, language: str) -> list[dict]:
    return [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**原因**\n" + _truncate_card_text(_localize_approval_reason(reason, language=language), 1200),
            },
        }
    ]


def _localize_approval_reason(reason: str, *, language: str) -> str:
    text = str(reason).strip()
    if language != "zh":
        return text
    if not text or re.search(r"[\u4e00-\u9fff]", text):
        return text
    normalized = re.sub(r"\s+", " ", text).strip().rstrip("?")
    lower = normalized.lower()
    if lower.startswith("do you want to allow "):
        action = normalized[len("Do you want to allow ") :]
        action_lower = action.lower()
        if "push" in action_lower and "origin" in action_lower:
            return "是否允许将新的提交推送到 origin 远程仓库？"
        if "push" in action_lower:
            return "是否允许将本地提交推送到远程仓库？"
        if "staging" in action_lower or "stage" in action_lower:
            return "是否允许暂存本次仓库变更？"
        if "creating" in action_lower and "commit" in action_lower:
            return "是否允许在该仓库创建请求的 git commit？"
        if "commit" in action_lower:
            return "是否允许执行本次 git commit 操作？"
        if "install" in action_lower or "dependency" in action_lower:
            return "是否允许安装任务所需的依赖？"
        return f"是否允许执行该提权操作：{action}？"
    return text


def _truncate_card_text(text: str, limit: int) -> str:
    normalized = str(text).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 14].rstrip() + "\n...[truncated]"


def _snapshot_value(snapshot: Any, name: str, default: str) -> str:
    value = str(getattr(snapshot, name, "") or "").strip()
    return _truncate_card_text(value or default, 300)


def _cardkit_progress_steps(snapshot: Any) -> str:
    lines: list[str] = []
    completed_items = [_truncate_card_text(item, 140) for item in list(getattr(snapshot, "completed_items", []) or [])[-6:]]
    if "生成执行计划" in completed_items:
        lines.append(_aligned_check_line("生成执行计划"))
    planned_nodes = list(getattr(snapshot, "planned_nodes", []) or [])
    completed_node_ids = set(getattr(snapshot, "completed_node_ids", []) or [])
    for node in planned_nodes:
        if not isinstance(node, dict):
            continue
        label = _cardkit_node_label(node)
        if not label:
            continue
        line = f"  {label}"
        if str(node.get("id") or "") in completed_node_ids:
            line = _aligned_check_line(line)
        lines.append(line)
    for item in completed_items:
        if item and item != "生成执行计划" and not item.startswith("完成 "):
            lines.append(_aligned_check_line(item))
    current = _snapshot_value(snapshot, "current_action", "")
    status = str(getattr(snapshot, "status", "running") or "running")
    if current and status != "completed" and not planned_nodes and current not in completed_items:
        lines.append(_truncate_card_text(current, 140))
    if status == "completed" and not any("任务已完成" in line for line in lines):
        lines.append(_aligned_check_line("任务已完成"))
    node_total = getattr(snapshot, "node_total", None)
    node_completed = getattr(snapshot, "node_completed", 0)
    if isinstance(node_total, int) and node_total > 0 and node_completed < node_total:
        remaining = max(node_total - node_completed - 1, 0)
        lines.extend(["等待后续节点"] * min(remaining, 2))
    return "\n".join(lines[-6:])


def _aligned_check_line(text: str) -> str:
    return f"{text}\t✓"


def _cardkit_node_label(node: dict[str, Any]) -> str:
    node_id = str(node.get("id") or "").strip()
    runtime = str(node.get("runtime") or "").strip()
    objective = str(node.get("objective") or "").strip()
    label = node_id or objective
    if runtime and label:
        label = f"{label} ({runtime})"
    return _truncate_card_text(label, 140)


def _cardkit_output_content(snapshot: Any, output_markdown: str | None) -> str:
    if output_markdown is not None:
        output = normalize_markdown(output_markdown)
        return output or "结果已生成。"
    if not bool(getattr(snapshot, "output_started", False)):
        return ""
    return "正在生成结果..."
