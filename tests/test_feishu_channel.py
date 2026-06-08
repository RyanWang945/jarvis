import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent_react import ChannelAttachment, ChannelMessage, TurnResult
from app.channels.feishu import (
    FeishuChannel,
    _ensure_feishu_no_proxy,
    _extract_codex_approval_from_reply,
    _extract_message_id,
)
from app.channels.feishu_renderer import FeishuRenderer
from app.config import get_settings
from app.task_runtime.coder_provider import CoderApprovalContinuationResult


@pytest.fixture(autouse=True)
def reset_feishu_progress_settings(monkeypatch):
    monkeypatch.setenv("JARVIS_FEISHU_PROGRESS_UPDATES_ENABLED", "false")
    monkeypatch.setenv("JARVIS_FEISHU_PROGRESS_MODE", "patch")
    monkeypatch.delenv("JARVIS_FEISHU_PROGRESS_MIN_INTERVAL_SECONDS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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


def test_feishu_no_proxy_includes_ws_hosts(monkeypatch) -> None:
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")

    _ensure_feishu_no_proxy()

    hosts = {item.strip() for item in os.environ["NO_PROXY"].split(",")}
    assert "localhost" in hosts
    assert "127.0.0.1" in hosts
    assert "open.feishu.cn" in hosts
    assert "msg-frontier.feishu.cn" in hosts
    assert ".feishu.cn" in hosts


def test_feishu_renderer_renders_codex_approval_buttons() -> None:
    renderer = FeishuRenderer(title="Jarvis")

    delivery = renderer.render_approval_card(
        approval_id="approval_1",
        conversation_id=7,
        turn_id=42,
        chat_id="chat_1",
        command='"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command \'git push origin HEAD\'',
        reason="Do you want to allow pushing the new README commit to the origin remote?",
    )

    assert delivery.msg_type == "interactive"
    card = json.loads(delivery.content)
    content = "\n".join(element["text"]["content"] for element in card["elements"] if "text" in element)
    assert "**Codex 权限审批**" in content
    assert "```" not in content
    assert card["elements"][2]["text"]["tag"] == "plain_text"
    assert "git push origin HEAD" in card["elements"][2]["text"]["content"]
    assert "是否允许将新的提交推送到 origin 远程仓库？" in content
    assert "该操作需要确认后继续。" in content
    action = card["elements"][-1]
    assert action["tag"] == "action"
    buttons = action["actions"]
    assert buttons[0]["text"]["content"] == "同意"
    approve_behavior = buttons[0]["behaviors"][0]
    assert approve_behavior["type"] == "callback"
    assert approve_behavior["value"]["decision"] == "approve"
    assert approve_behavior["value"]["conversation_id"] == 7
    assert approve_behavior["value"]["turn_id"] == 42
    assert approve_behavior["value"]["chat_id"] == "chat_1"
    assert approve_behavior["value"]["language"] == "zh"
    assert buttons[1]["text"]["content"] == "拒绝"
    reject_behavior = buttons[1]["behaviors"][0]
    assert reject_behavior["type"] == "callback"
    assert reject_behavior["value"]["decision"] == "reject"


def test_feishu_renderer_keeps_english_approval_reason_for_english_user() -> None:
    renderer = FeishuRenderer(title="Jarvis")

    delivery = renderer.render_approval_card(
        approval_id="approval_1",
        conversation_id=7,
        turn_id=42,
        command="git push origin main",
        reason="Do you want to allow pushing the new README commit to the origin remote?",
        language="en",
    )

    content = "\n".join(element["text"]["content"] for element in json.loads(delivery.content)["elements"] if "text" in element)
    assert "Do you want to allow pushing the new README commit to the origin remote?" in content
    assert "是否允许" not in content


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


def test_feishu_renderer_moves_model_usage_footer_to_note() -> None:
    renderer = FeishuRenderer(title="Jarvis")

    delivery = renderer.render(
        ChannelMessage(
            content=(
                "这是最终回复。\n\n"
                "---\n"
                "- 模型：`deepseek-v4-flash`\n"
                "- Token：输入 `4553` / 输出 `618` / 合计 `5171`"
            ),
            content_type="markdown",
        )
    )

    card = json.loads(delivery.content)
    body_content = "\n".join(
        element["text"]["content"]
        for element in card["elements"]
        if element.get("tag") == "div" and "text" in element
    )
    assert "- 模型：" not in body_content
    assert "- Token：" not in body_content
    assert card["elements"][-2] == {"tag": "hr"}
    assert card["elements"][-1]["tag"] == "note"
    assert card["elements"][-1]["elements"] == [
        {
            "tag": "plain_text",
            "content": "模型：deepseek-v4-flash · Token：输入 4553 / 输出 618 / 合计 5171",
        }
    ]


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


def test_feishu_channel_updates_error_card_for_failed_turn_result(monkeypatch) -> None:
    channel = FeishuChannel(app_id="app", app_secret="secret")
    updated: list[tuple[str, str, str]] = []
    drained: list[tuple[int, str]] = []

    class FakeRuntime:
        def run_turn(self, turn_id: int) -> TurnResult:
            return TurnResult(
                turn_id=turn_id,
                conversation_id=7,
                status="failed",
                message=ChannelMessage(content="", content_type="markdown"),
            )

    def fake_update(message_id: str, delivery) -> None:
        card = json.loads(delivery.content)
        content = "\n".join(element["text"]["content"] for element in card["elements"] if "text" in element)
        updated.append((message_id, delivery.msg_type, content))

    monkeypatch.setattr("app.channels.feishu.get_agent_runtime", lambda: FakeRuntime())
    monkeypatch.setattr(channel, "_send_thinking_card", lambda chat_id, text: "om_thinking")
    monkeypatch.setattr(channel, "_update_card_message", fake_update)
    monkeypatch.setattr(channel, "_submit_next_queued_turn", lambda conversation_id, chat_id: drained.append((conversation_id, chat_id)))

    channel._handle_agent_run("ou_1", "chat_1", "dm", "触发超时", 7, 42)

    assert len(updated) == 1
    assert updated[0][0] == "om_thinking"
    assert updated[0][1] == "interactive"
    assert "**❌ Request Failed**" in updated[0][2]
    assert "调用模型时出错" in updated[0][2]
    assert drained == [(7, "chat_1")]


def test_feishu_channel_injects_progress_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_FEISHU_PROGRESS_UPDATES_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FEISHU_PROGRESS_MIN_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    channel = FeishuChannel(app_id="app", app_secret="secret")
    updated: list[tuple[str, str, str]] = []

    class FakeRuntime:
        def run_turn(self, turn_id: int, *, progress) -> TurnResult:
            progress.emit("planning_started", turn_id=turn_id, summary="正在生成执行计划")
            return TurnResult(
                turn_id=turn_id,
                conversation_id=7,
                status="completed",
                message=ChannelMessage(content="# Final\n\nDone", content_type="markdown"),
            )

    def fake_update(message_id: str, delivery) -> None:
        card = json.loads(delivery.content)
        content = "\n".join(element["text"]["content"] for element in card["elements"] if "text" in element)
        updated.append((message_id, delivery.msg_type, content))

    monkeypatch.setattr("app.channels.feishu.get_agent_runtime", lambda: FakeRuntime())
    monkeypatch.setattr(channel, "_send_thinking_card", lambda chat_id, text: "om_thinking")
    monkeypatch.setattr(channel, "_update_card_message", fake_update)
    monkeypatch.setattr(channel, "_submit_next_queued_turn", lambda conversation_id, chat_id: None)

    try:
        channel._handle_agent_run("ou_1", "chat_1", "dm", "查资料", 7, 42)
    finally:
        get_settings.cache_clear()

    assert updated[0][0] == "om_thinking"
    assert "Jarvis 正在处理" in updated[0][2]
    assert "正在生成执行计划" in updated[0][2]
    assert updated[-1][0] == "om_thinking"
    assert "**✅ Completed**" in updated[-1][2]


def test_feishu_channel_does_not_send_progress_entry_for_fast_reply(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_FEISHU_PROGRESS_UPDATES_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FEISHU_PROGRESS_MIN_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    channel = FeishuChannel(app_id="app", app_secret="secret")
    sent: list[tuple[str, str, str]] = []
    updated: list[tuple[str, str]] = []

    class FakeRuntime:
        def run_turn(self, turn_id: int, *, progress) -> TurnResult:
            progress.emit("turn_started", turn_id=turn_id, summary="开始处理用户请求")
            progress.emit("turn_completed", turn_id=turn_id, summary="已生成直接回复")
            return TurnResult(
                turn_id=turn_id,
                conversation_id=7,
                status="completed",
                message=ChannelMessage(content="数学有时难，但能练会。", content_type="markdown"),
            )

    def fake_send(receive_id: str, delivery) -> dict:
        sent.append((receive_id, delivery.msg_type, delivery.content))
        return {"code": 0, "data": {"message_id": f"om_{len(sent)}"}}

    monkeypatch.setattr("app.channels.feishu.get_agent_runtime", lambda: FakeRuntime())
    monkeypatch.setattr(channel, "_send_delivery", fake_send)
    monkeypatch.setattr(channel, "_update_card_message", lambda message_id, delivery: updated.append((message_id, delivery.msg_type)))
    monkeypatch.setattr(channel, "_submit_next_queued_turn", lambda conversation_id, chat_id: None)

    try:
        channel._handle_agent_run("ou_1", "chat_1", "dm", "你觉得数学难吗", 7, 42)
    finally:
        get_settings.cache_clear()

    assert len(sent) == 1
    assert "数学有时难，但能练会。" in sent[0][2]
    assert "Jarvis Thinking" not in sent[0][2]
    assert updated == []


def test_feishu_channel_uses_cardkit_progress_mode(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_FEISHU_PROGRESS_UPDATES_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FEISHU_PROGRESS_MODE", "cardkit")
    monkeypatch.setenv("JARVIS_FEISHU_PROGRESS_MIN_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    channel = FeishuChannel(app_id="app", app_secret="secret")
    sent: list[tuple[str, dict]] = []
    updated: list[tuple[str, dict]] = []

    class FakeRuntime:
        def run_turn(self, turn_id: int, *, progress) -> TurnResult:
            progress.emit("planning_started", turn_id=turn_id, summary="正在生成执行计划")
            return TurnResult(
                turn_id=turn_id,
                conversation_id=7,
                status="completed",
                message=ChannelMessage(content="# Final\n\nDone", content_type="markdown"),
            )

    def fake_send(receive_id: str, delivery) -> dict:
        sent.append((receive_id, json.loads(delivery.content)))
        return {"code": 0, "data": {"message_id": "om_cardkit"}}

    def fake_update(message_id: str, delivery) -> None:
        updated.append((message_id, json.loads(delivery.content)))

    monkeypatch.setattr("app.channels.feishu.get_agent_runtime", lambda: FakeRuntime())
    monkeypatch.setattr(channel, "_send_delivery", fake_send)
    monkeypatch.setattr(channel, "_update_card_message", fake_update)
    monkeypatch.setattr(channel, "_submit_next_queued_turn", lambda conversation_id, chat_id: None)

    try:
        channel._handle_agent_run("ou_1", "chat_1", "dm", "查资料", 7, 42)
    finally:
        get_settings.cache_clear()

    assert sent[0][1]["schema"] == "2.0"
    assert "subtitle" not in sent[0][1]["header"]
    assert updated[0][0] == "om_cardkit"
    assert updated[0][1]["schema"] == "2.0"
    assert updated[-1][1]["schema"] == "2.0"
    final_content = json.dumps(updated[-1][1], ensure_ascii=False)
    assert "Final" in final_content
    assert "Done" in final_content


def test_feishu_channel_cardkit_progress_send_falls_back_to_thinking_card(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_FEISHU_PROGRESS_UPDATES_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FEISHU_PROGRESS_MODE", "cardkit")
    get_settings.cache_clear()
    channel = FeishuChannel(app_id="app", app_secret="secret")
    sent: list[dict] = []

    def fake_send(receive_id: str, delivery) -> dict:
        payload = json.loads(delivery.content)
        sent.append(payload)
        if payload.get("schema") == "2.0":
            raise RuntimeError("cardkit failed")
        return {"code": 0, "data": {"message_id": "om_thinking"}}

    monkeypatch.setattr(channel, "_send_delivery", fake_send)

    try:
        message_id = channel._send_progress_entry_card("chat_1", "查资料")
    finally:
        get_settings.cache_clear()

    assert message_id == "om_thinking"
    assert sent[0]["schema"] == "2.0"
    assert sent[1]["elements"][0]["text"]["content"] == "**🟡 Jarvis Thinking**"


def test_feishu_channel_keeps_old_runtime_signature_with_progress_enabled(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_FEISHU_PROGRESS_UPDATES_ENABLED", "true")
    get_settings.cache_clear()
    channel = FeishuChannel(app_id="app", app_secret="secret")
    updated: list[tuple[str, str]] = []

    class FakeRuntime:
        def run_turn(self, turn_id: int) -> TurnResult:
            return TurnResult(
                turn_id=turn_id,
                conversation_id=7,
                status="completed",
                message=ChannelMessage(content="# Final\n\nDone", content_type="markdown"),
            )

    monkeypatch.setattr("app.channels.feishu.get_agent_runtime", lambda: FakeRuntime())
    monkeypatch.setattr(channel, "_send_thinking_card", lambda chat_id, text: "om_thinking")
    monkeypatch.setattr(channel, "_update_card_message", lambda message_id, delivery: updated.append((message_id, delivery.msg_type)))
    monkeypatch.setattr(channel, "_submit_next_queued_turn", lambda conversation_id, chat_id: None)

    try:
        channel._handle_agent_run("ou_1", "chat_1", "dm", "查资料", 7, 42)
    finally:
        get_settings.cache_clear()

    assert updated[-1] == ("om_thinking", "interactive")


def test_feishu_channel_sends_image_attachments_once(monkeypatch) -> None:
    channel = FeishuChannel(app_id="app", app_secret="secret")
    image_dir = Path(".pytest_tmp_feishu_attachments")
    image_dir.mkdir(exist_ok=True)
    image_path = image_dir / "diagram.png"
    image_path.write_bytes(b"image")
    sent: list[tuple[str, str, str]] = []
    uploads: list[str] = []

    def fake_send(receive_id: str, delivery) -> dict:
        sent.append((receive_id, delivery.msg_type, delivery.content))
        return {"code": 0, "data": {"message_id": f"om_{delivery.msg_type}_{len(sent)}"}}

    def fake_upload(attachment: ChannelAttachment) -> str:
        uploads.append(attachment.path)
        return "img_key_1"

    monkeypatch.setattr(channel, "_send_delivery", fake_send)
    monkeypatch.setattr(channel, "_upload_image", fake_upload)

    message = ChannelMessage(
        content="# Final\n\nDone",
        content_type="markdown",
        attachments=(
            ChannelAttachment(
                artifact_id="turn:call:image",
                kind="image",
                path=str(image_path),
                mime_type="image/png",
                filename="diagram.png",
                size_bytes=image_path.stat().st_size,
                source_tool="delegate_to_codex",
            ),
        ),
    )

    try:
        channel._send_channel_message("chat_1", message)
        channel._send_message_attachments("chat_1", message)
    finally:
        try:
            image_path.unlink()
            image_dir.rmdir()
        except OSError:
            pass

    assert uploads == [str(image_path)]
    assert [item[1] for item in sent] == ["interactive", "image"]
    assert json.loads(sent[1][2]) == {"image_key": "img_key_1"}


def test_feishu_channel_sends_attachments_when_codex_requests_approval(monkeypatch) -> None:
    channel = FeishuChannel(app_id="app", app_secret="secret")
    updated: list[tuple[str, str]] = []
    sent_attachments: list[tuple[str, tuple[ChannelAttachment, ...]]] = []
    metadata_patches: list[tuple[int, dict]] = []
    attachment = ChannelAttachment(
        artifact_id="turn:call:svg:preview:png",
        kind="image",
        path="data/artifact_previews/preview.png",
        mime_type="image/png",
        filename="preview.png",
        size_bytes=100,
        source_tool="delegate_to_codex",
    )

    class FakeRuntime:
        def run_turn(self, turn_id: int) -> TurnResult:
            return TurnResult(
                turn_id=turn_id,
                conversation_id=7,
                status="completed",
                message=ChannelMessage(
                    content=(
                        "Codex requested approval (item/commandExecution/requestApproval).\n"
                        "Approval ID: approval_1\n"
                        "Command: Remove-Item tmp\n"
                        "Reason: Cleanup temp file."
                    ),
                    content_type="markdown",
                    attachments=(attachment,),
                ),
            )

    class FakeStore:
        def update_conversation_metadata(self, conversation_id: int, patch: dict) -> None:
            metadata_patches.append((conversation_id, patch))

    monkeypatch.setattr("app.channels.feishu.get_agent_runtime", lambda: FakeRuntime())
    monkeypatch.setattr("app.channels.feishu.get_conversation_store", lambda: FakeStore())
    monkeypatch.setattr(channel, "_send_thinking_card", lambda chat_id, text: "om_thinking")
    monkeypatch.setattr(channel, "_update_card_message", lambda message_id, delivery: updated.append((message_id, delivery.msg_type)))
    monkeypatch.setattr(
        channel,
        "_send_message_attachments",
        lambda chat_id, message: sent_attachments.append((chat_id, message.attachments)),
    )

    channel._handle_agent_run("ou_1", "chat_1", "dm", "生成 svg 图", 7, 42)

    assert updated == [("om_thinking", "interactive")]
    assert sent_attachments == [("chat_1", (attachment,))]
    assert metadata_patches[0][1]["codex_approvals"]["approval_1"]["status"] == "pending"


def test_feishu_image_upload_error_includes_response_payload(monkeypatch) -> None:
    channel = FeishuChannel(app_id="app", app_secret="secret")
    image_dir = Path(".pytest_tmp_feishu_upload_error_case")
    image_dir.mkdir(exist_ok=True)
    image_path = image_dir / "preview.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\npreview")

    class FakeResponse:
        status_code = 400
        text = '{"code":99991672,"msg":"Access denied."}'
        headers = {"x-tt-logid": "log_1"}

        def json(self):
            return {"code": 99991672, "msg": "Access denied."}

    class FakeHttp:
        def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(channel, "_ensure_token", lambda: "token")
    channel._http = FakeHttp()

    try:
        channel._upload_image(
            ChannelAttachment(
                artifact_id="artifact_1",
                kind="image",
                path=str(image_path),
                mime_type="image/png",
                filename="preview.png",
                size_bytes=image_path.stat().st_size,
                source_tool="delegate_to_codex",
            )
        )
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected upload failure")
    finally:
        try:
            image_path.unlink()
            image_dir.rmdir()
        except OSError:
            pass

    assert "status=400" in message
    assert "log_1" in message
    assert "99991672" in message
    assert "Access denied" in message


def test_feishu_card_action_updates_approval_card(monkeypatch) -> None:
    channel = FeishuChannel(app_id="app", app_secret="secret")
    updated: list[tuple[str, str, str]] = []
    metadata_patches: list[tuple[int, dict]] = []

    class FakeStore:
        def get_conversation(self, conversation_id: int):
            return SimpleNamespace(id=conversation_id, metadata={"codex_approvals": {"approval_1": {"status": "pending"}}})

        def update_conversation_metadata(self, conversation_id: int, patch: dict) -> None:
            metadata_patches.append((conversation_id, patch))

    def fake_update(message_id: str, delivery) -> None:
        card = json.loads(delivery.content)
        updated.append((message_id, delivery.msg_type, card["elements"][0]["text"]["content"]))

    monkeypatch.setattr("app.channels.feishu.get_conversation_store", lambda: FakeStore())
    monkeypatch.setattr(channel, "_update_card_message", fake_update)
    monkeypatch.setattr(channel._executor, "submit", lambda fn, *args: None)
    payload = SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id="ou_1"),
            action=SimpleNamespace(
                value={
                    "source": "jarvis_codex_approval",
                    "decision": "approve",
                    "conversation_id": 7,
                    "turn_id": 42,
                    "approval_id": "approval_1",
                    "command": "uv add httpx",
                    "reason": "Install dependency.",
                }
            ),
            context=SimpleNamespace(open_message_id="om_approval"),
        )
    )

    response = channel._on_card_action(payload)

    assert response.toast.content == "已同意 Codex 审批请求。"
    assert response.card.type == "raw"
    assert response.card.data["elements"][0]["text"]["content"] == "**Codex 权限审批：已同意**"
    assert all(element.get("tag") != "action" for element in response.card.data["elements"])
    assert updated == [("om_approval", "interactive", "**Codex 权限审批：已同意**")]
    assert metadata_patches[0][0] == 7
    assert metadata_patches[0][1]["codex_approvals"]["approval_1"]["status"] == "approved"


def test_feishu_card_action_refreshes_already_processed_card(monkeypatch) -> None:
    channel = FeishuChannel(app_id="app", app_secret="secret")
    updated: list[tuple[str, str, str]] = []

    class FakeStore:
        def get_conversation(self, conversation_id: int):
            return SimpleNamespace(id=conversation_id, metadata={"codex_approvals": {"approval_1": {"status": "approved"}}})

    def fake_update(message_id: str, delivery) -> None:
        card = json.loads(delivery.content)
        updated.append((message_id, delivery.msg_type, card["elements"][0]["text"]["content"]))
        assert all(element.get("tag") != "action" for element in card["elements"])

    monkeypatch.setattr("app.channels.feishu.get_conversation_store", lambda: FakeStore())
    monkeypatch.setattr(channel, "_update_card_message", fake_update)
    payload = SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id="ou_1"),
            action=SimpleNamespace(
                value={
                    "source": "jarvis_codex_approval",
                    "decision": "approve",
                    "conversation_id": 7,
                    "turn_id": 42,
                    "approval_id": "approval_1",
                    "command": "git push origin main",
                    "reason": "Push changes.",
                }
            ),
            context=SimpleNamespace(open_message_id="om_approval"),
        )
    )

    response = channel._on_card_action(payload)

    assert response.toast.content == "该审批已处理。"
    assert response.card.type == "raw"
    assert response.card.data["elements"][0]["text"]["content"] == "**Codex 权限审批：已同意**"
    assert all(element.get("tag") != "action" for element in response.card.data["elements"])
    assert updated == [("om_approval", "interactive", "**Codex 权限审批：已同意**")]


