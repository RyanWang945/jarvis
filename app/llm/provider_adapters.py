from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class NormalizedToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class NormalizedLLMResponse:
    content: str
    tool_calls: tuple[NormalizedToolCall, ...]
    reasoning_content: str | None
    usage: TokenUsage | None
    model: str
    finish_reason: str | None
    raw: dict[str, Any]


class ProviderAdapter(Protocol):
    provider: str

    def normalize_response(self, response: dict[str, Any], *, fallback_model: str) -> NormalizedLLMResponse:
        ...


class OpenAICompatibleAdapter:
    provider = "openai_compatible"

    def normalize_response(self, response: dict[str, Any], *, fallback_model: str) -> NormalizedLLMResponse:
        message = _extract_message(response)
        content = message.get("content") if isinstance(message.get("content"), str) else ""
        model = _first_non_empty_string(response.get("_model"), response.get("model"), fallback_model)
        finish_reason = _extract_finish_reason(response)
        reasoning_content = _first_non_empty_string(
            message.get("reasoning_content"),
            message.get("reasoning"),
            response.get("reasoning_content"),
        )
        usage = _normalize_usage(response.get("_usage") if "_usage" in response else response.get("usage"))
        tool_calls = tuple(_normalize_tool_calls(message.get("tool_calls")))
        return NormalizedLLMResponse(
            content=content or "",
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
            usage=usage,
            model=model,
            finish_reason=finish_reason,
            raw=dict(response),
        )


class DeepSeekAdapter(OpenAICompatibleAdapter):
    provider = "deepseek"


class KimiAdapter(OpenAICompatibleAdapter):
    provider = "kimi"


class GeminiOpenAIAdapter(OpenAICompatibleAdapter):
    provider = "gemini"


def adapter_for_provider(provider: str) -> ProviderAdapter:
    normalized = provider.strip().lower()
    if normalized == "deepseek":
        return DeepSeekAdapter()
    if normalized == "kimi":
        return KimiAdapter()
    if normalized == "gemini":
        return GeminiOpenAIAdapter()
    return OpenAICompatibleAdapter()


def normalize_response(
    response: dict[str, Any],
    *,
    provider: str = "deepseek",
    fallback_model: str,
) -> NormalizedLLMResponse:
    return adapter_for_provider(provider).normalize_response(response, fallback_model=fallback_model)


def normalized_to_legacy_dict(response: NormalizedLLMResponse) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": response.content,
        "tool_calls": [
            {
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "arguments": json.dumps(tool_call.args, ensure_ascii=False),
                },
            }
            for tool_call in response.tool_calls
        ],
        "_model": response.model,
    }
    if response.reasoning_content is not None:
        result["reasoning_content"] = response.reasoning_content
    if response.usage is not None:
        result["_usage"] = {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
        }
    if response.finish_reason is not None:
        result["finish_reason"] = response.finish_reason
    return result


def _extract_message(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                return message
    return response


def _extract_finish_reason(response: dict[str, Any]) -> str | None:
    if isinstance(response.get("finish_reason"), str):
        return response["finish_reason"]
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict) and isinstance(first.get("finish_reason"), str):
            return first["finish_reason"]
    return None


def _normalize_tool_calls(raw_tool_calls: Any) -> list[NormalizedToolCall]:
    if not isinstance(raw_tool_calls, list):
        return []
    normalized: list[NormalizedToolCall] = []
    for index, raw in enumerate(raw_tool_calls):
        if not isinstance(raw, dict):
            continue
        name, raw_args = _tool_name_and_args(raw)
        if not name:
            continue
        args = _normalize_args(raw_args)
        tool_call_id = str(raw.get("id") or "").strip()
        if not tool_call_id:
            tool_call_id = _fallback_tool_call_id(index=index, name=name, args=args)
        normalized.append(NormalizedToolCall(id=tool_call_id, name=name, args=args))
    return normalized


def _tool_name_and_args(raw: dict[str, Any]) -> tuple[str, Any]:
    function = raw.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "").strip(), function.get("arguments")
    return str(raw.get("name") or raw.get("tool_name") or "").strip(), raw.get("args") or raw.get("arguments")


def _normalize_args(raw_args: Any) -> dict[str, Any]:
    if isinstance(raw_args, dict):
        return dict(raw_args)
    if raw_args is None:
        return {}
    if isinstance(raw_args, str):
        text = raw_args.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _fallback_tool_call_id(*, index: int, name: str, args: dict[str, Any]) -> str:
    payload = json.dumps({"index": index, "name": name, "args": args}, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    return f"call_{digest}"


def _normalize_usage(raw_usage: Any) -> TokenUsage | None:
    if not isinstance(raw_usage, dict):
        return None
    prompt = _int_value(raw_usage.get("prompt_tokens"), raw_usage.get("input_tokens"))
    completion = _int_value(raw_usage.get("completion_tokens"), raw_usage.get("output_tokens"))
    total = _int_value(raw_usage.get("total_tokens"))
    if total <= 0 and (prompt > 0 or completion > 0):
        total = prompt + completion
    if prompt <= 0 and completion <= 0 and total <= 0:
        return None
    return TokenUsage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)


def _int_value(*values: Any) -> int:
    for value in values:
        if value is None:
            continue
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            continue
    return 0


def _first_non_empty_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None
