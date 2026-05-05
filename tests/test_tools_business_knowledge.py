import json

from app.tools.runtime import execute_tool, get_tool_definition


def test_business_knowledge_search_tool_queries_opensearch_backend(monkeypatch) -> None:
    calls: list[dict] = []

    class FakeService:
        def search(self, **kwargs):
            calls.append(kwargs)
            return [
                type(
                    "Hit",
                    (),
                    {
                        "chunk_id": "deepresearch:1:chunk:0000",
                        "doc_id": "deepresearch:1",
                        "score": 1.25,
                        "source": {
                            "source_id": "deepresearch:run-1",
                            "source_type": "deep_research",
                            "title": "AI Market Research",
                            "content": "Business corpus evidence.",
                        },
                    },
                )()
            ]

    import app.tools.business_knowledge as business_knowledge

    monkeypatch.setattr(
        business_knowledge,
        "get_business_knowledge_service",
        lambda: FakeService(),
    )

    tool = get_tool_definition("business_knowledge_search")
    result = execute_tool(
        tool,
        {
            "query": "AI market",
            "source_type": "deep_research",
            "source_id": "deepresearch:run-1",
            "top_k": 3,
        },
    )

    payload = json.loads(result.stdout)
    assert result.ok is True
    assert payload["backend"] == "opensearch"
    assert payload["hits"][0]["source_type"] == "deep_research"
    assert calls[0]["mode"] == "rrf_v2"
    assert calls[0]["top_k"] == 3
    assert calls[0]["source_type"] == "deep_research"
    assert calls[0]["filters"]["source_id"] == "deepresearch:run-1"
    assert calls[0]["filters"]["source_type"] == "deep_research"


def test_business_knowledge_search_tool_defaults_sec_filing_profile(monkeypatch) -> None:
    calls: list[dict] = []

    class FakeService:
        def search(self, **kwargs):
            calls.append(kwargs)
            return []

    import app.tools.business_knowledge as business_knowledge

    monkeypatch.setattr(
        business_knowledge,
        "get_business_knowledge_service",
        lambda: FakeService(),
    )

    tool = get_tool_definition("business_knowledge_search")
    result = execute_tool(
        tool,
        {
            "query": "revenue growth",
            "source_type": "sec_filing",
            "ticker": "MSFT",
            "form_type": "10-K",
            "fiscal_year": 2025,
        },
    )

    assert result.ok is True
    assert calls[0]["language"] == "en"
    assert calls[0]["chunk_profile_id"] == "sec_filing_medium_v1"
    assert calls[0]["filters"]["ticker"] == "MSFT"
    assert calls[0]["filters"]["form_type"] == "10-K"
    assert calls[0]["filters"]["fiscal_year"] == 2025


def test_business_knowledge_search_tool_allows_rerank_mode(monkeypatch) -> None:
    calls: list[dict] = []

    class FakeService:
        def search(self, **kwargs):
            calls.append(kwargs)
            return []

    import app.tools.business_knowledge as business_knowledge

    monkeypatch.setattr(
        business_knowledge,
        "get_business_knowledge_service",
        lambda: FakeService(),
    )

    tool = get_tool_definition("business_knowledge_search")
    result = execute_tool(
        tool,
        {
            "query": "数学应用",
            "mode": "rrf_v2_rerank",
        },
    )

    assert result.ok is True
    assert calls[0]["mode"] == "rrf_v2_rerank"
