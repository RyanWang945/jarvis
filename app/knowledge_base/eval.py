from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import math
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import quantiles
from typing import Any

from app.config import Settings
from app.knowledge_base.embedding import DashScopeEmbeddingClient
from app.knowledge_base.reranking import RerankerClient
from app.knowledge_base.search import OpenSearchClient
from app.knowledge_base.search import SearchHit
from app.llm.client import ChatClient, parse_json_content
from app.prompting import PromptRegistry


@dataclass(frozen=True)
class GeneratedQuery:
    query_text: str
    query_type: str
    difficulty: str
    gold_answer: str
    generated_by: str


@dataclass(frozen=True)
class EvalDatasetResult:
    dataset_id: str
    generated_queries: int
    generation_method: str
    query_model: str | None


@dataclass(frozen=True)
class EvalRunSummary:
    eval_run_id: str
    dataset_id: str
    retrieval_mode: str
    top_k: int
    query_count: int
    recall_at_k: float
    precision_at_k: float
    mrr: float
    ndcg: float
    chunk_hit_rate: float
    span_hit_rate: float | None
    doc_hit_rate: float | None
    boundary_spill_rate: float
    p95_latency_ms: int
    avg_latency_ms: int


@dataclass(frozen=True)
class EvalEvidenceSpan:
    doc_id: str
    char_start: int
    char_end: int
    source_chunk_id: str | None = None
    evidence_id: str | None = None
    role: str | None = None


class QueryGenerationService:
    def __init__(self, settings: Settings, *, prompt_registry: PromptRegistry | None = None) -> None:
        self._settings = settings
        self._prompt_registry = prompt_registry or PromptRegistry()

    def generate(
        self,
        *,
        document: dict[str, Any],
        chunk: dict[str, Any],
        mode: str,
    ) -> GeneratedQuery:
        if mode == "llm":
            try:
                return self._generate_with_llm(document=document, chunk=chunk)
            except Exception:
                return self._generate_heuristic(document=document, chunk=chunk)
        return self._generate_heuristic(document=document, chunk=chunk)

    def _generate_with_llm(self, *, document: dict[str, Any], chunk: dict[str, Any]) -> GeneratedQuery:
        preferred_style = _preferred_query_style(chunk["chunk_id"])
        client = ChatClient(
            api_key=_provider_api_key(self._settings),
            base_url=_provider_base_url(self._settings),
            model=_provider_model(self._settings),
            timeout_seconds=self._settings.llm_timeout_seconds,
        )
        prompt = self._prompt_registry.load("kb_eval_query_generation")
        message = client.chat(
            prompt.render(
                {
                    "input_json": json.dumps(
                        {
                            "title": document["title"],
                            "chunk_text": chunk["normalized_content"][:1200],
                            "preferred_style": preferred_style,
                        },
                        ensure_ascii=False,
                    )
                }
            ),
            response_format=prompt.response_format,
        )
        body = parse_json_content(message)
        query_text = str(body.get("query_text") or "").strip()
        if not query_text:
            raise ValueError("LLM returned empty query_text")
        return GeneratedQuery(
            query_text=query_text,
            query_type=str(body.get("query_type") or "fact").strip(),
            difficulty=str(body.get("difficulty") or "medium").strip(),
            gold_answer=str(body.get("gold_answer") or document["title"]).strip(),
            generated_by=f"llm:{_provider_model(self._settings)}",
        )

    def _generate_heuristic(self, *, document: dict[str, Any], chunk: dict[str, Any]) -> GeneratedQuery:
        title = document["title"]
        snippet = chunk["normalized_content"][:80].strip()
        return GeneratedQuery(
            query_text=f"{title}是什么？",
            query_type="definition",
            difficulty="easy",
            gold_answer=snippet,
            generated_by="heuristic",
        )


def _preferred_query_style(chunk_id: str) -> str:
    styles = [
        "fact",
        "entity",
        "paraphrase",
        "fact",
        "paraphrase",
        "definition",
    ]
    return styles[sum(ord(char) for char in chunk_id) % len(styles)]


