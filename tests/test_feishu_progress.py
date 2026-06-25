from __future__ import annotations

import json

from app.channels.feishu_progress import FeishuCardKitProgressSink, FeishuProgressSink, ProgressSnapshot
from app.channels.feishu_payload_validator import validate_cardkit_progress_text, validate_feishu_delivery
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


def test_feishu_renderer_renders_cardkit_progress_card() -> None:
    renderer = FeishuRenderer(title="Jarvis")
    snapshot = ProgressSnapshot(
        current_stage="执行节点",
        current_action="正在搜索相关新闻",
        completed_items=["生成执行计划"],
        recent_events=["开始规划任务", "正在搜索相关新闻"],
        node_total=3,
        node_completed=1,
    )

    delivery = renderer.render_cardkit_progress_card(snapshot)

    assert delivery.msg_type == "interactive"
    card = json.loads(delivery.content)
    assert card["schema"] == "2.0"
    assert "subtitle" not in card["header"]
    element_ids = {item.get("element_id") for item in card["body"]["elements"]}
    assert "progress_stream" in element_ids
    assert "progress_steps" not in element_ids
    assert "progress_output" not in element_ids
    content = json.dumps(card, ensure_ascii=False)
    assert "正在搜索相关新闻" in content
    assert "\\t✓" not in content
    assert "输出结果" not in content


def test_feishu_renderer_omits_cardkit_output_placeholder_while_aggregating() -> None:
    renderer = FeishuRenderer(title="Jarvis")
    snapshot = ProgressSnapshot(
        current_stage="汇总",
        current_action="正在汇总执行结果",
        output_started=True,
        status="running",
    )

    delivery = renderer.render_cardkit_progress_card(snapshot)

    card = json.loads(delivery.content)
    element_ids = {item.get("element_id") for item in card["body"]["elements"]}
    content = json.dumps(card, ensure_ascii=False)
    assert "progress_output" not in element_ids
    assert "正在汇总结果" in content
    assert "正在生成结果" not in content


def test_feishu_renderer_renders_cardkit_output_after_started() -> None:
    renderer = FeishuRenderer(title="Jarvis")
    snapshot = ProgressSnapshot(
        completed_items=["生成执行计划"],
        planned_nodes=[{"id": "research_game", "runtime": "react", "objective": "Research game"}],
        completed_node_ids=["research_game"],
        output_started=True,
        status="completed",
    )

    delivery = renderer.render_cardkit_progress_card(snapshot, output_markdown="# Final\n\nDone")

    content = json.dumps(json.loads(delivery.content), ensure_ascii=False)
    assert "任务完成" in content
    assert "\\t✓" not in content
    assert "Final" in content
    assert "Done" in content


def test_feishu_renderer_extracts_cardkit_output_usage_footer() -> None:
    renderer = FeishuRenderer(title="Jarvis")
    snapshot = ProgressSnapshot(
        completed_items=["生成执行计划"],
        planned_nodes=[{"id": "research_game", "runtime": "react", "objective": "Research game"}],
        completed_node_ids=["research_game"],
        output_started=True,
        status="completed",
    )

    delivery = renderer.render_cardkit_progress_card(
        snapshot,
        output_markdown="Final\n\n---\n- Token：输入 `1547` / 输出 `855` / 合计 `2402`",
    )

    card = json.loads(delivery.content)
    elements = card["body"]["elements"]
    output = next(item for item in elements if item.get("element_id") == "progress_output")
    usage = next(item for item in elements if item.get("element_id") == "progress_usage")
    assert output["content"] == "Final"
    assert usage["content"] == "<font color='grey'>**用量：** 2.4k tokens · 输入 1.5k / 输出 855</font>"
    assert usage["text_size"] == "notation"
    assert "`1547`" not in json.dumps(card, ensure_ascii=False)


def test_feishu_renderer_renders_cardkit_usage_from_metadata() -> None:
    renderer = FeishuRenderer(title="Jarvis")
    snapshot = ProgressSnapshot(
        completed_items=["生成执行计划"],
        planned_nodes=[{"id": "research_game", "runtime": "react", "objective": "Research game"}],
        completed_node_ids=["research_game"],
        output_started=True,
        status="completed",
    )

    delivery = renderer.render_cardkit_progress_card(
        snapshot,
        output_markdown="Final",
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )

    card = json.loads(delivery.content)
    elements = card["body"]["elements"]
    output = next(item for item in elements if item.get("element_id") == "progress_output")
    usage = next(item for item in elements if item.get("element_id") == "progress_usage")
    assert output["content"] == "Final"
    assert usage["content"] == "<font color='grey'>**用量：** 15 tokens · 输入 10 / 输出 5</font>"
    assert usage["text_size"] == "notation"


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


