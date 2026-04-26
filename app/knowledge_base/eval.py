from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import quantiles
from typing import Any

from app.config import Settings
from app.knowledge_base.embedding import DashScopeEmbeddingClient
from app.knowledge_base.search import OpenSearchClient
from app.knowledge_base.search import SearchHit
from app.llm.client import ChatClient, LLMMessage, parse_json_content


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
    boundary_spill_rate: float
    p95_latency_ms: int
    avg_latency_ms: int


class QueryGenerationService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

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
        client = ChatClient(
            api_key=_provider_api_key(self._settings),
            base_url=_provider_base_url(self._settings),
            model=_provider_model(self._settings),
            timeout_seconds=self._settings.llm_timeout_seconds,
        )
        message = client.chat(
            [
                LLMMessage(
                    role="system",
                    content=(
                        "You generate evaluation queries for a retrieval system. "
                        "Return strict JSON with keys: query_text, query_type, difficulty, gold_answer."
                    ),
                ),
                LLMMessage(
                    role="user",
                    content=json.dumps(
                        {
                            "title": document["title"],
                            "chunk_text": chunk["normalized_content"][:1200],
                            "instructions": [
                                "Generate one natural-language query that this chunk should answer.",
                                "Avoid copying the first sentence verbatim.",
                                "Use query_type in {fact, definition, entity, paraphrase}.",
                                "Use difficulty in {easy, medium, hard}.",
                                "Keep gold_answer short.",
                            ],
                        },
                        ensure_ascii=False,
                    ),
                ),
            ],
            response_format={"type": "json_object"},
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


class KnowledgeBaseEvaluationService:
    def __init__(self, *, settings: Settings, db: Any, kb_service: Any) -> None:
        self._settings = settings
        self._db = db
        self._kb_service = kb_service
        self._generator = QueryGenerationService(settings)

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
        query_count = 0
        for document in documents:
            chunks = self._db.chunks.list_by_document(
                document["doc_id"],
                chunk_profile_id=chunk_profile_id,
            )[:chunks_per_document]
            for chunk in chunks:
                generated = self._generator.generate(
                    document=document,
                    chunk=chunk,
                    mode=generation_mode,
                )
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
                        "gold_evidence_json": [chunk["chunk_id"]],
                        "generated_by": generated.generated_by,
                        "review_status": "generated",
                    }
                )
                query_count += 1
        return EvalDatasetResult(
            dataset_id=dataset_id,
            generated_queries=query_count,
            generation_method=generation_mode,
            query_model=query_model,
        )

    def run_evaluation(
        self,
        *,
        dataset_id: str,
        retrieval_mode: str,
        top_k: int,
        chunk_profile_id: str,
        language: str,
    ) -> EvalRunSummary:
        dataset = self._db.eval_datasets.get(dataset_id)
        if dataset is None:
            raise ValueError(f"Unknown dataset_id: {dataset_id}")
        profile = self._db.chunk_profiles.get(chunk_profile_id)
        if profile is None:
            raise ValueError(f"Unknown chunk_profile_id: {chunk_profile_id}")
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
        boundary_spills = 0
        precision_values: list[float] = []
        for query in queries:
            started = time.perf_counter()
            search_hits = self._search_query(
                query=query,
                retrieval_mode=retrieval_mode,
                top_k=top_k,
                language=language,
                chunk_profile_id=chunk_profile_id,
                query_vectors=query_vectors,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            latencies.append(latency_ms)
            target_chunk_id = query["target_chunk_id"]
            hit_rank = _find_hit_rank(search_hits, target_chunk_id)
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
            chunk_hit_rate=hits / query_count if query_count else 0.0,
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
    ) -> list[SearchHit]:
        if retrieval_mode == "bm25":
            return self._kb_service.search(
                query=query["query_text"],
                language=language,
                chunk_profile_id=chunk_profile_id,
                mode="bm25",
                top_k=top_k,
            )
        opensearch_client = OpenSearchClient(
            base_url=self._settings.opensearch_base_url,
            index_prefix=self._settings.opensearch_index_prefix,
            username=self._settings.opensearch_username,
            password=self._settings.opensearch_password,
        )
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
        bm25_hits = opensearch_client.bm25_search(
            index_name=index_name,
            query=query["query_text"],
            top_k=top_k,
        )
        vector_hits = opensearch_client.vector_search(
            index_name=index_name,
            query_vector=query_vector,
            top_k=top_k,
        )
        from app.knowledge_base.search import combine_hybrid_hits

        return combine_hybrid_hits(
            bm25_hits=bm25_hits,
            vector_hits=vector_hits,
            top_k=top_k,
        )

    def get_run_summary(self, eval_run_id: str) -> EvalRunSummary:
        run = self._db.eval_runs.get(eval_run_id)
        if run is None:
            raise ValueError(f"Unknown eval_run_id: {eval_run_id}")
        results = self._db.eval_results.list_by_run(eval_run_id)
        query_count = len(results)
        hits = sum(int(item["hit"]) for item in results)
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
            chunk_hit_rate=hits / query_count if query_count else 0.0,
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
