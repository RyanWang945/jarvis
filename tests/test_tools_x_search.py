import json

from app.config import get_settings
from app.tools.common import ToolExecutionRequest
from app.tools.x_search import run_x_search


class _FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_x_search_builds_xai_payload_and_returns_citations(monkeypatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("JARVIS_XAI_API_KEY", "test-xai-key")
    captured: dict = {}

    def _fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeResponse(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "People are discussing Jarvis runtime boundaries.",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "title": "Post",
                                        "url": "https://x.com/example/status/1",
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "server_side_tool_usage_details": {"x_search_calls": 1},
                },
            }
        )

    monkeypatch.setattr("app.tools.x_search.httpx.post", _fake_post)

    result = run_x_search(
        ToolExecutionRequest(
            tool_name="x_search",
            workdir=None,
            args={
                "query": "Jarvis runtime",
                "handles": ["openai"],
                "date_from": "2026-05-01",
                "include_images": True,
                "max_results": 3,
            },
        )
    )

    assert result.ok is True
    assert captured["url"] == "https://api.x.ai/v1/responses"
    assert captured["headers"]["Authorization"] == "Bearer test-xai-key"
    tool = captured["json"]["tools"][0]
    assert tool == {
        "type": "x_search",
        "allowed_x_handles": ["openai"],
        "from_date": "2026-05-01",
        "enable_image_understanding": True,
    }
    payload = json.loads(result.stdout)
    assert payload["text"] == "People are discussing Jarvis runtime boundaries."
    assert payload["citations"][0]["url"] == "https://x.com/example/status/1"
    assert "Citation URLs: https://x.com/example/status/1" in result.summary


def test_x_search_rejects_conflicting_handle_filters(monkeypatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("JARVIS_XAI_API_KEY", "test-xai-key")

    result = run_x_search(
        ToolExecutionRequest(
            tool_name="x_search",
            workdir=None,
            args={"query": "Jarvis", "handles": ["openai"], "exclude_handles": ["xai"]},
        )
    )

    assert result.ok is False
    assert "cannot both be provided" in result.stderr
