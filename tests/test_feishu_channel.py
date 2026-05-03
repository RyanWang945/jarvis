import json
from types import SimpleNamespace

from app.agent_react import ChannelMessage
from app.channels.feishu import FeishuChannel, _extract_message_id, _extract_codex_approval_from_reply
from app.channels.feishu_renderer import FeishuRenderer
from app.tools.codex_app_server import CodexApprovalContinuationResult


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

    def fake_respond(approval_id: str, *, approved: bool, timeout_seconds: int, trusted_command_prefixes=None):
        calls.append((approval_id, approved))
        return CodexApprovalContinuationResult(status="completed", final_text="Codex finished in-place.")

    class FakeStore:
        def get_conversation(self, conversation_id: int):
            return SimpleNamespace(id=conversation_id, metadata={"codex_approval_prefixes": ["git add"]})

        def update_conversation_metadata(self, conversation_id: int, patch: dict) -> None:
            metadata_patches.append((conversation_id, patch))

    monkeypatch.setattr("app.channels.feishu.get_conversation_store", lambda: FakeStore())
    monkeypatch.setattr("app.channels.feishu.respond_to_codex_approval", fake_respond)
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

    def fake_respond(approval_id: str, *, approved: bool, timeout_seconds: int, trusted_command_prefixes=None):
        return CodexApprovalContinuationResult(
            status="approval_requested",
            approval_requests=[
                {
                    "id": "approval_2",
                    "command": "git commit -m test",
                    "reason": "Create the requested commit.",
                }
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
    monkeypatch.setattr("app.channels.feishu.respond_to_codex_approval", fake_respond)
    monkeypatch.setattr(channel, "_send_delivery", fake_send)

    channel._complete_codex_approval("chat_1", 7, 42, "approval_1", True, "om_approval")

    assert sent == [("chat_1", "interactive", "git commit -m test")]
    assert metadata_patches[0][1]["codex_approvals"]["approval_2"]["status"] == "pending"
    assert metadata_patches[0][1]["codex_approvals"]["approval_2"]["turn_id"] == 42


def test_feishu_approval_completion_reports_expired_codex_session(monkeypatch) -> None:
    channel = FeishuChannel(app_id="app", app_secret="secret")
    sent: list[tuple[str, str]] = []
    metadata_patches: list[tuple[int, dict]] = []

    def fake_respond(approval_id: str, *, approved: bool, timeout_seconds: int, trusted_command_prefixes=None):
        return CodexApprovalContinuationResult(
            status="missing",
            error="Codex approval session is no longer active.",
        )

    class FakeStore:
        def get_conversation(self, conversation_id: int):
            return SimpleNamespace(id=conversation_id, metadata={})

        def update_conversation_metadata(self, conversation_id: int, patch: dict) -> None:
            metadata_patches.append((conversation_id, patch))

    monkeypatch.setattr("app.channels.feishu.get_conversation_store", lambda: FakeStore())
    monkeypatch.setattr("app.channels.feishu.respond_to_codex_approval", fake_respond)
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