class KnowledgeBaseEvaluationService:
    _query_generation_max_workers = 4

    def __init__(self, *, settings: Settings, db: Any, kb_service: Any) -> None:
        self._settings = settings
        self._db = db
        self._kb_service = kb_service
        self._generator = QueryGenerationService(settings)
        self._opensearch_client_instance: OpenSearchClient | None = None
        self._reranker_client_instance: RerankerClient | None = None

    def generate_dataset(
        self,
        *,
        source_id: str,
        chunk_profile_id: str,
        generation_mode: str,
        max_documents: int,
        chunks_per_document: int,
    ) -> EvalDatasetResult:
        dataset_id = f"kb_eval_dataset_{uuid.uuid4()}"
        query_model = _provider_model(self._settings) if generation_mode == "llm" else None
        self._db.eval_datasets.save(
            {
                "dataset_id": dataset_id,
                "name": f"{source_id}:{chunk_profile_id}:{generation_mode}",
                "source_id": source_id,
                "generation_method": generation_mode,
                "query_model": query_model,
                "sample_doc_count": max_documents,
            }
        )
        documents = self._db.documents.list_by_source(source_id, limit=max_documents)
        tasks = self._build_generation_tasks(
            documents=documents,
            chunk_profile_id=chunk_profile_id,
            chunks_per_document=chunks_per_document,
        )
        query_count = 0
        max_workers = min(self._query_generation_max_workers, len(tasks))
        if max_workers > 0:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [
                    executor.submit(
                        self._generator.generate,
                        document=task["document"],
                        chunk=task["chunk"],
                        mode=generation_mode,
                    )
                    for task in tasks
                ]
                for task, future in zip(tasks, futures, strict=True):
                    generated = future.result()
                    self._save_generated_query(
                        dataset_id=dataset_id,
                        document=task["document"],
                        chunk=task["chunk"],
                        generated=generated,
                    )
                    query_count += 1
        return EvalDatasetResult(
            dataset_id=dataset_id,
            generated_queries=query_count,
            generation_method=generation_mode,
            query_model=query_model,
        )

    def _build_generation_tasks(
        self,
        *,
        documents: list[dict[str, Any]],
        chunk_profile_id: str,
        chunks_per_document: int,
    ) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for document in documents:
            chunks = self._db.chunks.list_by_document(
                document["doc_id"],
                chunk_profile_id=chunk_profile_id,
            )[:chunks_per_document]
            for chunk in chunks:
                tasks.append({"document": document, "chunk": chunk})
        return tasks

    def _save_generated_query(
        self,
        *,
        dataset_id: str,
        document: dict[str, Any],
        chunk: dict[str, Any],
        generated: GeneratedQuery,
    ) -> None:
        self._db.eval_queries.save(
            {
                "query_id": f"kb_eval_query_{uuid.uuid4()}",
                "dataset_id": dataset_id,
                "doc_id": document["doc_id"],
                "target_chunk_id": chunk["chunk_id"],
                "query_text": generated.query_text,
                "query_type": generated.query_type,
                "difficulty": generated.difficulty,
                "gold_answer": generated.gold_answer,
                "gold_evidence_json": _build_span_gold_evidence(chunk),
                "generated_by": generated.generated_by,
                "review_status": "generated",
            }
        )

    def run_evaluation(
        self,
        *,
        dataset_id: str,
        retrieval_mode: str,
        top_k: int,
        chunk_profile_id: str,
        language: str,
        retrieval_params: dict[str, Any] | None = None,
    ) -> EvalRunSummary:
        dataset = self._db.eval_datasets.get(dataset_id)
        if dataset is None:
            raise ValueError(f"Unknown dataset_id: {dataset_id}")
        profile = self._db.chunk_profiles.get(chunk_profile_id)
        if profile is None:
            raise ValueError(f"Unknown chunk_profile_id: {chunk_profile_id}")
        resolved_params = self._resolve_retrieval_params(
            retrieval_mode=retrieval_mode,
            top_k=top_k,
            retrieval_params=retrieval_params,
        )
        eval_run_id = f"kb_eval_run_{uuid.uuid4()}"
        index_name = f"{self._settings.opensearch_index_prefix}_{language}_{chunk_profile_id}"
        self._db.eval_runs.save(
            {
                "eval_run_id": eval_run_id,
                "dataset_id": dataset_id,
                "retrieval_mode": retrieval_mode,
                "top_k": top_k,
                "chunk_profile_id": chunk_profile_id,
                "chunker_version": profile["chunker_version"],
                "embedding_model": self._settings.dashscope_embedding_model if retrieval_mode != "bm25" else None,
                "index_name": index_name,
                "params_json": resolved_params,
                "status": "running",
                "started_at": _utc_now(),
            }
        )
        queries = self._db.eval_queries.list_by_dataset(dataset_id)
        query_vectors = self._embed_queries(queries, retrieval_mode)
        latencies: list[int] = []
        mrr_values: list[float] = []
        ndcg_values: list[float] = []
        hits = 0
        chunk_hits = 0
        span_hits = 0
        span_query_count = 0
        doc_hits = 0
        doc_query_count = 0
        boundary_spills = 0
        precision_values: list[float] = []
        evidence_role = _resolve_evidence_role(resolved_params)
        for query in queries:
            started = time.perf_counter()
            search_hits = self._search_query(
                query=query,
                retrieval_mode=retrieval_mode,
                top_k=top_k,
                language=language,
                chunk_profile_id=chunk_profile_id,
                query_vectors=query_vectors,
                retrieval_params=resolved_params,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            latencies.append(latency_ms)
            target_chunk_id = query["target_chunk_id"]
            chunk_hit_rank = _find_hit_rank(search_hits, target_chunk_id)
            chunk_hits += 1 if chunk_hit_rank is not None else 0

            evidence_spans = _resolve_gold_evidence_spans(
                db=self._db,
                query=query,
                evidence_role=evidence_role,
            )
            span_hit_rank = None
            if evidence_spans:
                span_query_count += 1
                span_hit_rank = _find_span_hit_rank(
                    db=self._db,
                    hits=search_hits,
                    evidence_spans=evidence_spans,
                )
                span_hits += 1 if span_hit_rank is not None else 0

            target_doc_ids = _target_doc_ids(query=query, evidence_spans=evidence_spans)
            doc_hit_rank = None
            if target_doc_ids:
                doc_query_count += 1
                doc_hit_rank = _find_doc_hit_rank(
                    db=self._db,
                    hits=search_hits,
                    target_doc_ids=target_doc_ids,
                )
                doc_hits += 1 if doc_hit_rank is not None else 0

            uses_span_primary = evidence_role in {"answer", "legacy_chunk", "any"} or bool(evidence_spans)
            hit_rank = span_hit_rank if uses_span_primary else chunk_hit_rank
            hit = 1 if hit_rank is not None else 0
            hits += hit
            precision_values.append((1.0 / top_k) if hit else 0.0)
            if hit_rank is None and _has_boundary_spill(
                db=self._db,
                hits=search_hits,
                target_chunk_id=target_chunk_id,
            ):
                boundary_spills += 1
            mrr = 1.0 / hit_rank if hit_rank is not None else 0.0
            ndcg = 1.0 / _log2(hit_rank + 1) if hit_rank is not None else 0.0
            mrr_values.append(mrr)
            ndcg_values.append(ndcg)
            self._db.eval_results.save(
                {
                    "result_id": f"kb_eval_result_{uuid.uuid4()}",
                    "eval_run_id": eval_run_id,
                    "query_id": query["query_id"],
                    "hit": hit,
                    "hit_rank": hit_rank,
                    "mrr_score": mrr,
                    "ndcg_score": ndcg,
                    "retrieved_chunk_ids_json": [item.chunk_id for item in search_hits],
                    "retrieved_scores_json": [item.score for item in search_hits],
                    "latency_ms": latency_ms,
                }
            )
        self._db.eval_runs.save(
            {
                "eval_run_id": eval_run_id,
                "dataset_id": dataset_id,
                "retrieval_mode": retrieval_mode,
                "top_k": top_k,
                "chunk_profile_id": chunk_profile_id,
                "chunker_version": profile["chunker_version"],
                "embedding_model": self._settings.dashscope_embedding_model if retrieval_mode != "bm25" else None,
                "index_name": index_name,
                "params_json": resolved_params,
                "status": "succeeded",
                "finished_at": _utc_now(),
            }
        )
        query_count = len(queries)
        return EvalRunSummary(
            eval_run_id=eval_run_id,
            dataset_id=dataset_id,
            retrieval_mode=retrieval_mode,
            top_k=top_k,
            query_count=query_count,
            recall_at_k=hits / query_count if query_count else 0.0,
            precision_at_k=sum(precision_values) / query_count if query_count else 0.0,
            mrr=sum(mrr_values) / query_count if query_count else 0.0,
            ndcg=sum(ndcg_values) / query_count if query_count else 0.0,
            chunk_hit_rate=chunk_hits / query_count if query_count else 0.0,
            span_hit_rate=span_hits / span_query_count if span_query_count else None,
            doc_hit_rate=doc_hits / doc_query_count if doc_query_count else None,
            boundary_spill_rate=boundary_spills / query_count if query_count else 0.0,
            p95_latency_ms=_p95(latencies),
            avg_latency_ms=int(sum(latencies) / query_count) if query_count else 0,
        )

    def _embed_queries(self, queries: list[dict[str, Any]], retrieval_mode: str) -> dict[str, list[float]]:
        if retrieval_mode == "bm25" or not queries:
            return {}
        client = DashScopeEmbeddingClient(
            api_key=_dashscope_api_key(self._settings),
            base_url=self._settings.dashscope_base_url,
            model=self._settings.dashscope_embedding_model,
            batch_size=self._settings.dashscope_embedding_batch_size,
            max_workers=self._settings.dashscope_embedding_max_workers,
        )
        result = client.embed_texts([query["query_text"] for query in queries])
        return {
            query["query_id"]: vector.embedding
            for query, vector in zip(queries, result.vectors, strict=True)
        }

    def _search_query(
        self,
        *,
        query: dict[str, Any],
        retrieval_mode: str,
        top_k: int,
        language: str,
        chunk_profile_id: str,
        query_vectors: dict[str, list[float]],
        retrieval_params: dict[str, Any] | None,
    ) -> list[SearchHit]:
        if retrieval_mode == "bm25":
            return self._opensearch_client().bm25_search(
                index_name=self._opensearch_client().index_name(
                    language=language,
                    chunk_profile_id=chunk_profile_id,
                ),
                query=query["query_text"],
                top_k=top_k,
            )
        opensearch_client = self._opensearch_client()
        index_name = opensearch_client.index_name(
            language=language,
            chunk_profile_id=chunk_profile_id,
        )
        query_vector = query_vectors[query["query_id"]]
        if retrieval_mode == "vector":
            return opensearch_client.vector_search(
                index_name=index_name,
                query_vector=query_vector,
                top_k=top_k,
            )
        if retrieval_mode in {"hybrid", "rrf"}:
            bm25_top_k = top_k
            vector_top_k = top_k
            rrf_k = 60
        elif retrieval_mode == "rrf_v2":
            bm25_top_k = int((retrieval_params or {}).get("bm25_candidate_k", 20))
            vector_top_k = int((retrieval_params or {}).get("vector_candidate_k", 20))
            rrf_k = int((retrieval_params or {}).get("rrf_k", 60))
        elif retrieval_mode == "rrf_v2_rerank":
            rerank_input_top_k = int(
                (retrieval_params or {}).get(
                    "rerank_input_top_k",
                    self._settings.knowledge_reranker_input_top_k,
                )
            )
            bm25_top_k = int((retrieval_params or {}).get("bm25_candidate_k", rerank_input_top_k))
            vector_top_k = int((retrieval_params or {}).get("vector_candidate_k", rerank_input_top_k))
            rrf_k = int((retrieval_params or {}).get("rrf_k", 60))
        else:
            raise ValueError(f"Unsupported retrieval mode: {retrieval_mode}")
        bm25_hits = opensearch_client.bm25_search(
            index_name=index_name,
            query=query["query_text"],
            top_k=bm25_top_k,
        )
        vector_hits = opensearch_client.vector_search(
            index_name=index_name,
            query_vector=query_vector,
            top_k=vector_top_k,
        )
        if retrieval_mode == "hybrid":
            from app.knowledge_base.search import combine_hybrid_hits

            return combine_hybrid_hits(
                bm25_hits=bm25_hits,
                vector_hits=vector_hits,
                top_k=top_k,
            )
        if retrieval_mode == "rrf":
            from app.knowledge_base.search import combine_rrf_hits

            return combine_rrf_hits(
                bm25_hits=bm25_hits,
                vector_hits=vector_hits,
                top_k=top_k,
                k=rrf_k,
            )
        if retrieval_mode == "rrf_v2":
            from app.knowledge_base.search import combine_rrf_hits

            return combine_rrf_hits(
                bm25_hits=bm25_hits,
                vector_hits=vector_hits,
                top_k=top_k,
                k=rrf_k,
            )
        if retrieval_mode == "rrf_v2_rerank":
            from app.knowledge_base.search import combine_rrf_hits

            rerank_input_top_k = int(
                (retrieval_params or {}).get(
                    "rerank_input_top_k",
                    self._settings.knowledge_reranker_input_top_k,
                )
            )
            candidates = combine_rrf_hits(
                bm25_hits=bm25_hits,
                vector_hits=vector_hits,
                top_k=rerank_input_top_k,
                k=rrf_k,
            )
            reranker = self._reranker_client()
            if reranker is None:
                return candidates[:top_k]
            try:
                reranked = reranker.rerank_hits(
                    query=query["query_text"],
                    hits=candidates,
                    top_n=top_k,
                    max_length=int(
                        (retrieval_params or {}).get(
                            "rerank_max_length",
                            self._settings.knowledge_reranker_max_length,
                        )
                    ),
                )
            except Exception:
                return candidates[:top_k]
            return reranked or candidates[:top_k]
        raise ValueError(f"Unsupported retrieval mode: {retrieval_mode}")

    def _resolve_retrieval_params(
        self,
        *,
        retrieval_mode: str,
        top_k: int,
        retrieval_params: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if retrieval_mode not in {"rrf_v2", "rrf_v2_rerank"}:
            return retrieval_params
        params = dict(retrieval_params or {})
        if retrieval_mode == "rrf_v2_rerank":
            candidate_default = int(params.get("rerank_input_top_k", self._settings.knowledge_reranker_input_top_k))
        else:
            candidate_default = 20
        params.setdefault("bm25_candidate_k", candidate_default)
        params.setdefault("vector_candidate_k", candidate_default)
        params.setdefault("rrf_k", 60)
        params.setdefault("final_top_k", top_k)
        if retrieval_mode == "rrf_v2_rerank":
            params.setdefault("rerank_input_top_k", candidate_default)
            params.setdefault("rerank_max_length", self._settings.knowledge_reranker_max_length)
        return params

    def _opensearch_client(self) -> OpenSearchClient:
        if self._opensearch_client_instance is None:
            self._opensearch_client_instance = OpenSearchClient(
                base_url=self._settings.opensearch_base_url,
                index_prefix=self._settings.opensearch_index_prefix,
                username=self._settings.opensearch_username,
                password=self._settings.opensearch_password,
            )
        return self._opensearch_client_instance

    def _reranker_client(self) -> RerankerClient | None:
        if not self._settings.knowledge_reranker_base_url:
            return None
        if self._reranker_client_instance is None:
            self._reranker_client_instance = RerankerClient(
                base_url=self._settings.knowledge_reranker_base_url,
                timeout_seconds=self._settings.knowledge_reranker_timeout_seconds,
            )
        return self._reranker_client_instance

    def get_run_summary(self, eval_run_id: str) -> EvalRunSummary:
        run = self._db.eval_runs.get(eval_run_id)
        if run is None:
            raise ValueError(f"Unknown eval_run_id: {eval_run_id}")
        results = self._db.eval_results.list_by_run(eval_run_id)
        query_count = len(results)
        hits = sum(int(item["hit"]) for item in results)
        run_params = _load_json_value(run.get("params_json"))
        hit_rates = _calculate_persisted_hit_rates(
            self._db,
            results,
            evidence_role=_resolve_evidence_role(run_params),
        )
        latencies = [int(item["latency_ms"]) for item in results]
        return EvalRunSummary(
            eval_run_id=eval_run_id,
            dataset_id=run["dataset_id"],
            retrieval_mode=run["retrieval_mode"],
            top_k=int(run["top_k"]),
            query_count=query_count,
            recall_at_k=hits / query_count if query_count else 0.0,
            precision_at_k=(hits / query_count / int(run["top_k"])) if query_count else 0.0,
            mrr=sum(float(item["mrr_score"]) for item in results) / query_count if query_count else 0.0,
            ndcg=sum(float(item["ndcg_score"]) for item in results) / query_count if query_count else 0.0,
            chunk_hit_rate=hit_rates["chunk_hit_rate"],
            span_hit_rate=hit_rates["span_hit_rate"],
            doc_hit_rate=hit_rates["doc_hit_rate"],
            boundary_spill_rate=_boundary_spill_rate(self._db, results),
            p95_latency_ms=_p95(latencies),
            avg_latency_ms=int(sum(latencies) / query_count) if query_count else 0,
        )


def _find_hit_rank(hits: list[SearchHit], target_chunk_id: str | None) -> int | None:
    if target_chunk_id is None:
        return None
    for index, hit in enumerate(hits, start=1):
        if hit.chunk_id == target_chunk_id:
            return index
    return None


def _calculate_persisted_hit_rates(
    db: Any,
    results: list[dict[str, Any]],
    *,
    evidence_role: str | None,
) -> dict[str, float | None]:
    query_count = len(results)
    if query_count == 0:
        return {
            "chunk_hit_rate": 0.0,
            "span_hit_rate": None,
            "doc_hit_rate": None,
        }

    chunk_hits = 0
    span_hits = 0
    span_query_count = 0
    doc_hits = 0
    doc_query_count = 0
    for result in results:
        query = _query_for_result(db=db, result=result)
        if query is None:
            continue
        hits = _hits_from_result(db=db, result=result)
        if _find_hit_rank(hits, query.get("target_chunk_id")) is not None:
            chunk_hits += 1

        evidence_spans = _resolve_gold_evidence_spans(
            db=db,
            query=query,
            evidence_role=evidence_role,
        )
        if evidence_spans:
            span_query_count += 1
            if _find_span_hit_rank(db=db, hits=hits, evidence_spans=evidence_spans) is not None:
                span_hits += 1

        target_doc_ids = _target_doc_ids(query=query, evidence_spans=evidence_spans)
        if target_doc_ids:
            doc_query_count += 1
            if _find_doc_hit_rank(db=db, hits=hits, target_doc_ids=target_doc_ids) is not None:
                doc_hits += 1

    return {
        "chunk_hit_rate": chunk_hits / query_count,
        "span_hit_rate": span_hits / span_query_count if span_query_count else None,
        "doc_hit_rate": doc_hits / doc_query_count if doc_query_count else None,
    }


def _query_for_result(*, db: Any, result: dict[str, Any]) -> dict[str, Any] | None:
    row = db.conn.execute(
        "SELECT * FROM kb_eval_queries WHERE query_id = ?",
        (result["query_id"],),
    ).fetchone()
    return dict(row) if row else None


def _hits_from_result(*, db: Any, result: dict[str, Any]) -> list[SearchHit]:
    hit_ids = json.loads(result["retrieved_chunk_ids_json"])
    hits: list[SearchHit] = []
    for chunk_id in hit_ids:
        chunk = db.chunks.get(chunk_id)
        hits.append(
            SearchHit(
                chunk_id=chunk_id,
                doc_id=chunk["doc_id"] if chunk else "",
                score=0.0,
                source={},
            )
        )
    return hits


def _build_span_gold_evidence(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": "span_v1",
        "evidence": [
            {
                "evidence_id": f"{chunk['chunk_id']}:span:0",
                "type": "span",
                "doc_id": chunk["doc_id"],
                "char_start": int(chunk["char_start"]),
                "char_end": int(chunk["char_end"]),
                "source_chunk_id": chunk["chunk_id"],
            }
        ],
    }


def _resolve_gold_evidence_spans(
    *,
    db: Any,
    query: dict[str, Any],
    evidence_role: str | None = None,
) -> list[EvalEvidenceSpan]:
    payload = _load_json_value(query.get("gold_evidence_json"))
    spans = _evidence_spans_from_payload(payload, evidence_role=evidence_role)
    if spans:
        return spans
    if evidence_role == "answer":
        return []

    chunk_ids = _legacy_gold_chunk_ids(payload)
    target_chunk_id = query.get("target_chunk_id")
    if target_chunk_id:
        chunk_ids.append(target_chunk_id)

    resolved: list[EvalEvidenceSpan] = []
    seen: set[str] = set()
    for chunk_id in chunk_ids:
        if not chunk_id or chunk_id in seen:
            continue
        seen.add(chunk_id)
        chunk = db.chunks.get(chunk_id)
        if chunk is None:
            continue
        resolved.append(
            EvalEvidenceSpan(
                doc_id=chunk["doc_id"],
                char_start=int(chunk["char_start"]),
                char_end=int(chunk["char_end"]),
                source_chunk_id=chunk["chunk_id"],
                evidence_id=f"{chunk['chunk_id']}:legacy-span",
                role="legacy_chunk",
            )
        )
    return resolved


def _load_json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _evidence_spans_from_payload(payload: Any, *, evidence_role: str | None) -> list[EvalEvidenceSpan]:
    evidence_items: list[Any]
    if isinstance(payload, dict):
        evidence_items = list(payload.get("evidence") or [])
    elif isinstance(payload, list):
        evidence_items = payload
    else:
        return []

    spans: list[EvalEvidenceSpan] = []
    for item in evidence_items:
        if not isinstance(item, dict):
            continue
        if item.get("type", "span") != "span":
            continue
        role = str(item["role"]) if item.get("role") else None
        if evidence_role and evidence_role != "any" and role != evidence_role:
            continue
        try:
            doc_id = str(item["doc_id"])
            char_start = int(item["char_start"])
            char_end = int(item["char_end"])
        except (KeyError, TypeError, ValueError):
            continue
        if char_end <= char_start:
            continue
        spans.append(
            EvalEvidenceSpan(
                doc_id=doc_id,
                char_start=char_start,
                char_end=char_end,
                source_chunk_id=str(item["source_chunk_id"]) if item.get("source_chunk_id") else None,
                evidence_id=str(item["evidence_id"]) if item.get("evidence_id") else None,
                role=role,
            )
        )
    return spans


def _legacy_gold_chunk_ids(payload: Any) -> list[str]:
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, str)]


