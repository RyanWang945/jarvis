from __future__ import annotations

import json
import threading
from typing import Any

import httpx

from app.tools.mcp.config import McpServerConfig


class HttpMcpClient:
    """Synchronous MCP streamable-HTTP client for Jarvis tool calls."""

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._request_id = 0
        self._session_id: str | None = None
        self._lock = threading.Lock()
        self._client = httpx.Client(timeout=config.tool_timeout_sec)
        self._initialized = False

    def initialize(self) -> dict[str, Any]:
        with self._lock:
            if self._initialized:
                return {}
            response = self._request_locked(
                "initialize",
                {
                    "protocolVersion": self.config.protocol_version,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "jarvis",
                        "version": "0.1.0",
                    },
                },
                timeout=self.config.startup_timeout_sec,
            )
            self._notify_locked("notifications/initialized", {})
            self._initialized = True
            return response

    def list_tools(self) -> list[dict[str, Any]]:
        self.initialize()
        collected: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            with self._lock:
                response = self._request_locked("tools/list", params, timeout=self.config.startup_timeout_sec)
            result = response.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"MCP server '{self.config.name}' returned invalid tools/list result.")
            tools = result.get("tools", [])
            if not isinstance(tools, list):
                raise RuntimeError(f"MCP server '{self.config.name}' returned invalid tools list.")
            collected.extend(item for item in tools if isinstance(item, dict))
            next_cursor = result.get("nextCursor") or result.get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return collected
            cursor = next_cursor

    def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        self.initialize()
        with self._lock:
            return self._request_locked(
                "tools/call",
                {"name": tool_name, "arguments": arguments or {}},
                timeout=self.config.tool_timeout_sec,
            )

    def close(self) -> None:
        self._client.close()

    def _request_locked(self, method: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        response = self._post(payload, timeout=timeout)
        if response.get("id") != request_id:
            raise RuntimeError(f"MCP server '{self.config.name}' returned mismatched response id.")
        if "error" in response:
            raise RuntimeError(f"MCP server '{self.config.name}' returned error: {response['error']}")
        return response

    def _notify_locked(self, method: str, params: dict[str, Any]) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._post(payload, timeout=self.config.startup_timeout_sec, allow_empty=True)

    def _post(self, payload: dict[str, Any], *, timeout: float, allow_empty: bool = False) -> dict[str, Any]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self.config.protocol_version,
            **self.config.request_headers(),
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        with self._client.stream("POST", self.config.url, json=payload, headers=headers, timeout=timeout) as response:
            session_id = response.headers.get("mcp-session-id") or response.headers.get("Mcp-Session-Id")
            if session_id:
                self._session_id = session_id

            if allow_empty and response.status_code in {202, 204}:
                return {}
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                return _parse_sse_lines(response.iter_lines())

            text = response.read().decode(response.encoding or "utf-8", errors="replace").strip()
        if not text:
            if allow_empty:
                return {}
            raise RuntimeError(f"MCP server '{self.config.name}' returned an empty response.")

        parsed = json.loads(text)
        if isinstance(parsed, list):
            if not parsed:
                return {}
            parsed = parsed[0]
        if not isinstance(parsed, dict):
            raise RuntimeError(f"MCP server '{self.config.name}' returned non-object JSON-RPC response.")
        return parsed


def _parse_sse_lines(lines: Any) -> dict[str, Any]:
    data_lines: list[str] = []
    for line in lines:
        text = str(line or "").strip()
        if not text:
            if data_lines:
                break
            continue
        if text.startswith("data:"):
            data_lines.append(text.removeprefix("data:").strip())
            continue
        if data_lines and text.startswith("event:"):
            break
    if not data_lines:
        raise RuntimeError("MCP SSE response did not contain data lines.")
    parsed = json.loads("\n".join(data_lines))
    if not isinstance(parsed, dict):
        raise RuntimeError("MCP SSE data was not a JSON object.")
    return parsed


def _parse_sse_json(text: str) -> dict[str, Any]:
    return _parse_sse_lines(text.splitlines())
