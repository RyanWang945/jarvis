from __future__ import annotations

import json

from app.channels.feishu_progress import FeishuProgressSink, ProgressSnapshot
from app.channels.feishu_renderer import FeishuRenderer
from app.progress import ProgressEvent


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_feishu_renderer_renders_progress_card() -> None:
    renderer = FeishuRenderer(title="Jarvis")
    snapshot = ProgressSnapshot(
        current_stage="执行节点",
        current_action="开始执行 main (react)",
        completed_items=["生成执行计划"],
        recent_events=["开始规划任务", "开始执行 main (react)"],
        node_total=2,
        node_completed=1,
    )

    delivery = renderer.render_progress_card(snapshot)

    assert delivery.msg_type == "interactive"
    card = json.loads(delivery.content)
    content = "\n".join(element["text"]["content"] for element in card["elements"] if "text" in element)
    assert "Jarvis 正在处理" in content
    assert "**当前阶段**: 执行节点" in content
    assert "**节点进度**: 1/2" in content
    assert "生成执行计划" in content


def test_feishu_progress_sink_merges_and_throttles_updates() -> None:
    clock = Clock()
    renderer = FeishuRenderer(title="Jarvis")
    updates: list[str] = []
    sink = FeishuProgressSink(
        message_id="om_progress",
        renderer=renderer,
        update_card=lambda message_id, delivery: updates.append(message_id),
        min_interval_seconds=2.0,
        clock=clock,
    )

    sink.on_progress(ProgressEvent("planning_started", summary="正在生成执行计划"))
    sink.on_progress(ProgressEvent("plan_created", summary="已生成 2 个执行节点", data={"node_count": 2}))
    clock.advance(2.1)
    sink.on_progress(ProgressEvent("node_started", node_id="main", data={"runtime": "react"}))
    sink.close()

    assert updates == ["om_progress", "om_progress"]
    assert sink.snapshot.node_total == 2
    assert sink.snapshot.current_stage == "执行节点"


def test_feishu_progress_sink_forces_failed_update() -> None:
    clock = Clock()
    renderer = FeishuRenderer(title="Jarvis")
    updates: list[str] = []
    sink = FeishuProgressSink(
        message_id="om_progress",
        renderer=renderer,
        update_card=lambda message_id, delivery: updates.append(message_id),
        min_interval_seconds=60.0,
        clock=clock,
    )

    sink.on_progress(ProgressEvent("planning_started", summary="正在生成执行计划"))
    sink.on_progress(ProgressEvent("node_failed", node_id="main", status="failed", summary="节点失败"))

    assert updates == ["om_progress", "om_progress"]
    assert sink.snapshot.status == "failed"
