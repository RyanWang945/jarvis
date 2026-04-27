from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--input-json", required=True)
    parser.add_argument("--dataset-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    records = [
        json.loads(line)
        for line in Path(args.input_json).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    dataset_records = [record for record in records if record["dataset_id"] == args.dataset_id]
    if not dataset_records:
        raise ValueError(f"No records found for dataset_id={args.dataset_id}")

    conn = sqlite3.connect(args.db, timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("DELETE FROM kb_eval_queries WHERE dataset_id = ?", (args.dataset_id,))
        for record in dataset_records:
            conn.execute(
                """
                INSERT INTO kb_eval_queries (
                    query_id, dataset_id, doc_id, target_chunk_id, query_text, query_type,
                    difficulty, gold_answer, gold_evidence_json, generated_by, review_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["query_id"],
                    record["dataset_id"],
                    record["doc_id"],
                    record["target_chunk_id"],
                    record["query_text"],
                    record["query_type"],
                    record["difficulty"],
                    record["gold_answer"],
                    json.dumps(record["gold_evidence"], ensure_ascii=False),
                    record["generated_by"],
                    record["review_status"],
                ),
            )
        conn.commit()
    finally:
        conn.close()

    print(
        json.dumps(
            {
                "dataset_id": args.dataset_id,
                "imported_queries": len(dataset_records),
                "input_json": args.input_json,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
