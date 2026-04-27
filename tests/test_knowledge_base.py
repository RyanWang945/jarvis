import json

from fastapi.testclient import TestClient
from httpx import Request, Response

from app.config import get_settings
from app.knowledge_base.chunking import chunk_text
from app.knowledge_base.embedding import DashScopeEmbeddingClient
from app.knowledge_base.eval import GeneratedQuery, KnowledgeBaseEvaluationService
from app.knowledge_base.indexing import KnowledgeBaseIndexService
from app.knowledge_base.parsers.alibaba_pdf import AlibabaDocumentAnalyzeClient
from app.knowledge_base.repositories import get_knowledge_base_db
from app.knowledge_base.sec_parse import SecFilingParseService
from app.knowledge_base.search import OpenSearchClient, combine_hybrid_hits
from app.main import create_app


def test_knowledge_base_db_initializes_default_chunk_profile(tmp_path) -> None:
    db = get_knowledge_base_db(tmp_path / "knowledge.db")

    profile = db.chunk_profiles.get("medium_overlap_v1")

    assert profile is not None
    assert profile["chunker_version"] == "v1"
    assert profile["target_size"] == 800
    assert profile["overlap_size"] == 120


def test_knowledge_base_repositories_round_trip(tmp_path) -> None:
    db = get_knowledge_base_db(tmp_path / "knowledge.db")
    db.sources.save(
        {
            "source_id": "wikipedia_zh_simp_20231101",
            "name": "wikipedia",
            "language": "zh",
            "dataset_version": "20231101_zh_simp",
            "file_path": "data/wikipedia/wikipedia_20231101_zh_simp.jsonl",
            "description": "Wikipedia zh dump",
        }
    )
    db.ingest_jobs.save(
        {
            "job_id": "job-1",
            "source_id": "wikipedia_zh_simp_20231101",
            "file_path": "data/wikipedia/wikipedia_20231101_zh_simp.jsonl",
            "limit_n": 10,
            "status": "running",
        }
    )
    db.documents.save(
        {
            "doc_id": "wiki:13",
            "source_id": "wikipedia_zh_simp_20231101",
            "external_id": "13",
            "title": "数学",
            "url": "https://zh.wikipedia.org/wiki/%E6%95%B0%E5%AD%A6",
            "text": "数学是研究数量、结构与变化的学科。",
            "text_hash": "hash-doc-1",
            "char_count": 18,
            "language": "zh",
            "metadata_json": {"source": "sample"},
            "ingest_job_id": "job-1",
        }
    )
    db.chunks.save(
        {
            "chunk_id": "wiki:13:chunk:0000",
            "doc_id": "wiki:13",
            "chunk_profile_id": "medium_overlap_v1",
            "chunk_index": 0,
            "chunker_version": "v1",
            "section_path": None,
            "raw_content": "数学是研究数量、结构与变化的学科。",
            "normalized_content": "数学是研究数量、结构与变化的学科。",
            "content_hash": "hash-chunk-1",
            "char_start": 0,
            "char_end": 18,
            "char_count": 18,
            "token_estimate": 18,
            "overlap_prev_chars": 0,
            "metadata_json": {"language": "zh"},
        }
    )

    source = db.sources.get("wikipedia_zh_simp_20231101")
    document = db.documents.get_by_source_external("wikipedia_zh_simp_20231101", "13")
    chunks = db.chunks.list_by_document("wiki:13", chunk_profile_id="medium_overlap_v1")
    job = db.ingest_jobs.get("job-1")

    assert source is not None
    assert source["language"] == "zh"
    assert document is not None
    assert document["title"] == "数学"
    assert document["metadata_json"] == '{"source": "sample"}'
    assert len(chunks) == 1
    assert chunks[0]["raw_content"] == "数学是研究数量、结构与变化的学科。"
    assert chunks[0]["normalized_content"] == "数学是研究数量、结构与变化的学科。"
    assert job is not None
    assert job["limit_n"] == 10


