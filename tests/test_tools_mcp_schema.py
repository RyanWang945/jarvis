from app.tools.mcp.schema import MAX_TOOL_NAME_LENGTH, mcp_input_schema, qualify_tool_name, sanitize_json_schema


def test_qualify_tool_name_sanitizes_and_hashes_long_names() -> None:
    qualified = qualify_tool_name(
        "server.one",
        "extremely_lengthy_function_name_that_absolutely_surpasses_all_reasonable_limits",
    )

    assert len(qualified) == MAX_TOOL_NAME_LENGTH
    assert qualified.startswith("mcp__server_one__")
    assert all(char.isascii() and (char.isalnum() or char in {"_", "-"}) for char in qualified)


def test_mcp_input_schema_infers_missing_type_and_properties() -> None:
    schema = mcp_input_schema(
        {
            "name": "demo",
            "inputSchema": {
                "properties": {
                    "series_id": {"description": "FRED series id"},
                    "include_metadata": True,
                },
                "required": ["series_id"],
            },
        }
    )

    assert schema["type"] == "object"
    assert schema["properties"]["series_id"]["type"] == "string"
    assert schema["properties"]["include_metadata"] == {"type": "string"}


def test_sanitize_json_schema_adds_array_items() -> None:
    schema = sanitize_json_schema({"type": "array"})

    assert schema == {"type": "array", "items": {"type": "string"}}
