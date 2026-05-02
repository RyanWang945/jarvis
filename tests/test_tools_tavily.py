from app.tools.common import ToolExecutionRequest
from app.tools.tavily import run_tavily_search


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_tavily_search_includes_original_source_urls(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TAVILY_API_KEY", "test-key")

    def _fake_post(url, json, timeout):
        return _FakeResponse(
            {
                "answer": "DeepSeek pricing page lists the current token prices.",
                "results": [
                    {
                        "title": "DeepSeek API Pricing",
                        "url": "https://api-docs.deepseek.com/quick_start/pricing",
                        "content": "Pricing details from the official DeepSeek documentation.",
                        "score": 0.98,
                    },
                    {
                        "title": "DeepSeek Home",
                        "url": "https://www.deepseek.com/",
                        "content": "DeepSeek official website.",
                        "score": 0.72,
                    },
                ],
            }
        )

    monkeypatch.setattr("app.tools.tavily.httpx.post", _fake_post)

    result = run_tavily_search(
        ToolExecutionRequest(
            tool_name="tavily_search",
            workdir=None,
            args={"query": "deepseek api pricing"},
            timeout_seconds=30,
        )
    )

    assert result.ok is True
    assert "Original URL: https://api-docs.deepseek.com/quick_start/pricing" in result.stdout
    assert "Original source URLs:" in result.stdout
    assert "https://www.deepseek.com/" in result.stdout
    assert "Source URLs: https://api-docs.deepseek.com/quick_start/pricing, https://www.deepseek.com/" in result.summary
