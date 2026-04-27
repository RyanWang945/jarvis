from __future__ import annotations

import json
from dataclasses import dataclass

from app.knowledge_base.embedding import DashScopeEmbeddingClient, EmbeddingBatchResult, EmbeddingVector
from app.knowledge_base.repositories import KnowledgeBaseDB
from app.knowledge_base.search import OpenSearchClient, SearchHit, combine_hybrid_hits


@dataclass(frozen=True)
class IndexResult:
    index_name: str
    source_id: str
    chunk_profile_id: str
    indexed_chunks: int
    embedded_chunks: int
    embedding_model: str


class KnowledgeBaseIndexService:
    _document_batch_size = 100

    def __init__(
        self,
        *,
        db: KnowledgeBaseDB,
        embedding_client: DashScopeEmbeddingClient,
        opensearch_client: OpenSearchClient,
    ) -> None:
        self._db = db
        self._embedding_client = embedding_client
        self._opensearch_client = opensearch_client

    def index_source(
        self,
        *,
        source_id: str,
        chunk_profile_id: str,
        top_limit: int | None = None,
    ) -> IndexResult:
        offset = 0
        indexed_chunks = 0
        embedding_model: str | None = None
        index_name: str | None = None
        index_ensured = False
        documents_found = False

        while True:
            limit = self._document_batch_size
            if top_limit is not None:
                remaining = top_limit - offset
                if remaining <= 0:
                    break
                limit = min(limit, remaining)
            documents = self._db.documents.list_by_source(
                source_id,
                limit=limit,
                offset=offset,
            )
            if not documents:
                break
            documents_found = True
            offset += len(documents)

            chunks: list[dict] = []
            for document in documents:
                chunks.extend(
                    self._db.chunks.list_by_document(
                        document["doc_id"],
                        chunk_profile_id=chunk_profile_id,
                    )
                )
            if not chunks:
                continue

            embedding_result = self._embed_missing_chunks(chunks)
            embeddings_by_chunk_id: dict[str, list[float]] = {}
            for chunk, vector in zip(chunks, embedding_result.vectors, strict=True):
                embeddings_by_chunk_id[chunk["chunk_id"]] = vector.embedding

            if index_name is None:
                first_document = documents[0]
                index_name = self._opensearch_client.index_name(
                    language=first_document["language"],
                    chunk_profile_id=chunk_profile_id,
                )
            if not index_ensured:
                self._opensearch_client.ensure_index(
                    index_name=index_name,
                    embedding_dim=embedding_result.dimensions,
                )
                index_ensured = True
            self._opensearch_client.bulk_index(
                index_name=index_name,
                documents=[
                    _build_index_document(
                        document=_document_map(documents, chunk["doc_id"]),
                        chunk=chunk,
                        embedding=embeddings_by_chunk_id[chunk["chunk_id"]],
                        embedding_model=embedding_result.model,
                    )
                    for chunk in chunks
                ],
            )
            indexed_chunks += len(chunks)
            embedding_model = embedding_result.model

        if not documents_found:
            raise ValueError(f"No documents found for source: {source_id}")
        if indexed_chunks == 0:
            raise ValueError("No chunks found to index")
        if index_name is None or embedding_model is None:
            raise ValueError("Indexing did not produce an index name or embedding model")
        return IndexResult(
            index_name=index_name,
            source_id=source_id,
            chunk_profile_id=chunk_profile_id,
            indexed_chunks=indexed_chunks,
            embedded_chunks=indexed_chunks,
            embedding_model=embedding_model,
        )

    def _embed_missing_chunks(self, chunks: list[dict]) -> EmbeddingBatchResult:
        stored_embeddings = {
            embedding["chunk_id"]: embedding
            for embedding in self._db.chunk_embeddings.list_by_chunk_ids(
                [chunk["chunk_id"] for chunk in chunks]
            )
        }
        vectors_by_position: dict[int, EmbeddingVector] = {}
        missing_positions: list[int] = []
        missing_texts: list[str] = []
        model: str | None = None
        dimensions: int | None = None

        for index, chunk in enumerate(chunks):
            stored = stored_embeddings.get(chunk["chunk_id"])
            if stored and stored["text_hash"] == chunk["content_hash"]:
                embedding = json.loads(stored["embedding_json"])
                vectors_by_position[index] = EmbeddingVector(index=index, embedding=embedding)
                model = model or stored["embedding_model"]
                dimensions = dimensions or stored["embedding_dim"]
                continue
            missing_positions.append(index)
            missing_texts.append(chunk["normalized_content"])

        if missing_texts:
            embedded = self._embedding_client.embed_texts(missing_texts)
            model = embedded.model
            dimensions = embedded.dimensions
            for vector in embedded.vectors:
                chunk_index = missing_positions[vector.index]
                chunk = chunks[chunk_index]
                vectors_by_position[chunk_index] = EmbeddingVector(
                    index=chunk_index,
                    embedding=vector.embedding,
                )
                self._db.chunk_embeddings.save(
                    {
                        "chunk_id": chunk["chunk_id"],
                        "embedding_model": embedded.model,
                        "embedding_dim": embedded.dimensions,
                        "embedding_json": vector.embedding,
                        "text_hash": chunk["content_hash"],
                    }
                )

        if model is None or dimensions is None:
            raise ValueError("No embeddings found or generated")

        return EmbeddingBatchResult(
            model=model,
            dimensions=dimensions,
            vectors=[vectors_by_position[index] for index in range(len(chunks))],
        )

    def search(
        self,
        *,
        query: str,
        language: str,
        chunk_profile_id: str,
        mode: str,
        top_k: int,
    ) -> list[SearchHit]:
        index_name = self._opensearch_client.index_name(
            language=language,
            chunk_profile_id=chunk_profile_id,
        )
        if mode == "bm25":
            return self._opensearch_client.bm25_search(
                index_name=index_name,
                query=query,
                top_k=top_k,
            )
        query_vector = self._embedding_client.embed_texts([query]).vectors[0].embedding
        if mode == "vector":
            return self._opensearch_client.vector_search(
                index_name=index_name,
                query_vector=query_vector,
                top_k=top_k,
            )
        if mode == "hybrid":
            bm25_hits = self._opensearch_client.bm25_search(
                index_name=index_name,
                query=query,
                top_k=top_k,
            )
            vector_hits = self._opensearch_client.vector_search(
                index_name=index_name,
                query_vector=query_vector,
                top_k=top_k,
            )
            return combine_hybrid_hits(
                bm25_hits=bm25_hits,
                vector_hits=vector_hits,
                top_k=top_k,
            )
        raise ValueError(f"Unsupported search mode: {mode}")


def _build_index_document(
    *,
    document: dict,
    chunk: dict,
    embedding: list[float],
    embedding_model: str,
) -> dict:
    return {
        "chunk_id": chunk["chunk_id"],
        "doc_id": chunk["doc_id"],
        "source_id": document["source_id"],
        "external_id": document["external_id"],
        "language": document["language"],
        "chunk_profile_id": chunk["chunk_profile_id"],
        "title": document["title"],
        "url": document["url"],
        "content": chunk["normalized_content"],
        "section_path": chunk["section_path"],
        "chunk_index": chunk["chunk_index"],
        "char_count": chunk["char_count"],
        "token_estimate": chunk["token_estimate"],
        "chunker_version": chunk["chunker_version"],
        "embedding_model": embedding_model,
        "embedding": embedding,
        "text_hash": chunk["content_hash"],
        "created_at": _normalize_opensearch_date(chunk["created_at"]),
    }


def _document_map(documents: list[dict], doc_id: str) -> dict:
    for document in documents:
        if document["doc_id"] == doc_id:
            return document
    raise KeyError(doc_id)


def _normalize_opensearch_date(value: str) -> str:
    if "T" in value:
        return value
    return value.replace(" ", "T") + "Z"
