import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    instruction TEXT,
    summary TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    title TEXT,
    description TEXT,
    status TEXT NOT NULL,
    resource_key TEXT,
    dod TEXT,
    verification_cmd TEXT,
    tool_name TEXT,
    worker_type TEXT,
    order_id TEXT,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 0,
    result_summary TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS work_orders (
    order_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    ca_thread_id TEXT NOT NULL,
    worker_type TEXT NOT NULL,
    action TEXT NOT NULL,
    args TEXT,
    workdir TEXT,
    risk_level TEXT NOT NULL,
    reason TEXT,
    verification_cmd TEXT,
    timeout_seconds INTEGER DEFAULT 30,
    status TEXT NOT NULL DEFAULT 'pending',
    dispatched_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS work_results (
    order_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    ca_thread_id TEXT NOT NULL,
    worker_type TEXT NOT NULL,
    ok INTEGER NOT NULL,
    exit_code INTEGER,
    stdout TEXT,
    stderr TEXT,
    artifacts TEXT,
    summary TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    order_id TEXT,
    action_kind TEXT,
    command TEXT,
    risk_level TEXT,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'waiting',
    approved_by TEXT,
    approved_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    task_id TEXT,
    order_id TEXT,
    node TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS resource_locks (
    resource_key TEXT PRIMARY KEY,
    owner_thread_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'held',
    acquired_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

"""


def init_business_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _ensure_column(conn, "work_results", "artifacts", "TEXT")
    _dedupe_runs(conn)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_thread_id ON runs(thread_id)")
    conn.commit()
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _dedupe_runs(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT rowid, thread_id
        FROM runs
        ORDER BY updated_at DESC, created_at DESC, rowid DESC
        """
    ).fetchall()
    seen: set[str] = set()
    duplicate_rowids: list[int] = []
    for row in rows:
        if row["thread_id"] in seen:
            duplicate_rowids.append(row["rowid"])
        else:
            seen.add(row["thread_id"])
    if duplicate_rowids:
        conn.executemany(
            "DELETE FROM runs WHERE rowid = ?",
            [(rowid,) for rowid in duplicate_rowids],
        )