def _resolve_evidence_role(retrieval_params: Any) -> str | None:
    if not isinstance(retrieval_params, dict):
        return None
    role = retrieval_params.get("evidence_role")
    if not isinstance(role, str):
        return None
    normalized = role.strip()
    if normalized in {"answer", "legacy_chunk", "any"}:
        return normalized
    return None


def _find_span_hit_rank(
    *,
    db: Any,
    hits: list[SearchHit],
    evidence_spans: list[EvalEvidenceSpan],
) -> int | None:
    for index, hit in enumerate(hits, start=1):
        candidate = _candidate_span_for_hit(db=db, hit=hit)
        if candidate is None:
            continue
        for evidence in evidence_spans:
            if candidate.doc_id != evidence.doc_id:
                continue
            overlap = _overlap_chars(
                candidate.char_start,
                candidate.char_end,
                evidence.char_start,
                evidence.char_end,
            )
            if overlap >= _required_span_overlap(evidence):
                return index
    return None


def _candidate_span_for_hit(*, db: Any, hit: SearchHit) -> EvalEvidenceSpan | None:
    chunk = db.chunks.get(hit.chunk_id)
    if chunk is not None:
        return EvalEvidenceSpan(
            doc_id=chunk["doc_id"],
            char_start=int(chunk["char_start"]),
            char_end=int(chunk["char_end"]),
            source_chunk_id=chunk["chunk_id"],
        )
    source = getattr(hit, "source", {}) or {}
    try:
        doc_id = str(getattr(hit, "doc_id", None) or source["doc_id"])
        char_start = int(source["char_start"])
        char_end = int(source["char_end"])
    except (KeyError, TypeError, ValueError):
        return None
    if char_end <= char_start:
        return None
    return EvalEvidenceSpan(doc_id=doc_id, char_start=char_start, char_end=char_end, source_chunk_id=hit.chunk_id)


