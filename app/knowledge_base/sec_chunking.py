from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class SecChunkRecord:
    chunk_index: int
    raw_content: str
    normalized_content: str
    content_hash: str
    char_start: int
    char_end: int
    char_count: int
    token_estimate: int
    overlap_prev_chars: int
    is_boundary_forced: int
    section_path: str | None
    metadata: dict


def chunk_sec_blocks(
    blocks: list[dict],
    *,
    target_size: int,
    soft_min_size: int,
    hard_max_size: int,
    overlap_size: int,
    document_metadata: dict,
) -> list[SecChunkRecord]:
    if not blocks:
        return []

    chunks: list[SecChunkRecord] = []
    current_blocks: list[dict] = []
    current_length = 0
    char_cursor = 0

    for block in blocks:
        block_text = str(block.get("block_text") or "").strip()
        if not block_text:
            continue
        block_len = len(block_text)
        block_section = _section_path_value(block)

        if current_blocks:
            current_section = _section_path_value(current_blocks[0])
            section_changed = block_section != current_section
            would_exceed_target = current_length >= soft_min_size and current_length + block_len > target_size
            would_exceed_hard_max = current_length + block_len > hard_max_size
            if section_changed or would_exceed_target or would_exceed_hard_max:
                chunks.append(
                    _build_chunk(
                        current_blocks,
                        chunk_index=len(chunks),
                        char_start=char_cursor,
                        overlap_prev_chars=overlap_size if chunks else 0,
                        document_metadata=document_metadata,
                    )
                )
                current_text = chunks[-1].normalized_content
                char_cursor = max(char_cursor + len(current_text) - (overlap_size if overlap_size > 0 else 0), 0)
                overlap_blocks = [] if section_changed else _trailing_overlap_blocks(current_blocks, overlap_size)
                current_blocks = overlap_blocks.copy()
                current_length = sum(len(str(item.get("block_text") or "").strip()) for item in current_blocks)

        current_blocks.append(block)
        current_length += block_len

    if current_blocks:
        chunks.append(
            _build_chunk(
                current_blocks,
                chunk_index=len(chunks),
                char_start=char_cursor,
                overlap_prev_chars=overlap_size if chunks else 0,
                document_metadata=document_metadata,
            )
        )

    return chunks


def _build_chunk(
    blocks: list[dict],
    *,
    chunk_index: int,
    char_start: int,
    overlap_prev_chars: int,
    document_metadata: dict,
) -> SecChunkRecord:
    text_parts = [str(block.get("block_text") or "").strip() for block in blocks if str(block.get("block_text") or "").strip()]
    content = "\n\n".join(text_parts).strip()
    section_path_list = blocks[-1].get("section_path") or blocks[0].get("section_path")
    metadata = {
        "page_start": _first_non_null(blocks, "page_number"),
        "page_end": _last_non_null(blocks, "page_number"),
        "section_title": blocks[-1].get("section_heading") or blocks[0].get("section_heading"),
        "section_path": section_path_list,
        "block_types": [str(block.get("block_type")) for block in blocks],
        "company_name": document_metadata.get("company_name"),
        "ticker": document_metadata.get("ticker"),
        "form_type": document_metadata.get("form_type"),
        "filing_date": document_metadata.get("filing_date"),
        "fiscal_year": document_metadata.get("fiscal_year"),
        "fiscal_period": document_metadata.get("fiscal_period"),
        "is_table_chunk": any(block.get("block_type") == "table" for block in blocks),
        "image_count": sum(1 for block in blocks if block.get("block_type") == "image"),
    }
    return SecChunkRecord(
        chunk_index=chunk_index,
        raw_content=content,
        normalized_content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        char_start=char_start,
        char_end=char_start + len(content),
        char_count=len(content),
        token_estimate=max(1, len(content)),
        overlap_prev_chars=overlap_prev_chars if chunk_index > 0 else 0,
        is_boundary_forced=0,
        section_path=_join_section_path(section_path_list),
        metadata=metadata,
    )


def _trailing_overlap_blocks(blocks: list[dict], overlap_size: int) -> list[dict]:
    if overlap_size <= 0 or not blocks:
        return []
    kept: list[dict] = []
    total = 0
    for block in reversed(blocks):
        block_text = str(block.get("block_text") or "").strip()
        if not block_text:
            continue
        kept.append(block)
        total += len(block_text)
        if total >= overlap_size:
            break
    kept.reverse()
    return kept


def _section_path_value(block: dict) -> tuple[str, ...] | None:
    value = block.get("section_path")
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return None


def _join_section_path(value: list[str] | None) -> str | None:
    if not value:
        return None
    return " > ".join(str(item) for item in value)


def _first_non_null(blocks: list[dict], key: str) -> int | None:
    for block in blocks:
        value = block.get(key)
        if isinstance(value, int):
            return value
    return None


def _last_non_null(blocks: list[dict], key: str) -> int | None:
    for block in reversed(blocks):
        value = block.get(key)
        if isinstance(value, int):
            return value
    return None
