from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import quantiles
from typing import Any

from app.config import get_settings
from app.knowledge_base.embedding import DashScopeEmbeddingClient
from app.knowledge_base.eval import (
    _find_doc_hit_rank,
    _find_hit_rank,
    _find_span_hit_rank,
    _resolve_gold_evidence_spans,
)
from app.knowledge_base.search import OpenSearchClient, SearchHit, combine_rrf_hits


@dataclass(frozen=True)
class RoleMetrics:
    evidence_role: str
    query_count: int
    eligible_queries: int
    recall_at_k: float
    recall_at_k_eligible: float
    chunk_hit_rate: float
    doc_hit_rate: float
    precision_at_k: float
    mrr: float
    ndcg: float


@dataclass(frozen=True)
class EvalReport:
    dataset_id: str | None
    query_count: int
    retrieval_mode: str
    top_k: int
    bm25_candidate_k: int
    vector_candidate_k: int
    avg_latency_ms: int
    p95_latency_ms: int
    roles: list[RoleMetrics]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default="data/knowledge.db")
    parser.add_argument("--dataset-id", default=None)
    parser.add_argument("--retrieval-mode", default="rrf_v2", choices=["bm25", "vector", "rrf_v2"])
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--bm25-candidate-k", type=int, default=20)
    parser.add_argument("--vector-candidate-k", type=int, default=20)
    parser.add_argument("--chunk-profile-id", default="medium_overlap_v1")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--output-md", default=None)
    parser.add_argument("--output-json", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = evaluate_roles(
        db_path=Path(args.db_path),
        dataset_id=args.dataset_id,
        retrieval_mode=args.retrieval_mode,
        top_k=args.top_k,
        bm25_candidate_k=args.bm25_candidate_k,
        vector_candidate_k=args.vector_candidate_k,
        chunk_profile_id=args.chunk_profile_id,
        language=args.language,
    )
    body = json.dumps(_report_dict(report), ensure_ascii=False, indent=2)
    print(body)
    if args.output_json:
        Path(args.output_json).write_text(body + "\n", encoding="utf-8")
    if args.output_md:
        Path(args.output_md).write_text(_render_markdown(report), encoding="utf-8")
    return 0


def evaluate_roles(
    *,
    db_path: Path,
    dataset_id: str | None,
    retrieval_mode: str,
    top_k: int,
    bm25_candidate_k: int,
    vector_candidate_k: int,
    chunk_profile_id: str,
    language: str,
) -> EvalReport:
    settings = get_settings()
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    db = _EvalDBAdapter(conn)
    queries = _load_queries(conn, dataset_id=dataset_id)
    if not queries:
        raise ValueError("No eval queries found")

    opensearch = OpenSearchClient(
        base_url=settings.opensearch_base_url,
        index_prefix=settings.opensearch_index_prefix,
        username=settings.opensearch_username,
        password=settings.opensearch_password,
    )
    index_name = opensearch.index_name(language=language, chunk_profile_id=chunk_profile_id)
    query_vectors = _embed_queries(queries) if retrieval_mode in {"vector", "rrf_v2"} else {}

    retrieved_by_query: dict[str, list[SearchHit]] = {}
    latencies: list[int] = []
    for query in queries:
        started = time.perf_counter()
        if retrieval_mode == "bm25":
            hits = opensearch.bm25_search(index_name=index_name, query=query["query_text"], top_k=top_k)
        elif retrieval_mode == "vector":
            hits = opensearch.vector_search(
                index_name=index_name,
                query_vector=query_vectors[query["query_id"]],
                top_k=top_k,
            )
        else:
            bm25_hits = opensearch.bm25_search(
                index_name=index_name,
                query=query["query_text"],
                top_k=bm25_candidate_k,
            )
            vector_hits = opensearch.vector_search(
                index_name=index_name,
                query_vector=query_vectors[query["query_id"]],
                top_k=vector_candidate_k,
            )
            hits = combine_rrf_hits(bm25_hits=bm25_hits, vector_hits=vector_hits, top_k=top_k, k=60)
        latencies.append(int((time.perf_counter() - started) * 1000))
        retrieved_by_query[query["query_id"]] = hits

    roles = [
        _calculate_role_metrics(db, queries, retrieved_by_query, role=role, top_k=top_k)
        for role in ["legacy_chunk", "answer", "any"]
    ]
    return EvalReport(
        dataset_id=dataset_id,
        query_count=len(queries),
        retrieval_mode=retrieval_mode,
        top_k=top_k,
        bm25_candidate_k=bm25_candidate_k,
        vector_candidate_k=vector_candidate_k,
        avg_latency_ms=int(sum(latencies) / len(latencies)) if latencies else 0,
        p95_latency_ms=_p95(latencies),
        roles=roles,
    )


def _load_queries(conn: sqlite3.Connection, *, dataset_id: str | None) -> list[dict[str, Any]]:
    sql = """
        SELECT query_id, dataset_id, doc_id, target_chunk_id, query_text, gold_answer, gold_evidence_json
        FROM kb_eval_queries
    """
    params: list[Any] = []
    if dataset_id:
        sql += " WHERE dataset_id = ?"
        params.append(dataset_id)
    sql += " ORDER BY created_at, query_id"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


class _EvalDBAdapter:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.chunks = _ChunkLookup(conn)


class _ChunkLookup:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, chunk_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM kb_chunks WHERE chunk_id = ?", (chunk_id,)).fetchone()
        return dict(row) if row else None


def _embed_queries(queries: list[dict[str, Any]]) -> dict[str, list[float]]:
    settings = get_settings()
    if not settings.dashscope_api_key:
        raise ValueError("JARVIS_DASHSCOPE_API_KEY is required for vector/rrf eval")
    client = DashScopeEmbeddingClient(
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
        model=settings.dashscope_embedding_model,
        batch_size=settings.dashscope_embedding_batch_size,
        max_workers=settings.dashscope_embedding_max_workers,
    )
    result = client.embed_texts([query["query_text"] for query in queries])
    return {
        query["query_id"]: vector.embedding
        for query, vector in zip(queries, result.vectors, strict=True)
    }


def _calculate_role_metrics(
    db: Any,
    queries: list[dict[str, Any]],
    retrieved_by_query: dict[str, list[SearchHit]],
    *,
    role: str,
    top_k: int,
) -> RoleMetrics:
    query_count = len(queries)
    eligible_queries = 0
    hits = 0
    eligible_hits = 0
    chunk_hits = 0
    doc_hits = 0
    precision_values: list[float] = []
    mrr_values: list[float] = []
    ndcg_values: list[float] = []
    for query in queries:
        retrieved = retrieved_by_query[query["query_id"]]
        spans = _resolve_gold_evidence_spans(db=db, query=query, evidence_role=role)
        span_rank = _find_span_hit_rank(db=db, hits=retrieved, evidence_spans=spans) if spans else None
        doc_ids = {span.doc_id for span in spans}
        doc_rank = _find_doc_hit_rank(db=db, hits=retrieved, target_doc_ids=doc_ids) if doc_ids else None
        chunk_rank = _find_hit_rank(retrieved, query.get("target_chunk_id"))

        if spans:
            eligible_queries += 1
        if span_rank is not None:
            hits += 1
            eligible_hits += 1
            precision_values.append(1.0 / top_k)
            mrr_values.append(1.0 / span_rank)
            ndcg_values.append(1.0 / math.log2(span_rank + 1))
        else:
            precision_values.append(0.0)
            mrr_values.append(0.0)
            ndcg_values.append(0.0)
        if chunk_rank is not None:
            chunk_hits += 1
        if doc_rank is not None:
            doc_hits += 1

    return RoleMetrics(
        evidence_role=role,
        query_count=query_count,
        eligible_queries=eligible_queries,
        recall_at_k=hits / query_count if query_count else 0.0,
        recall_at_k_eligible=eligible_hits / eligible_queries if eligible_queries else 0.0,
        chunk_hit_rate=chunk_hits / query_count if query_count else 0.0,
        doc_hit_rate=doc_hits / query_count if query_count else 0.0,
        precision_at_k=sum(precision_values) / query_count if query_count else 0.0,
        mrr=sum(mrr_values) / query_count if query_count else 0.0,
        ndcg=sum(ndcg_values) / query_count if query_count else 0.0,
    )


def _report_dict(report: EvalReport) -> dict[str, Any]:
    body = asdict(report)
    body["roles"] = [asdict(role) for role in report.roles]
    return body


def _render_markdown(report: EvalReport) -> str:
    lines = [
        "# Wiki Evidence Role Evaluation",
        "",
        f"- Dataset ID: `{report.dataset_id or 'all'}`",
        f"- Query count: `{report.query_count}`",
        f"- Retrieval mode: `{report.retrieval_mode}`",
        f"- Top K: `{report.top_k}`",
        f"- Candidate: BM25 `{report.bm25_candidate_k}` + Vector `{report.vector_candidate_k}`",
        f"- Avg latency: `{report.avg_latency_ms} ms`",
        f"- P95 latency: `{report.p95_latency_ms} ms`",
        "",
        "| Evidence role | Eligible | Recall@K | Recall@K eligible | Chunk hit | Doc hit | Precision@K | MRR | nDCG |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for role in report.roles:
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{role.evidence_role}`",
                    f"{role.eligible_queries}/{role.query_count}",
                    f"{role.recall_at_k:.4f}",
                    f"{role.recall_at_k_eligible:.4f}",
                    f"{role.chunk_hit_rate:.4f}",
                    f"{role.doc_hit_rate:.4f}",
                    f"{role.precision_at_k:.4f}",
                    f"{role.mrr:.4f}",
                    f"{role.ndcg:.4f}",
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    if len(values) == 1:
        return values[0]
    return int(quantiles(values, n=20, method="inclusive")[18])


if __name__ == "__main__":
    raise SystemExit(main())