def _overlap_chars(left_start: int, left_end: int, right_start: int, right_end: int) -> int:
    return max(0, min(left_end, right_end) - max(left_start, right_start))


def _required_span_overlap(evidence: EvalEvidenceSpan) -> int:
    span_length = max(1, evidence.char_end - evidence.char_start)
    return max(1, min(80, math.ceil(span_length * 0.3)))


def _target_doc_ids(*, query: dict[str, Any], evidence_spans: list[EvalEvidenceSpan]) -> set[str]:
    doc_ids = {span.doc_id for span in evidence_spans}
    query_doc_id = query.get("doc_id")
    if query_doc_id:
        doc_ids.add(str(query_doc_id))
    return doc_ids


def _find_doc_hit_rank(*, db: Any, hits: list[SearchHit], target_doc_ids: set[str]) -> int | None:
    for index, hit in enumerate(hits, start=1):
        doc_id = getattr(hit, "doc_id", None)
        if not doc_id:
            chunk = db.chunks.get(hit.chunk_id)
            doc_id = chunk["doc_id"] if chunk else None
        if doc_id in target_doc_ids:
            return index
    return None


def _has_boundary_spill(*, db: Any, hits: list[SearchHit], target_chunk_id: str | None) -> bool:
    if target_chunk_id is None:
        return False
    target = db.chunks.get(target_chunk_id)
    if target is None:
        return False
    for hit in hits:
        chunk = db.chunks.get(hit.chunk_id)
        if not chunk:
            continue
        if chunk["doc_id"] == target["doc_id"] and abs(int(chunk["chunk_index"]) - int(target["chunk_index"])) <= 1:
            return True
    return False


