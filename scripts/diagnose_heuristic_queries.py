from __future__ import annotations

import argparse
import json
import sqlite3
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.knowledge_base.eval import QueryGenerationService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--query-json", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--max-workers", type=int, default=4)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    generator = QueryGenerationService(settings)
    output_path = Path(args.output_json)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(f"file:{args.source_db}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    records = [
        json.loads(line)
        for line in Path(args.query_json).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    tasks = []
    for record in records:
        if record["generated_by"] != "heuristic":
            continue
        document = dict(
            conn.execute("SELECT * FROM kb_documents WHERE doc_id = ?", (record["doc_id"],)).fetchone()
        )
        chunk = dict(
            conn.execute("SELECT * FROM kb_chunks WHERE chunk_id = ?", (record["target_chunk_id"],)).fetchone()
        )
        tasks.append({"record": record, "document": document, "chunk": chunk})

    print(
        json.dumps(
            {
                "event": "start",
                "heuristic_queries": len(tasks),
                "max_workers": min(args.max_workers, len(tasks)) if tasks else 0,
                "output_json": str(output_path),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    with output_path.open("w", encoding="utf-8") as handle:
        if not tasks:
            print(json.dumps({"event": "done", "total": 0}, ensure_ascii=False), flush=True)
            return 0

        completed = 0
        next_index = 0
        active: dict[Future, dict[str, Any]] = {}
        started_at = time.perf_counter()
        max_workers = min(args.max_workers, len(tasks))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            while next_index < len(tasks) and len(active) < max_workers:
                task = tasks[next_index]
                next_index += 1
                active[executor.submit(_diagnose_one, generator, task)] = task

            while active:
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    result = future.result()
                    completed += 1
                    handle.write(json.dumps(result, ensure_ascii=False) + "\n")
                    handle.flush()
                    print(
                        json.dumps(
                            {
                                "event": "progress",
                                "completed": completed,
                                "target": len(tasks),
                                "elapsed_ms": result["elapsed_ms"],
                                "status": result["status"],
                                "error_type": result.get("error_type"),
                                "query_text": result["original_query_text"],
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    active.pop(future)
                    if next_index < len(tasks):
                        task = tasks[next_index]
                        next_index += 1
                        active[executor.submit(_diagnose_one, generator, task)] = task

        print(
            json.dumps(
                {
                    "event": "done",
                    "total": len(tasks),
                    "total_elapsed_ms": int((time.perf_counter() - started_at) * 1000),
                    "output_json": str(output_path),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return 0


def _diagnose_one(generator: QueryGenerationService, task: dict[str, Any]) -> dict[str, Any]:
    record = task["record"]
    started = time.perf_counter()
    try:
        generated = generator._generate_with_llm(
            document=task["document"],
            chunk=task["chunk"],
        )
        return {
            "status": "success",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "query_id": record["query_id"],
            "doc_id": record["doc_id"],
            "target_chunk_id": record["target_chunk_id"],
            "original_query_text": record["query_text"],
            "generated_query_text": generated.query_text,
            "generated_by": generated.generated_by,
        }
    except Exception as exc:
        return {
            "status": "error",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "query_id": record["query_id"],
            "doc_id": record["doc_id"],
            "target_chunk_id": record["target_chunk_id"],
            "original_query_text": record["query_text"],
            "error_type": exc.__class__.__name__,
            "error_message": str(exc),
        }


if __name__ == "__main__":
    raise SystemExit(main())
