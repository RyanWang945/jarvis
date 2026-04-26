from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import time

import httpx


@dataclass(frozen=True)
class EmbeddingVector:
    index: int
    embedding: list[float]


@dataclass(frozen=True)
class EmbeddingBatchResult:
    model: str
    dimensions: int
    vectors: list[EmbeddingVector]


class DashScopeEmbeddingClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        batch_size: int = 16,
        max_workers: int = 2,
        timeout_seconds: float = 60.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._batch_size = batch_size
        self._max_workers = max_workers
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client

    def embed_texts(self, texts: list[str]) -> EmbeddingBatchResult:
        if not texts:
            raise ValueError("texts must not be empty")
        batches = [
            texts[index : index + self._batch_size]
            for index in range(0, len(texts), self._batch_size)
        ]
        if len(batches) == 1:
            body = self._embed_batch(batches[0])
            vectors = [
                EmbeddingVector(index=item["index"], embedding=item["embedding"])
                for item in body["data"]
            ]
            dimensions = len(vectors[0].embedding)
            return EmbeddingBatchResult(
                model=body.get("model", self._model),
                dimensions=dimensions,
                vectors=vectors,
            )

        with ThreadPoolExecutor(max_workers=min(self._max_workers, len(batches))) as executor:
            batch_results = list(executor.map(self._embed_batch, batches))
        vectors: list[EmbeddingVector] = []
        offset = 0
        for body, batch in zip(batch_results, batches, strict=True):
            for item in body["data"]:
                vectors.append(
                    EmbeddingVector(
                        index=offset + int(item["index"]),
                        embedding=item["embedding"],
                    )
                )
            offset += len(batch)
        vectors.sort(key=lambda item: item.index)
        dimensions = len(vectors[0].embedding)
        return EmbeddingBatchResult(
            model=batch_results[0].get("model", self._model),
            dimensions=dimensions,
            vectors=vectors,
        )

    def _embed_batch(self, texts: list[str]) -> dict:
        payload = {
            "model": self._model,
            "input": texts,
        }
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self._client.post(
                    f"{self._base_url}/embeddings",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                if response.status_code >= 400:
                    raise RuntimeError(
                        f"DashScope embeddings request failed: {response.status_code} {response.text}"
                    )
                return response.json()
            except (httpx.HTTPError, RuntimeError) as exc:
                last_error = exc
                if attempt == 2:
                    break
                time.sleep(1.5 * (attempt + 1))
        if last_error is None:
            raise RuntimeError("DashScope embeddings request failed without details")
        raise last_error

    @property
    def _client(self) -> httpx.Client:
        if self._http_client is not None:
            return self._http_client
        return httpx.Client(timeout=self._timeout_seconds, trust_env=False)
