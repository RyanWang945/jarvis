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
            return self.render_markdown_card(
                message.content,
                usage_footer=_model_usage_footer_from_metadata(message.metadata),
            )
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

    def render_cardkit_progress_card(
        self,
        snapshot: Any,
        *,
        output_markdown: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> FeishuDelivery:
        elements = [
            {
                "tag": "markdown",
                "element_id": "progress_stream",
                "content": _cardkit_stream_status(snapshot),
            },
        ]
        output, usage_footer = _cardkit_output_content(
            snapshot,
            output_markdown,
            usage_footer=_model_usage_footer_from_totals(usage),
        )
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
        if usage_footer is not None:
            elements.extend(_cardkit_model_usage_elements(usage_footer))
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

    def render_markdown_card(
        self,
        markdown: str,
        *,
        usage_footer: _ModelUsageFooter | None = None,
    ) -> FeishuDelivery:
        parsed_footer = extract_model_usage_footer(markdown)
        usage_footer = usage_footer or parsed_footer
        markdown_body = parsed_footer.body if parsed_footer is not None else markdown
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

    title = pairs[0][1] or pairs[0][0] or "Record"
    rendered = [f"**{title}**"]
    for header, value in pairs[1:]:
        safe_value = value or "-"
        rendered.append(f"- **{header}**：{safe_value}")
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
                    "tag": "lark_md",
                    "content": _model_usage_text(footer),
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


def _cardkit_stream_status(snapshot: Any) -> str:
    status = str(getattr(snapshot, "status", "running") or "running")
    current_action = _snapshot_value(snapshot, "current_action", "")
    current_stage = str(getattr(snapshot, "current_stage", "") or "").strip()
    if status == "completed":
        return "任务完成"
    if status == "failed":
        detail = _normalize_progress_action(current_action, current_stage)
        return _truncate_card_text(f"任务失败：{detail}" if detail and detail != "任务执行失败" else "任务失败", 140)
    return _normalize_progress_action(current_action, current_stage) or _stage_fallback_status(current_stage)


def _normalize_progress_action(action: str, stage: str = "") -> str:
    text = " ".join(str(action or "").split())
    if not text:
        return ""
    if text in {"正在生成执行计划", "开始规划任务"} or "生成执行计划" in text:
        return "生成计划中"
    if text.startswith("已生成 ") and "执行节点" in text:
        return "执行计划已生成"
    if "汇总" in text:
        if "完成" in text:
            return "汇总完成"
        return "正在汇总结果"
    if "任务已完成" in text or "任务完成" in text:
        return "任务完成"
    if text.startswith("正在执行 ") and text.endswith(" 节点"):
        return _truncate_card_text(text, 140)
    if text.startswith("完成 ") and stage == "执行节点":
        return "节点执行完成"
    if " 节点 completed:" in text or " 节点 completed：" in text:
        return "节点执行完成"
    return _truncate_card_text(text, 140)


def _stage_fallback_status(stage: str) -> str:
    if stage == "规划":
        return "生成计划中"
    if stage == "汇总":
        return "正在汇总结果"
    if stage == "完成":
        return "任务完成"
    if stage == "失败":
        return "任务失败"
    return "正在理解请求"


def _cardkit_output_content(
    snapshot: Any,
    output_markdown: str | None,
    *,
    usage_footer: _ModelUsageFooter | None = None,
) -> tuple[str, _ModelUsageFooter | None]:
    if output_markdown is not None:
        parsed_footer = extract_model_usage_footer(output_markdown)
        usage_footer = usage_footer or parsed_footer
        markdown_body = parsed_footer.body if parsed_footer is not None else output_markdown
        output = normalize_markdown(markdown_body)
        return output or "结果已生成。", usage_footer
    return "", None


def _cardkit_model_usage_elements(footer: _ModelUsageFooter) -> list[dict[str, Any]]:
    return [
        {"tag": "hr", "element_id": "progress_usage_divider"},
        {
            "tag": "markdown",
            "element_id": "progress_usage",
            "content": _model_usage_text(footer, muted=True),
            "text_size": "notation",
        },
    ]


def _model_usage_text(footer: _ModelUsageFooter, *, muted: bool = False) -> str:
    text = (
        f"**用量：** {_format_usage_number(footer.total_tokens)} tokens · "
        f"输入 {_format_usage_number(footer.prompt_tokens)} / "
        f"输出 {_format_usage_number(footer.completion_tokens)}"
    )
    return f"<font color='grey'>{text}</font>" if muted else text


def _format_usage_number(value: str) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    if number >= 1000:
        return f"{number / 1000:.1f}k"
    return str(number)


def _model_usage_footer_from_metadata(metadata: dict[str, Any] | None) -> _ModelUsageFooter | None:
    if not isinstance(metadata, dict):
        return None
    usage = metadata.get("usage")
    return _model_usage_footer_from_totals(usage if isinstance(usage, dict) else None)


def _model_usage_footer_from_totals(totals: dict[str, Any] | None) -> _ModelUsageFooter | None:
    if not isinstance(totals, dict):
        return None
    prompt = _int_usage_value(totals.get("prompt_tokens"), totals.get("input_tokens"))
    completion = _int_usage_value(totals.get("completion_tokens"), totals.get("output_tokens"))
    total = _int_usage_value(totals.get("total_tokens"))
    if total <= 0 and (prompt > 0 or completion > 0):
        total = prompt + completion
    if prompt <= 0 and completion <= 0 and total <= 0:
        return None
    return _ModelUsageFooter(
        body="",
        model=str(totals.get("model") or "").strip() or None,
        prompt_tokens=str(prompt),
        completion_tokens=str(completion),
        total_tokens=str(total),
    )


def _int_usage_value(*values: Any) -> int:
    for value in values:
        if value is None:
            continue
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            continue
    return 0
