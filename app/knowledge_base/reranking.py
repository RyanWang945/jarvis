from __future__ import annotations

import httpx

from app.knowledge_base.search import SearchHit


class RerankerClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 3.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client
        self._owned_client: httpx.Client | None = None

    def rerank_hits(
        self,
        *,
        query: str,
        hits: list[SearchHit],
        top_n: int,
        max_length: int = 1024,
    ) -> list[SearchHit]:
        if not hits:
            return []
        response = self._client.post(
            f"{self._base_url}/rerank",
            json={
                "query": query,
                "top_n": top_n,
                "max_length": max_length,
                "documents": [_document_payload(hit, rank) for rank, hit in enumerate(hits, start=1)],
            },
        )
        response.raise_for_status()
        body = response.json()
        hits_by_id = {hit.chunk_id: hit for hit in hits}
        ranked: list[SearchHit] = []
        for item in body.get("results", []):
            original = hits_by_id.get(str(item.get("id") or ""))
            if original is None:
                continue
            reranker_score = float(item.get("score") or 0.0)
            source = dict(original.source or {})
            source["retrieval_score"] = original.score
            source["reranker_score"] = reranker_score
            source["reranker_rank"] = item.get("rank")
            source["reranker_provider"] = body.get("provider")
            source["reranker_model"] = body.get("model")
            source["reranker_latency_ms"] = body.get("latency_ms")
            ranked.append(
                SearchHit(
                    chunk_id=original.chunk_id,
                    doc_id=original.doc_id,
                    score=reranker_score,
                    source=source,
                )
            )
        return ranked

    @property
    def _client(self) -> httpx.Client:
        if self._http_client is not None:
            return self._http_client
        if self._owned_client is None:
            self._owned_client = httpx.Client(timeout=self._timeout_seconds, trust_env=False)
        return self._owned_client


def _document_payload(hit: SearchHit, rank: int) -> dict:
    source = dict(hit.source or {})
    return {
        "id": hit.chunk_id,
        "text": str(source.get("content") or ""),
        "metadata": {
            "doc_id": hit.doc_id,
            "source_id": source.get("source_id"),
            "source_type": source.get("source_type"),
            "title": source.get("title"),
            "section_path": source.get("section_path"),
            "section_title": source.get("section_title"),
            "retrieval_score": hit.score,
            "retrieval_rank": rank,
        },
    }
