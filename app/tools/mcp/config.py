from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.config import Settings


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    name: str
    url: str
    transport: str = "streamable_http"
    enabled: bool = True
    required: bool = False
    startup_timeout_sec: float = 10.0
    tool_timeout_sec: float = 60.0
    enabled_tools: tuple[str, ...] | None = None
    disabled_tools: tuple[str, ...] = ()
    http_headers: dict[str, str] = field(default_factory=dict)
    env_http_headers: dict[str, str] = field(default_factory=dict)
    bearer_token_env_var: str | None = None

    def request_headers(self) -> dict[str, str]:
        headers = dict(self.http_headers)
        for header, env_name in self.env_http_headers.items():
            value = os.getenv(env_name)
            if value:
                headers[header] = value
        if self.bearer_token_env_var:
            token = os.getenv(self.bearer_token_env_var)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    def allows_tool(self, tool_name: str) -> bool:
        if self.enabled_tools is not None and tool_name not in self.enabled_tools:
            return False
        return tool_name not in self.disabled_tools


def load_mcp_server_configs(settings: Settings) -> list[McpServerConfig]:
    if not settings.mcp_enabled:
        return []

    raw: dict[str, Any] = {}
    if settings.mcp_servers_json:
        raw = _load_json(settings.mcp_servers_json)
    elif settings.mcp_config_path is not None:
        config_path = _resolve_config_path(settings.mcp_config_path, settings.workspace_root)
        if not config_path.exists():
            return []
        raw = _load_yaml(config_path)
    else:
        return []

    servers = raw.get("mcpServers") or raw.get("mcp_servers") or raw.get("mcp")
    if not isinstance(servers, dict):
        raise ValueError("MCP config must define mcpServers.")

    loaded: list[McpServerConfig] = []
    for name, item in servers.items():
        if not isinstance(name, str) or not isinstance(item, dict):
            raise ValueError("Each MCP server entry must be a mapping.")
        config = _load_server(name, item, settings=settings)
        if _is_tushare_server(config) and not settings.tushare_mcp_enabled:
            continue
        if config.enabled:
            loaded.append(config)
    return loaded


def _resolve_config_path(path: Path, workspace_root: Path) -> Path:
    return path if path.is_absolute() else workspace_root / path


def _load_json(raw: str) -> dict[str, Any]:
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("JARVIS_MCP_SERVERS_JSON must be a JSON object.")
    return parsed


def _load_yaml(path: Path) -> dict[str, Any]:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} must contain a YAML mapping.")
    return parsed


def _load_server(name: str, item: dict[str, Any], *, settings: Settings) -> McpServerConfig:
    transport = str(item.get("transport") or item.get("type") or "streamable_http").replace("-", "_")
    if transport == "http":
        transport = "streamable_http"
    if transport not in {"streamable_http", "sse"}:
        raise ValueError(f"MCP server '{name}' must use an HTTP transport.")

    url = item.get("url") or item.get("endpoint")
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"MCP server '{name}' requires url.")
    expanded_url = _expand_config_vars(url.strip(), settings=settings)

    http_headers = _string_map(item.get("http_headers") or item.get("headers") or {}, f"{name}.http_headers")
    env_http_headers = _string_map(item.get("env_http_headers") or {}, f"{name}.env_http_headers")
    enabled_tools = _optional_string_tuple(item.get("enabled_tools"), f"{name}.enabled_tools")
    disabled_tools = _string_tuple(item.get("disabled_tools"), f"{name}.disabled_tools")

    return McpServerConfig(
        name=name,
        url=expanded_url,
        transport=transport,
        enabled=bool(item.get("enabled", True)),
        required=bool(item.get("required", False)),
        startup_timeout_sec=_positive_float(item.get("startup_timeout_sec"), default=10.0),
        tool_timeout_sec=_positive_float(item.get("tool_timeout_sec"), default=60.0),
        enabled_tools=enabled_tools,
        disabled_tools=disabled_tools,
        http_headers=http_headers,
        env_http_headers=env_http_headers,
        bearer_token_env_var=_optional_string(item.get("bearer_token_env_var")),
    )


def _string_map(value: object, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a mapping.")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{field_name} keys must be strings.")
        result[key] = str(item)
    return result


def _optional_string_tuple(value: object, field_name: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    return _string_tuple(value, field_name)


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings.")
    return tuple(value)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_CONFIG_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_config_vars(value: str, *, settings: Settings) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        env_value = os.getenv(name)
        if env_value is not None:
            return env_value
        if name == "JARVIS_TUSHARE_TOKEN" and settings.tushare_token:
            return settings.tushare_token
        return match.group(0)

    return os.path.expandvars(_CONFIG_VAR_PATTERN.sub(replace, value))


def _is_tushare_server(config: McpServerConfig) -> bool:
    text = f"{config.name} {config.url}".lower()
    return "tushare" in text


def _positive_float(value: object, *, default: float) -> float:
    if value is None:
        return default
    parsed = float(value)
    if parsed <= 0:
        raise ValueError("timeout values must be positive.")
    return parsed