def test_feishu_ws_card_payload_routes_legacy_card_action(monkeypatch) -> None:
    channel = FeishuChannel(app_id="app", app_secret="secret")
    updated: list[tuple[str, str, str]] = []
    metadata_patches: list[tuple[int, dict]] = []

    class FakeStore:
        def get_conversation(self, conversation_id: int):
            return SimpleNamespace(id=conversation_id, metadata={"codex_approvals": {"approval_1": {"status": "pending"}}})

        def update_conversation_metadata(self, conversation_id: int, patch: dict) -> None:
            metadata_patches.append((conversation_id, patch))

    def fake_update(message_id: str, delivery) -> None:
        card = json.loads(delivery.content)
        updated.append((message_id, delivery.msg_type, card["elements"][0]["text"]["content"]))

    monkeypatch.setattr("app.channels.feishu.get_conversation_store", lambda: FakeStore())
    monkeypatch.setattr(channel, "_update_card_message", fake_update)
    monkeypatch.setattr(channel._executor, "submit", lambda fn, *args: None)
    payload = {
        "open_id": "ou_1",
        "open_message_id": "om_approval",
        "open_chat_id": "chat_1",
        "action": {
            "value": {
                "source": "jarvis_codex_approval",
                "decision": "approve",
                "conversation_id": 7,
                "turn_id": 42,
                "approval_id": "approval_1",
                "command": "git add -A",
                "reason": "Stage changes.",
            }
        },
    }

    response = channel._on_ws_card_payload(json.dumps(payload).encode("utf-8"))

    assert response.toast.content == "已同意 Codex 审批请求。"
    assert response.card.type == "raw"
    assert response.card.data["elements"][0]["text"]["content"] == "**Codex 权限审批：已同意**"
    assert all(element.get("tag") != "action" for element in response.card.data["elements"])
    assert updated == [("om_approval", "interactive", "**Codex 权限审批：已同意**")]
    assert metadata_patches[0][1]["codex_approvals"]["approval_1"]["status"] == "approved"


