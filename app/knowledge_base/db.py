import json
import sqlite3
from pathlib import Path

from app.sqlite_utils import connect_sqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_sources (
    source_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'generic',
    language TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    file_path TEXT NOT NULL,
    description TEXT,
    owner TEXT,
    region TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kb_documents (
    doc_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    language TEXT NOT NULL,
    metadata_json TEXT,
    ingest_job_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_documents_source_external
ON kb_documents(source_id, external_id);

CREATE INDEX IF NOT EXISTS idx_kb_documents_title
ON kb_documents(title);

CREATE TABLE IF NOT EXISTS kb_chunk_profiles (
    chunk_profile_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    language TEXT,
    chunker_version TEXT NOT NULL,
    target_size INTEGER NOT NULL,
    soft_min_size INTEGER NOT NULL,
    hard_max_size INTEGER NOT NULL,
    overlap_size INTEGER NOT NULL,
    boundary_rules_json TEXT,
    normalization_rules_json TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kb_chunks (
    chunk_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    chunk_profile_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunker_version TEXT NOT NULL,
    section_path TEXT,
    raw_content TEXT NOT NULL,
    normalized_content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    char_start INTEGER NOT NULL,
    char_end INTEGER NOT NULL,
    char_count INTEGER NOT NULL,
    token_estimate INTEGER NOT NULL,
    overlap_prev_chars INTEGER NOT NULL,
    is_boundary_forced INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_chunks_doc_profile_index
ON kb_chunks(doc_id, chunk_profile_id, chunk_index);

CREATE INDEX IF NOT EXISTS idx_kb_chunks_profile
ON kb_chunks(chunk_profile_id);

CREATE TABLE IF NOT EXISTS kb_chunk_embeddings (
    chunk_id TEXT PRIMARY KEY,
    embedding_model TEXT NOT NULL,
    embedding_dim INTEGER NOT NULL,
    embedding_json TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kb_ingest_jobs (
    job_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    limit_n INTEGER,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    documents_seen INTEGER NOT NULL DEFAULT 0,
    documents_inserted INTEGER NOT NULL DEFAULT 0,
    documents_updated INTEGER NOT NULL DEFAULT 0,
    documents_skipped INTEGER NOT NULL DEFAULT 0,
    chunks_created INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kb_parse_artifacts (
    artifact_id TEXT PRIMARY KEY,
    filing_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    parser_vendor TEXT NOT NULL,
    parser_model TEXT,
    parser_version TEXT,
    parse_config_json TEXT,
    input_sha256 TEXT,
    raw_output_path TEXT,
    normalized_output_path TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kb_chunk_runs (
    chunk_run_id TEXT PRIMARY KEY,
    filing_id TEXT NOT NULL,
    parse_artifact_id TEXT NOT NULL,
    chunk_profile_id TEXT NOT NULL,
    chunker_version TEXT NOT NULL,
    config_json TEXT,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kb_index_runs (
    index_run_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    chunk_run_id TEXT NOT NULL,
    index_name TEXT NOT NULL,
    embedding_model TEXT,
    embedding_dim INTEGER,
    opensearch_mapping_version TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kb_eval_datasets (
    dataset_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_id TEXT NOT NULL,
    generation_method TEXT NOT NULL,
    query_model TEXT,
    sample_doc_count INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kb_eval_queries (
    query_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    target_chunk_id TEXT,
    query_text TEXT NOT NULL,
    query_type TEXT NOT NULL,
    difficulty TEXT NOT NULL,
    gold_answer TEXT,
    gold_evidence_json TEXT,
    generated_by TEXT,
    review_status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kb_eval_runs (
    eval_run_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    retrieval_mode TEXT NOT NULL,
    top_k INTEGER NOT NULL,
    chunk_profile_id TEXT NOT NULL,
    chunker_version TEXT NOT NULL,
    embedding_model TEXT,
    index_name TEXT NOT NULL,
    params_json TEXT,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kb_eval_results (
    result_id TEXT PRIMARY KEY,
    eval_run_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    hit INTEGER NOT NULL,
    hit_rank INTEGER,
    mrr_score REAL NOT NULL,
    ndcg_score REAL NOT NULL,
    retrieved_chunk_ids_json TEXT NOT NULL,
    retrieved_scores_json TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


DEFAULT_CHUNK_PROFILES = (
    {
        "chunk_profile_id": "medium_overlap_v1",
        "name": "Medium Overlap V1",
        "language": None,
        "chunker_version": "v1",
        "target_size": 800,
        "soft_min_size": 500,
        "hard_max_size": 1200,
        "overlap_size": 120,
        "boundary_rules_json": None,
        "normalization_rules_json": None,
        "is_active": 1,
    },
    {
        "chunk_profile_id": "sec_filing_medium_v1",
        "name": "SEC Filing Medium V1",
        "language": "en",
        "chunker_version": "sec_v1",
        "target_size": 1600,
        "soft_min_size": 900,
        "hard_max_size": 2400,
        "overlap_size": 200,
        "boundary_rules_json": {
            "mode": "section_aware",
            "preserve_table_context": True,
        },
        "normalization_rules_json": {
            "strip_image_placeholders": False,
        },
        "is_active": 1,
    },
)


def init_knowledge_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(db_path)
    conn.executescript(SCHEMA)
    _migrate_schema(conn)
    _seed_chunk_profiles(conn)
    conn.commit()
    return conn


def _seed_chunk_profiles(conn: sqlite3.Connection) -> None:
    for profile in DEFAULT_CHUNK_PROFILES:
        conn.execute(
            """
            INSERT OR IGNORE INTO kb_chunk_profiles (
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
            """,
                (
                    profile["chunk_profile_id"],
                    profile["name"],
                    profile["language"],
                    profile["chunker_version"],
                    profile["target_size"],
                    profile["soft_min_size"],
                    profile["hard_max_size"],
                    profile["overlap_size"],
                    _dump_json(profile["boundary_rules_json"]),
                    _dump_json(profile["normalization_rules_json"]),
                    profile["is_active"],
                ),
            )


def _migrate_schema(conn: sqlite3.Connection) -> None:
    _ensure_columns(
        conn,
        table_name="kb_sources",
        columns={
            "source_type": "TEXT NOT NULL DEFAULT 'generic'",
            "owner": "TEXT",
            "region": "TEXT",
            "metadata_json": "TEXT",
        },
    )
    _ensure_columns(
        conn,
        table_name="kb_eval_runs",
        columns={
            "params_json": "TEXT",
        },
    )


def _ensure_columns(conn: sqlite3.Connection, *, table_name: str, columns: dict[str, str]) -> None:
    existing = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    for column_name, column_def in columns.items():
        if column_name in existing:
            continue
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")


def _dump_json(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
