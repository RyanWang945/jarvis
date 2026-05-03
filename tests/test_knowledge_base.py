import json
import math

from fastapi.testclient import TestClient
from httpx import Request, Response

from app.config import get_settings
from app.knowledge_base.chunking import chunk_text
from app.knowledge_base.embedding import DashScopeEmbeddingClient
from app.knowledge_base.eval import GeneratedQuery, KnowledgeBaseEvaluationService
from app.knowledge_base.indexing import KnowledgeBaseIndexService
from app.knowledge_base.parsers.alibaba_pdf import AlibabaDocumentAnalyzeClient
from app.knowledge_base.repositories import get_knowledge_base_db
from app.knowledge_base.sec_chunking import chunk_sec_blocks
from app.knowledge_base.sec_blocks import normalize_aliyun_markdown
from app.knowledge_base.sec_parse import SecFilingParseService
from app.knowledge_base.search import OpenSearchClient, combine_hybrid_hits, combine_rrf_hits
from app.main import create_app


def test_knowledge_base_db_initializes_default_chunk_profile(tmp_path) -> None:
    db = get_knowledge_base_db(tmp_path / "knowledge.db")

    profile = db.chunk_profiles.get("medium_overlap_v1")
    sec_profile = db.chunk_profiles.get("sec_filing_medium_v1")
    journal_mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
    busy_timeout = db.conn.execute("PRAGMA busy_timeout").fetchone()[0]

    assert profile is not None
    assert profile["chunker_version"] == "v1"
    assert profile["target_size"] == 800
    assert profile["overlap_size"] == 120
    assert sec_profile is not None
    assert sec_profile["chunker_version"] == "sec_v1"
    assert sec_profile["target_size"] == 1600
    assert sec_profile["overlap_size"] == 200
    assert str(journal_mode).lower() == "delete"
    assert int(busy_timeout) == 5000


