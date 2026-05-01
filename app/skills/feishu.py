import logging
from typing import Any

from app.skills.base import SkillRequest, SkillResult

logger = logging.getLogger(__name__)

_active_channel: Any = None


def set_active_channel(channel: Any) -> None:
    global _active_channel
    _active_channel = channel


class FeishuMessageSkill:
    name = "feishu_message"

    def run(self, request: SkillRequest) -> SkillResult:
        if _active_channel is None:
            return SkillResult(
                ok=False,
                exit_code=1,
                stderr="Feishu channel is not active.",
                summary="Feishu channel is not active.",
            )

        receive_id = request.args.get("receive_id")
        text = request.args.get("text")
        if not receive_id or not text:
            return SkillResult(
                ok=False,
                exit_code=1,
                stderr="Missing receive_id or text argument.",
                summary="Failed to send Feishu message: missing receive_id or text.",
            )

        ok = _active_channel.send_message(receive_id, text)
        if ok:
            return SkillResult(
                ok=True,
                exit_code=0,
                stdout="",
                summary=f"Sent Feishu message to {receive_id}.",
            )
        return SkillResult(
            ok=False,
            exit_code=1,
            stderr="send_message returned False.",
            summary="Failed to send Feishu message: send_message returned False.",
        )
