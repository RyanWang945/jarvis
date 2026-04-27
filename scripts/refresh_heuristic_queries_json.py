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
    parser.add_argument("--input-json", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--max-workers", type=int, default=4)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    generator = QueryGenerationService(settings)
    input_path = Path(args.input_json)
    output_path = Path(args.output_json)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    records = [
        json.loads(line)
        for line in input_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    conn = sqlite3.connect(f"file:{args.source_db}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row

    tasks = []
    for index, record in enumerate(records):
        if record["generated_by"] != "heuristic":
            continue
        document = dict(
            conn.execute("SELECT * FROM kb_documents WHERE doc_id = ?", (record["doc_id"],)).fetchone()
        )
        chunk = dict(
            conn.execute("SELECT * FROM kb_chunks WHERE chunk_id = ?", (record["target_chunk_id"],)).fetchone()
        )
        tasks.append({"index": index, "record": record, "document": document, "chunk": chunk})

    print(
        json.dumps(
            {
                "event": "start",
                "total_records": len(records),
                "heuristic_records": len(tasks),
                "output_json": str(output_path),
                "max_workers": min(args.max_workers, len(tasks)) if tasks else 0,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    updated_records = list(records)
    if tasks:
        next_index = 0
        completed = 0
        active: dict[Future, dict[str, Any]] = {}
        max_workers = min(args.max_workers, len(tasks))
        started_at = time.perf_counter()
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            while next_index < len(tasks) and len(active) < max_workers:
                task = tasks[next_index]
                next_index += 1
                active[executor.submit(_refresh_one, generator, task)] = task

            while active:
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    task = active.pop(future)
                    result = future.result()
                    updated_records[task["index"]] = result["record"]
                    completed += 1
                    print(
                        json.dumps(
                            {
                                "event": "progress",
                                "completed": completed,
                                "target": len(tasks),
                                "elapsed_ms": result["elapsed_ms"],
                                "status": result["status"],
                                "query_text": result["record"]["query_text"],
                                "generated_by": result["record"]["generated_by"],
                                "original_query_text": task["record"]["query_text"],
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    if next_index < len(tasks):
                        task = tasks[next_index]
                        next_index += 1
                        active[executor.submit(_refresh_one, generator, task)] = task

        print(
            json.dumps(
                {
                    "event": "generation_done",
                    "heuristic_records": len(tasks),
                    "total_elapsed_ms": int((time.perf_counter() - started_at) * 1000),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    with output_path.open("w", encoding="utf-8") as handle:
        for record in updated_records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    llm_count = sum(1 for record in updated_records if str(record["generated_by"]).startswith("llm:"))
    heuristic_count = sum(1 for record in updated_records if record["generated_by"] == "heuristic")
    print(
        json.dumps(
            {
                "event": "done",
                "total_records": len(updated_records),
                "llm_records": llm_count,
                "heuristic_records": heuristic_count,
                "output_json": str(output_path),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


def _refresh_one(generator: QueryGenerationService, task: dict[str, Any]) -> dict[str, Any]:
    record = dict(task["record"])
    started = time.perf_counter()
    try:
        generated = generator._generate_with_llm(
            document=task["document"],
            chunk=task["chunk"],
        )
        record["query_text"] = generated.query_text
        record["query_type"] = generated.query_type
        record["difficulty"] = generated.difficulty
        record["gold_answer"] = generated.gold_answer
        record["generated_by"] = generated.generated_by
        status = "success"
    except Exception as exc:
        record["refresh_error_type"] = exc.__class__.__name__
        record["refresh_error_message"] = str(exc)
        status = "error"
    return {
        "status": status,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "record": record,
    }


if __name__ == "__main__":
    raise SystemExit(main())
