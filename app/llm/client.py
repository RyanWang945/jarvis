import json
import logging
from typing import Any, Literal

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

LLMRole = Literal["system", "user", "assistant", "tool"]


class LLMMessage(BaseModel):
    role: LLMRole
    content: str
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    reasoning_content: str | None = None


class ChatClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_seconds = timeout_seconds

    def chat(
        self,
        messages: list[LLMMessage],
        *,
        response_format: dict[str, str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        def _msg_dict(msg: LLMMessage) -> dict[str, Any]:
            d: dict[str, Any] = {"role": msg.role}
            # Omit empty content when tool_calls are present; some providers
            # reject content="" alongside tool_calls.
            if msg.content or not msg.tool_calls:
                d["content"] = msg.content
            if msg.tool_call_id is not None:
                d["tool_call_id"] = msg.tool_call_id
            if msg.tool_calls is not None:
                d["tool_calls"] = msg.tool_calls
            if msg.reasoning_content is not None:
                d["reasoning_content"] = msg.reasoning_content
            return d

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [_msg_dict(m) for m in messages],
            "stream": False,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

        response = httpx.post(
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self._timeout_seconds,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("llm request failed status=%s body=%s", exc.response.status_code, exc.response.text)
            raise
        body = response.json()
        message = body["choices"][0]["message"]
        return message if isinstance(message, dict) else {"content": str(message)}


def parse_json_content(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return {}
    parsed = json.loads(content)
    return parsed if isinstance(parsed, dict) else {}
