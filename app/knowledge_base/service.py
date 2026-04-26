from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.knowledge_base.embedding import DashScopeEmbeddingClient
from app.knowledge_base.eval import EvalDatasetResult, EvalRunSummary, KnowledgeBaseEvaluationService
from app.knowledge_base.ingest import IngestResult, WikipediaIngestService
from app.knowledge_base.indexing import IndexResult, KnowledgeBaseIndexService
from app.knowledge_base.repositories import KnowledgeBaseDB, get_knowledge_base_db
from app.knowledge_base.search import OpenSearchClient, SearchHit


@dataclass(frozen=True)
class KnowledgeBaseInfo:
    db_path: str
    default_language: str
    default_chunk_profile: str
    active_chunk_profiles: list[dict]


class KnowledgeBaseService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._db_path = self._resolve_db_path(settings)
        self._db: KnowledgeBaseDB = get_knowledge_base_db(self._db_path)

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def db(self) -> KnowledgeBaseDB:
        return self._db

    def get_info(self) -> KnowledgeBaseInfo:
        return KnowledgeBaseInfo(
            db_path=str(self._db_path),
            default_language=self._settings.knowledge_default_language,
            default_chunk_profile=self._settings.knowledge_default_chunk_profile,
            active_chunk_profiles=self._db.chunk_profiles.list_active(),
        )

    def health_check(self) -> dict[str, str]:
        self._db.conn.execute("SELECT 1").fetchone()
        return {
            "status": "ok",
            "db_path": str(self._db_path),
        }

    def ingest_wikipedia(
        self,
        *,
        file_path: str,
        source_id: str | None = None,
        language: str | None = None,
        limit_n: int | None = None,
        chunk_profile_id: str | None = None,
    ) -> IngestResult:
        ingest_service = WikipediaIngestService(self._db)
        return ingest_service.ingest(
            file_path=Path(file_path),
            source_id=source_id,
            language=language or self._settings.knowledge_default_language,
            limit_n=limit_n,
            chunk_profile_id=chunk_profile_id or self._settings.knowledge_default_chunk_profile,
        )

    def index_source(
        self,
        *,
        source_id: str,
        chunk_profile_id: str | None = None,
        top_limit: int | None = None,
    ) -> IndexResult:
        service = KnowledgeBaseIndexService(
            db=self._db,
            embedding_client=self._embedding_client(),
            opensearch_client=self._opensearch_client(),
        )
        return service.index_source(
            source_id=source_id,
            chunk_profile_id=chunk_profile_id or self._settings.knowledge_default_chunk_profile,
            top_limit=top_limit,
        )

    def search(
        self,
        *,
        query: str,
        language: str | None = None,
        chunk_profile_id: str | None = None,
        mode: str,
        top_k: int,
    ) -> list[SearchHit]:
        service = KnowledgeBaseIndexService(
            db=self._db,
            embedding_client=self._embedding_client(),
            opensearch_client=self._opensearch_client(),
        )
        return service.search(
            query=query,
            language=language or self._settings.knowledge_default_language,
            chunk_profile_id=chunk_profile_id or self._settings.knowledge_default_chunk_profile,
            mode=mode,
            top_k=top_k,
        )

    def generate_eval_dataset(
        self,
        *,
        source_id: str,
        chunk_profile_id: str | None = None,
        generation_mode: str = "llm",
        max_documents: int = 10,
        chunks_per_document: int = 1,
    ) -> EvalDatasetResult:
        service = KnowledgeBaseEvaluationService(
            settings=self._settings,
            db=self._db,
            kb_service=self,
        )
        return service.generate_dataset(
            source_id=source_id,
            chunk_profile_id=chunk_profile_id or self._settings.knowledge_default_chunk_profile,
            generation_mode=generation_mode,
            max_documents=max_documents,
            chunks_per_document=chunks_per_document,
        )

    def run_eval(
        self,
        *,
        dataset_id: str,
        retrieval_mode: str,
        top_k: int,
        language: str | None = None,
        chunk_profile_id: str | None = None,
    ) -> EvalRunSummary:
        service = KnowledgeBaseEvaluationService(
            settings=self._settings,
            db=self._db,
            kb_service=self,
        )
        return service.run_evaluation(
            dataset_id=dataset_id,
            retrieval_mode=retrieval_mode,
            top_k=top_k,
            language=language or self._settings.knowledge_default_language,
            chunk_profile_id=chunk_profile_id or self._settings.knowledge_default_chunk_profile,
        )

    def get_eval_run_summary(self, eval_run_id: str) -> EvalRunSummary:
        service = KnowledgeBaseEvaluationService(
            settings=self._settings,
            db=self._db,
            kb_service=self,
        )
        return service.get_run_summary(eval_run_id)

    @staticmethod
    def _resolve_db_path(settings: Settings) -> Path:
        if settings.knowledge_db_path is not None:
            return settings.knowledge_db_path
        return settings.data_dir / "knowledge.db"

    def _embedding_client(self) -> DashScopeEmbeddingClient:
        if not self._settings.dashscope_api_key:
            raise ValueError("JARVIS_DASHSCOPE_API_KEY is required for embeddings")
        return DashScopeEmbeddingClient(
            api_key=self._settings.dashscope_api_key,
            base_url=self._settings.dashscope_base_url,
            model=self._settings.dashscope_embedding_model,
            batch_size=self._settings.dashscope_embedding_batch_size,
            max_workers=self._settings.dashscope_embedding_max_workers,
        )

    def _opensearch_client(self) -> OpenSearchClient:
        return OpenSearchClient(
            base_url=self._settings.opensearch_base_url,
            index_prefix=self._settings.opensearch_index_prefix,
            username=self._settings.opensearch_username,
            password=self._settings.opensearch_password,
            bulk_batch_size=self._settings.opensearch_bulk_batch_size,
            bulk_max_retries=self._settings.opensearch_bulk_max_retries,
        )
