from functools import lru_cache

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.config import get_settings
from app.knowledge_base.service import KnowledgeBaseService

router = APIRouter(prefix="/kb", tags=["knowledge-base"])


class KnowledgeBaseHealthResponse(BaseModel):
    status: str
    db_path: str


class KnowledgeBaseInfoResponse(BaseModel):
    db_path: str
    default_language: str
    default_chunk_profile: str
    active_chunk_profiles: list[dict]


class KnowledgeBaseIngestRequest(BaseModel):
    file_path: str = Field(min_length=1)
    source_id: str | None = None
    language: str | None = None
    limit_n: int | None = Field(default=None, ge=1)
    chunk_profile_id: str | None = None


class KnowledgeBaseIngestResponse(BaseModel):
    job_id: str
    source_id: str
    file_path: str
    limit_n: int | None
    documents_seen: int
    documents_inserted: int
    documents_updated: int
    documents_skipped: int
    chunks_created: int
    status: str


class KnowledgeBaseIndexRequest(BaseModel):
    source_id: str = Field(min_length=1)
    chunk_profile_id: str | None = None
    top_limit: int | None = Field(default=None, ge=1)


class KnowledgeBaseIndexResponse(BaseModel):
    index_name: str
    source_id: str
    chunk_profile_id: str
    indexed_chunks: int
    embedded_chunks: int
    embedding_model: str


class KnowledgeBaseSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    mode: str = Field(pattern="^(bm25|vector|hybrid)$")
    language: str | None = None
    chunk_profile_id: str | None = None
    top_k: int = Field(default=5, ge=1, le=50)


class KnowledgeBaseSearchHitResponse(BaseModel):
    chunk_id: str
    doc_id: str
    score: float
    source: dict


class KnowledgeBaseSearchResponse(BaseModel):
    hits: list[KnowledgeBaseSearchHitResponse]


class KnowledgeBaseEvalDatasetRequest(BaseModel):
    source_id: str = Field(min_length=1)
    chunk_profile_id: str | None = None
    generation_mode: str = Field(default="llm", pattern="^(llm|heuristic)$")
    max_documents: int = Field(default=10, ge=1, le=200)
    chunks_per_document: int = Field(default=1, ge=1, le=10)


class KnowledgeBaseEvalDatasetResponse(BaseModel):
    dataset_id: str
    generated_queries: int
    generation_method: str
    query_model: str | None


class KnowledgeBaseEvalRunRequest(BaseModel):
    dataset_id: str = Field(min_length=1)
    retrieval_mode: str = Field(pattern="^(bm25|vector|hybrid)$")
    top_k: int = Field(default=5, ge=1, le=50)
    language: str | None = None
    chunk_profile_id: str | None = None


class KnowledgeBaseEvalRunResponse(BaseModel):
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


@router.get("/health", response_model=KnowledgeBaseHealthResponse)
def knowledge_base_health() -> KnowledgeBaseHealthResponse:
    return KnowledgeBaseHealthResponse(**get_knowledge_base_service().health_check())


@router.get("/info", response_model=KnowledgeBaseInfoResponse)
def knowledge_base_info() -> KnowledgeBaseInfoResponse:
    info = get_knowledge_base_service().get_info()
    return KnowledgeBaseInfoResponse(
        db_path=info.db_path,
        default_language=info.default_language,
        default_chunk_profile=info.default_chunk_profile,
        active_chunk_profiles=info.active_chunk_profiles,
    )


@router.post("/ingest", response_model=KnowledgeBaseIngestResponse)
def knowledge_base_ingest(request: KnowledgeBaseIngestRequest) -> KnowledgeBaseIngestResponse:
    result = get_knowledge_base_service().ingest_wikipedia(
        file_path=request.file_path,
        source_id=request.source_id,
        language=request.language,
        limit_n=request.limit_n,
        chunk_profile_id=request.chunk_profile_id,
    )
    return KnowledgeBaseIngestResponse(
        job_id=result.job_id,
        source_id=result.source_id,
        file_path=result.file_path,
        limit_n=result.limit_n,
        documents_seen=result.documents_seen,
        documents_inserted=result.documents_inserted,
        documents_updated=result.documents_updated,
        documents_skipped=result.documents_skipped,
        chunks_created=result.chunks_created,
        status=result.status,
    )


@router.post("/index", response_model=KnowledgeBaseIndexResponse)
def knowledge_base_index(request: KnowledgeBaseIndexRequest) -> KnowledgeBaseIndexResponse:
    result = get_knowledge_base_service().index_source(
        source_id=request.source_id,
        chunk_profile_id=request.chunk_profile_id,
        top_limit=request.top_limit,
    )
    return KnowledgeBaseIndexResponse(
        index_name=result.index_name,
        source_id=result.source_id,
        chunk_profile_id=result.chunk_profile_id,
        indexed_chunks=result.indexed_chunks,
        embedded_chunks=result.embedded_chunks,
        embedding_model=result.embedding_model,
    )


@router.post("/search", response_model=KnowledgeBaseSearchResponse)
def knowledge_base_search(request: KnowledgeBaseSearchRequest) -> KnowledgeBaseSearchResponse:
    hits = get_knowledge_base_service().search(
        query=request.query,
        language=request.language,
        chunk_profile_id=request.chunk_profile_id,
        mode=request.mode,
        top_k=request.top_k,
    )
    return KnowledgeBaseSearchResponse(
        hits=[
            KnowledgeBaseSearchHitResponse(
                chunk_id=hit.chunk_id,
                doc_id=hit.doc_id,
                score=hit.score,
                source=hit.source,
            )
            for hit in hits
        ]
    )


@router.post("/eval/datasets", response_model=KnowledgeBaseEvalDatasetResponse)
def knowledge_base_generate_eval_dataset(
    request: KnowledgeBaseEvalDatasetRequest,
) -> KnowledgeBaseEvalDatasetResponse:
    result = get_knowledge_base_service().generate_eval_dataset(
        source_id=request.source_id,
        chunk_profile_id=request.chunk_profile_id,
        generation_mode=request.generation_mode,
        max_documents=request.max_documents,
        chunks_per_document=request.chunks_per_document,
    )
    return KnowledgeBaseEvalDatasetResponse(
        dataset_id=result.dataset_id,
        generated_queries=result.generated_queries,
        generation_method=result.generation_method,
        query_model=result.query_model,
    )


@router.post("/eval/run", response_model=KnowledgeBaseEvalRunResponse)
def knowledge_base_run_eval(
    request: KnowledgeBaseEvalRunRequest,
) -> KnowledgeBaseEvalRunResponse:
    summary = get_knowledge_base_service().run_eval(
        dataset_id=request.dataset_id,
        retrieval_mode=request.retrieval_mode,
        top_k=request.top_k,
        language=request.language,
        chunk_profile_id=request.chunk_profile_id,
    )
    return KnowledgeBaseEvalRunResponse(**summary.__dict__)


@router.get("/eval/runs/{eval_run_id}", response_model=KnowledgeBaseEvalRunResponse)
def knowledge_base_eval_run_summary(eval_run_id: str) -> KnowledgeBaseEvalRunResponse:
    summary = get_knowledge_base_service().get_eval_run_summary(eval_run_id)
    return KnowledgeBaseEvalRunResponse(**summary.__dict__)


@lru_cache
def get_knowledge_base_service() -> KnowledgeBaseService:
    return KnowledgeBaseService(get_settings())
