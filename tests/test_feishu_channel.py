import json

from app.agent_react import ChannelMessage
from app.channels.feishu import FeishuChannel
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
    assert card["header"]["title"]["content"] == "Jarvis"
    assert card["elements"][0]["tag"] == "div"
    assert card["elements"][0]["text"]["tag"] == "lark_md"
    assert card["elements"][0]["text"]["content"].startswith("**Title**")
    assert "- one" in card["elements"][0]["text"]["content"]
    assert "```python" in card["elements"][0]["text"]["content"]


def test_feishu_renderer_adapts_commonmark_to_feishu_friendly_markdown() -> None:
    renderer = FeishuRenderer(title="Jarvis")

    delivery = renderer.render(
        ChannelMessage(
            content=(
                "# Qing Yu Nian\n\n"
                "## Story Outline\n\n"
                "- Background\n"
                "- Main plot\n\n"
                "### Highlights\n\n"
                "1. Power struggle\n"
                "2. Sci-fi element\n\n"
                "> A well-blended story"
            ),
            content_type="markdown",
        )
    )

    content = json.loads(delivery.content)["elements"][0]["text"]["content"]
    assert "**Qing Yu Nian**" in content
    assert "**Story Outline**" in content
    assert "**Highlights**" in content
    assert "- Background" in content
    assert "1. Power struggle" in content
    assert "**引文**" in content
    assert "A well-blended story" in content


def test_feishu_renderer_downgrades_oversized_markdown() -> None:
    renderer = FeishuRenderer(title="Jarvis")
    huge_markdown = "\n\n".join(f"## Section {i}\n" + ("x" * 3600) for i in range(20))

    delivery = renderer.render(ChannelMessage(content=huge_markdown, content_type="markdown"))

    assert delivery.msg_type == "text"
    assert "Section 0" in json.loads(delivery.content)["text"]


def test_feishu_renderer_adapts_markdown_table_for_message_page() -> None:
    renderer = FeishuRenderer(title="Jarvis")

    delivery = renderer.render(
        ChannelMessage(
            content=(
                "## Characters\n\n"
                "| Name | Role | Skill |\n"
                "| --- | --- | --- |\n"
                "| Fan Xian | Lead | Strategy |\n"
                "| Wu Zhu | Guardian | Combat |\n"
            ),
            content_type="markdown",
        )
    )

    content = json.loads(delivery.content)["elements"][0]["text"]["content"]
    assert "**Characters**" in content
    assert "**Table**" in content
    assert "- **Name**: Fan Xian | **Role**: Lead | **Skill**: Strategy" in content
    assert "- **Name**: Wu Zhu | **Role**: Guardian | **Skill**: Combat" in content


def test_feishu_channel_retries_text_fallback_when_interactive_fails(monkeypatch) -> None:
    channel = FeishuChannel(app_id="app", app_secret="secret")
    attempts: list[str] = []

    def fake_send(receive_id: str, delivery) -> None:
        attempts.append(delivery.msg_type)
        if delivery.msg_type == "interactive":
            raise RuntimeError("interactive failed")

    monkeypatch.setattr(channel, "_send_delivery", fake_send)

    channel._send_channel_message(
        "chat_1",
        ChannelMessage(content="# Title\n\nhello", content_type="markdown"),
    )

    assert attempts == ["interactive", "text"]
