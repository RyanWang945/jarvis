import json
from typing import Any, Literal

import httpx
from pydantic import BaseModel

LLMRole = Literal["system", "user", "assistant"]


class LLMMessage(BaseModel):
    role: LLMRole
    content: str


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
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [message.model_dump() for message in messages],
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
        response.raise_for_status()
        body = response.json()
        message = body["choices"][0]["message"]
        return message if isinstance(message, dict) else {"content": str(message)}


def parse_json_content(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return {}
    parsed = json.loads(content)
    return parsed if isinstance(parsed, dict) else {}
