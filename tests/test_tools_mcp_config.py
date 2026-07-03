from pathlib import Path

from app.config import Settings
from app.tools.mcp.config import load_mcp_server_configs


def test_settings_reads_tushare_token_from_env(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TUSHARE_TOKEN", "test-tushare-token")

    settings = Settings()

    assert settings.tushare_token == "test-tushare-token"
    assert settings.tushare_mcp_enabled is False


def test_settings_reads_ifind_mcp_token_from_env(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_IFIND_MCP_TOKEN", "test-ifind-token")

    settings = Settings()

    assert settings.ifind_mcp_token == "test-ifind-token"


def test_load_tushare_mcp_config_from_json_expands_token(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TUSHARE_TOKEN", "test-tushare-token")
    settings = Settings(
        mcp_enabled=True,
        tushare_mcp_enabled=True,
        mcp_servers_json=(
            '{"mcpServers":{"tushareMcp":'
            '{"url":"https://api.tushare.pro/mcp/?token=${JARVIS_TUSHARE_TOKEN}",'
            '"enabled_tools":["stock_basic","daily","daily_basic"]}}}'
        ),
    )

    servers = load_mcp_server_configs(settings)

    assert len(servers) == 1
    assert servers[0].name == "tushareMcp"
    assert servers[0].url == "https://api.tushare.pro/mcp/?token=test-tushare-token"
    assert servers[0].transport == "streamable_http"
    assert servers[0].enabled_tools == ("stock_basic", "daily", "daily_basic")


def test_tushare_mcp_config_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TUSHARE_TOKEN", "test-tushare-token")
    settings = Settings(
        mcp_enabled=True,
        mcp_servers_json='{"mcpServers":{"tushareMcp":{"url":"https://api.tushare.pro/mcp/?token=${JARVIS_TUSHARE_TOKEN}"}}}',
    )

    assert load_mcp_server_configs(settings) == []


def test_load_tushare_mcp_config_expands_token_from_settings(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_TUSHARE_TOKEN", raising=False)
    settings = Settings(
        tushare_token="settings-token",
        tushare_mcp_enabled=True,
        mcp_enabled=True,
        mcp_servers_json='{"mcpServers":{"tushareMcp":{"url":"https://api.tushare.pro/mcp/?token=${JARVIS_TUSHARE_TOKEN}"}}}',
    )

    servers = load_mcp_server_configs(settings)

    assert servers[0].url == "https://api.tushare.pro/mcp/?token=settings-token"


def test_load_http_mcp_config_from_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp.yaml"
    config_path.write_text(
        """
mcpServers:
  fred:
    transport: streamable-http
    url: http://127.0.0.1:8765/mcp
    enabled: true
    startup_timeout_sec: 3
    tool_timeout_sec: 9
    protocol_version: "2025-03-26"
    enabled_tools:
      - fred_get_macro_snapshot
    disabled_tools:
      - fred_search_series
""",
        encoding="utf-8",
    )
    settings = Settings(mcp_enabled=True, mcp_config_path=config_path)

    servers = load_mcp_server_configs(settings)

    assert len(servers) == 1
    assert servers[0].name == "fred"
    assert servers[0].url == "http://127.0.0.1:8765/mcp"
    assert servers[0].transport == "streamable_http"
    assert servers[0].protocol_version == "2025-03-26"
    assert servers[0].startup_timeout_sec == 3
    assert servers[0].tool_timeout_sec == 9
    assert servers[0].enabled_tools == ("fred_get_macro_snapshot",)
    assert servers[0].disabled_tools == ("fred_search_series",)


def test_mcp_env_http_headers_can_resolve_from_settings() -> None:
    settings = Settings(
        ifind_mcp_token="settings-ifind-token",
        mcp_enabled=True,
        mcp_servers_json=(
            '{"mcpServers":{"ifind_stock":'
            '{"url":"https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-stock-mcp",'
            '"env_http_headers":{"Authorization":"JARVIS_IFIND_MCP_TOKEN"}}}}'
        ),
    )

    servers = load_mcp_server_configs(settings)

    assert servers[0].request_headers()["Authorization"] == "settings-ifind-token"


def test_mcp_config_ignores_missing_file_when_enabled(tmp_path: Path) -> None:
    settings = Settings(mcp_enabled=True, mcp_config_path=tmp_path / "missing.yaml")

    assert load_mcp_server_configs(settings) == []


def test_mcp_config_uses_default_protocol_version() -> None:
    settings = Settings(
        mcp_enabled=True,
        mcp_servers_json='{"mcpServers":{"fred":{"url":"http://127.0.0.1:8765/mcp"}}}',
    )

    servers = load_mcp_server_configs(settings)

    assert servers[0].protocol_version == "2024-11-05"