def test_feishu_approval_completion_responds_to_live_codex_session(monkeypatch) -> None:
    channel = FeishuChannel(app_id="app", app_secret="secret")
    sent: list[tuple[str, str, str]] = []
    calls: list[tuple[str, bool]] = []
    metadata_patches: list[tuple[int, dict]] = []

    def fake_resume(approval_id: str, *, approved: bool, timeout_seconds: int, provider: str = "codex", trusted_command_prefixes=None):
        assert provider == "codex"
        calls.append((approval_id, approved))
        return CoderApprovalContinuationResult(status="completed", final_text="Codex finished in-place.")

    class FakeStore:
        def get_conversation(self, conversation_id: int):
            return SimpleNamespace(id=conversation_id, metadata={"codex_approval_prefixes": ["git add"]})

        def update_conversation_metadata(self, conversation_id: int, patch: dict) -> None:
            metadata_patches.append((conversation_id, patch))

    monkeypatch.setattr("app.channels.feishu.get_conversation_store", lambda: FakeStore())
    monkeypatch.setattr("app.channels.feishu.resume_coder_approval", fake_resume)
    monkeypatch.setattr(
        channel,
        "_send_channel_message",
        lambda chat_id, message: sent.append((chat_id, message.content_type, message.content)),
    )

    channel._complete_codex_approval("chat_1", 7, 42, "approval_1", True)

    assert calls == [("approval_1", True)]
    assert sent == [("chat_1", "markdown", "Codex finished in-place.")]
    assert metadata_patches[0][1]["codex_approvals"]["approval_1"]["status"] == "completed"


