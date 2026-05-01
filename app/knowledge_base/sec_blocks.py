from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass


_heading_re = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_list_line_re = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+|\([a-zA-Z0-9]+\)\s+)")
_image_re = re.compile(r"!\[[^\]]*\]\([^)]+\)|!\[[^\]]*\]")
_table_re = re.compile(r"<table[\s>].*?</table>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class NormalizedBlock:
    page_number: int | None
    block_type: str
    block_text: str
    block_order: int
    section_heading: str | None
    section_path: list[str] | None
    bbox: dict | None = None
    metadata: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_aliyun_markdown(markdown: str) -> list[dict]:
    blocks: list[NormalizedBlock] = []
    section_stack: list[str] = []
    block_order = 0

    for segment in _split_segments(markdown):
        block_type, text, metadata = _classify_segment(segment)
        if not text:
            continue

        section_heading = section_stack[-1] if section_stack else None
        section_path = list(section_stack) if section_stack else None
        if block_type == "heading":
            heading_level = metadata["heading_level"]
            while len(section_stack) >= heading_level:
                section_stack.pop()
            section_stack.append(text)
            section_heading = text
            section_path = list(section_stack)

        blocks.append(
            NormalizedBlock(
                page_number=None,
                block_type=block_type,
                block_text=text,
                block_order=block_order,
                section_heading=section_heading,
                section_path=section_path,
                metadata=metadata or None,
            )
        )
        block_order += 1

    return [block.to_dict() for block in blocks]


def dump_normalized_blocks(markdown: str) -> str:
    return json.dumps(normalize_aliyun_markdown(markdown), ensure_ascii=False, indent=2)


def _split_segments(markdown: str) -> list[str]:
    normalized = markdown.replace("\r\n", "\n").strip()
    if not normalized:
        return []
    parts = re.split(r"\n\s*\n+", normalized)
    return [part.strip() for part in parts if part.strip()]


def _classify_segment(segment: str) -> tuple[str, str, dict]:
    heading_match = _heading_re.match(segment)
    if heading_match:
        text = heading_match.group(2).strip()
        return "heading", text, {"heading_level": len(heading_match.group(1))}

    if _table_re.search(segment):
        return "table", segment.strip(), {"contains_html_table": True}

    if _image_re.search(segment):
        return "image", segment.strip(), {"image_placeholder": True}

    lines = [line.strip() for line in segment.splitlines() if line.strip()]
    if lines and all(_list_line_re.match(line) for line in lines):
        return "list", "\n".join(lines), {"list_item_count": len(lines)}

    return "paragraph", _collapse_paragraph(segment), {}


def _collapse_paragraph(segment: str) -> str:
    lines = [line.strip() for line in segment.splitlines() if line.strip()]
    if not lines:
        return ""
    return "\n".join(lines) if len(lines) > 1 and all(_list_line_re.match(line) for line in lines) else " ".join(lines)