def test_knowledge_base_info_route_reports_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    from app.knowledge_base.api import get_knowledge_base_service

    get_knowledge_base_service.cache_clear()
    client = TestClient(create_app())

    response = client.get("/kb/info")

    assert response.status_code == 200
    body = response.json()
    assert body["default_language"] == "zh"
    assert body["default_chunk_profile"] == "medium_overlap_v1"
    assert body["active_chunk_profiles"][0]["chunk_profile_id"] == "medium_overlap_v1"

    get_knowledge_base_service.cache_clear()
    get_settings.cache_clear()


def test_chunk_text_creates_overlapping_chunks() -> None:
    text = (
        "第一段介绍数学是什么。第二段继续解释数学与结构的关系。"
        "第三段描述数学和变化。第四段说明数学在科学中的作用。"
        "第五段补充数学在工程和经济中的应用。"
    )

    chunks = chunk_text(
        text,
        target_size=24,
        soft_min_size=12,
        hard_max_size=30,
        overlap_size=6,
        language="zh",
    )

    assert len(chunks) >= 2
    assert chunks[1].overlap_prev_chars == 6
    assert chunks[0].char_count <= 30
    assert chunks[1].char_start < chunks[0].char_end


def test_ingest_wikipedia_creates_documents_and_chunks(tmp_path) -> None:
    sample_path = tmp_path / "sample.jsonl"
    records = [
        {
            "id": "13",
            "url": "https://zh.wikipedia.org/wiki/%E6%95%B0%E5%AD%A6",
            "title": "数学",
            "text": "数学是研究数量、结构与变化的学科。它在科学与工程中有广泛应用。",
        },
        {
            "id": "14",
            "url": "https://zh.wikipedia.org/wiki/%E7%89%A9%E7%90%86%E5%AD%A6",
            "title": "物理学",
            "text": "物理学研究物质、能量与相互作用。它依赖数学工具。",
        },
    ]
    sample_path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records),
        encoding="utf-8",
    )

    from app.knowledge_base.service import KnowledgeBaseService

    monkey_settings = get_settings()
    service = KnowledgeBaseService(monkey_settings.model_copy(update={"data_dir": tmp_path}))

    result = service.ingest_wikipedia(file_path=str(sample_path), limit_n=1)

    assert result.status == "succeeded"
    assert result.documents_seen == 1
    assert result.documents_inserted == 1
    assert result.documents_updated == 0
    assert result.documents_skipped == 0
    assert result.chunks_created >= 1

    documents = service.db.documents.list_by_source(result.source_id)
    assert len(documents) == 1
    assert documents[0]["title"] == "数学"
    chunks = service.db.chunks.list_by_document(documents[0]["doc_id"], chunk_profile_id="medium_overlap_v1")
    assert len(chunks) >= 1
    assert chunks[0]["normalized_content"]


def test_ingest_wikipedia_continues_existing_source(tmp_path) -> None:
    sample_path = tmp_path / "sample.jsonl"
    records = [
        {
            "id": str(index),
            "url": f"https://example.com/{index}",
            "title": f"title-{index}",
            "text": f"content-{index}",
        }
        for index in range(1, 4)
    ]
    sample_path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records),
        encoding="utf-8",
    )

    from app.knowledge_base.service import KnowledgeBaseService

    service = KnowledgeBaseService(get_settings().model_copy(update={"data_dir": tmp_path}))

    first = service.ingest_wikipedia(
        file_path=str(sample_path),
        source_id="wikipedia_resume_test",
        limit_n=2,
    )
    resumed = service.ingest_wikipedia(
        file_path=str(sample_path),
        source_id="wikipedia_resume_test",
        limit_n=3,
    )

    documents = service.db.documents.list_by_source("wikipedia_resume_test")

    assert first.documents_inserted == 2
    assert resumed.documents_seen == 3
    assert resumed.documents_skipped == 2
    assert resumed.documents_inserted == 1
    assert len(documents) == 3


