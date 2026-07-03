import json
from typing import Any

from app.tools.common import ToolExecutionRequest
from app.tools.mcp.config import McpServerConfig
from app.tools.mcp.http_client import HttpMcpClient, _parse_sse_json
from app.tools.mcp.manager import McpToolManager


class FakeMcpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def initialize(self) -> dict[str, Any]:
        return {"result": {"protocolVersion": "2024-11-05"}}

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "fred_get_macro_snapshot",
                "description": "Get macro snapshot.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "include_metadata": {"type": "boolean"},
                    },
                },
            },
            {
                "name": "fred_search_series",
                "description": "Search series.",
                "inputSchema": {"type": "object"},
            },
        ]

    def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = dict(arguments or {})
        self.calls.append((tool_name, args))
        return {
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"ok": True, "data": {"tool": tool_name, "args": args}}),
                    }
                ]
            }
        }

    def close(self) -> None:
        return None


def test_parse_sse_json_uses_first_data_event() -> None:
    payload = (
        "event: message\n"
        'data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n'
        "\n"
    )

    parsed = _parse_sse_json(payload)

    assert parsed["id"] == 1
    assert parsed["result"]["ok"] is True


def test_http_client_uses_configured_protocol_version_header() -> None:
    captured: dict[str, Any] = {}

    class FakeResponse:
        headers: dict[str, str] = {}
        status_code = 204

        def raise_for_status(self) -> None:
            return None

    class FakeStream:
        def __enter__(self) -> FakeResponse:
            return FakeResponse()

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    class FakeHttpClient:
        def stream(self, method: str, url: str, **kwargs: Any) -> FakeStream:
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = kwargs["headers"]
            return FakeStream()

        def close(self) -> None:
            return None

    client = HttpMcpClient(
        McpServerConfig(
            name="ifind_stock",
            url="https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-stock-mcp",
            protocol_version="2025-03-26",
        )
    )
    client._client = FakeHttpClient()  # type: ignore[assignment]

    client._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=1, allow_empty=True)

    assert captured["headers"]["MCP-Protocol-Version"] == "2025-03-26"


def test_manager_registers_filtered_mcp_tools_and_executes_original_tool_name() -> None:
    fake = FakeMcpClient()
    manager = McpToolManager(
        [
            McpServerConfig(
                name="fred",
                url="http://example.test/mcp",
                enabled_tools=("fred_get_macro_snapshot",),
            )
        ],
        client_factory=lambda _config: fake,
        cache_ttl_seconds=0,
    )

    tools = {tool.name: tool for tool in manager.list_tool_definitions()}

    assert list(tools) == ["mcp__fred__fred_get_macro_snapshot"]
    result = tools["mcp__fred__fred_get_macro_snapshot"].handler(
        ToolExecutionRequest(
            tool_name="mcp__fred__fred_get_macro_snapshot",
            workdir=None,
            args={"include_metadata": False},
        )
    )

    assert result.ok
    assert fake.calls == [("fred_get_macro_snapshot", {"include_metadata": False})]
    assert json.loads(result.stdout)["data"]["tool"] == "fred_get_macro_snapshot"


def test_manager_registers_tushare_mcp_tools_with_qualified_names() -> None:
    class TushareClient(FakeMcpClient):
        def list_tools(self) -> list[dict[str, Any]]:
            return [
                {
                    "name": "daily_basic",
                    "description": "Get daily basic A-share indicators.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "ts_code": {"type": "string"},
                            "trade_date": {"type": "string"},
                        },
                    },
                }
            ]

    manager = McpToolManager(
        [McpServerConfig(name="tushareMcp", url="https://api.tushare.pro/mcp/?token=test")],
        client_factory=lambda _config: TushareClient(),
        cache_ttl_seconds=0,
    )

    tools = {tool.name: tool for tool in manager.list_tool_definitions()}

    assert list(tools) == ["mcp__tushareMcp__daily_basic"]
    assert tools["mcp__tushareMcp__daily_basic"].args_schema["properties"]["ts_code"]["type"] == "string"


def test_manager_marks_business_error_envelope_as_failed() -> None:
    class ErrorClient(FakeMcpClient):
        def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
            return {"result": {"content": [{"type": "text", "text": json.dumps({"ok": False})}]}}

    manager = McpToolManager(
        [McpServerConfig(name="fred", url="http://example.test/mcp")],
        client_factory=lambda _config: ErrorClient(),
        cache_ttl_seconds=0,
    )
    tool = manager.list_tool_definitions()[0]

    result = tool.handler(ToolExecutionRequest(tool_name=tool.name, workdir=None, args={}))

    assert not result.ok
    assert result.stderr == result.stdout
