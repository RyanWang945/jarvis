from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.knowledge_base.ingest import BaseIngestService, IngestResult, IngestSource
from app.knowledge_base.repositories import KnowledgeBaseDB
from app.knowledge_base.sec_blocks import dump_normalized_blocks
from app.knowledge_base.sec_chunking import chunk_sec_blocks


_filename_re = re.compile(
    r"^(?P<company>.+?)_(?P<year>\d{4})(?P<quarter>Q[1-4])?_(?P<form>10-K|10-Q|10K|10Q)$",
    re.IGNORECASE,
)


class SecFilingIngestService(BaseIngestService):
    def __init__(self, *, db: KnowledgeBaseDB, raw_parse_dir: Path, normalized_blocks_dir: Path) -> None:
        super().__init__(db)
        self.raw_parse_dir = raw_parse_dir
        self.normalized_blocks_dir = normalized_blocks_dir

    def ingest(
        self,
        *,
        source_id: str | None = None,
        file_names: list[str] | None = None,
        chunk_profile_id: str = "sec_filing_medium_v1",
    ) -> IngestResult:
        resolved_source_id = source_id or "sec_filing_local"
        job_id = f"kb_ingest_{uuid.uuid4()}"
        started_at = _utc_now()
        self._save_source(
            IngestSource(
                source_id=resolved_source_id,
                name="sec_filings",
                source_type="sec_filing",
                language="en",
                dataset_version="local_v1",
                file_path=str(self.raw_parse_dir),
                description="Local SEC filing parse artifacts",
                metadata_json={
                    "raw_parse_dir": str(self.raw_parse_dir),
                    "normalized_blocks_dir": str(self.normalized_blocks_dir),
                },
            )
        )
        self._start_job(
            job_id=job_id,
            source_id=resolved_source_id,
            file_path=str(self.raw_parse_dir),
            limit_n=len(file_names) if file_names else None,
            started_at=started_at,
        )

        documents_seen = 0
        documents_inserted = 0
        documents_updated = 0
        documents_skipped = 0
        chunks_created = 0
        profile = self._db.chunk_profiles.get(chunk_profile_id)
        if profile is None:
            raise ValueError(f"Unknown chunk profile: {chunk_profile_id}")

        try:
            for raw_parse_path in self._list_raw_parse_files(file_names=file_names):
                documents_seen += 1
                raw_payload = json.loads(raw_parse_path.read_text(encoding="utf-8"))
                normalized_path = self.normalized_blocks_dir / f"{raw_parse_path.stem.replace('.aliyun', '')}.blocks.json"
                if not normalized_path.exists():
                    self._materialize_normalized_blocks(raw_payload=raw_payload, normalized_path=normalized_path)
                normalized_blocks = json.loads(normalized_path.read_text(encoding="utf-8"))
                document = _build_sec_document(
                    source_id=resolved_source_id,
                    ingest_job_id=job_id,
                    raw_parse_path=raw_parse_path,
                    normalized_path=normalized_path,
                    raw_payload=raw_payload,
                    normalized_blocks=normalized_blocks,
                )
                existing = self._db.documents.get_by_source_external(
                    resolved_source_id,
                    document["external_id"],
                )
                if existing and existing["text_hash"] == document["text_hash"]:
                    documents_skipped += 1
                else:
                    if existing:
                        documents_updated += 1
                    else:
                        documents_inserted += 1
                    self._db.documents.save(document)

                self._db.chunks.delete_by_document(document["doc_id"], chunk_profile_id=chunk_profile_id)
                sec_chunks = chunk_sec_blocks(
                    normalized_blocks,
                    target_size=profile["target_size"],
                    soft_min_size=profile["soft_min_size"],
                    hard_max_size=profile["hard_max_size"],
                    overlap_size=profile["overlap_size"],
                    document_metadata=document["metadata_json"],
                )
                for sec_chunk in sec_chunks:
                    self._db.chunks.save(
                        {
                            "chunk_id": f"{document['doc_id']}:chunk:{sec_chunk.chunk_index:04d}",
                            "doc_id": document["doc_id"],
                            "chunk_profile_id": chunk_profile_id,
                            "chunk_index": sec_chunk.chunk_index,
                            "chunker_version": profile["chunker_version"],
                            "section_path": sec_chunk.section_path,
                            "raw_content": sec_chunk.raw_content,
                            "normalized_content": sec_chunk.normalized_content,
                            "content_hash": sec_chunk.content_hash,
                            "char_start": sec_chunk.char_start,
                            "char_end": sec_chunk.char_end,
                            "char_count": sec_chunk.char_count,
                            "token_estimate": sec_chunk.token_estimate,
                            "overlap_prev_chars": sec_chunk.overlap_prev_chars,
                            "is_boundary_forced": sec_chunk.is_boundary_forced,
                            "metadata_json": sec_chunk.metadata,
                        }
                    )
                    chunks_created += 1

                self._db.parse_artifacts.save(
                    _build_parse_artifact(
                        document=document,
                        raw_parse_path=raw_parse_path,
                        normalized_path=normalized_path,
                        raw_payload=raw_payload,
                    )
                )
                self._db.chunk_runs.save(
                    {
                        "chunk_run_id": f"{document['doc_id']}:chunk-run:{chunk_profile_id}",
                        "filing_id": document["doc_id"],
                        "parse_artifact_id": f"{document['doc_id']}:artifact:parse:v1",
                        "chunk_profile_id": chunk_profile_id,
                        "chunker_version": profile["chunker_version"],
                        "config_json": {
                            "target_size": profile["target_size"],
                            "soft_min_size": profile["soft_min_size"],
                            "hard_max_size": profile["hard_max_size"],
                            "overlap_size": profile["overlap_size"],
                        },
                        "chunk_count": len(sec_chunks),
                        "status": "succeeded",
                    }
                )

            self._finish_job(
                job_id=job_id,
                source_id=resolved_source_id,
                file_path=str(self.raw_parse_dir),
                limit_n=len(file_names) if file_names else None,
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
                source_id=resolved_source_id,
                file_path=str(self.raw_parse_dir),
                limit_n=len(file_names) if file_names else None,
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
            source_id=resolved_source_id,
            file_path=str(self.raw_parse_dir),
            limit_n=len(file_names) if file_names else None,
            documents_seen=documents_seen,
            documents_inserted=documents_inserted,
            documents_updated=documents_updated,
            documents_skipped=documents_skipped,
            chunks_created=chunks_created,
            status="succeeded",
        )

    def _list_raw_parse_files(self, *, file_names: list[str] | None) -> list[Path]:
        if file_names:
            names = [name if name.endswith(".aliyun.json") else f"{Path(name).stem}.aliyun.json" for name in file_names]
            return [path for path in ((self.raw_parse_dir / name).resolve() for name in names) if path.is_file()]
        return sorted(self.raw_parse_dir.glob("*.aliyun.json"))

    def _materialize_normalized_blocks(self, *, raw_payload: dict, normalized_path: Path) -> None:
        content = (
            raw_payload.get("final_task_response", {})
            .get("result", {})
            .get("data", {})
            .get("content")
        )
        if not isinstance(content, str) or not content.strip():
            raise FileNotFoundError(f"Missing normalized blocks and raw markdown content: {normalized_path}")
        normalized_path.parent.mkdir(parents=True, exist_ok=True)
        normalized_path.write_text(
            dump_normalized_blocks(content),
            encoding="utf-8",
        )


