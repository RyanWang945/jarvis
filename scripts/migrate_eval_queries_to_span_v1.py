from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MigrationSummary:
    db_path: str
    total_queries: int
    already_span_v1: int
    updated_queries: int
    skipped_no_legacy_chunk: int
    skipped_missing_chunk: int
    dry_run: bool
    backup_path: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate kb_eval_queries.gold_evidence_json from legacy chunk ids to span_v1 evidence."
    )
    parser.add_argument("--db-path", default="data/knowledge.db")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--backup-dir", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = migrate_eval_queries_to_span_v1(
        Path(args.db_path),
        dry_run=args.dry_run,
        create_backup=not args.no_backup,
        backup_dir=Path(args.backup_dir) if args.backup_dir else None,
    )
    print(json.dumps(asdict(summary), ensure_ascii=False, indent=2))
    return 0


def migrate_eval_queries_to_span_v1(
    db_path: Path,
    *,
    dry_run: bool = False,
    create_backup: bool = True,
    backup_dir: Path | None = None,
) -> MigrationSummary:
    resolved_db_path = db_path.resolve()
    if not resolved_db_path.exists():
        raise FileNotFoundError(resolved_db_path)

    backup_path: Path | None = None
    if create_backup and not dry_run:
        backup_path = _backup_database(resolved_db_path, backup_dir=backup_dir)

    conn = sqlite3.connect(resolved_db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT query_id, target_chunk_id, gold_evidence_json
            FROM kb_eval_queries
            ORDER BY created_at, query_id
            """
        ).fetchall()
        total_queries = len(rows)
        already_span_v1 = 0
        updated_queries = 0
        skipped_no_legacy_chunk = 0
        skipped_missing_chunk = 0

        for row in rows:
            payload = _load_json(row["gold_evidence_json"])
            if _is_span_v1(payload):
                already_span_v1 += 1
                continue

            chunk_ids = _legacy_chunk_ids(payload)
            target_chunk_id = row["target_chunk_id"]
            if target_chunk_id:
                chunk_ids.append(str(target_chunk_id))
            chunk_ids = _dedupe(chunk_ids)
            if not chunk_ids:
                skipped_no_legacy_chunk += 1
                continue

            evidence = _resolve_evidence(conn, query_id=row["query_id"], chunk_ids=chunk_ids)
            if not evidence:
                skipped_missing_chunk += 1
                continue

            updated_queries += 1
            if dry_run:
                continue
            conn.execute(
                """
                UPDATE kb_eval_queries
                SET gold_evidence_json = ?
                WHERE query_id = ?
                """,
                (
                    json.dumps(
                        {
                            "version": "span_v1",
                            "migration": {
                                "from": "chunk_legacy",
                                "migrated_at": _utc_now(),
                            },
                            "evidence": evidence,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    row["query_id"],
                ),
            )
        if not dry_run:
            conn.commit()
    except Exception:
        if not dry_run:
            conn.rollback()
        raise
    finally:
        conn.close()

    return MigrationSummary(
        db_path=str(resolved_db_path),
        total_queries=total_queries,
        already_span_v1=already_span_v1,
        updated_queries=updated_queries,
        skipped_no_legacy_chunk=skipped_no_legacy_chunk,
        skipped_missing_chunk=skipped_missing_chunk,
        dry_run=dry_run,
        backup_path=str(backup_path) if backup_path else None,
    )


def _backup_database(db_path: Path, *, backup_dir: Path | None) -> Path:
    target_dir = backup_dir.resolve() if backup_dir else db_path.parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = target_dir / f"{db_path.stem}_before_span_v1_{timestamp}{db_path.suffix}"
    shutil.copy2(db_path, backup_path)
    wal_path = db_path.with_name(f"{db_path.name}-wal")
    shm_path = db_path.with_name(f"{db_path.name}-shm")
    if wal_path.exists():
        shutil.copy2(wal_path, backup_path.with_name(f"{backup_path.name}-wal"))
    if shm_path.exists():
        shutil.copy2(shm_path, backup_path.with_name(f"{backup_path.name}-shm"))
    return backup_path


def _load_json(raw: Any) -> Any:
    if raw is None:
        return None
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _is_span_v1(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("version") == "span_v1"


def _legacy_chunk_ids(payload: Any) -> list[str]:
    if not isinstance(payload, list):
        return []
    chunk_ids: list[str] = []
    for item in payload:
        if isinstance(item, str):
            chunk_ids.append(item)
        elif isinstance(item, dict) and item.get("type") == "chunk_legacy" and item.get("chunk_id"):
            chunk_ids.append(str(item["chunk_id"]))
    return chunk_ids


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _resolve_evidence(conn: sqlite3.Connection, *, query_id: str, chunk_ids: list[str]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for index, chunk_id in enumerate(chunk_ids):
        row = conn.execute(
            """
            SELECT
                chunk_id,
                doc_id,
                chunk_profile_id,
                chunk_index,
                content_hash,
                normalized_content,
                char_start,
                char_end
            FROM kb_chunks
            WHERE chunk_id = ?
            """,
            (chunk_id,),
        ).fetchone()
        if row is None:
            continue
        char_start = int(row["char_start"])
        char_end = int(row["char_end"])
        if char_end <= char_start:
            continue
        evidence.append(
            {
                "evidence_id": f"{query_id}:evidence:{index}",
                "type": "span",
                "doc_id": row["doc_id"],
                "char_start": char_start,
                "char_end": char_end,
                "source_chunk_id": row["chunk_id"],
                "source_chunk_profile_id": row["chunk_profile_id"],
                "source_chunk_index": int(row["chunk_index"]),
                "evidence_hash": row["content_hash"],
                "evidence_text": row["normalized_content"],
            }
        )
    return evidence


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
