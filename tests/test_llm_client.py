from app.config import get_settings
from app.llm.client import LLMMessage, parse_json_content
from app.llm.model_profiles import LLMNode
from app.llm.model_router import ModelRouter
from app.llm.provider_adapters import normalize_response


def test_parse_json_content_uses_first_json_object() -> None:
    parsed = parse_json_content(
        {
            "content": (
                '{"turn_type":"chat","confidence":0.8}\n'
                '{"extra":"provider accidentally emitted more data"}'
            )
        }
    )

    assert parsed == {"turn_type": "chat", "confidence": 0.8}


def test_provider_adapter_normalizes_tool_call_args_dict_and_usage_aliases() -> None:
    normalized = normalize_response(
        {
            "model": "moonshot-v1-8k",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "delegate_to_codex",
                                    "arguments": {"instruction": "inspect repo"},
                                }
                            }
                        ],
                    },
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
        provider="kimi",
        fallback_model="fallback",
    )

    assert normalized.model == "moonshot-v1-8k"
    assert normalized.finish_reason == "tool_calls"
    assert normalized.tool_calls[0].name == "delegate_to_codex"
    assert normalized.tool_calls[0].args == {"instruction": "inspect repo"}
    assert normalized.tool_calls[0].id.startswith("call_")
    assert normalized.usage is not None
    assert normalized.usage.prompt_tokens == 10
    assert normalized.usage.completion_tokens == 5
    assert normalized.usage.total_tokens == 15


def test_chat_normalized_strips_reasoning_when_profile_disallows(monkeypatch) -> None:
    captured = {}

    def _fake_post(url, *, headers, json, timeout):
        captured["messages"] = json["messages"]

        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"model": "kimi", "choices": [{"message": {"content": "ok"}}]}

        return _Response()

    monkeypatch.setattr("app.llm.client.httpx.post", _fake_post)
    from app.llm.client import ChatClient

    client = ChatClient(
        api_key="key",
        base_url="https://example.test",
        model="kimi",
        timeout_seconds=1,
        provider="kimi",
        supports_reasoning_content=False,
    )

    response = client.chat_normalized(
        [LLMMessage(role="assistant", content="prior", reasoning_content="secret")]
    )

    assert response.content == "ok"
    assert "reasoning_content" not in captured["messages"][0]


def test_model_router_uses_node_override_and_short_classifier_timeout(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "deepseek-key")
    monkeypatch.setenv("JARVIS_KIMI_API_KEY", "kimi-key")
    monkeypatch.setenv("JARVIS_LLM_TIMEOUT_SECONDS", "60")
    get_settings.cache_clear()

    resolved = ModelRouter(get_settings()).resolve(
        LLMNode.INTENT_CLASSIFIER,
        {"runtime_profile": {"model_overrides": {"intent_classifier": "moonshot-v1-8k"}}},
    )

    assert resolved.profile.provider == "kimi"
    assert resolved.policy.timeout_seconds == 10.0
    get_settings.cache_clear()