def _build_sec_document(
    *,
    source_id: str,
    ingest_job_id: str,
    raw_parse_path: Path,
    normalized_path: Path,
    raw_payload: dict,
    normalized_blocks: list[dict],
) -> dict:
    external_id = raw_parse_path.stem.replace(".aliyun", "")
    meta = _infer_sec_metadata(external_id)
    text = _document_text(normalized_blocks)
    text_hash = _sha256(text)
    source_file = str(raw_payload.get("source_file") or "")
    task_id = str(raw_payload.get("task_id") or "")
    title = meta.get("company_name") or external_id
    return {
        "doc_id": f"{source_id}:{external_id}",
        "source_id": source_id,
        "external_id": external_id,
        "title": title,
        "url": source_file,
        "text": text,
        "text_hash": text_hash,
        "char_count": len(text),
        "language": "en",
        "metadata_json": {
            **meta,
            "pdf_path": source_file,
            "parser_vendor": "aliyun",
            "parser_version": "ops-document-analyze-002",
            "parse_job_id": task_id,
            "raw_parse_path": str(raw_parse_path),
            "normalized_blocks_path": str(normalized_path),
            "block_count": len(normalized_blocks),
        },
        "ingest_job_id": ingest_job_id,
    }


def _build_parse_artifact(
    *,
    document: dict,
    raw_parse_path: Path,
    normalized_path: Path,
    raw_payload: dict,
) -> dict:
    metadata = document["metadata_json"]
    source_file = metadata["pdf_path"]
    return {
        "artifact_id": f"{document['doc_id']}:artifact:parse:v1",
        "filing_id": document["doc_id"],
        "artifact_type": "aliyun_parse_markdown",
        "parser_vendor": "aliyun",
        "parser_model": "ops-document-analyze-002",
        "parser_version": "v1",
        "parse_config_json": {
            "raw_parse_path": str(raw_parse_path),
        },
        "input_sha256": _sha256(Path(source_file).name if source_file else raw_parse_path.name),
        "raw_output_path": str(raw_parse_path),
        "normalized_output_path": str(normalized_path),
        "status": str(raw_payload.get("final_task_response", {}).get("result", {}).get("status") or "UNKNOWN"),
    }


def _infer_sec_metadata(external_id: str) -> dict:
    match = _filename_re.match(external_id)
    if not match:
        return {
            "company_name": external_id,
            "form_type": None,
            "fiscal_year": None,
            "fiscal_period": None,
            "ticker": None,
        }
    company = match.group("company").replace("_", " ").strip()
    quarter = match.group("quarter")
    raw_form = match.group("form").upper()
    form_type = "10-Q" if raw_form in {"10Q", "10-Q"} else "10-K" if raw_form in {"10K", "10-K"} else raw_form
    return {
        "company_name": company,
        "form_type": form_type,
        "fiscal_year": int(match.group("year")),
        "fiscal_period": quarter.upper() if quarter else "FY",
        "ticker": None,
    }


def _document_text(normalized_blocks: list[dict]) -> str:
    parts = [str(block.get("block_text", "")).strip() for block in normalized_blocks]
    return "\n\n".join(part for part in parts if part)


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