def test_knowledge_base_repositories_round_trip(tmp_path) -> None:
    db = get_knowledge_base_db(tmp_path / "knowledge.db")
    db.sources.save(
        {
            "source_id": "wikipedia_zh_simp_20231101",
            "name": "wikipedia",
            "source_type": "wikipedia",
            "language": "zh",
            "dataset_version": "20231101_zh_simp",
            "file_path": "data/wikipedia/wikipedia_20231101_zh_simp.jsonl",
            "description": "Wikipedia zh dump",
            "owner": "jarvis",
            "region": "global",
            "metadata_json": {"domain": "encyclopedia"},
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
    assert source["source_type"] == "wikipedia"
    assert source["owner"] == "jarvis"
    assert source["metadata_json"] == '{"domain": "encyclopedia"}'
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


def test_knowledge_base_run_tracking_repositories_round_trip(tmp_path) -> None:
    db = get_knowledge_base_db(tmp_path / "knowledge.db")

    db.parse_artifacts.save(
        {
            "artifact_id": "artifact-1",
            "filing_id": "filing-1",
            "artifact_type": "aliyun_raw_json",
            "parser_vendor": "aliyun",
            "parser_model": "ops-document-analyze-002",
            "parser_version": "2026-04-28",
            "parse_config_json": {"enable_semantic": True},
            "input_sha256": "sha256-1",
            "raw_output_path": "data/sec-pdf/aliyun-raw/sample.json",
            "normalized_output_path": "data/sec-pdf/normalized/sample.blocks.json",
            "status": "succeeded",
        }
    )
    db.chunk_runs.save(
        {
            "chunk_run_id": "chunk-run-1",
            "filing_id": "filing-1",
            "parse_artifact_id": "artifact-1",
            "chunk_profile_id": "sec_filing_medium_v1",
            "chunker_version": "sec_v1",
            "config_json": {"preserve_table_context": True},
            "chunk_count": 12,
            "status": "succeeded",
        }
    )
    db.index_runs.save(
        {
            "index_run_id": "index-run-1",
            "source_id": "sec_demo",
            "chunk_run_id": "chunk-run-1",
            "index_name": "kb_sec_en_sec_filing_medium_v1",
            "embedding_model": "text-embedding-v4",
            "embedding_dim": 1024,
            "opensearch_mapping_version": "sec_v1",
            "status": "succeeded",
        }
    )

    artifact = db.parse_artifacts.get("artifact-1")
    chunk_run = db.chunk_runs.get("chunk-run-1")
    index_run = db.index_runs.get("index-run-1")

    assert artifact is not None
    assert artifact["parser_vendor"] == "aliyun"
    assert artifact["parse_config_json"] == '{"enable_semantic": true}'
    assert chunk_run is not None
    assert chunk_run["chunk_count"] == 12
    assert chunk_run["config_json"] == '{"preserve_table_context": true}'
    assert index_run is not None
    assert index_run["embedding_dim"] == 1024
    assert index_run["opensearch_mapping_version"] == "sec_v1"


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
            "source_type": "wikipedia",
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
    created_index_mapping: dict | None = None

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
        nonlocal created_index_mapping
        indexed_requests.append(request.url.path)
        if request.method == "PUT":
            created_index_mapping = json.loads(request.content.decode("utf-8"))
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
    properties = created_index_mapping["mappings"]["properties"]
    assert properties["title"]["analyzer"] == "smartcn"
    assert properties["content"]["analyzer"] == "smartcn"


def test_index_service_reuses_existing_chunk_embeddings(tmp_path) -> None:
    db = get_knowledge_base_db(tmp_path / "knowledge.db")
    db.sources.save(
        {
            "source_id": "wikipedia_zh_sample",
            "name": "wikipedia",
            "source_type": "wikipedia",
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
    assert '"source_type": "wikipedia"' in indexed_payloads[0]


def test_index_service_indexes_only_documents_from_ingest_job(tmp_path) -> None:
    db = get_knowledge_base_db(tmp_path / "knowledge.db")
    db.sources.save(
        {
            "source_id": "wikipedia_zh_sample",
            "name": "wikipedia",
            "source_type": "wikipedia",
            "language": "zh",
            "dataset_version": "sample",
            "file_path": str(tmp_path / "sample.jsonl"),
            "description": "sample",
        }
    )
    for external_id, ingest_job_id in (("13", "job-new"), ("18", "job-old")):
        doc_id = f"wikipedia_zh_sample:{external_id}"
        db.documents.save(
            {
                "doc_id": doc_id,
                "source_id": "wikipedia_zh_sample",
                "external_id": external_id,
                "title": f"title-{external_id}",
                "url": f"https://example.com/{external_id}",
                "text": f"content-{external_id}",
                "text_hash": f"doc-hash-{external_id}",
                "char_count": 10,
                "language": "zh",
                "metadata_json": None,
                "ingest_job_id": ingest_job_id,
            }
        )
        db.chunks.save(
            {
                "chunk_id": f"{doc_id}:chunk:0000",
                "doc_id": doc_id,
                "chunk_profile_id": "medium_overlap_v1",
                "chunk_index": 0,
                "chunker_version": "v1",
                "section_path": None,
                "raw_content": f"chunk-{external_id}",
                "normalized_content": f"chunk-{external_id}",
                "content_hash": f"chunk-hash-{external_id}",
                "char_start": 0,
                "char_end": 8,
                "char_count": 8,
                "token_estimate": 8,
                "overlap_prev_chars": 0,
                "metadata_json": None,
            }
        )

    embedded_requests: list[dict] = []
    indexed_payloads: list[str] = []

    def embedding_handler(request: Request) -> Response:
        payload = json.loads(request.content.decode("utf-8"))
        embedded_requests.append(payload)
        return Response(
            200,
            json={
                "model": "text-embedding-v4",
                "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}],
            },
        )

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

    result = service.index_ingest_job(
        ingest_job_id="job-new",
        source_id="wikipedia_zh_sample",
        chunk_profile_id="medium_overlap_v1",
    )

    assert result.indexed_chunks == 1
    assert embedded_requests[0]["input"] == ["chunk-13"]
    assert "wikipedia_zh_sample:13:chunk:0000" in indexed_payloads[0]
    assert "wikipedia_zh_sample:18:chunk:0000" not in indexed_payloads[0]


def test_index_service_uses_dedicated_sec_index_and_records_index_run(tmp_path) -> None:
    db = get_knowledge_base_db(tmp_path / "knowledge.db")
    db.sources.save(
        {
            "source_id": "sec_filing_local",
            "name": "sec_filings",
            "source_type": "sec_filing",
            "language": "en",
            "dataset_version": "local_v1",
            "file_path": str(tmp_path / "sec-pdf" / "aliyun-raw"),
            "description": "sample",
        }
    )
    db.documents.save(
        {
            "doc_id": "sec_filing_local:3M_2023Q2_10Q",
            "source_id": "sec_filing_local",
            "external_id": "3M_2023Q2_10Q",
            "title": "3M",
            "url": str(tmp_path / "sec-pdf" / "3M_2023Q2_10Q.pdf"),
            "text": "Item 1. Business\n\nRevenue grew.",
            "text_hash": "doc-hash",
            "char_count": 31,
            "language": "en",
            "metadata_json": {
                "company_name": "3M",
                "ticker": "MMM",
                "form_type": "10-Q",
                "fiscal_year": 2023,
                "fiscal_period": "Q2",
            },
            "ingest_job_id": "job-1",
        }
    )
    db.chunks.save(
        {
            "chunk_id": "sec_filing_local:3M_2023Q2_10Q:chunk:0000",
            "doc_id": "sec_filing_local:3M_2023Q2_10Q",
            "chunk_profile_id": "sec_filing_medium_v1",
            "chunk_index": 0,
            "chunker_version": "sec_v1",
            "section_path": "Item 1. Business",
            "raw_content": "Item 1. Business\n\nRevenue grew.",
            "normalized_content": "Item 1. Business\n\nRevenue grew.",
            "content_hash": "chunk-hash",
            "char_start": 0,
            "char_end": 31,
            "char_count": 31,
            "token_estimate": 31,
            "overlap_prev_chars": 0,
            "metadata_json": {
                "section_title": "Item 1. Business",
                "is_table_chunk": False,
            },
        }
    )
    db.chunk_runs.save(
        {
            "chunk_run_id": "sec_filing_local:3M_2023Q2_10Q:chunk-run:sec_filing_medium_v1",
            "filing_id": "sec_filing_local:3M_2023Q2_10Q",
            "parse_artifact_id": "sec_filing_local:3M_2023Q2_10Q:artifact:parse:v1",
            "chunk_profile_id": "sec_filing_medium_v1",
            "chunker_version": "sec_v1",
            "config_json": {"target_size": 1600},
            "chunk_count": 1,
            "status": "succeeded",
        }
    )

    indexed_requests: list[str] = []

    def embedding_handler(request: Request) -> Response:
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
        source_id="sec_filing_local",
        chunk_profile_id="sec_filing_medium_v1",
    )

    index_run = db.index_runs.get("sec_filing_local:3M_2023Q2_10Q:index-run:sec_filing_medium_v1")

    assert result.index_name == "kb_sec_en_sec_filing_medium_v1"
    assert any("/kb_sec_en_sec_filing_medium_v1" in path for path in indexed_requests)
    assert index_run is not None
    assert index_run["embedding_model"] == "text-embedding-v4"


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


def test_rrf_search_combines_bm25_and_vector_ranks() -> None:
    bm25_hits = [
        type("Hit", (), {"chunk_id": "c1", "doc_id": "d1", "score": 10.0, "source": {"chunk_id": "c1", "doc_id": "d1"}})(),
        type("Hit", (), {"chunk_id": "c2", "doc_id": "d2", "score": 5.0, "source": {"chunk_id": "c2", "doc_id": "d2"}})(),
    ]
    vector_hits = [
        type("Hit", (), {"chunk_id": "c2", "doc_id": "d2", "score": 8.0, "source": {"chunk_id": "c2", "doc_id": "d2"}})(),
        type("Hit", (), {"chunk_id": "c3", "doc_id": "d3", "score": 4.0, "source": {"chunk_id": "c3", "doc_id": "d3"}})(),
    ]

    hits = combine_rrf_hits(
        bm25_hits=bm25_hits,
        vector_hits=vector_hits,
        top_k=3,
    )

    assert [hit.chunk_id for hit in hits] == ["c2", "c1", "c3"]


def test_eval_run_persists_rrf_v2_params(tmp_path) -> None:
    db = get_knowledge_base_db(tmp_path / "knowledge.db")
    db.sources.save(
        {
            "source_id": "src1",
            "name": "wikipedia",
            "source_type": "wikipedia",
            "language": "zh",
            "dataset_version": "sample",
            "file_path": "sample.jsonl",
            "description": "sample",
        }
    )
    db.documents.save(
        {
            "doc_id": "src1:1",
            "source_id": "src1",
            "external_id": "1",
            "title": "title-1",
            "url": "https://example.com/1",
            "text": "content-1",
            "text_hash": "doc-hash-1",
            "char_count": 9,
            "language": "zh",
            "metadata_json": None,
            "ingest_job_id": "job-1",
        }
    )
    db.chunks.save(
        {
            "chunk_id": "src1:1:chunk:0000",
            "doc_id": "src1:1",
            "chunk_profile_id": "medium_overlap_v1",
            "chunk_index": 0,
            "chunker_version": "v1",
            "section_path": None,
            "raw_content": "chunk-1",
            "normalized_content": "chunk-1",
            "content_hash": "chunk-hash-1",
            "char_start": 0,
            "char_end": 7,
            "char_count": 7,
            "token_estimate": 7,
            "overlap_prev_chars": 0,
            "metadata_json": None,
        }
    )
    db.eval_datasets.save(
        {
            "dataset_id": "ds-1",
            "name": "src1:medium_overlap_v1:llm",
            "source_id": "src1",
            "generation_method": "llm",
            "query_model": "test-model",
            "sample_doc_count": 1,
        }
    )
    db.eval_queries.save(
        {
            "query_id": "q-1",
            "dataset_id": "ds-1",
            "doc_id": "src1:1",
            "target_chunk_id": "src1:1:chunk:0000",
            "query_text": "title-1是什么",
            "query_type": "definition",
            "difficulty": "easy",
            "gold_answer": "title-1",
            "gold_evidence_json": ["src1:1:chunk:0000"],
            "generated_by": "test",
            "review_status": "generated",
        }
    )

    service = KnowledgeBaseEvaluationService(
        settings=get_settings().model_copy(update={"data_dir": tmp_path}),
        db=db,
        kb_service=object(),
    )

    class FakeOpenSearchClient:
        def index_name(self, *, language: str, chunk_profile_id: str):
            return f"kb_wikipedia_{language}_{chunk_profile_id}"

        def bm25_search(self, *, index_name: str, query: str, top_k: int):
            return []

        def vector_search(self, *, index_name: str, query_vector: list[float], top_k: int):
            return [
                type(
                    "Hit",
                    (),
                    {
                        "chunk_id": "src1:1:chunk:0000",
                        "doc_id": "src1:1",
                        "score": 1.0,
                        "source": {"title": "title-1"},
                    },
                )()
            ]

    class FakeEmbeddingClient:
        def embed_texts(self, texts):
            return type(
                "EmbeddingResult",
                (),
                {
                    "vectors": [type("Vector", (), {"embedding": [0.1, 0.2, 0.3]})()],
                },
            )()

    service._opensearch_client_instance = FakeOpenSearchClient()
    service._kb_service = type("KB", (), {})()
    original_embed_queries = service._embed_queries
    service._embed_queries = lambda queries, retrieval_mode: {"q-1": [0.1, 0.2, 0.3]}

    summary = service.run_evaluation(
        dataset_id="ds-1",
        retrieval_mode="rrf_v2",
        top_k=5,
        chunk_profile_id="medium_overlap_v1",
        language="zh",
        retrieval_params={
            "bm25_candidate_k": 20,
            "vector_candidate_k": 20,
            "rrf_k": 60,
            "final_top_k": 5,
        },
    )

    run = db.eval_runs.get(summary.eval_run_id)
    assert run is not None
    assert json.loads(run["params_json"]) == {
        "bm25_candidate_k": 20,
        "vector_candidate_k": 20,
        "rrf_k": 60,
        "final_top_k": 5,
    }


def test_opensearch_client_sec_bm25_search_adds_filters() -> None:
    captured_body: dict | None = None

    def handler(request: Request) -> Response:
        nonlocal captured_body
        captured_body = json.loads(request.content.decode("utf-8"))
        return Response(200, json={"hits": {"hits": []}})

    client = OpenSearchClient(
        base_url="http://127.0.0.1:9200",
        index_prefix="kb_wikipedia",
        http_client=MockHttpxClient(handler),
    )

    client.bm25_search(
        index_name="kb_sec_en_sec_filing_medium_v1",
        query="cloud growth",
        top_k=5,
        filters={
            "ticker": "MSFT",
            "form_type": "10-K",
            "fiscal_year": 2025,
            "section_title": "MD&A",
        },
    )

    filter_terms = captured_body["query"]["bool"]["filter"]
    assert {"term": {"ticker": "MSFT"}} in filter_terms
    assert {"term": {"form_type": "10-K"}} in filter_terms
    assert {"term": {"fiscal_year": 2025}} in filter_terms
    assert {"term": {"section_title.keyword": "MD&A"}} in filter_terms


def test_opensearch_client_bm25_search_adds_business_corpus_filters() -> None:
    captured_body: dict | None = None

    def handler(request: Request) -> Response:
        nonlocal captured_body
        captured_body = json.loads(request.content.decode("utf-8"))
        return Response(200, json={"hits": {"hits": []}})

    client = OpenSearchClient(
        base_url="http://127.0.0.1:9200",
        index_prefix="kb_business",
        http_client=MockHttpxClient(handler),
    )

    client.bm25_search(
        index_name="kb_business_zh_medium_overlap_v1",
        query="market research",
        top_k=5,
        filters={
            "source_id": "deepresearch:run-1",
            "source_type": "deep_research",
            "language": "zh",
            "chunk_profile_id": "medium_overlap_v1",
        },
    )

    filter_terms = captured_body["query"]["bool"]["filter"]
    assert {"term": {"source_id": "deepresearch:run-1"}} in filter_terms
    assert {"term": {"source_type": "deep_research"}} in filter_terms
    assert {"term": {"language": "zh"}} in filter_terms
    assert {"term": {"chunk_profile_id": "medium_overlap_v1"}} in filter_terms


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
            "source_type": "wikipedia",
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


def test_eval_recall_at_10_counts_tenth_rank_hit(tmp_path) -> None:
    db = get_knowledge_base_db(tmp_path / "knowledge.db")
    db.sources.save(
        {
            "source_id": "src1",
            "name": "wikipedia",
            "source_type": "wikipedia",
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
    target_chunk_id = "src1:13:chunk:0009"
    for index in range(10):
        chunk_id = f"src1:13:chunk:{index:04d}"
        db.chunks.save(
            {
                "chunk_id": chunk_id,
                "doc_id": "src1:13",
                "chunk_profile_id": "medium_overlap_v1",
                "chunk_index": index,
                "chunker_version": "v1",
                "section_path": None,
                "raw_content": f"chunk-{index}",
                "normalized_content": f"chunk-{index}",
                "content_hash": f"chunk-hash-{index}",
                "char_start": index,
                "char_end": index + 1,
                "char_count": 1,
                "token_estimate": 1,
                "overlap_prev_chars": 0,
                "metadata_json": None,
            }
        )
    db.eval_datasets.save(
        {
            "dataset_id": "ds-1",
            "name": "src1:medium_overlap_v1:test",
            "source_id": "src1",
            "generation_method": "test",
            "query_model": None,
            "sample_doc_count": 1,
        }
    )
    db.eval_queries.save(
        {
            "query_id": "q-1",
            "dataset_id": "ds-1",
            "doc_id": "src1:13",
            "target_chunk_id": target_chunk_id,
            "query_text": "数学是什么",
            "query_type": "fact",
            "difficulty": "easy",
            "gold_answer": "数学",
            "gold_evidence_json": [target_chunk_id],
            "generated_by": "test",
            "review_status": "generated",
        }
    )

    service = KnowledgeBaseEvaluationService(
        settings=get_settings().model_copy(update={"data_dir": tmp_path}),
        db=db,
        kb_service=object(),
    )

    class FakeOpenSearchClient:
        def index_name(self, *, language: str, chunk_profile_id: str) -> str:
            return f"kb_wikipedia_{language}_{chunk_profile_id}"

        def bm25_search(self, *, index_name: str, query: str, top_k: int):
            return [
                type(
                    "Hit",
                    (),
                    {
                        "chunk_id": f"src1:13:chunk:{index:04d}",
                        "doc_id": "src1:13",
                        "score": float(10 - index),
                        "source": {"title": "数学"},
                    },
                )()
                for index in range(top_k)
            ]

    service._opensearch_client_instance = FakeOpenSearchClient()

    summary = service.run_evaluation(
        dataset_id="ds-1",
        retrieval_mode="bm25",
        top_k=10,
        chunk_profile_id="medium_overlap_v1",
        language="zh",
    )

    assert summary.recall_at_k == 1.0
    assert summary.precision_at_k == 0.1
    assert summary.mrr == 0.1
    assert round(summary.ndcg, 6) == round(1.0 / math.log2(11), 6)


def test_eval_dataset_generation_persists_multiple_queries(tmp_path) -> None:
    db = get_knowledge_base_db(tmp_path / "knowledge.db")
    db.sources.save(
        {
            "source_id": "src1",
            "name": "wikipedia",
            "source_type": "wikipedia",
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


def test_normalize_aliyun_markdown_extracts_structured_blocks() -> None:
    markdown = """# Item 1. Business

Overview paragraph line one.
line two continues.

## Available Information

- first bullet
- second bullet

<table><tr><td>Revenue</td><td>100</td></tr></table>

![IMAGE]
"""

    blocks = normalize_aliyun_markdown(markdown)

    assert [block["block_type"] for block in blocks] == [
        "heading",
        "paragraph",
        "heading",
        "list",
        "table",
        "image",
    ]
    assert blocks[0]["section_path"] == ["Item 1. Business"]
    assert blocks[1]["section_heading"] == "Item 1. Business"
    assert blocks[2]["metadata"]["heading_level"] == 2
    assert blocks[3]["metadata"]["list_item_count"] == 2
    assert blocks[4]["metadata"]["contains_html_table"] is True
    assert blocks[5]["metadata"]["image_placeholder"] is True


def test_chunk_sec_blocks_preserves_section_and_financial_metadata() -> None:
    blocks = [
        {
            "page_number": None,
            "block_type": "heading",
            "block_text": "Item 1. Business",
            "block_order": 0,
            "section_heading": "Item 1. Business",
            "section_path": ["Item 1. Business"],
            "bbox": None,
            "metadata": {"heading_level": 1},
        },
        {
            "page_number": None,
            "block_type": "paragraph",
            "block_text": "Revenue grew due to product demand.",
            "block_order": 1,
            "section_heading": "Item 1. Business",
            "section_path": ["Item 1. Business"],
            "bbox": None,
            "metadata": None,
        },
        {
            "page_number": None,
            "block_type": "heading",
            "block_text": "Item 1A. Risk Factors",
            "block_order": 2,
            "section_heading": "Item 1A. Risk Factors",
            "section_path": ["Item 1A. Risk Factors"],
            "bbox": None,
            "metadata": {"heading_level": 1},
        },
        {
            "page_number": None,
            "block_type": "paragraph",
            "block_text": "Supply chain constraints may affect production.",
            "block_order": 3,
            "section_heading": "Item 1A. Risk Factors",
            "section_path": ["Item 1A. Risk Factors"],
            "bbox": None,
            "metadata": None,
        },
    ]

    chunks = chunk_sec_blocks(
        blocks,
        target_size=80,
        soft_min_size=30,
        hard_max_size=120,
        overlap_size=10,
        document_metadata={
            "company_name": "3M",
            "ticker": "MMM",
            "form_type": "10-Q",
            "filing_date": "2023-06-30",
            "fiscal_year": 2023,
            "fiscal_period": "Q2",
        },
    )

    assert len(chunks) == 2
    assert chunks[0].section_path == "Item 1. Business"
    assert chunks[0].metadata["company_name"] == "3M"
    assert chunks[0].metadata["form_type"] == "10-Q"
    assert chunks[1].section_path == "Item 1A. Risk Factors"
    assert chunks[1].metadata["fiscal_period"] == "Q2"


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
    normalized_output_path = tmp_path / "normalized-blocks" / "3M_2023Q2_10Q.blocks.json"
    assert output_path.exists()
    assert normalized_output_path.exists()
    saved = json.loads(output_path.read_text(encoding="utf-8"))
    normalized = json.loads(normalized_output_path.read_text(encoding="utf-8"))
    assert saved["task_id"] == "task-1"
    assert saved["final_task_response"]["result"]["status"] == "SUCCESS"
    assert normalized[0]["block_type"] == "heading"
    assert normalized[1]["block_type"] == "paragraph"


def test_sec_filing_ingest_service_persists_documents_and_parse_artifacts(tmp_path) -> None:
    raw_dir = tmp_path / "sec-pdf" / "aliyun-raw"
    normalized_dir = tmp_path / "sec-pdf" / "normalized-blocks"
    raw_dir.mkdir(parents=True)
    normalized_dir.mkdir(parents=True)

    raw_payload = {
        "source_file": str(tmp_path / "sec-pdf" / "3M_2023Q2_10Q.pdf"),
        "task_id": "task-1",
        "final_task_response": {
            "result": {
                "status": "SUCCESS",
                "data": {
                    "content_type": "markdown",
                },
            }
        },
    }
    normalized_blocks = [
        {
            "page_number": None,
            "block_type": "heading",
            "block_text": "Item 1. Business",
            "block_order": 0,
            "section_heading": "Item 1. Business",
            "section_path": ["Item 1. Business"],
            "bbox": None,
            "metadata": {"heading_level": 1},
        },
        {
            "page_number": None,
            "block_type": "paragraph",
            "block_text": "Revenue grew due to product demand.",
            "block_order": 1,
            "section_heading": "Item 1. Business",
            "section_path": ["Item 1. Business"],
            "bbox": None,
            "metadata": None,
        },
    ]
    (raw_dir / "3M_2023Q2_10Q.aliyun.json").write_text(
        json.dumps(raw_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    (normalized_dir / "3M_2023Q2_10Q.blocks.json").write_text(
        json.dumps(normalized_blocks, ensure_ascii=False),
        encoding="utf-8",
    )

    from app.knowledge_base.sec_ingest import SecFilingIngestService

    db = get_knowledge_base_db(tmp_path / "knowledge.db")
    service = SecFilingIngestService(
        db=db,
        raw_parse_dir=raw_dir,
        normalized_blocks_dir=normalized_dir,
    )

    result = service.ingest()

    documents = db.documents.list_by_source(result.source_id)
    artifact = db.parse_artifacts.get(f"{result.source_id}:3M_2023Q2_10Q:artifact:parse:v1")
    chunks = db.chunks.list_by_document(f"{result.source_id}:3M_2023Q2_10Q", chunk_profile_id="sec_filing_medium_v1")
    chunk_run = db.chunk_runs.get(f"{result.source_id}:3M_2023Q2_10Q:chunk-run:sec_filing_medium_v1")

    assert result.status == "succeeded"
    assert result.documents_seen == 1
    assert result.documents_inserted == 1
    assert result.chunks_created >= 1
    assert len(documents) == 1
    assert documents[0]["title"] == "3M"
    assert "Revenue grew due to product demand." in documents[0]["text"]
    assert '"form_type": "10-Q"' in documents[0]["metadata_json"]
    assert artifact is not None
    assert artifact["status"] == "SUCCESS"
    assert artifact["raw_output_path"].endswith("3M_2023Q2_10Q.aliyun.json")
    assert len(chunks) >= 1
    assert '"company_name": "3M"' in chunks[0]["metadata_json"]
    assert '"section_title": "Item 1. Business"' in chunks[0]["metadata_json"]
    assert chunk_run is not None
    assert chunk_run["chunk_count"] >= 1


def test_sec_ingest_route_returns_summary(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    from app.knowledge_base.api import get_knowledge_base_service

    class FakeService:
        def ingest_sec_filings(self, **kwargs):
            return type(
                "Result",
                (),
                {
                    "job_id": "job-1",
                    "source_id": "sec_filing_local",
                    "file_path": str(tmp_path / "sec-pdf" / "aliyun-raw"),
                    "limit_n": 1,
                    "documents_seen": 1,
                    "documents_inserted": 1,
                    "documents_updated": 0,
                    "documents_skipped": 0,
                    "chunks_created": 2,
                    "status": "succeeded",
                },
            )()

    get_knowledge_base_service.cache_clear()
    app = create_app()
    import app.knowledge_base.api as kb_api

    original = kb_api.get_knowledge_base_service
    kb_api.get_knowledge_base_service = lambda: FakeService()
    try:
        client = TestClient(app)
        response = client.post("/kb/sec/ingest", json={"file_names": ["3M_2023Q2_10Q.pdf"]})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "succeeded"
        assert body["documents_inserted"] == 1
        assert body["chunks_created"] == 2
    finally:
        kb_api.get_knowledge_base_service = original
        get_settings.cache_clear()


def test_sec_index_route_returns_summary(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    from app.knowledge_base.api import get_knowledge_base_service

    class FakeService:
        def index_sec_source(self, **kwargs):
            return type(
                "Result",
                (),
                {
                    "index_name": "kb_sec_en_sec_filing_medium_v1",
                    "source_id": "sec_filing_local",
                    "chunk_profile_id": "sec_filing_medium_v1",
                    "indexed_chunks": 2,
                    "embedded_chunks": 2,
                    "embedding_model": "text-embedding-v4",
                },
            )()

    get_knowledge_base_service.cache_clear()
    app = create_app()
    import app.knowledge_base.api as kb_api

    original = kb_api.get_knowledge_base_service
    kb_api.get_knowledge_base_service = lambda: FakeService()
    try:
        client = TestClient(app)
        response = client.post("/kb/sec/index", json={"source_id": "sec_filing_local"})
        assert response.status_code == 200
        body = response.json()
        assert body["index_name"] == "kb_sec_en_sec_filing_medium_v1"
        assert body["indexed_chunks"] == 2
    finally:
        kb_api.get_knowledge_base_service = original
        get_settings.cache_clear()


def test_sec_search_route_returns_hits(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_DASHSCOPE_API_KEY", "test-key")
    get_settings.cache_clear()
    from app.knowledge_base.api import get_knowledge_base_service

    class FakeService:
        def search_sec(self, **kwargs):
            return [
                type(
                    "Hit",
                    (),
                    {
                        "chunk_id": "sec:chunk:1",
                        "doc_id": "sec:doc:1",
                        "score": 1.0,
                        "source": {"company_name": "Microsoft", "section_title": "MD&A"},
                    },
                )()
            ]

    get_knowledge_base_service.cache_clear()
    app = create_app()
    import app.knowledge_base.api as kb_api

    original = kb_api.get_knowledge_base_service
    kb_api.get_knowledge_base_service = lambda: FakeService()
    try:
        client = TestClient(app)
        response = client.post(
            "/kb/sec/search",
            json={"query": "cloud growth", "mode": "bm25", "form_type": "10-K", "fiscal_year": 2025},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["hits"][0]["chunk_id"] == "sec:chunk:1"
        assert body["hits"][0]["source"]["section_title"] == "MD&A"
    finally:
        kb_api.get_knowledge_base_service = original
        get_settings.cache_clear()


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