def test_feishu_approval_completion_sends_new_card_for_next_codex_approval(monkeypatch) -> None:
    channel = FeishuChannel(app_id="app", app_secret="secret")
    sent: list[tuple[str, str, str]] = []
    metadata_patches: list[tuple[int, dict]] = []

    def fake_resume(approval_id: str, *, approved: bool, timeout_seconds: int, provider: str = "codex", trusted_command_prefixes=None):
        assert provider == "codex"
        return CoderApprovalContinuationResult(
            status="approval_requested",
            approval_requests=[
                SimpleNamespace(
                    approval_id="approval_2",
                    command="git commit -m test",
                    reason="Create the requested commit.",
                )
            ],
        )

    class FakeStore:
        def get_conversation(self, conversation_id: int):
            return SimpleNamespace(id=conversation_id, metadata={})

        def update_conversation_metadata(self, conversation_id: int, patch: dict) -> None:
            metadata_patches.append((conversation_id, patch))

    def fake_send(chat_id: str, delivery) -> dict:
        card = json.loads(delivery.content)
        command = card["elements"][2]["text"]["content"]
        sent.append((chat_id, delivery.msg_type, command))
        return {"code": 0, "data": {"message_id": "om_next_approval"}}

    monkeypatch.setattr("app.channels.feishu.get_conversation_store", lambda: FakeStore())
    monkeypatch.setattr("app.channels.feishu.resume_coder_approval", fake_resume)
    monkeypatch.setattr(channel, "_send_delivery", fake_send)

    channel._complete_codex_approval("chat_1", 7, 42, "approval_1", True, "om_approval")

    assert sent == [("chat_1", "interactive", "git commit -m test")]
    assert metadata_patches[0][1]["codex_approvals"]["approval_2"]["status"] == "pending"
    assert metadata_patches[0][1]["codex_approvals"]["approval_2"]["turn_id"] == 42


