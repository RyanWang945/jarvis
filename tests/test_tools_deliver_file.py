from pathlib import Path
from uuid import uuid4

from app.agent_react.delivery import register_delivery_handler
from app.api.agent import InMemoryConversationStore
from app.tools.common import ToolArtifact, ToolExecutionRequest
from app.tools.deliver_file import run_deliver_file


def test_deliver_file_sends_artifact_once_with_delivery_manager(monkeypatch) -> None:
    store = InMemoryConversationStore()
    artifact_dir = Path("data") / "artifact_previews" / f"deliver-file-{uuid4().hex}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    image_path = artifact_dir / "diagram.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    artifact = ToolArtifact(
        artifact_id="artifact:image:1",
        kind="image",
        turn_id=42,
        tool_call_id="call_1",
        path=str(image_path),
        mime_type="image/png",
        filename="diagram.png",
        size_bytes=image_path.stat().st_size,
        source_tool="delegate_to_codex",
    )
    store.upsert_artifact(artifact, conversation_id=7)

    class FakeHandler:
        channel = "feishu"

        def __init__(self) -> None:
            self.uploads = 0
            self.sends = 0

        def upload_attachment(self, attachment):
            self.uploads += 1
            return "image_key_1"

        def send_attachment(self, external_chat_id, attachment, upload_key):
            self.sends += 1
            return f"om_{self.sends}"

        def send_failure_notice(self, external_chat_id, attachment, error_message):
            raise AssertionError(error_message)

    handler = FakeHandler()
    register_delivery_handler(handler)

    monkeypatch.setattr("app.tools.deliver_file._conversation_store", lambda: store)

    try:
        request = ToolExecutionRequest(
            tool_name="deliver_file",
            workdir=None,
            args={
                "artifact_id": artifact.artifact_id,
                "conversation_id": 7,
                "turn_id": 42,
                "platform": "feishu",
                "external_chat_id": "chat_1",
            },
        )
        first = run_deliver_file(request)
        second = run_deliver_file(request)
    finally:
        try:
            image_path.unlink()
            artifact_dir.rmdir()
        except OSError:
            pass

    assert first.ok
    assert second.ok
    assert handler.uploads == 1
    assert handler.sends == 1
    assert store.find_sent_delivery(
        channel="feishu",
        external_chat_id="chat_1",
        artifact_id=artifact.artifact_id,
        purposes=("explicit",),
    ) is not None


def test_deliver_file_accepts_file_path_alias(monkeypatch) -> None:
    store = InMemoryConversationStore()
    artifact_dir = Path("data") / "artifact_previews" / f"deliver-file-alias-{uuid4().hex}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    image_path = artifact_dir / "diagram.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    class FakeHandler:
        channel = "feishu"

        def upload_attachment(self, attachment):
            return "image_key_1"

        def send_attachment(self, external_chat_id, attachment, upload_key):
            return "om_1"

        def send_failure_notice(self, external_chat_id, attachment, error_message):
            raise AssertionError(error_message)

    register_delivery_handler(FakeHandler())

    monkeypatch.setattr("app.tools.deliver_file._conversation_store", lambda: store)

    try:
        result = run_deliver_file(
            ToolExecutionRequest(
                tool_name="deliver_file",
                workdir=None,
                args={
                    "file_path": str(image_path),
                    "conversation_id": 7,
                    "turn_id": 42,
                    "platform": "feishu",
                    "external_chat_id": "chat_1",
                },
            )
        )
    finally:
        try:
            image_path.unlink()
            artifact_dir.rmdir()
        except OSError:
            pass

    assert result.ok