def test_feishu_progress_sink_can_skip_close_flush() -> None:
    clock = Clock()
    renderer = FeishuRenderer(title="Jarvis")
    updates: list[str] = []
    sink = FeishuProgressSink(
        message_id="om_progress",
        renderer=renderer,
        update_card=lambda message_id, delivery: updates.append(message_id),
        min_interval_seconds=60.0,
        flush_on_close=False,
        clock=clock,
    )

    sink.on_progress(ProgressEvent("planning_started", summary="正在生成执行计划"))
    sink.on_progress(
        ProgressEvent(
            "plan_created",
            summary="已生成 1 个执行节点",
            data={"node_count": 1, "nodes": [{"id": "research_game", "runtime": "react", "objective": "Research game"}]},
        )
    )
    sink.close()

    assert updates == ["om_progress"]
    assert sink.snapshot.node_total == 1
    assert sink.snapshot.planned_nodes[0]["id"] == "research_game"


def test_feishu_cardkit_progress_sink_uses_cardkit_renderer() -> None:
    clock = Clock()
    renderer = FeishuRenderer(title="Jarvis")
    payloads: list[dict] = []
    sink = FeishuCardKitProgressSink(
        message_id="om_progress",
        renderer=renderer,
        update_card=lambda message_id, delivery: payloads.append(json.loads(delivery.content)),
        min_interval_seconds=0,
        clock=clock,
    )

    sink.on_progress(ProgressEvent("planning_started", summary="正在生成执行计划"))

    assert payloads[0]["schema"] == "2.0"
    assert payloads[0]["body"]["elements"][0]["element_id"] == "progress_stream"
    assert payloads[0]["body"]["elements"][0]["content"] == "生成计划中"


def test_feishu_cardkit_progress_sink_streams_single_line_status() -> None:
    clock = Clock()
    renderer = FeishuRenderer(title="Jarvis")
    statuses: list[str] = []
    sink = FeishuCardKitProgressSink(
        message_id="om_progress",
        renderer=renderer,
        update_card=lambda message_id, delivery: statuses.append(
            json.loads(delivery.content)["body"]["elements"][0]["content"]
        ),
        min_interval_seconds=0,
        clock=clock,
    )

    sink.on_progress(ProgressEvent("planning_started", summary="正在生成执行计划"))
    sink.on_progress(
        ProgressEvent(
            "node_started",
            node_id="gold_price",
            summary="开始执行 react 节点：分析金价",
            data={"runtime": "react"},
        )
    )
    sink.on_progress(ProgressEvent("aggregation_started", summary="正在汇总执行结果"))
    sink.on_progress(ProgressEvent("turn_completed", summary="任务已完成，正在返回结果"))

    assert statuses == ["生成计划中", "正在执行 gold_price 节点", "正在汇总结果", "任务完成"]


def test_feishu_cardkit_progress_sink_forces_node_started_after_plan_update() -> None:
    clock = Clock()
    renderer = FeishuRenderer(title="Jarvis")
    statuses: list[str] = []
    sink = FeishuCardKitProgressSink(
        message_id="om_progress",
        renderer=renderer,
        update_card=lambda message_id, delivery: statuses.append(
            json.loads(delivery.content)["body"]["elements"][0]["content"]
        ),
        min_interval_seconds=60.0,
        clock=clock,
    )

    sink.on_progress(ProgressEvent("planning_started", summary="正在生成执行计划"))
    clock.advance(61.0)
    sink.on_progress(ProgressEvent("plan_created", summary="已生成 1 个执行节点", data={"node_count": 1}))
    sink.on_progress(ProgressEvent("node_started", node_id="research_compare", data={"runtime": "react"}))

    assert statuses == ["生成计划中", "执行计划已生成", "正在执行 research_compare 节点"]


def test_feishu_payload_validator_accepts_basic_cardkit_output() -> None:
    validation = validate_cardkit_progress_text(
        "最终结果\n\n- 第一项\n- 第二项",
        node_id="research_game",
        runtime="react",
    )

    assert validation.ok
    assert validation.msg_type == "interactive"
    assert validation.element_count == 3
    assert validation.content_bytes > 0
    assert any(block["element_id"] == "progress_output" for block in validation.markdown_blocks)


def test_feishu_payload_validator_flags_risky_cardkit_markdown() -> None:
    validation = validate_cardkit_progress_text(
        "可接受 @Claude 委派任务。\n\n---\n- Token：输入 `57524` / 输出 `3434` / 合计 `60958`",
        node_id="research_claude_tag",
        runtime="react",
    )

    codes = {issue.code for issue in validation.issues}
    assert validation.ok
    assert "markdown_contains_raw_at_mention" in codes
    assert "markdown_contains_tab" not in codes


def test_feishu_payload_validator_detects_invalid_json_delivery() -> None:
    delivery = FeishuRenderer(title="Jarvis").render_text_fallback("hello")
    broken = type(delivery)(msg_type=delivery.msg_type, content="{")

    validation = validate_feishu_delivery(broken)

    assert not validation.ok
    assert validation.issues[0].code == "invalid_json"
