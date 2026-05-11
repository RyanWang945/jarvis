from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.knowledge_base.chunking import chunk_text, normalize_text
from app.knowledge_base.repositories import KnowledgeBaseDB


LEGACY_WIKIPEDIA_CHUNK_PROFILE_IDS = {"medium_overlap_v1"}


@dataclass(frozen=True)
class IngestResult:
    job_id: str
    source_id: str
    file_path: str
    limit_n: int | None
    documents_seen: int
    documents_inserted: int
    documents_updated: int
    documents_skipped: int
    chunks_created: int
    status: str


@dataclass(frozen=True)
class IngestSource:
    source_id: str
    name: str
    source_type: str
    language: str
    dataset_version: str
    file_path: str
    description: str | None = None
    owner: str | None = None
    region: str | None = None
    metadata_json: dict | None = None


class BaseIngestService:
    def __init__(self, db: KnowledgeBaseDB) -> None:
        self._db = db

    def _save_source(self, source: IngestSource) -> None:
        self._db.sources.save(
            {
                "source_id": source.source_id,
                "name": source.name,
                "source_type": source.source_type,
                "language": source.language,
                "dataset_version": source.dataset_version,
                "file_path": source.file_path,
                "description": source.description,
                "owner": source.owner,
                "region": source.region,
                "metadata_json": source.metadata_json,
            }
        )

    def _start_job(
        self,
        *,
        job_id: str,
        source_id: str,
        file_path: str,
        limit_n: int | None,
        started_at: str,
    ) -> None:
        self._db.ingest_jobs.save(
            {
                "job_id": job_id,
                "source_id": source_id,
                "file_path": file_path,
                "limit_n": limit_n,
                "status": "running",
                "started_at": started_at,
            }
        )

    def _finish_job(
        self,
        *,
        job_id: str,
        source_id: str,
        file_path: str,
        limit_n: int | None,
        started_at: str,
        documents_seen: int,
        documents_inserted: int,
        documents_updated: int,
        documents_skipped: int,
        chunks_created: int,
        status: str,
        error_message: str | None = None,
    ) -> None:
        self._db.ingest_jobs.save(
            {
                "job_id": job_id,
                "source_id": source_id,
                "file_path": file_path,
                "limit_n": limit_n,
                "status": status,
                "started_at": started_at,
                "finished_at": _utc_now(),
                "documents_seen": documents_seen,
                "documents_inserted": documents_inserted,
                "documents_updated": documents_updated,
                "documents_skipped": documents_skipped,
                "chunks_created": chunks_created,
                "error_message": error_message,
            }
        )


