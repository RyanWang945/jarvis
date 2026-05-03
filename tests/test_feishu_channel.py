import json

from app.agent_react import ChannelMessage
from app.channels.feishu import FeishuChannel, _extract_message_id
from app.channels.feishu_renderer import FeishuRenderer


def test_feishu_renderer_prefers_interactive_for_markdown() -> None:
    renderer = FeishuRenderer(title="Jarvis")

    delivery = renderer.render(
        ChannelMessage(
            content="# Title\n\n- one\n- two\n\n```python\nprint('hi')\n```",
            content_type="markdown",
        )
    )

    assert delivery.msg_type == "interactive"
    card = json.loads(delivery.content)
    assert card["config"]["update_multi"] is True
    assert card["header"]["title"]["content"] == "Jarvis"
    assert card["elements"][0]["tag"] == "div"
    assert card["elements"][0]["text"]["tag"] == "lark_md"
    assert card["elements"][0]["text"]["content"] == "**✅ Completed**"
    assert card["elements"][1]["text"]["content"].startswith("**Title**")


def test_feishu_renderer_renders_thinking_card() -> None:
    renderer = FeishuRenderer(title="Jarvis")

    delivery = renderer.render_thinking_card("Please summarize the architecture tradeoffs.")

    assert delivery.msg_type == "interactive"
    card = json.loads(delivery.content)
    assert card["config"]["update_multi"] is True
    content = "\n".join(element["text"]["content"] for element in card["elements"])
    assert "**🟡 Jarvis Thinking**" in content
    assert "正在整理问题" in content
    assert "architecture tradeoffs" not in content


def test_feishu_renderer_adapts_commonmark_and_table() -> None:
    renderer = FeishuRenderer(title="Jarvis")

    delivery = renderer.render(
        ChannelMessage(
            content=(
                "# Qing Yu Nian\n\n"
                "## Story Outline\n\n"
                "> A well-blended story\n\n"
                "| Name | Role | Skill |\n"
                "| --- | --- | --- |\n"
                "| Fan Xian | Lead | Strategy |\n"
            ),
            content_type="markdown",
        )
    )

    content = json.loads(delivery.content)["elements"][0]["text"]["content"]
    all_content = "\n".join(element["text"]["content"] for element in json.loads(delivery.content)["elements"])
    assert "**✅ Completed**" in all_content
    assert "**Qing Yu Nian**" in all_content
    assert "**Story Outline**" in all_content
    assert "**Quote**" in all_content
    assert "A well-blended story" in all_content
    assert "**Fan Xian | Lead**" in all_content
    assert "**Skill**: Strategy" in all_content


def test_feishu_renderer_repairs_quad_asterisk_labels() -> None:
    renderer = FeishuRenderer(title="Jarvis")

    delivery = renderer.render(
        ChannelMessage(
            content=(
                "****定价市场 | 全球美元计价\n"
                "固高科技股价: A股人民币计价\n\n"
                "****驱动因素 | 地缘政治、美联储政策、美元走势\n"
                "固高科技股价: 公司业绩、行业景气度、A股资金面\n\n"
                "****资产属性 | 避险资产\n"
                "固高科技股价: 成长型科技股（工业自动化/运动控制）"
            ),
            content_type="markdown",
        )
    )

    all_content = "\n".join(element["text"]["content"] for element in json.loads(delivery.content)["elements"])
    assert "****定价市场" not in all_content
    assert "****驱动因素" not in all_content
    assert "****资产属性" not in all_content
    assert "**定价市场** | 全球美元计价" in all_content
    assert "**驱动因素** | 地缘政治、美联储政策、美元走势" in all_content
    assert "**资产属性** | 避险资产" in all_content


def test_feishu_channel_retries_text_fallback_when_interactive_fails(monkeypatch) -> None:
    channel = FeishuChannel(app_id="app", app_secret="secret")
    attempts: list[str] = []

    def fake_send(receive_id: str, delivery) -> dict:
        attempts.append(delivery.msg_type)
        if delivery.msg_type == "interactive":
            raise RuntimeError("interactive failed")
        return {"code": 0, "data": {"message_id": "om_fallback"}}

    monkeypatch.setattr(channel, "_send_delivery", fake_send)

    channel._send_channel_message(
        "chat_1",
        ChannelMessage(content="# Title\n\nhello", content_type="markdown"),
    )

    assert attempts == ["interactive", "text"]


def test_feishu_channel_updates_thinking_card(monkeypatch) -> None:
    channel = FeishuChannel(app_id="app", app_secret="secret")
    sent: list[tuple[str, str]] = []
    updated: list[tuple[str, str]] = []

    def fake_send(receive_id: str, delivery) -> dict:
        sent.append((receive_id, delivery.msg_type))
        return {"code": 0, "data": {"message_id": "om_thinking"}}

    def fake_update(message_id: str, delivery) -> None:
        updated.append((message_id, delivery.msg_type))

    monkeypatch.setattr(channel, "_send_delivery", fake_send)
    monkeypatch.setattr(channel, "_update_card_message", fake_update)

    thinking_id = channel._send_thinking_card("chat_1", "Draft the answer.")
    channel._update_channel_message(
        thinking_id or "",
        ChannelMessage(content="# Final\n\nDone", content_type="markdown"),
    )

    assert thinking_id == "om_thinking"
    assert sent == [("chat_1", "interactive")]
    assert updated == [("om_thinking", "interactive")]


def test_extract_message_id_reads_feishu_send_payload() -> None:
    assert _extract_message_id({"data": {"message_id": "om_123"}}) == "om_123"
    assert _extract_message_id({"data": {}}) is None
