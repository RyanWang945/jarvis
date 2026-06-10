from __future__ import annotations

import copy
import hashlib
from typing import Any

MCP_TOOL_PREFIX = "mcp"
MCP_TOOL_DELIMITER = "__"
MAX_TOOL_NAME_LENGTH = 64


def qualify_tool_name(server_name: str, tool_name: str) -> str:
    raw = f"{MCP_TOOL_PREFIX}{MCP_TOOL_DELIMITER}{server_name}{MCP_TOOL_DELIMITER}{tool_name}"
    qualified = sanitize_tool_name(raw)
    if len(qualified) <= MAX_TOOL_NAME_LENGTH:
        return qualified
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return f"{qualified[: MAX_TOOL_NAME_LENGTH - len(digest)]}{digest}"


def sanitize_tool_name(name: str) -> str:
    sanitized = "".join(char if char.isascii() and (char.isalnum() or char in {"_", "-"}) else "_" for char in name)
    return sanitized or "_"


def mcp_input_schema(tool: dict[str, Any]) -> dict[str, Any]:
    raw_schema = tool.get("inputSchema")
    if raw_schema is None:
        raw_schema = tool.get("input_schema")
    if not isinstance(raw_schema, dict):
        raw_schema = {"type": "object", "properties": {}}
    schema = copy.deepcopy(raw_schema)
    schema = sanitize_json_schema(schema)
    if schema.get("type") != "object":
        schema = {"type": "object", "properties": {"value": schema}}
    schema.setdefault("properties", {})
    return schema


def mcp_tool_description(server_name: str, tool: dict[str, Any]) -> str:
    description = tool.get("description")
    if isinstance(description, str) and description.strip():
        return description.strip()
    title = tool.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    name = str(tool.get("name") or "unknown")
    return f"MCP tool '{name}' from server '{server_name}'."


def sanitize_json_schema(value: Any) -> Any:
    if isinstance(value, bool):
        return {"type": "string"}
    if isinstance(value, list):
        return [sanitize_json_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    properties = value.get("properties")
    if isinstance(properties, dict):
        for key, child in list(properties.items()):
            properties[key] = sanitize_json_schema(child)

    if "items" in value:
        value["items"] = sanitize_json_schema(value["items"])
    for key in ("oneOf", "anyOf", "allOf", "prefixItems"):
        items = value.get(key)
        if isinstance(items, list):
            value[key] = [sanitize_json_schema(item) for item in items]

    schema_type = _schema_type(value)
    value["type"] = schema_type

    if schema_type == "object":
        if not isinstance(value.get("properties"), dict):
            value["properties"] = {}
        additional = value.get("additionalProperties")
        if isinstance(additional, dict):
            value["additionalProperties"] = sanitize_json_schema(additional)
    elif schema_type == "array" and "items" not in value:
        value["items"] = {"type": "string"}
    return value


def _schema_type(schema: dict[str, Any]) -> str:
    raw_type = schema.get("type")
    if isinstance(raw_type, str) and raw_type:
        return raw_type
    if isinstance(raw_type, list):
        for item in raw_type:
            if isinstance(item, str) and item in {"object", "array", "string", "number", "integer", "boolean"}:
                return item
    if any(key in schema for key in ("properties", "required", "additionalProperties")):
        return "object"
    if any(key in schema for key in ("items", "prefixItems")):
        return "array"
    if any(key in schema for key in ("enum", "const", "format")):
        return "string"
    if any(key in schema for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf")):
        return "number"
    return "string"
