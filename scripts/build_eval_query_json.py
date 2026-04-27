from __future__ import annotations

import argparse
import json
import sqlite3
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.knowledge_base.eval import QueryGenerationService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--chunk-profile-id", default="medium_overlap_v1")
    parser.add_argument("--target-count", type=int, required=True)
    parser.add_argument("--chunks-per-document", type=int, default=1)
    parser.add_argument("--mode", default="llm")
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--output-json", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    generator = QueryGenerationService(settings)
    output_path = Path(args.output_json)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(f"file:{args.source_db}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row

    existing_queries = _existing_queries(conn, args.dataset_id)
    existing_by_chunk = {row["target_chunk_id"]: row for row in existing_queries}
    documents = _documents(conn, args.source_id, args.target_count)
    tasks = _build_tasks(
        conn=conn,
        documents=documents,
        chunk_profile_id=args.chunk_profile_id,
        chunks_per_document=args.chunks_per_document,
        existing_chunk_ids=set(existing_by_chunk),
        target_count=args.target_count,
    )
    total_target = min(args.target_count, len(existing_queries) + len(tasks))

    with output_path.open("w", encoding="utf-8") as handle:
        for row in existing_queries[:total_target]:
            handle.write(json.dumps(_serialize_existing(row), ensure_ascii=False) + "\n")

    print(
        json.dumps(
            {
                "event": "start",
                "dataset_id": args.dataset_id,
                "source_id": args.source_id,
                "existing_queries": min(len(existing_queries), total_target),
                "pending_queries": len(tasks),
                "target_queries": total_target,
                "output_json": str(output_path),
                "max_workers": min(args.max_workers, len(tasks)) if tasks else 0,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    if not tasks:
        print(
            json.dumps(
                {
                    "event": "done",
                    "total_queries": min(len(existing_queries), total_target),
                    "generated_now": 0,
                    "output_json": str(output_path),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0

    completed_now = 0
    next_index = 0
    active: dict[Future, dict[str, Any]] = {}
    max_workers = min(args.max_workers, len(tasks))
    started_at = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        while next_index < len(tasks) and len(active) < max_workers:
            task = tasks[next_index]
            next_index += 1
            active[executor.submit(_generate_one, generator, task, args.mode)] = task

        with output_path.open("a", encoding="utf-8") as handle:
            while active:
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    task = active.pop(future)
                    generated, elapsed_ms = future.result()
                    completed_now += 1
                    record = {
                        "query_id": f"json_eval_query_{uuid.uuid4()}",
                        "dataset_id": args.dataset_id,
                        "doc_id": task["document"]["doc_id"],
                        "target_chunk_id": task["chunk"]["chunk_id"],
                        "query_text": generated.query_text,
                        "query_type": generated.query_type,
                        "difficulty": generated.difficulty,
                        "gold_answer": generated.gold_answer,
                        "gold_evidence": [task["chunk"]["chunk_id"]],
                        "generated_by": generated.generated_by,
                        "review_status": "generated",
                    }
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                    print(
                        json.dumps(
                            {
                                "event": "progress",
                                "completed": min(len(existing_queries), total_target) + completed_now,
                                "target": total_target,
                                "generated_now": completed_now,
                                "elapsed_ms": elapsed_ms,
                                "query_text": generated.query_text,
                                "generated_by": generated.generated_by,
                                "doc_id": task["document"]["doc_id"],
                                "chunk_id": task["chunk"]["chunk_id"],
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    if next_index < len(tasks):
                        next_task = tasks[next_index]
                        next_index += 1
                        active[executor.submit(_generate_one, generator, next_task, args.mode)] = next_task

    print(
        json.dumps(
            {
                "event": "done",
                "total_queries": min(len(existing_queries), total_target) + completed_now,
                "generated_now": completed_now,
                "total_elapsed_ms": int((time.perf_counter() - started_at) * 1000),
                "output_json": str(output_path),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


def _existing_queries(conn: sqlite3.Connection, dataset_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT query_id, dataset_id, doc_id, target_chunk_id, query_text, query_type,
               difficulty, gold_answer, gold_evidence_json, generated_by, review_status, created_at
        FROM kb_eval_queries
        WHERE dataset_id = ?
        ORDER BY created_at, query_id
        """,
        (dataset_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _documents(conn: sqlite3.Connection, source_id: str, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM kb_documents
        WHERE source_id = ?
        ORDER BY created_at, doc_id
        LIMIT ?
        """,
        (source_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _chunks(
    conn: sqlite3.Connection,
    doc_id: str,
    chunk_profile_id: str,
    chunks_per_document: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM kb_chunks
        WHERE doc_id = ? AND chunk_profile_id = ?
        ORDER BY chunk_index
        LIMIT ?
        """,
        (doc_id, chunk_profile_id, chunks_per_document),
    ).fetchall()
    return [dict(row) for row in rows]


def _build_tasks(
    *,
    conn: sqlite3.Connection,
    documents: list[dict[str, Any]],
    chunk_profile_id: str,
    chunks_per_document: int,
    existing_chunk_ids: set[str],
    target_count: int,
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    remaining = max(target_count - len(existing_chunk_ids), 0)
    for document in documents:
        for chunk in _chunks(conn, document["doc_id"], chunk_profile_id, chunks_per_document):
            if chunk["chunk_id"] in existing_chunk_ids:
                continue
            tasks.append({"document": document, "chunk": chunk})
            if len(tasks) >= remaining:
                return tasks
    return tasks


def _generate_one(generator: QueryGenerationService, task: dict[str, Any], mode: str):
    started = time.perf_counter()
    generated = generator.generate(
        document=task["document"],
        chunk=task["chunk"],
        mode=mode,
    )
    return generated, int((time.perf_counter() - started) * 1000)


def _serialize_existing(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "query_id": row["query_id"],
        "dataset_id": row["dataset_id"],
        "doc_id": row["doc_id"],
        "target_chunk_id": row["target_chunk_id"],
        "query_text": row["query_text"],
        "query_type": row["query_type"],
        "difficulty": row["difficulty"],
        "gold_answer": row["gold_answer"],
        "gold_evidence": json.loads(row["gold_evidence_json"]),
        "generated_by": row["generated_by"],
        "review_status": row["review_status"],
    }


if __name__ == "__main__":
    raise SystemExit(main())
