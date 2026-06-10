from __future__ import annotations

import atexit
import json
import logging
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Protocol

from app.config import get_settings
from app.tools.common import ToolExecutionRequest, ToolExecutionResult
from app.tools.definitions import ToolDefinition
from app.tools.mcp.config import McpServerConfig, load_mcp_server_configs
from app.tools.mcp.http_client import HttpMcpClient
from app.tools.mcp.schema import mcp_input_schema, mcp_tool_description, qualify_tool_name

logger = logging.getLogger(__name__)


class McpClient(Protocol):
    def initialize(self) -> dict[str, Any]:
        ...

    def list_tools(self) -> list[dict[str, Any]]:
        ...

    def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        ...

    def close(self) -> None:
        ...


ClientFactory = Callable[[McpServerConfig], McpClient]


@dataclass(frozen=True, slots=True)
class McpToolBinding:
    qualified_name: str
    server_name: str
    tool_name: str


class McpToolManager:
    def __init__(
        self,
        configs: list[McpServerConfig],
        *,
        client_factory: ClientFactory | None = None,
        cache_ttl_seconds: float = 300.0,
    ) -> None:
        self._configs = {config.name: config for config in configs}
        self._client_factory = client_factory or HttpMcpClient
        self._cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        self._clients: dict[str, McpClient] = {}
        self._definitions: dict[str, ToolDefinition] = {}
        self._bindings: dict[str, McpToolBinding] = {}
        self._loaded_at = 0.0
        self._lock = threading.RLock()

    def list_tool_definitions(self) -> list[ToolDefinition]:
        self._ensure_loaded()
        return list(self._definitions.values())

    def get_tool_definition(self, name: str) -> ToolDefinition | None:
        self._ensure_loaded()
        return self._definitions.get(name)

    def close(self) -> None:
        with self._lock:
            for client in self._clients.values():
                try:
                    client.close()
                except Exception:
                    logger.debug("failed closing MCP client", exc_info=True)
            self._clients.clear()

    def refresh(self) -> None:
        with self._lock:
            self._definitions.clear()
            self._bindings.clear()
            self._loaded_at = 0.0
            self._ensure_loaded_locked(force=True)

    def _ensure_loaded(self) -> None:
        with self._lock:
            expired = self._cache_ttl_seconds > 0 and time.monotonic() - self._loaded_at > self._cache_ttl_seconds
            if self._loaded_at > 0 and not expired:
                return
            self._ensure_loaded_locked(force=expired)

    def _ensure_loaded_locked(self, *, force: bool) -> None:
        if self._loaded_at > 0 and not force:
            return
        definitions: dict[str, ToolDefinition] = {}
        bindings: dict[str, McpToolBinding] = {}

        for config in self._configs.values():
            try:
                client = self._client_for(config)
                tools = client.list_tools()
            except Exception as exc:
                message = f"Failed to load MCP server '{config.name}': {exc}"
                if config.required:
                    raise RuntimeError(message) from exc
                logger.warning(message)
                continue

            for tool in tools:
                raw_name = str(tool.get("name") or "").strip()
                if not raw_name or not config.allows_tool(raw_name):
                    continue
                qualified_name = qualify_tool_name(config.name, raw_name)
                if qualified_name in definitions:
                    logger.warning("Skipping duplicate MCP tool name: %s", qualified_name)
                    continue
                binding = McpToolBinding(
                    qualified_name=qualified_name,
                    server_name=config.name,
                    tool_name=raw_name,
                )
                bindings[qualified_name] = binding
                definitions[qualified_name] = ToolDefinition(
                    name=qualified_name,
                    description=mcp_tool_description(config.name, tool),
                    args_schema=mcp_input_schema(tool),
                    handler=self._make_handler(qualified_name),
                    risk_level="low",
                )

        self._definitions = definitions
        self._bindings = bindings
        self._loaded_at = time.monotonic()

    def _client_for(self, config: McpServerConfig) -> McpClient:
        client = self._clients.get(config.name)
        if client is None:
            client = self._client_factory(config)
            self._clients[config.name] = client
        return client

    def _make_handler(self, qualified_name: str):
        def handler(request: ToolExecutionRequest) -> ToolExecutionResult:
            return self._execute(qualified_name, request)

        return handler

    def _execute(self, qualified_name: str, request: ToolExecutionRequest) -> ToolExecutionResult:
        with self._lock:
            binding = self._bindings.get(qualified_name)
            if binding is None:
                self._ensure_loaded_locked(force=True)
                binding = self._bindings.get(qualified_name)
            if binding is None:
                return ToolExecutionResult(
                    ok=False,
                    exit_code=None,
                    stderr=f"unknown MCP tool: {qualified_name}",
                    summary=f"Unknown MCP tool: {qualified_name}",
                )
            config = self._configs[binding.server_name]
            client = self._client_for(config)

        try:
            response = client.call_tool(binding.tool_name, request.args)
        except Exception as exc:
            return ToolExecutionResult(
                ok=False,
                exit_code=None,
                stderr=str(exc),
                summary=f"MCP tool {qualified_name} failed.",
            )

        return _mcp_response_to_result(qualified_name, response)


def _mcp_response_to_result(tool_name: str, response: dict[str, Any]) -> ToolExecutionResult:
    result = response.get("result") if isinstance(response.get("result"), dict) else response
    if not isinstance(result, dict):
        text = json.dumps(response, ensure_ascii=False, default=str)
        return ToolExecutionResult(ok=True, exit_code=0, stdout=text, summary=f"MCP tool {tool_name} completed.")

    content = result.get("content", [])
    text_parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    text_parts.append(item["text"])
                elif "text" in item:
                    text_parts.append(str(item["text"]))

    structured_content = result.get("structuredContent") or result.get("structured_content")
    if not text_parts and structured_content is not None:
        text_parts.append(json.dumps(structured_content, ensure_ascii=False, default=str))

    stdout = "\n".join(text_parts) if text_parts else json.dumps(result, ensure_ascii=False, default=str)
    is_error = bool(result.get("isError") or result.get("is_error"))
    business_ok = _business_ok(stdout)
    ok = not is_error and business_ok is not False
    summary = f"MCP tool {tool_name} {'completed' if ok else 'failed'}."
    return ToolExecutionResult(
        ok=ok,
        exit_code=0 if ok else None,
        stdout=stdout,
        stderr="" if ok else stdout,
        summary=summary,
    )


def _business_ok(stdout: str) -> bool | None:
    text = stdout.strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict) and isinstance(parsed.get("ok"), bool):
        return parsed["ok"]
    return None


@lru_cache(maxsize=1)
def get_mcp_tool_manager() -> McpToolManager:
    settings = get_settings()
    manager = McpToolManager(
        load_mcp_server_configs(settings),
        cache_ttl_seconds=settings.mcp_tools_cache_ttl_seconds,
    )
    atexit.register(manager.close)
    return manager


def reset_mcp_tool_manager_for_tests() -> None:
    try:
        get_mcp_tool_manager().close()
    finally:
        get_mcp_tool_manager.cache_clear()
