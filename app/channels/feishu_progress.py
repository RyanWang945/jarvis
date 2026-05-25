from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.channels.feishu_renderer import FeishuDelivery, FeishuRenderer
from app.progress import ProgressEvent

logger = logging.getLogger(__name__)


@dataclass
class ProgressSnapshot:
    title: str = "Jarvis 正在处理"
    current_stage: str = "准备中"
    current_action: str = "正在理解请求"
    completed_items: list[str] = field(default_factory=list)
    recent_events: list[str] = field(default_factory=list)
    planned_nodes: list[dict[str, str]] = field(default_factory=list)
    completed_node_ids: list[str] = field(default_factory=list)
    node_total: int | None = None
    node_completed: int = 0
    tool_running: str | None = None
    output_started: bool = False
    status: str = "running"
    started_at: float = field(default_factory=time.time)
    updated_at: float = 0.0


class FeishuProgressSink:
    def __init__(
        self,
        *,
        message_id: str,
        renderer: FeishuRenderer,
        update_card: Callable[[str, FeishuDelivery], None],
        min_interval_seconds: float = 2.0,
        max_recent_events: int = 5,
        flush_on_close: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._message_id = message_id
        self._renderer = renderer
        self._update_card = update_card
        self._min_interval_seconds = max(0.0, min_interval_seconds)
        self._max_recent_events = max(1, max_recent_events)
        self._clock = clock
        self._flush_on_close = flush_on_close
        self._snapshot = ProgressSnapshot(started_at=clock())
        self._last_flush_at = 0.0
        self._last_hash = ""
        self._closed = False

    @property
    def snapshot(self) -> ProgressSnapshot:
        return self._snapshot

    def on_progress(self, event: ProgressEvent) -> None:
        if self._closed:
            return
        force = event.event_type in {"turn_failed", "node_failed", "turn_completed"}
        self._merge(event)
        self.flush(force=force)

    def flush(self, *, force: bool = False) -> None:
        if self._closed and not force:
            return
        now = self._clock()
        if not force and self._last_flush_at and now - self._last_flush_at < self._min_interval_seconds:
            return
        delivery = self._renderer.render_progress_card(self._snapshot)
        digest = hashlib.sha1(delivery.content.encode("utf-8")).hexdigest()
        if digest == self._last_hash:
            return
        self._update_card(self._message_id, delivery)
        self._last_hash = digest
        self._last_flush_at = now
        self._snapshot.updated_at = now

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._flush_on_close:
                self.flush(force=True)
        finally:
            self._closed = True

    def _merge(self, event: ProgressEvent) -> None:
        event_type = event.event_type
        if event_type == "turn_started":
            self._snapshot.current_stage = "启动"
            self._snapshot.current_action = event.summary or "正在准备处理请求"
            self._append_recent(event.summary or "开始处理")
            return
        if event_type == "planning_started":
            self._snapshot.current_stage = "规划"
            self._snapshot.current_action = event.summary or "正在生成执行计划"
            self._append_recent("开始规划任务")
            return
        if event_type == "plan_created":
            node_total = _int_value(event.data.get("node_count"))
            if node_total is not None:
                self._snapshot.node_total = node_total
            runtimes = event.data.get("runtimes")
            suffix = f"：{', '.join(runtimes)}" if isinstance(runtimes, list) and runtimes else ""
            nodes = event.data.get("nodes")
            if isinstance(nodes, list):
                self._snapshot.planned_nodes = [_node_info(item) for item in nodes if isinstance(item, dict)]
            self._snapshot.current_stage = "计划已生成"
            self._snapshot.current_action = event.summary or f"已生成 {self._snapshot.node_total or 0} 个执行节点{suffix}"
            self._add_completed("生成执行计划")
            self._append_recent(self._snapshot.current_action)
            return
        if event_type == "node_started":
            self._snapshot.current_stage = "执行节点"
            self._snapshot.current_action = event.summary or _node_label(event, prefix="开始执行")
            self._append_recent(self._snapshot.current_action)
            return
        if event_type in {"node_completed", "node_failed"}:
            failed = event_type == "node_failed" or event.status in {"failed", "blocked"}
            self._snapshot.current_stage = "执行节点"
            label = event.summary or _node_label(event, prefix="节点失败" if failed else "节点完成")
            if failed:
                self._snapshot.status = "failed"
                self._snapshot.current_action = label
            else:
                self._snapshot.node_completed += 1
                if event.node_id and event.node_id not in self._snapshot.completed_node_ids:
                    self._snapshot.completed_node_ids.append(event.node_id)
                self._snapshot.current_action = label
                self._add_completed(_node_label(event, prefix="完成"))
            self._append_recent(label)
            return
        if event_type == "aggregation_started":
            self._snapshot.current_stage = "汇总"
            self._snapshot.current_action = event.summary or "正在汇总执行结果"
            self._snapshot.output_started = True
            self._append_recent("开始汇总结果")
            return
        if event_type == "aggregation_completed":
            self._snapshot.current_stage = "汇总"
            self._snapshot.current_action = event.summary or "结果汇总完成"
            self._add_completed("汇总结果")
            self._append_recent(self._snapshot.current_action)
            return
        if event_type == "turn_completed":
            self._snapshot.status = "completed"
            self._snapshot.current_stage = "完成"
            self._snapshot.current_action = event.summary or "任务已完成，正在返回结果"
            self._snapshot.output_started = True
            self._append_recent(self._snapshot.current_action)
            return
        if event_type == "turn_failed":
            self._snapshot.status = "failed"
            self._snapshot.current_stage = "失败"
            self._snapshot.current_action = event.summary or "任务执行失败"
            self._append_recent(self._snapshot.current_action)

    def _append_recent(self, text: str) -> None:
        value = _clean(text)
        if not value:
            return
        if self._snapshot.recent_events and self._snapshot.recent_events[-1] == value:
            return
        self._snapshot.recent_events.append(value)
        self._snapshot.recent_events = self._snapshot.recent_events[-self._max_recent_events :]

    def _add_completed(self, text: str) -> None:
        value = _clean(text)
        if value and value not in self._snapshot.completed_items:
            self._snapshot.completed_items.append(value)
            self._snapshot.completed_items = self._snapshot.completed_items[-6:]


class FeishuCardKitProgressSink(FeishuProgressSink):
    def flush(self, *, force: bool = False) -> None:
        if self._closed and not force:
            return
        now = self._clock()
        if not force and self._last_flush_at and now - self._last_flush_at < self._min_interval_seconds:
            return
        delivery = self._renderer.render_cardkit_progress_card(self._snapshot)
        digest = hashlib.sha1(delivery.content.encode("utf-8")).hexdigest()
        if digest == self._last_hash:
            return
        self._update_card(self._message_id, delivery)
        self._last_hash = digest
        self._last_flush_at = now
        self._snapshot.updated_at = now


def _node_label(event: ProgressEvent, *, prefix: str) -> str:
    runtime = event.data.get("runtime")
    if event.node_id and runtime:
        return f"{prefix} {event.node_id} ({runtime})"
    if event.node_id:
        return f"{prefix} {event.node_id}"
    return prefix


def _node_info(item: dict) -> dict[str, str]:
    node_id = str(item.get("id") or item.get("node_id") or "").strip()
    runtime = str(item.get("runtime") or "").strip()
    objective = str(item.get("objective") or "").strip()
    result = {"id": node_id, "runtime": runtime, "objective": objective}
    return {key: value for key, value in result.items() if value}


def _clean(value: str, *, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 14].rstrip() + "...[truncated]"


def _int_value(value: object) -> int | None:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None