def _boundary_spill_rate(db: Any, results: list[dict[str, Any]]) -> float:
    if not results:
        return 0.0
    spills = 0
    for result in results:
        if int(result["hit"]) == 1:
            continue
        hit_ids = json.loads(result["retrieved_chunk_ids_json"])
        hits = [SearchHit(chunk_id=item, doc_id="", score=0.0, source={}) for item in hit_ids]
        query_row = db.conn.execute(
            "SELECT target_chunk_id FROM kb_eval_queries WHERE query_id = ?",
            (result["query_id"],),
        ).fetchone()
        target_chunk_id = query_row["target_chunk_id"] if query_row else None
        if _has_boundary_spill(db=db, hits=hits, target_chunk_id=target_chunk_id):
            spills += 1
    return spills / len(results)


def _provider_api_key(settings: Settings) -> str:
    provider = settings.llm_provider.lower()
    if provider == "deepseek" and settings.deepseek_api_key:
        return settings.deepseek_api_key
    if provider == "kimi" and settings.kimi_api_key:
        return settings.kimi_api_key
    if provider == "gemini" and settings.gemini_api_key:
        return settings.gemini_api_key
    raise ValueError("No configured LLM API key for eval generation")


def _dashscope_api_key(settings: Settings) -> str:
    if not settings.dashscope_api_key:
        raise ValueError("JARVIS_DASHSCOPE_API_KEY is required for vector/hybrid eval")
    return settings.dashscope_api_key


def _provider_base_url(settings: Settings) -> str:
    provider = settings.llm_provider.lower()
    if provider == "deepseek":
        return settings.deepseek_base_url
    if provider == "kimi":
        return settings.kimi_base_url
    if provider == "gemini":
        return settings.gemini_base_url
    raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")


def _provider_model(settings: Settings) -> str:
    provider = settings.llm_provider.lower()
    if provider == "deepseek":
        return settings.deepseek_model
    if provider == "kimi":
        return settings.kimi_model
    if provider == "gemini":
        return settings.gemini_model
    raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    if len(values) == 1:
        return values[0]
    return int(quantiles(values, n=20, method="inclusive")[18])


def _log2(value: int) -> float:
    import math

    return math.log2(value)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
