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
from app.knowledge_base.repositories import get_knowledge_base_db


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--chunk-profile-id", default="medium_overlap_v1")
    parser.add_argument("--target-count", type=int, required=True)
    parser.add_argument("--chunks-per-document", type=int, default=1)
    parser.add_argument("--mode", default="llm")
    parser.add_argument("--max-workers", type=int, default=4)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    db_path = _resolve_db_path(settings)
    db = get_knowledge_base_db(db_path)
    generator = QueryGenerationService(settings)

    existing_chunk_ids = {
        row["target_chunk_id"]
        for row in db.eval_queries.list_by_dataset(args.dataset_id)
    }
    documents = db.documents.list_by_source(args.source_id, limit=args.target_count)
    tasks = _build_tasks(
        db=db,
        documents=documents,
        chunk_profile_id=args.chunk_profile_id,
        chunks_per_document=args.chunks_per_document,
        existing_chunk_ids=existing_chunk_ids,
        target_count=args.target_count,
    )
    total_existing = len(existing_chunk_ids)
    total_target = min(args.target_count, total_existing + len(tasks))

    print(
        json.dumps(
            {
                "event": "start",
                "dataset_id": args.dataset_id,
                "source_id": args.source_id,
                "existing_queries": total_existing,
                "pending_queries": len(tasks),
                "target_queries": total_target,
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
                    "dataset_id": args.dataset_id,
                    "generated_now": 0,
                    "total_queries": total_existing,
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

        while active:
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                task = active.pop(future)
                generated, elapsed_ms = future.result()
                _save_query(
                    db_path=db_path,
                    dataset_id=args.dataset_id,
                    document=task["document"],
                    chunk=task["chunk"],
                    generated=generated,
                )
                completed_now += 1
                total_completed = total_existing + completed_now
                print(
                    json.dumps(
                        {
                            "event": "progress",
                            "dataset_id": args.dataset_id,
                            "completed": total_completed,
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
                "dataset_id": args.dataset_id,
                "generated_now": completed_now,
                "total_queries": total_existing + completed_now,
                "total_elapsed_ms": int((time.perf_counter() - started_at) * 1000),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


def _resolve_db_path(settings: Any) -> Path:
    if settings.knowledge_db_path is not None:
        return settings.knowledge_db_path
    return settings.data_dir / "knowledge.db"


def _build_tasks(
    *,
    db: Any,
    documents: list[dict[str, Any]],
    chunk_profile_id: str,
    chunks_per_document: int,
    existing_chunk_ids: set[str],
    target_count: int,
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    remaining = max(target_count - len(existing_chunk_ids), 0)
    for document in documents:
        chunks = db.chunks.list_by_document(
            document["doc_id"],
            chunk_profile_id=chunk_profile_id,
        )[:chunks_per_document]
        for chunk in chunks:
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


def _save_query(
    *,
    db_path: Path,
    dataset_id: str,
    document: dict[str, Any],
    chunk: dict[str, Any],
    generated: Any,
) -> None:
    payload = {
        "query_id": f"kb_eval_query_{uuid.uuid4()}",
        "dataset_id": dataset_id,
        "doc_id": document["doc_id"],
        "target_chunk_id": chunk["chunk_id"],
        "query_text": generated.query_text,
        "query_type": generated.query_type,
        "difficulty": generated.difficulty,
        "gold_answer": generated.gold_answer,
        "gold_evidence_json": [chunk["chunk_id"]],
        "generated_by": generated.generated_by,
        "review_status": "generated",
    }
    for attempt in range(5):
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA journal_mode=MEMORY")
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute(
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
                    payload["query_id"],
                    payload["dataset_id"],
                    payload["doc_id"],
                    payload["target_chunk_id"],
                    payload["query_text"],
                    payload["query_type"],
                    payload["difficulty"],
                    payload["gold_answer"],
                    json.dumps(payload["gold_evidence_json"], ensure_ascii=False),
                    payload["generated_by"],
                    payload["review_status"],
                ),
            )
            conn.commit()
            return
        except sqlite3.OperationalError:
            if attempt == 4:
                raise
            time.sleep(1.0 * (attempt + 1))
        finally:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
