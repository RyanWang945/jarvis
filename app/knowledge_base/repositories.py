from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any


class SourceRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, source: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO kb_sources (
                source_id,
                name,
                language,
                dataset_version,
                file_path,
                description
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                name=excluded.name,
                language=excluded.language,
                dataset_version=excluded.dataset_version,
                file_path=excluded.file_path,
                description=excluded.description
            """,
            (
                source["source_id"],
                source["name"],
                source["language"],
                source["dataset_version"],
                source["file_path"],
                source.get("description"),
            ),
        )
        self._conn.commit()

    def get(self, source_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM kb_sources WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        return dict(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM kb_sources ORDER BY created_at, source_id"
        ).fetchall()
        return [dict(row) for row in rows]


class DocumentRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, document: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO kb_documents (
                doc_id,
                source_id,
                external_id,
                title,
                url,
                text,
                text_hash,
                char_count,
                language,
                metadata_json,
                ingest_job_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, external_id) DO UPDATE SET
                doc_id=excluded.doc_id,
                title=excluded.title,
                url=excluded.url,
                text=excluded.text,
                text_hash=excluded.text_hash,
                char_count=excluded.char_count,
                language=excluded.language,
                metadata_json=excluded.metadata_json,
                ingest_job_id=excluded.ingest_job_id,
                updated_at=datetime('now')
            """,
            (
                document["doc_id"],
                document["source_id"],
                document["external_id"],
                document["title"],
                document["url"],
                document["text"],
                document["text_hash"],
                document["char_count"],
                document["language"],
                _dump_json(document.get("metadata_json")),
                document["ingest_job_id"],
            ),
        )
        self._conn.commit()

    def get(self, doc_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM kb_documents WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_by_source_external(self, source_id: str, external_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT * FROM kb_documents
            WHERE source_id = ? AND external_id = ?
            """,
            (source_id, external_id),
        ).fetchone()
        return dict(row) if row else None

    def list_by_source(
        self,
        source_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM kb_documents WHERE source_id = ? ORDER BY created_at, doc_id"
        params: list[Any] = [source_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        if offset:
            if limit is None:
                sql += " LIMIT -1"
            sql += " OFFSET ?"
            params.append(offset)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


class ChunkProfileRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, profile: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO kb_chunk_profiles (
                chunk_profile_id,
                name,
                language,
                chunker_version,
                target_size,
                soft_min_size,
                hard_max_size,
                overlap_size,
                boundary_rules_json,
                normalization_rules_json,
                is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chunk_profile_id) DO UPDATE SET
                name=excluded.name,
                language=excluded.language,
                chunker_version=excluded.chunker_version,
                target_size=excluded.target_size,
                soft_min_size=excluded.soft_min_size,
                hard_max_size=excluded.hard_max_size,
                overlap_size=excluded.overlap_size,
                boundary_rules_json=excluded.boundary_rules_json,
                normalization_rules_json=excluded.normalization_rules_json,
                is_active=excluded.is_active
            """,
            (
                profile["chunk_profile_id"],
                profile["name"],
                profile.get("language"),
                profile["chunker_version"],
                profile["target_size"],
                profile["soft_min_size"],
                profile["hard_max_size"],
                profile["overlap_size"],
                _dump_json(profile.get("boundary_rules_json")),
                _dump_json(profile.get("normalization_rules_json")),
                profile.get("is_active", 1),
            ),
        )
        self._conn.commit()

    def list_active(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT
                chunk_profile_id,
                name,
                language,
                chunker_version,
                target_size,
                soft_min_size,
                hard_max_size,
                overlap_size,
                is_active,
                created_at
            FROM kb_chunk_profiles
            WHERE is_active = 1
            ORDER BY chunk_profile_id
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def get(self, chunk_profile_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT
                chunk_profile_id,
                name,
                language,
                chunker_version,
                target_size,
                soft_min_size,
                hard_max_size,
                overlap_size,
                boundary_rules_json,
                normalization_rules_json,
                is_active,
                created_at
            FROM kb_chunk_profiles
            WHERE chunk_profile_id = ?
            """,
            (chunk_profile_id,),
        ).fetchone()
        return dict(row) if row else None


class ChunkRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, chunk: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO kb_chunks (
                chunk_id,
                doc_id,
                chunk_profile_id,
                chunk_index,
                chunker_version,
                section_path,
                raw_content,
                normalized_content,
                content_hash,
                char_start,
                char_end,
                char_count,
                token_estimate,
                overlap_prev_chars,
                is_boundary_forced,
                metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chunk_id) DO UPDATE SET
                doc_id=excluded.doc_id,
                chunk_profile_id=excluded.chunk_profile_id,
                chunk_index=excluded.chunk_index,
                chunker_version=excluded.chunker_version,
                section_path=excluded.section_path,
                raw_content=excluded.raw_content,
                normalized_content=excluded.normalized_content,
                content_hash=excluded.content_hash,
                char_start=excluded.char_start,
                char_end=excluded.char_end,
                char_count=excluded.char_count,
                token_estimate=excluded.token_estimate,
                overlap_prev_chars=excluded.overlap_prev_chars,
                is_boundary_forced=excluded.is_boundary_forced,
                metadata_json=excluded.metadata_json
            """,
            (
                chunk["chunk_id"],
                chunk["doc_id"],
                chunk["chunk_profile_id"],
                chunk["chunk_index"],
                chunk["chunker_version"],
                chunk.get("section_path"),
                chunk["raw_content"],
                chunk["normalized_content"],
                chunk["content_hash"],
                chunk["char_start"],
                chunk["char_end"],
                chunk["char_count"],
                chunk["token_estimate"],
                chunk["overlap_prev_chars"],
                chunk.get("is_boundary_forced", 0),
                _dump_json(chunk.get("metadata_json")),
            ),
        )
        self._conn.commit()

    def list_by_document(self, doc_id: str, *, chunk_profile_id: str | None = None) -> list[dict[str, Any]]:
        if chunk_profile_id is None:
            rows = self._conn.execute(
                """
                SELECT * FROM kb_chunks
                WHERE doc_id = ?
                ORDER BY chunk_profile_id, chunk_index
                """,
                (doc_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM kb_chunks
                WHERE doc_id = ? AND chunk_profile_id = ?
                ORDER BY chunk_index
                """,
                (doc_id, chunk_profile_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_by_document(self, doc_id: str, *, chunk_profile_id: str | None = None) -> int:
        if chunk_profile_id is None:
            cursor = self._conn.execute(
                "DELETE FROM kb_chunks WHERE doc_id = ?",
                (doc_id,),
            )
        else:
            cursor = self._conn.execute(
                "DELETE FROM kb_chunks WHERE doc_id = ? AND chunk_profile_id = ?",
                (doc_id, chunk_profile_id),
            )
        self._conn.commit()
        return cursor.rowcount

    def get(self, chunk_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM kb_chunks WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        return dict(row) if row else None


class IngestJobRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, job: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO kb_ingest_jobs (
                job_id,
                source_id,
                file_path,
                limit_n,
                status,
                started_at,
                finished_at,
                documents_seen,
                documents_inserted,
                documents_updated,
                documents_skipped,
                chunks_created,
                error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                source_id=excluded.source_id,
                file_path=excluded.file_path,
                limit_n=excluded.limit_n,
                status=excluded.status,
                started_at=excluded.started_at,
                finished_at=excluded.finished_at,
                documents_seen=excluded.documents_seen,
                documents_inserted=excluded.documents_inserted,
                documents_updated=excluded.documents_updated,
                documents_skipped=excluded.documents_skipped,
                chunks_created=excluded.chunks_created,
                error_message=excluded.error_message
            """,
            (
                job["job_id"],
                job["source_id"],
                job["file_path"],
                job.get("limit_n"),
                job["status"],
                job.get("started_at"),
                job.get("finished_at"),
                job.get("documents_seen", 0),
                job.get("documents_inserted", 0),
                job.get("documents_updated", 0),
                job.get("documents_skipped", 0),
                job.get("chunks_created", 0),
                job.get("error_message"),
            ),
        )
        self._conn.commit()

    def get(self, job_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM kb_ingest_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        return dict(row) if row else None


class ChunkEmbeddingRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, embedding: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO kb_chunk_embeddings (
                chunk_id,
                embedding_model,
                embedding_dim,
                embedding_json,
                text_hash
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chunk_id) DO UPDATE SET
                embedding_model=excluded.embedding_model,
                embedding_dim=excluded.embedding_dim,
                embedding_json=excluded.embedding_json,
                text_hash=excluded.text_hash,
                updated_at=datetime('now')
            """,
            (
                embedding["chunk_id"],
                embedding["embedding_model"],
                embedding["embedding_dim"],
                _dump_json(embedding["embedding_json"]),
                embedding["text_hash"],
            ),
        )
        self._conn.commit()

    def get(self, chunk_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM kb_chunk_embeddings WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_by_chunk_ids(self, chunk_ids: list[str]) -> list[dict[str, Any]]:
        if not chunk_ids:
            return []
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = self._conn.execute(
            f"SELECT * FROM kb_chunk_embeddings WHERE chunk_id IN ({placeholders})",
            chunk_ids,
        ).fetchall()
        return [dict(row) for row in rows]


class EvalDatasetRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, dataset: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO kb_eval_datasets (
                dataset_id, name, source_id, generation_method, query_model, sample_doc_count
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(dataset_id) DO UPDATE SET
                name=excluded.name,
                source_id=excluded.source_id,
                generation_method=excluded.generation_method,
                query_model=excluded.query_model,
                sample_doc_count=excluded.sample_doc_count
            """,
            (
                dataset["dataset_id"],
                dataset["name"],
                dataset["source_id"],
                dataset["generation_method"],
                dataset.get("query_model"),
                dataset["sample_doc_count"],
            ),
        )
        self._conn.commit()

    def get(self, dataset_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM kb_eval_datasets WHERE dataset_id = ?",
            (dataset_id,),
        ).fetchone()
        return dict(row) if row else None


class EvalQueryRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, query: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO kb_eval_queries (
                query_id, dataset_id, doc_id, target_chunk_id, query_text, query_type,
                difficulty, gold_answer, gold_evidence_json, generated_by, review_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(query_id) DO UPDATE SET
                query_text=excluded.query_text,
                query_type=excluded.query_type,
                difficulty=excluded.difficulty,
                gold_answer=excluded.gold_answer,
                gold_evidence_json=excluded.gold_evidence_json,
                generated_by=excluded.generated_by,
                review_status=excluded.review_status
            """,
            (
                query["query_id"],
                query["dataset_id"],
                query["doc_id"],
                query.get("target_chunk_id"),
                query["query_text"],
                query["query_type"],
                query["difficulty"],
                query.get("gold_answer"),
                _dump_json(query.get("gold_evidence_json")),
                query.get("generated_by"),
                query["review_status"],
            ),
        )
        self._conn.commit()

    def list_by_dataset(self, dataset_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM kb_eval_queries WHERE dataset_id = ? ORDER BY created_at, query_id",
            (dataset_id,),
        ).fetchall()
        return [dict(row) for row in rows]


class EvalRunRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, run: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO kb_eval_runs (
                eval_run_id, dataset_id, retrieval_mode, top_k, chunk_profile_id,
                chunker_version, embedding_model, index_name, status, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(eval_run_id) DO UPDATE SET
                retrieval_mode=excluded.retrieval_mode,
                top_k=excluded.top_k,
                chunk_profile_id=excluded.chunk_profile_id,
                chunker_version=excluded.chunker_version,
                embedding_model=excluded.embedding_model,
                index_name=excluded.index_name,
                status=excluded.status,
                started_at=excluded.started_at,
                finished_at=excluded.finished_at
            """,
            (
                run["eval_run_id"],
                run["dataset_id"],
                run["retrieval_mode"],
                run["top_k"],
                run["chunk_profile_id"],
                run["chunker_version"],
                run.get("embedding_model"),
                run["index_name"],
                run["status"],
                run.get("started_at"),
                run.get("finished_at"),
            ),
        )
        self._conn.commit()

    def get(self, eval_run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM kb_eval_runs WHERE eval_run_id = ?",
            (eval_run_id,),
        ).fetchone()
        return dict(row) if row else None


class EvalResultRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, result: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO kb_eval_results (
                result_id, eval_run_id, query_id, hit, hit_rank, mrr_score, ndcg_score,
                retrieved_chunk_ids_json, retrieved_scores_json, latency_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(result_id) DO UPDATE SET
                hit=excluded.hit,
                hit_rank=excluded.hit_rank,
                mrr_score=excluded.mrr_score,
                ndcg_score=excluded.ndcg_score,
                retrieved_chunk_ids_json=excluded.retrieved_chunk_ids_json,
                retrieved_scores_json=excluded.retrieved_scores_json,
                latency_ms=excluded.latency_ms
            """,
            (
                result["result_id"],
                result["eval_run_id"],
                result["query_id"],
                result["hit"],
                result.get("hit_rank"),
                result["mrr_score"],
                result["ndcg_score"],
                _dump_json(result["retrieved_chunk_ids_json"]),
                _dump_json(result["retrieved_scores_json"]),
                result["latency_ms"],
            ),
        )
        self._conn.commit()

    def list_by_run(self, eval_run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM kb_eval_results WHERE eval_run_id = ? ORDER BY created_at, result_id",
            (eval_run_id,),
        ).fetchall()
        return [dict(row) for row in rows]


@dataclass(frozen=True)
class KnowledgeBaseDB:
    conn: sqlite3.Connection
    sources: SourceRepository
    documents: DocumentRepository
    chunk_profiles: ChunkProfileRepository
    chunks: ChunkRepository
    chunk_embeddings: ChunkEmbeddingRepository
    ingest_jobs: IngestJobRepository
    eval_datasets: EvalDatasetRepository
    eval_queries: EvalQueryRepository
    eval_runs: EvalRunRepository
    eval_results: EvalResultRepository


def get_knowledge_base_db(db_path: Any) -> KnowledgeBaseDB:
    from app.knowledge_base.db import init_knowledge_db

    conn = init_knowledge_db(db_path)
    return KnowledgeBaseDB(
        conn=conn,
        sources=SourceRepository(conn),
        documents=DocumentRepository(conn),
        chunk_profiles=ChunkProfileRepository(conn),
        chunks=ChunkRepository(conn),
        chunk_embeddings=ChunkEmbeddingRepository(conn),
        ingest_jobs=IngestJobRepository(conn),
        eval_datasets=EvalDatasetRepository(conn),
        eval_queries=EvalQueryRepository(conn),
        eval_runs=EvalRunRepository(conn),
        eval_results=EvalResultRepository(conn),
    )


def _dump_json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
