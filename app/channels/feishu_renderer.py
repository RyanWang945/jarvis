from __future__ import annotations

import json
import re
from dataclasses import dataclass

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

    def render_error_card(self, message: str) -> FeishuDelivery:
        return self._render_card_from_blocks(
            [
                "**❌ Request Failed**",
                message.strip() or "Something went wrong.",
            ],
            update_multi=True,
        )

    def render_markdown_card(self, markdown: str) -> FeishuDelivery:
        normalized = normalize_markdown(markdown)
        if not normalized:
            return self.render_text_fallback("")

        blocks = split_markdown_blocks(normalized)
        if len(blocks) > _MAX_CARD_ELEMENTS:
            return self.render_text_fallback(downgrade_markdown_to_text(normalized))
        return self._render_card_from_blocks(
            ["**✅ Completed**", *blocks],
            update_multi=True,
        )

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
        elements = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": block,
                },
            }
            for block in blocks
        ]
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
            return self.render_text_fallback(downgrade_markdown_to_text("\n\n".join(blocks)))
        return FeishuDelivery(msg_type="interactive", content=payload)


def normalize_markdown(markdown: str) -> str:
    normalized = markdown.replace("\r\n", "\n").strip()
    normalized = adapt_markdown_for_feishu(normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized


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
            title = _strip_trailing_heading_marks(heading.group(2))
            if rendered and rendered[-1] != "":
                rendered.append("")
            rendered.append(f"**{title}**")
            rendered.append("")
            index += 1
            continue

        quote = re.match(r"^>\s?(.*)$", stripped)
        if quote:
            if rendered and rendered[-1] != "":
                rendered.append("")
            rendered.append("**Quote**")
            rendered.append(quote.group(1))
            rendered.append("")
            index += 1
            continue

        ordered = re.match(r"^(\d+)\.\s+(.+)$", stripped)
        if ordered:
            rendered.append(f"{ordered.group(1)}. {ordered.group(2)}")
            index += 1
            continue

        bullet = re.match(r"^([*+-])\s+(.+)$", stripped)
        if bullet:
            rendered.append(f"- {bullet.group(2)}")
            index += 1
            continue

        rendered.append(stripped)
        index += 1

    while rendered and rendered[-1] == "":
        rendered.pop()
    return "\n".join(rendered)


def _strip_trailing_heading_marks(value: str) -> str:
    return re.sub(r"\s+#+\s*$", "", value).strip()


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
