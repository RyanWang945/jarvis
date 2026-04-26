import json

from fastapi.testclient import TestClient
from httpx import Request, Response

from app.config import get_settings
from app.knowledge_base.chunking import chunk_text
from app.knowledge_base.embedding import DashScopeEmbeddingClient
from app.knowledge_base.eval import KnowledgeBaseEvaluationService
from app.knowledge_base.indexing import KnowledgeBaseIndexService
from app.knowledge_base.repositories import get_knowledge_base_db
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

    class FakeKBService:
        def search(self, **kwargs):
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

    settings = get_settings().model_copy(update={"data_dir": tmp_path})
    service = KnowledgeBaseEvaluationService(settings=settings, db=db, kb_service=FakeKBService())
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


class MockHttpxClient:
    def __init__(self, handler):
        import httpx

        self._client = httpx.Client(transport=httpx.MockTransport(handler))

    def post(self, *args, **kwargs):
        return self._client.post(*args, **kwargs)

    def put(self, *args, **kwargs):
        return self._client.put(*args, **kwargs)
