from __future__ import annotations

import json
from dataclasses import dataclass
import time

import httpx


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    doc_id: str
    score: float
    source: dict


class OpenSearchClient:
    def __init__(
        self,
        *,
        base_url: str,
        index_prefix: str,
        username: str | None = None,
        password: str | None = None,
        bulk_batch_size: int = 100,
        bulk_max_retries: int = 4,
        timeout_seconds: float = 30.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._index_prefix = index_prefix
        self._auth = (username, password) if username and password else None
        self._bulk_batch_size = bulk_batch_size
        self._bulk_max_retries = bulk_max_retries
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client
        self._owned_client: httpx.Client | None = None

    def index_name(self, *, language: str, chunk_profile_id: str) -> str:
        return f"{self._index_prefix}_{language}_{chunk_profile_id}"

    def ensure_index(self, *, index_name: str, embedding_dim: int | None = None) -> None:
        mapping = {
            "settings": {
                "index": {
                    "knn": True,
                }
            },
            "mappings": {
                "properties": {
                    "chunk_id": {"type": "keyword"},
                    "doc_id": {"type": "keyword"},
                    "source_id": {"type": "keyword"},
                    "external_id": {"type": "keyword"},
                    "language": {"type": "keyword"},
                    "chunk_profile_id": {"type": "keyword"},
                    "title": {
                        "type": "text",
                        "fields": {
                            "keyword": {"type": "keyword"},
                            "raw": {"type": "keyword"},
                        },
                    },
                    "url": {"type": "keyword"},
                    "content": {
                        "type": "text",
                        "fields": {
                            "raw": {"type": "keyword", "ignore_above": 32766},
                        },
                    },
                    "section_path": {"type": "keyword"},
                    "chunk_index": {"type": "integer"},
                    "char_count": {"type": "integer"},
                    "token_estimate": {"type": "integer"},
                    "chunker_version": {"type": "keyword"},
                    "embedding_model": {"type": "keyword"},
                    "text_hash": {"type": "keyword"},
                    "created_at": {"type": "date"},
                }
            }
        }
        if embedding_dim is not None:
            mapping["mappings"]["properties"]["embedding"] = {
                "type": "knn_vector",
                "dimension": embedding_dim,
            }
        response = self._client.put(
            f"{self._base_url}/{index_name}",
            auth=self._auth,
            json=mapping,
        )
        if response.status_code not in (200, 201):
            if response.status_code == 400 and "resource_already_exists_exception" in response.text:
                return
            response.raise_for_status()

    def bulk_index(self, *, index_name: str, documents: list[dict]) -> None:
        for batch_start in range(0, len(documents), self._bulk_batch_size):
            batch = documents[batch_start : batch_start + self._bulk_batch_size]
            self._bulk_index_batch(index_name=index_name, documents=batch)
        refresh = self._client.post(
            f"{self._base_url}/{index_name}/_refresh",
            auth=self._auth,
        )
        refresh.raise_for_status()

    def _bulk_index_batch(self, *, index_name: str, documents: list[dict]) -> None:
        lines: list[str] = []
        for document in documents:
            lines.append(json.dumps({"index": {"_index": index_name, "_id": document["chunk_id"]}}))
            lines.append(json.dumps(document, ensure_ascii=False))
        payload = "\n".join(lines) + "\n"
        last_error: Exception | None = None
        for attempt in range(self._bulk_max_retries + 1):
            try:
                response = self._client.post(
                    f"{self._base_url}/_bulk",
                    auth=self._auth,
                    headers={"Content-Type": "application/x-ndjson"},
                    content=payload.encode("utf-8"),
                )
                if response.status_code == 429:
                    raise RuntimeError(f"OpenSearch bulk rate limited: {response.text}")
                response.raise_for_status()
                body = response.json()
                if body.get("errors"):
                    raise RuntimeError(f"OpenSearch bulk indexing returned errors: {body}")
                return
            except (httpx.HTTPError, RuntimeError) as exc:
                last_error = exc
                if attempt >= self._bulk_max_retries:
                    break
                time.sleep(2 * (attempt + 1))
        if last_error is None:
            raise RuntimeError("OpenSearch bulk indexing failed without details")
        raise last_error

    def bm25_search(self, *, index_name: str, query: str, top_k: int) -> list[SearchHit]:
        body = {
            "size": top_k,
            "query": {
                "bool": {
                    "should": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": ["title^2", "content"],
                            }
                        },
                        {"term": {"title.raw": {"value": query, "boost": 8}}},
                        {"wildcard": {"title.raw": {"value": f"*{query}*", "boost": 6}}},
                        {"wildcard": {"content.raw": {"value": f"*{query}*", "boost": 3}}},
                    ],
                    "minimum_should_match": 1,
                }
            },
        }
        response = self._client.post(
            f"{self._base_url}/{index_name}/_search",
            auth=self._auth,
            json=body,
        )
        response.raise_for_status()
        return _parse_hits(response.json())

    def vector_search(
        self,
        *,
        index_name: str,
        query_vector: list[float],
        top_k: int,
    ) -> list[SearchHit]:
        body = {
            "size": top_k,
            "query": {
                "knn": {
                    "embedding": {
                        "vector": query_vector,
                        "k": top_k,
                    }
                }
            },
        }
        response = self._client.post(
            f"{self._base_url}/{index_name}/_search",
            auth=self._auth,
            json=body,
        )
        response.raise_for_status()
        return _parse_hits(response.json())

    @property
    def _client(self) -> httpx.Client:
        if self._http_client is not None:
            return self._http_client
        if self._owned_client is None:
            self._owned_client = httpx.Client(timeout=self._timeout_seconds, trust_env=False)
        return self._owned_client


def combine_hybrid_hits(
    *,
    bm25_hits: list[SearchHit],
    vector_hits: list[SearchHit],
    alpha: float = 0.45,
    beta: float = 0.55,
    top_k: int,
) -> list[SearchHit]:
    combined: dict[str, dict] = {}
    for hit in bm25_hits:
        combined[hit.chunk_id] = {
            "hit": hit,
            "bm25": hit.score,
            "vector": 0.0,
        }
    for hit in vector_hits:
        existing = combined.get(hit.chunk_id)
        if existing is None:
            combined[hit.chunk_id] = {
                "hit": hit,
                "bm25": 0.0,
                "vector": hit.score,
            }
        else:
            existing["vector"] = hit.score

    bm25_max = max((item["bm25"] for item in combined.values()), default=1.0)
    vector_max = max((item["vector"] for item in combined.values()), default=1.0)
    rescored: list[SearchHit] = []
    for item in combined.values():
        bm25_norm = item["bm25"] / bm25_max if bm25_max else 0.0
        vector_norm = item["vector"] / vector_max if vector_max else 0.0
        final_score = alpha * bm25_norm + beta * vector_norm
        hit = item["hit"]
        rescored.append(
            SearchHit(
                chunk_id=hit.chunk_id,
                doc_id=hit.doc_id,
                score=final_score,
                source=hit.source,
            )
        )
    rescored.sort(key=lambda item: item.score, reverse=True)
    return rescored[:top_k]


def _parse_hits(body: dict) -> list[SearchHit]:
    return [
        SearchHit(
            chunk_id=hit["_source"]["chunk_id"],
            doc_id=hit["_source"]["doc_id"],
            score=float(hit["_score"]),
            source=hit["_source"],
        )
        for hit in body.get("hits", {}).get("hits", [])
    ]