def test_feishu_approval_completion_reports_expired_codex_session(monkeypatch) -> None:
    channel = FeishuChannel(app_id="app", app_secret="secret")
    sent: list[tuple[str, str]] = []
    metadata_patches: list[tuple[int, dict]] = []

    def fake_resume(approval_id: str, *, approved: bool, timeout_seconds: int, provider: str = "codex", trusted_command_prefixes=None):
        assert provider == "codex"
        return CoderApprovalContinuationResult(
            status="missing",
            error="Codex approval session is no longer active.",
        )

    class FakeStore:
        def get_conversation(self, conversation_id: int):
            return SimpleNamespace(id=conversation_id, metadata={})

        def update_conversation_metadata(self, conversation_id: int, patch: dict) -> None:
            metadata_patches.append((conversation_id, patch))

    monkeypatch.setattr("app.channels.feishu.get_conversation_store", lambda: FakeStore())
    monkeypatch.setattr("app.channels.feishu.resume_coder_approval", fake_resume)
    monkeypatch.setattr(
        channel,
        "_send_text_message",
        lambda chat_id, text: sent.append((chat_id, text)),
    )

    channel._complete_codex_approval("chat_1", 7, 42, "approval_1", True)

    assert sent == [
        (
            "chat_1",
            "Codex 审批会话已失效，通常是 Jarvis 重启或审批卡过期导致。请重新发起任务。",
        )
    ]
    assert metadata_patches[0][1]["codex_approvals"]["approval_1"]["status"] == "missing"


