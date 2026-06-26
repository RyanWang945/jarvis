from pathlib import Path

from app.config import Settings
from app.tools.mcp.config import load_mcp_server_configs


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
    assert servers[0].startup_timeout_sec == 3
    assert servers[0].tool_timeout_sec == 9
    assert servers[0].enabled_tools == ("fred_get_macro_snapshot",)
    assert servers[0].disabled_tools == ("fred_search_series",)


def test_mcp_config_ignores_missing_file_when_enabled(tmp_path: Path) -> None:
    settings = Settings(mcp_enabled=True, mcp_config_path=tmp_path / "missing.yaml")

    assert load_mcp_server_configs(settings) == []