def test_knowledge_base_ingest_route_runs_end_to_end(tmp_path, monkeypatch) -> None:
    sample_path = tmp_path / "sample.jsonl"
    sample_path.write_text(
        json.dumps(
            {
                "id": "13",
                "url": "https://zh.wikipedia.org/wiki/%E6%95%B0%E5%AD%A6",
                "title": "数学",
                "text": "数学是研究数量、结构与变化的学科。它在科学与工程中有广泛应用。",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    from app.knowledge_base.api import get_knowledge_base_service

    get_knowledge_base_service.cache_clear()
    client = TestClient(create_app())

    response = client.post(
        "/kb/ingest",
        json={
            "file_path": str(sample_path),
            "limit_n": 1,
            "language": "zh",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["documents_seen"] == 1
    assert body["documents_inserted"] == 1
    assert body["chunks_created"] >= 1

    get_knowledge_base_service.cache_clear()
    get_settings.cache_clear()


def test_index_service_embeds_and_indexes_chunks(tmp_path) -> None:
    db = get_knowledge_base_db(tmp_path / "knowledge.db")
    db.sources.save(
        {
            "source_id": "wikipedia_zh_sample",
            "name": "wikipedia",
            "language": "zh",
            "dataset_version": "sample",
            "file_path": str(tmp_path / "sample.jsonl"),
            "description": "sample",
        }
    )
    db.documents.save(
        {
            "doc_id": "wikipedia_zh_sample:13",
            "source_id": "wikipedia_zh_sample",
            "external_id": "13",
            "title": "数学",
            "url": "https://zh.wikipedia.org/wiki/%E6%95%B0%E5%AD%A6",
            "text": "数学是研究数量、结构与变化的学科。",
            "text_hash": "doc-hash",
            "char_count": 18,
            "language": "zh",
            "metadata_json": None,
            "ingest_job_id": "job-1",
        }
    )
    db.chunks.save(
        {
            "chunk_id": "wikipedia_zh_sample:13:chunk:0000",
            "doc_id": "wikipedia_zh_sample:13",
            "chunk_profile_id": "medium_overlap_v1",
            "chunk_index": 0,
            "chunker_version": "v1",
            "section_path": None,
            "raw_content": "数学是研究数量、结构与变化的学科。",
            "normalized_content": "数学是研究数量、结构与变化的学科。",
            "content_hash": "chunk-hash",
            "char_start": 0,
            "char_end": 18,
            "char_count": 18,
            "token_estimate": 18,
            "overlap_prev_chars": 0,
            "metadata_json": None,
        }
    )

    embedded_requests: list[dict] = []
    indexed_requests: list[str] = []

    def embedding_handler(request: Request) -> Response:
        embedded_requests.append(json.loads(request.content.decode("utf-8")))
        return Response(
            200,
            json={
                "model": "text-embedding-v4",
                "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}],
            },
        )

    def search_handler(request: Request) -> Response:
        indexed_requests.append(request.url.path)
        if request.method == "PUT":
            return Response(200, json={"acknowledged": True})
        if request.url.path.endswith("/_bulk"):
            return Response(200, json={"errors": False, "items": []})
        if request.url.path.endswith("/_refresh"):
            return Response(200, json={"_shards": {"total": 1, "successful": 1, "failed": 0}})
        raise AssertionError(request.url.path)

    embedding_client = DashScopeEmbeddingClient(
        api_key="test",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="text-embedding-v4",
        http_client=MockHttpxClient(embedding_handler),
    )
    opensearch_client = OpenSearchClient(
        base_url="http://127.0.0.1:9200",
        index_prefix="kb_wikipedia",
        http_client=MockHttpxClient(search_handler),
    )
    service = KnowledgeBaseIndexService(
        db=db,
        embedding_client=embedding_client,
        opensearch_client=opensearch_client,
    )

    result = service.index_source(
        source_id="wikipedia_zh_sample",
        chunk_profile_id="medium_overlap_v1",
    )

    saved_embedding = db.chunk_embeddings.get("wikipedia_zh_sample:13:chunk:0000")
    assert result.indexed_chunks == 1
    assert result.embedded_chunks == 1
    assert saved_embedding is not None
    assert saved_embedding["embedding_model"] == "text-embedding-v4"
    assert embedded_requests[0]["input"] == ["数学是研究数量、结构与变化的学科。"]
    assert any(path.endswith("/_bulk") for path in indexed_requests)


def test_index_service_reuses_existing_chunk_embeddings(tmp_path) -> None:
    db = get_knowledge_base_db(tmp_path / "knowledge.db")
    db.sources.save(
        {
            "source_id": "wikipedia_zh_sample",
            "name": "wikipedia",
            "language": "zh",
            "dataset_version": "sample",
            "file_path": str(tmp_path / "sample.jsonl"),
            "description": "sample",
        }
    )
    db.documents.save(
        {
            "doc_id": "wikipedia_zh_sample:13",
            "source_id": "wikipedia_zh_sample",
            "external_id": "13",
            "title": "数学",
            "url": "https://zh.wikipedia.org/wiki/%E6%95%B0%E5%AD%A6",
            "text": "数学是研究数量、结构与变化的学科。",
            "text_hash": "doc-hash",
            "char_count": 18,
            "language": "zh",
            "metadata_json": None,
            "ingest_job_id": "job-1",
        }
    )
    db.chunks.save(
        {
            "chunk_id": "wikipedia_zh_sample:13:chunk:0000",
            "doc_id": "wikipedia_zh_sample:13",
            "chunk_profile_id": "medium_overlap_v1",
            "chunk_index": 0,
            "chunker_version": "v1",
            "section_path": None,
            "raw_content": "数学是研究数量、结构与变化的学科。",
            "normalized_content": "数学是研究数量、结构与变化的学科。",
            "content_hash": "chunk-hash",
            "char_start": 0,
            "char_end": 18,
            "char_count": 18,
            "token_estimate": 18,
            "overlap_prev_chars": 0,
            "metadata_json": None,
        }
    )
    db.chunk_embeddings.save(
        {
            "chunk_id": "wikipedia_zh_sample:13:chunk:0000",
            "embedding_model": "text-embedding-v4",
            "embedding_dim": 3,
            "embedding_json": [0.1, 0.2, 0.3],
            "text_hash": "chunk-hash",
        }
    )

    def embedding_handler(request: Request) -> Response:
        raise AssertionError("existing embedding should be reused")

    indexed_payloads: list[str] = []

    def search_handler(request: Request) -> Response:
        if request.method == "PUT":
            return Response(200, json={"acknowledged": True})
        if request.url.path.endswith("/_bulk"):
            indexed_payloads.append(request.content.decode("utf-8"))
            return Response(200, json={"errors": False, "items": []})
        if request.url.path.endswith("/_refresh"):
            return Response(200, json={"_shards": {"total": 1, "successful": 1, "failed": 0}})
        raise AssertionError(request.url.path)

    service = KnowledgeBaseIndexService(
        db=db,
        embedding_client=DashScopeEmbeddingClient(
            api_key="test",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model="text-embedding-v4",
            http_client=MockHttpxClient(embedding_handler),
        ),
        opensearch_client=OpenSearchClient(
            base_url="http://127.0.0.1:9200",
            index_prefix="kb_wikipedia",
            http_client=MockHttpxClient(search_handler),
        ),
    )

    result = service.index_source(
        source_id="wikipedia_zh_sample",
        chunk_profile_id="medium_overlap_v1",
    )

    assert result.indexed_chunks == 1
    assert result.embedded_chunks == 1
    assert '"embedding": [0.1, 0.2, 0.3]' in indexed_payloads[0]


def test_hybrid_search_combines_bm25_and_vector_scores() -> None:
    bm25_hits = [
        type("Hit", (), {"chunk_id": "c1", "doc_id": "d1", "score": 10.0, "source": {"chunk_id": "c1", "doc_id": "d1"}})(),
        type("Hit", (), {"chunk_id": "c2", "doc_id": "d2", "score": 5.0, "source": {"chunk_id": "c2", "doc_id": "d2"}})(),
    ]
    vector_hits = [
        type("Hit", (), {"chunk_id": "c2", "doc_id": "d2", "score": 8.0, "source": {"chunk_id": "c2", "doc_id": "d2"}})(),
        type("Hit", (), {"chunk_id": "c3", "doc_id": "d3", "score": 4.0, "source": {"chunk_id": "c3", "doc_id": "d3"}})(),
    ]

    hits = combine_hybrid_hits(
        bm25_hits=bm25_hits,
        vector_hits=vector_hits,
        top_k=3,
    )

    assert [hit.chunk_id for hit in hits] == ["c2", "c1", "c3"]


def test_opensearch_client_reuses_owned_http_client() -> None:
    client = OpenSearchClient(
        base_url="http://127.0.0.1:9200",
        index_prefix="kb_wikipedia",
    )

    assert client._client is client._client


def test_knowledge_base_service_caches_opensearch_client(tmp_path) -> None:
    from app.knowledge_base.service import KnowledgeBaseService

    service = KnowledgeBaseService(get_settings().model_copy(update={"data_dir": tmp_path}))

    assert service._opensearch_client() is service._opensearch_client()


def test_search_route_returns_hits(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_DASHSCOPE_API_KEY", "test-key")
    get_settings.cache_clear()
    from app.knowledge_base.api import get_knowledge_base_service

    class FakeService:
        def health_check(self):
            return {"status": "ok", "db_path": str(tmp_path / "knowledge.db")}

        def get_info(self):
            return type(
                "Info",
                (),
                {
                    "db_path": str(tmp_path / "knowledge.db"),
                    "default_language": "zh",
                    "default_chunk_profile": "medium_overlap_v1",
                    "active_chunk_profiles": [{"chunk_profile_id": "medium_overlap_v1"}],
                },
            )()

        def search(self, **kwargs):
            return [
                type(
                    "Hit",
                    (),
                    {
                        "chunk_id": "c1",
                        "doc_id": "d1",
                        "score": 1.0,
                        "source": {"title": "数学"},
                    },
                )()
            ]

    get_knowledge_base_service.cache_clear()
    app = create_app()
    app.dependency_overrides = {}
    import app.knowledge_base.api as kb_api

    original = kb_api.get_knowledge_base_service
    kb_api.get_knowledge_base_service = lambda: FakeService()
    try:
        client = TestClient(app)
        response = client.post("/kb/search", json={"query": "数学是什么", "mode": "bm25"})
        assert response.status_code == 200
        assert response.json()["hits"][0]["chunk_id"] == "c1"
    finally:
        kb_api.get_knowledge_base_service = original
        get_settings.cache_clear()


def test_eval_dataset_generation_and_run_with_heuristic(tmp_path) -> None:
    db = get_knowledge_base_db(tmp_path / "knowledge.db")
    db.sources.save(
        {
            "source_id": "src1",
            "name": "wikipedia",
            "language": "zh",
            "dataset_version": "sample",
            "file_path": "sample.jsonl",
            "description": "sample",
        }
    )
    db.documents.save(
        {
            "doc_id": "src1:13",
            "source_id": "src1",
            "external_id": "13",
            "title": "数学",
            "url": "https://example.com/math",
            "text": "数学是研究数量、结构与变化的学科。",
            "text_hash": "doc-hash",
            "char_count": 18,
            "language": "zh",
            "metadata_json": None,
            "ingest_job_id": "job-1",
        }
    )
    db.chunks.save(
        {
            "chunk_id": "src1:13:chunk:0000",
            "doc_id": "src1:13",
            "chunk_profile_id": "medium_overlap_v1",
            "chunk_index": 0,
            "chunker_version": "v1",
            "section_path": None,
            "raw_content": "数学是研究数量、结构与变化的学科。",
            "normalized_content": "数学是研究数量、结构与变化的学科。",
            "content_hash": "chunk-hash",
            "char_start": 0,
            "char_end": 18,
            "char_count": 18,
            "token_estimate": 18,
            "overlap_prev_chars": 0,
            "metadata_json": None,
        }
    )

    settings = get_settings().model_copy(update={"data_dir": tmp_path})
    service = KnowledgeBaseEvaluationService(settings=settings, db=db, kb_service=object())

    class FakeOpenSearchClient:
        def index_name(self, *, language: str, chunk_profile_id: str) -> str:
            return f"kb_wikipedia_{language}_{chunk_profile_id}"

        def bm25_search(self, *, index_name: str, query: str, top_k: int):
            return [
                type(
                    "Hit",
                    (),
                    {
                        "chunk_id": "src1:13:chunk:0000",
                        "doc_id": "src1:13",
                        "score": 1.0,
                        "source": {"title": "数学"},
                    },
                )()
            ]

    service._opensearch_client_instance = FakeOpenSearchClient()
    dataset = service.generate_dataset(
        source_id="src1",
        chunk_profile_id="medium_overlap_v1",
        generation_mode="heuristic",
        max_documents=1,
        chunks_per_document=1,
    )
    assert dataset.generated_queries == 1

    summary = service.run_evaluation(
        dataset_id=dataset.dataset_id,
        retrieval_mode="bm25",
        top_k=3,
        chunk_profile_id="medium_overlap_v1",
        language="zh",
    )
    assert summary.query_count == 1
    assert summary.recall_at_k == 1.0
    assert summary.mrr == 1.0
    assert summary.chunk_hit_rate == 1.0


def test_eval_dataset_generation_persists_multiple_queries(tmp_path) -> None:
    db = get_knowledge_base_db(tmp_path / "knowledge.db")
    db.sources.save(
        {
            "source_id": "src1",
            "name": "wikipedia",
            "language": "zh",
            "dataset_version": "sample",
            "file_path": "sample.jsonl",
            "description": "sample",
        }
    )
    for index in range(2):
        doc_id = f"src1:{index}"
        chunk_id = f"{doc_id}:chunk:0000"
        db.documents.save(
            {
                "doc_id": doc_id,
                "source_id": "src1",
                "external_id": str(index),
                "title": f"title-{index}",
                "url": f"https://example.com/{index}",
                "text": f"content-{index}",
                "text_hash": f"doc-hash-{index}",
                "char_count": 9,
                "language": "zh",
                "metadata_json": None,
                "ingest_job_id": "job-1",
            }
        )
        db.chunks.save(
            {
                "chunk_id": chunk_id,
                "doc_id": doc_id,
                "chunk_profile_id": "medium_overlap_v1",
                "chunk_index": 0,
                "chunker_version": "v1",
                "section_path": None,
                "raw_content": f"chunk-{index}",
                "normalized_content": f"chunk-{index}",
                "content_hash": f"chunk-hash-{index}",
                "char_start": 0,
                "char_end": 7,
                "char_count": 7,
                "token_estimate": 7,
                "overlap_prev_chars": 0,
                "metadata_json": None,
            }
        )

    service = KnowledgeBaseEvaluationService(
        settings=get_settings().model_copy(update={"data_dir": tmp_path}),
        db=db,
        kb_service=object(),
    )

    class FakeGenerator:
        def generate(self, *, document, chunk, mode):
            return GeneratedQuery(
                query_text=f"q:{document['title']}:{chunk['chunk_id']}",
                query_type="fact",
                difficulty="easy",
                gold_answer=document["title"],
                generated_by=f"fake:{mode}",
            )

    service._generator = FakeGenerator()

    dataset = service.generate_dataset(
        source_id="src1",
        chunk_profile_id="medium_overlap_v1",
        generation_mode="llm",
        max_documents=2,
        chunks_per_document=1,
    )

    queries = db.eval_queries.list_by_dataset(dataset.dataset_id)

    assert dataset.generated_queries == 2
    assert len(queries) == 2
    assert {query["generated_by"] for query in queries} == {"fake:llm"}


def test_alibaba_document_analyze_client_creates_async_task_from_local_pdf(tmp_path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 sample")
    captured_requests: list[dict] = []

    def handler(request: Request) -> Response:
        captured_requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "json": json.loads(request.content.decode("utf-8")),
            }
        )
        return Response(
            200,
            json={
                "request_id": "req-1",
                "latency": 5,
                "result": {"task_id": "task-123"},
            },
        )

    client = AlibabaDocumentAnalyzeClient(
        api_key="OS-test",
        endpoint="https://example.opensearch.aliyuncs.com",
        workspace="default",
        service_id="ops-document-analyze-002",
        http_client=MockHttpxClient(handler),
    )

    task = client.create_async_task_from_file(pdf_path)

    assert task.task_id == "task-123"
    assert captured_requests[0]["method"] == "POST"
    assert captured_requests[0]["url"].endswith(
        "/v3/openapi/workspaces/default/document-analyze/ops-document-analyze-002/async"
    )
    assert captured_requests[0]["headers"]["authorization"] == "Bearer OS-test"
    assert captured_requests[0]["json"]["service_id"] == "ops-document-analyze-002"
    assert captured_requests[0]["json"]["document"]["file_name"] == "sample.pdf"
    assert captured_requests[0]["json"]["document"]["file_type"] == "pdf"
    assert captured_requests[0]["json"]["strategy"]["enable_semantic"] is True
    assert captured_requests[0]["json"]["document"]["content"]
    assert task.raw_response["result"]["task_id"] == "task-123"


def test_alibaba_document_analyze_client_reads_async_task_status() -> None:
    captured_requests: list[dict] = []

    def handler(request: Request) -> Response:
        captured_requests.append(
            {
                "method": request.method,
                "url": str(request.url),
            }
        )
        return Response(
            200,
            json={
                "request_id": "req-2",
                "latency": 9,
                "result": {
                    "task_id": "task-456",
                    "status": "SUCCESS",
                    "data": {
                        "content": "# Title\n\nBody",
                        "content_type": "markdown",
                        "page_num": 15,
                    },
                },
                "usage": {
                    "token_count": 100,
                    "table_count": 2,
                    "image_count": 1,
                },
            },
        )

    client = AlibabaDocumentAnalyzeClient(
        api_key="OS-test",
        endpoint="https://example.opensearch.aliyuncs.com",
        service_id="ops-document-analyze-002",
        http_client=MockHttpxClient(handler),
    )

    result = client.get_async_task("task-456")

    assert result.task_id == "task-456"
    assert result.status == "SUCCESS"
    assert result.content_type == "markdown"
    assert result.page_num == 15
    assert result.usage["table_count"] == 2
    assert "task_id=task-456" in captured_requests[0]["url"]
    assert result.raw_response["result"]["status"] == "SUCCESS"


def test_alibaba_document_analyze_client_requires_exactly_one_document_input() -> None:
    client = AlibabaDocumentAnalyzeClient(
        api_key="OS-test",
        endpoint="https://example.opensearch.aliyuncs.com",
    )

    try:
        client.create_async_task(document_url="https://example.com/a.pdf", file_content_base64="abc")
    except ValueError as exc:
        assert "Exactly one" in str(exc)
    else:
        raise AssertionError("expected create_async_task to reject multiple document inputs")


def test_alibaba_document_analyze_client_requires_file_name_for_base64_upload() -> None:
    client = AlibabaDocumentAnalyzeClient(
        api_key="OS-test",
        endpoint="https://example.opensearch.aliyuncs.com",
    )

    try:
        client.create_async_task(file_content_base64="abc")
    except ValueError as exc:
        assert "file_name is required" in str(exc)
    else:
        raise AssertionError("expected create_async_task to require file_name for base64 upload")


def test_alibaba_document_analyze_client_rejects_oversized_base64_payload() -> None:
    client = AlibabaDocumentAnalyzeClient(
        api_key="OS-test",
        endpoint="https://example.opensearch.aliyuncs.com",
    )

    try:
        client._validate_request_size("a" * (8 * 1024 * 1024))
    except ValueError as exc:
        assert "8MB" in str(exc)
    else:
        raise AssertionError("expected oversized base64 payload to be rejected")


def test_sec_filing_parse_service_writes_raw_parse_json(tmp_path) -> None:
    input_dir = tmp_path / "sec-pdf"
    output_dir = tmp_path / "sec-raw"
    input_dir.mkdir()
    pdf_path = input_dir / "3M_2023Q2_10Q.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 sample")

    class FakeClient:
        def create_async_task_from_file(self, file_path):
            return type(
                "Task",
                (),
                {
                    "task_id": "task-1",
                    "request_id": "req-1",
                    "latency_ms": 1,
                    "raw_response": {"result": {"task_id": "task-1"}},
                },
            )()

        def get_async_task(self, task_id):
            return type(
                "Result",
                (),
                {
                    "task_id": task_id,
                    "status": "SUCCESS",
                    "content": "# Filing\n\nBody",
                    "content_type": "markdown",
                    "page_num": 9,
                    "error": None,
                    "usage": {"token_count": 123},
                    "request_id": "req-2",
                    "latency_ms": 2,
                    "raw_response": {
                        "result": {
                            "task_id": task_id,
                            "status": "SUCCESS",
                            "data": {"content_type": "markdown", "page_num": 9},
                        }
                    },
                },
            )()

    service = SecFilingParseService(
        client=FakeClient(),
        input_dir=input_dir,
        output_dir=output_dir,
    )

    result = service.parse_directory()

    assert result.files_total == 1
    assert result.parsed == 1
    assert result.failed == 0
    output_path = output_dir / "3M_2023Q2_10Q.aliyun.json"
    assert output_path.exists()
    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved["task_id"] == "task-1"
    assert saved["final_task_response"]["result"]["status"] == "SUCCESS"


def test_sec_parse_route_returns_batch_summary(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    from app.knowledge_base.api import get_knowledge_base_service

    class FakeService:
        def parse_sec_pdfs(self, **kwargs):
            return type(
                "Result",
                (),
                {
                    "input_dir": str(tmp_path / "sec-pdf"),
                    "output_dir": str(tmp_path / "sec-pdf" / "aliyun-raw"),
                    "files_total": 1,
                    "parsed": 1,
                    "skipped": 0,
                    "failed": 0,
                    "items": [
                        type(
                            "Item",
                            (),
                            {
                                "source_file": str(tmp_path / "sec-pdf" / "3M_2023Q2_10Q.pdf"),
                                "output_file": str(tmp_path / "sec-pdf" / "aliyun-raw" / "3M_2023Q2_10Q.aliyun.json"),
                                "task_id": "task-1",
                                "status": "SUCCESS",
                                "page_num": 9,
                                "skipped": False,
                            },
                        )()
                    ],
                },
            )()

    get_knowledge_base_service.cache_clear()
    app = create_app()
    import app.knowledge_base.api as kb_api

    original = kb_api.get_knowledge_base_service
    kb_api.get_knowledge_base_service = lambda: FakeService()
    try:
        client = TestClient(app)
        response = client.post("/kb/sec/parse", json={"limit": 1})
        assert response.status_code == 200
        body = response.json()
        assert body["parsed"] == 1
        assert body["items"][0]["status"] == "SUCCESS"
    finally:
        kb_api.get_knowledge_base_service = original
        get_settings.cache_clear()


class MockHttpxClient:
    def __init__(self, handler):
        import httpx

        self._client = httpx.Client(transport=httpx.MockTransport(handler))

    def post(self, *args, **kwargs):
        return self._client.post(*args, **kwargs)

    def get(self, *args, **kwargs):
        return self._client.get(*args, **kwargs)

    def put(self, *args, **kwargs):
        return self._client.put(*args, **kwargs)
