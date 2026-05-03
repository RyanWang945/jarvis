from app.llm.client import parse_json_content


def test_parse_json_content_uses_first_json_object() -> None:
    parsed = parse_json_content(
        {
            "content": (
                '{"turn_type":"chat","confidence":0.8}\n'
                '{"extra":"provider accidentally emitted more data"}'
            )
        }
    )

    assert parsed == {"turn_type": "chat", "confidence": 0.8}
