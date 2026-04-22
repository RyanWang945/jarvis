import logging
from typing import Any

from app.skills.base import SkillRequest, SkillResult

logger = logging.getLogger(__name__)

# Populated by app.channels.feishu when the channel starts.
_active_channel: Any = None


def set_active_channel(channel: Any) -> None:
    global _active_channel
    _active_channel = channel


class FeishuMessageSkill:
    """通过飞书 OpenAPI 向指定用户或群组发送文本消息。"""

    name = "feishu_message"

    def run(self, request: SkillRequest) -> SkillResult:
        if _active_channel is None:
            return SkillResult(
                ok=False,
                exit_code=1,
                stderr="Feishu channel is not active.",
                summary="飞书通道未启动，无法发送消息。",
            )

        receive_id = request.args.get("receive_id")
        text = request.args.get("text")
        if not receive_id or not text:
            return SkillResult(
                ok=False,
                exit_code=1,
                stderr="Missing receive_id or text argument.",
                summary="发送飞书消息失败：缺少 receive_id 或 text 参数。",
            )

        ok = _active_channel.send_message(receive_id, text)
        if ok:
            return SkillResult(
                ok=True,
                exit_code=0,
                stdout="",
                summary=f"已向 {receive_id} 发送飞书消息。",
            )
        return SkillResult(
            ok=False,
            exit_code=1,
            stderr="send_message returned False.",
            summary="发送飞书消息失败（API 调用返回错误）。",
        )
