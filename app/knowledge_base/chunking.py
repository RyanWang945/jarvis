from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkRecord:
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


def normalize_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def chunk_text(
    text: str,
    *,
    target_size: int,
    soft_min_size: int,
    hard_max_size: int,
    overlap_size: int,
    language: str,
) -> list[ChunkRecord]:
    normalized = normalize_text(text)
    if not normalized:
        return []

    units = _split_units(normalized, language=language)
    chunks: list[ChunkRecord] = []
    current = ""
    chunk_start = 0

    for unit in units:
        if not current:
            current = unit
            chunk_start = normalized.find(unit, _next_search_start(chunks))
            continue

        if len(current) < soft_min_size or len(current) + len(unit) <= target_size:
            current += unit
            continue

        if len(current) + len(unit) <= hard_max_size:
            current += unit
            continue

        chunks.append(
            _build_chunk(
                normalized=normalized,
                chunk_index=len(chunks),
                content=current,
                char_start=chunk_start,
                overlap_prev_chars=overlap_size if chunks else 0,
                is_boundary_forced=0,
            )
        )
        overlap = current[-overlap_size:] if overlap_size > 0 else ""
        next_start = max(chunk_start + len(current) - len(overlap), 0)
        current = overlap + unit
        chunk_start = next_start

    if current:
        chunks.append(
            _build_chunk(
                normalized=normalized,
                chunk_index=len(chunks),
                content=current,
                char_start=chunk_start,
                overlap_prev_chars=overlap_size if chunks else 0,
                is_boundary_forced=0,
            )
        )

    final_chunks: list[ChunkRecord] = []
    for chunk in chunks:
        if chunk.char_count <= hard_max_size:
            final_chunks.append(chunk)
            continue
        final_chunks.extend(
            _force_split_chunk(
                chunk,
                hard_max_size=hard_max_size,
                overlap_size=overlap_size,
            )
        )
    return _reindex_chunks(final_chunks)


def _split_units(text: str, *, language: str) -> list[str]:
    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
    units: list[str] = []
    for paragraph in paragraphs:
        units.extend(_split_sentences(paragraph, language=language))
    return units or [text]


def _split_sentences(text: str, *, language: str) -> list[str]:
    pattern = r"(?<=[。！？；.!?;])"
    if language == "en":
        pattern = r"(?<=[.!?;:])\s+"
    parts = re.split(pattern, text)
    sentences = [part.strip() for part in parts if part and part.strip()]
    return [sentence + ("\n\n" if index < len(sentences) - 1 else "") for index, sentence in enumerate(sentences)]


def _build_chunk(
    *,
    normalized: str,
    chunk_index: int,
    content: str,
    char_start: int,
    overlap_prev_chars: int,
    is_boundary_forced: int,
) -> ChunkRecord:
    chunk_text = content.strip()
    start = max(char_start, 0)
    end = start + len(chunk_text)
    return ChunkRecord(
        chunk_index=chunk_index,
        raw_content=chunk_text,
        normalized_content=chunk_text,
        content_hash=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
        char_start=start,
        char_end=end,
        char_count=len(chunk_text),
        token_estimate=_estimate_tokens(chunk_text),
        overlap_prev_chars=overlap_prev_chars if chunk_index > 0 else 0,
        is_boundary_forced=is_boundary_forced,
    )


def _force_split_chunk(
    chunk: ChunkRecord,
    *,
    hard_max_size: int,
    overlap_size: int,
) -> list[ChunkRecord]:
    pieces: list[ChunkRecord] = []
    start = 0
    step = max(hard_max_size - overlap_size, 1)
    while start < len(chunk.normalized_content):
        end = min(start + hard_max_size, len(chunk.normalized_content))
        content = chunk.normalized_content[start:end]
        pieces.append(
            ChunkRecord(
                chunk_index=len(pieces),
                raw_content=content,
                normalized_content=content,
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                char_start=chunk.char_start + start,
                char_end=chunk.char_start + end,
                char_count=len(content),
                token_estimate=_estimate_tokens(content),
                overlap_prev_chars=overlap_size if pieces else chunk.overlap_prev_chars,
                is_boundary_forced=1,
            )
        )
        if end >= len(chunk.normalized_content):
            break
        start += step
    return pieces


def _reindex_chunks(chunks: list[ChunkRecord]) -> list[ChunkRecord]:
    reindexed: list[ChunkRecord] = []
    for index, chunk in enumerate(chunks):
        reindexed.append(
            ChunkRecord(
                chunk_index=index,
                raw_content=chunk.raw_content,
                normalized_content=chunk.normalized_content,
                content_hash=chunk.content_hash,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                char_count=chunk.char_count,
                token_estimate=chunk.token_estimate,
                overlap_prev_chars=chunk.overlap_prev_chars if index > 0 else 0,
                is_boundary_forced=chunk.is_boundary_forced,
            )
        )
    return reindexed


def _estimate_tokens(text: str) -> int:
    return max(1, len(text))


def _next_search_start(chunks: list[ChunkRecord]) -> int:
    if not chunks:
        return 0
    return max(chunks[-1].char_end - chunks[-1].overlap_prev_chars, 0)