def test_feishu_channel_submits_next_queued_turn(monkeypatch) -> None:
    channel = FeishuChannel(app_id="app", app_secret="secret")
    submitted: list[tuple[object, tuple]] = []

    class FakeStore:
        def claim_next_queued_turn(self, conversation_id: int):
            assert conversation_id == 7
            return SimpleNamespace(id=43, trigger_message_id=3)

        def list_messages(self, conversation_id: int):
            assert conversation_id == 7
            return [SimpleNamespace(id=3, content="second task")]

        def get_conversation(self, conversation_id: int):
            assert conversation_id == 7
            return SimpleNamespace(chat_type="dm")

    monkeypatch.setattr("app.channels.feishu.get_conversation_store", lambda: FakeStore())
    monkeypatch.setattr(channel._executor, "submit", lambda fn, *args: submitted.append((fn, args)))

    channel._submit_next_queued_turn(7, "chat_1")

    assert submitted == [
        (
            channel._handle_agent_run,
            ("queued", "chat_1", "dm", "second task", 7, 43),
        )
    ]


def test_extract_codex_approval_from_reply() -> None:
    approval = _extract_codex_approval_from_reply(
        "Codex requested approval (exec-approval).\n"
        "Approval ID: approval_1\n"
        "Command: uv add httpx\n"
        "Reason: Install dependency.\n"
        "Approve this request, reject it, or ask Jarvis to continue with a safer alternative."
    )

    assert approval == {
        "approval_id": "approval_1",
        "command": "uv add httpx",
        "reason": "Install dependency.",
    }


def test_extract_message_id_reads_feishu_send_payload() -> None:
    assert _extract_message_id({"data": {"message_id": "om_123"}}) == "om_123"
    assert _extract_message_id({"data": {}}) is None