class WikipediaIngestService(BaseIngestService):
    def ingest(
        self,
        *,
        file_path: Path,
        source_id: str | None,
        language: str,
        limit_n: int | None,
        chunk_profile_id: str,
    ) -> IngestResult:
        profile = self._db.chunk_profiles.get(chunk_profile_id)
        if profile is None:
            raise ValueError(f"Unknown chunk profile: {chunk_profile_id}")

        resolved_path = file_path.resolve()
        dataset_version = resolved_path.stem
        source_key = source_id or f"wikipedia_{language}_{dataset_version}"
        job_id = f"kb_ingest_{uuid.uuid4()}"
        started_at = _utc_now()

        self._save_source(
            IngestSource(
                source_id=source_key,
                name="wikipedia",
                source_type="wikipedia",
                language=language,
                dataset_version=dataset_version,
                file_path=str(resolved_path),
                description=f"Wikipedia {language} dataset",
            )
        )
        self._start_job(
            job_id=job_id,
            source_id=source_key,
            file_path=str(resolved_path),
            limit_n=limit_n,
            started_at=started_at,
        )

        documents_seen = 0
        documents_inserted = 0
        documents_updated = 0
        documents_skipped = 0
        chunks_created = 0

        try:
            for record in _iterate_wikipedia_records(resolved_path, limit_n=limit_n):
                documents_seen += 1
                document = _build_document(
                    record=record,
                    source_id=source_key,
                    language=language,
                    ingest_job_id=job_id,
                )
                existing = self._db.documents.get_by_source_external(
                    source_key,
                    document["external_id"],
                )
                if existing and existing["text_hash"] == document["text_hash"]:
                    documents_skipped += 1
                    document = existing
                    existing_chunks = self._db.chunks.list_by_document(
                        document["doc_id"],
                        chunk_profile_id=chunk_profile_id,
                    )
                    if existing_chunks:
                        continue

                elif existing:
                    documents_updated += 1
                    self._db.documents.save(document)
                    self._db.chunks.delete_by_document(document["doc_id"], chunk_profile_id=chunk_profile_id)
                else:
                    documents_inserted += 1
                    self._db.documents.save(document)

                chunk_records = chunk_text(
                    document["text"],
                    target_size=profile["target_size"],
                    soft_min_size=profile["soft_min_size"],
                    hard_max_size=profile["hard_max_size"],
                    overlap_size=profile["overlap_size"],
                    language=language,
                )
                for chunk in chunk_records:
                    self._db.chunks.save(
                        {
                            "chunk_id": build_wikipedia_chunk_id(
                                doc_id=document["doc_id"],
                                chunk_profile_id=chunk_profile_id,
                                chunk_index=chunk.chunk_index,
                            ),
                            "doc_id": document["doc_id"],
                            "chunk_profile_id": chunk_profile_id,
                            "chunk_index": chunk.chunk_index,
                            "chunker_version": profile["chunker_version"],
                            "section_path": None,
                            "raw_content": chunk.raw_content,
                            "normalized_content": chunk.normalized_content,
                            "content_hash": chunk.content_hash,
                            "char_start": chunk.char_start,
                            "char_end": chunk.char_end,
                            "char_count": chunk.char_count,
                            "token_estimate": chunk.token_estimate,
                            "overlap_prev_chars": chunk.overlap_prev_chars,
                            "is_boundary_forced": chunk.is_boundary_forced,
                            "metadata_json": {"language": language},
                        }
                    )
                    chunks_created += 1

            self._finish_job(
                job_id=job_id,
                source_id=source_key,
                file_path=str(resolved_path),
                limit_n=limit_n,
                started_at=started_at,
                documents_seen=documents_seen,
                documents_inserted=documents_inserted,
                documents_updated=documents_updated,
                documents_skipped=documents_skipped,
                chunks_created=chunks_created,
                status="succeeded",
            )
        except Exception as exc:
            self._finish_job(
                job_id=job_id,
                source_id=source_key,
                file_path=str(resolved_path),
                limit_n=limit_n,
                started_at=started_at,
                documents_seen=documents_seen,
                documents_inserted=documents_inserted,
                documents_updated=documents_updated,
                documents_skipped=documents_skipped,
                chunks_created=chunks_created,
                status="failed",
                error_message=str(exc),
            )
            raise

        return IngestResult(
            job_id=job_id,
            source_id=source_key,
            file_path=str(resolved_path),
            limit_n=limit_n,
            documents_seen=documents_seen,
            documents_inserted=documents_inserted,
            documents_updated=documents_updated,
            documents_skipped=documents_skipped,
            chunks_created=chunks_created,
            status="succeeded",
        )


def _iterate_wikipedia_records(file_path: Path, *, limit_n: int | None) -> Iterator[dict]:
    records_seen = 0
    with file_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            content = line.strip()
            if not content:
                continue
            yield json.loads(content)
            records_seen += 1
            if limit_n is not None and records_seen >= limit_n:
                break


def _build_document(
    *,
    record: dict,
    source_id: str,
    language: str,
    ingest_job_id: str,
) -> dict:
    external_id = str(record["id"])
    normalized_text = normalize_text(record["text"])
    return {
        "doc_id": f"{source_id}:{external_id}",
        "source_id": source_id,
        "external_id": external_id,
        "title": record["title"].strip(),
        "url": record["url"].strip(),
        "text": normalized_text,
        "text_hash": _sha256(normalized_text),
        "char_count": len(normalized_text),
        "language": language,
        "metadata_json": None,
        "ingest_job_id": ingest_job_id,
    }


def build_wikipedia_chunk_id(*, doc_id: str, chunk_profile_id: str, chunk_index: int) -> str:
    if chunk_profile_id in LEGACY_WIKIPEDIA_CHUNK_PROFILE_IDS:
        return f"{doc_id}:chunk:{chunk_index:04d}"
    return f"{doc_id}:profile:{chunk_profile_id}:chunk:{chunk_index:04d}"


def _sha256(content: str) -> str:
    import hashlib

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
