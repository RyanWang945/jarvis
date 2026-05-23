# Jarvis ProgressReporter 与模块化飞书进度展示设计

| 项目 | 内容 |
|------|------|
| 作者 | Codex |
| 日期 | 2026-05-23 |
| 状态 | Draft |
| 相关设计 | `【2026-05-02】Jarvis Runtime 二层架构设计.md`, `【2026-05-13】Jarvis Task Plan 与 Artifact Context 上下文设计.md`, `【2026-5-1】Feishu Markdown 卡片渲染设计.md` |
| 目标 | 建立 runtime emit 机制，并将飞书进度展示模块化为事件合并与卡片更新适配器 |

---

## 1. 背景

当前飞书通道的用户可见执行状态比较粗：

```text
收到用户消息
-> 发送一张 thinking card
-> 同步执行 AgentRuntime.run_turn(turn_id)
-> 用最终回复覆盖 thinking card
```

现有实现已经具备两个基础能力：

- 飞书 interactive card 可以被 PATCH 更新。
- ReAct runtime 和 Task runtime 内部已有较多生命周期日志与 `tool_calls` 审计。

但这些能力没有形成统一的进度事件机制，因此用户在长任务中只能看到“正在思考”，看不到 agent 当前是在规划、执行节点、调用工具、等待审批，还是汇总结果。

本设计目标是把“运行时进度事实”和“飞书展示方式”解耦：

```text
Agent Runtime / Task Runtime
  emit structured progress events

ProgressReporter
  fan-out to sinks, isolate failures

FeishuProgressSink
  merge events, throttle updates, render progress card
```

---

## 2. 当前链路

### 2.1 飞书消息链路

当前主要入口：

```text
app/channels/feishu.py
```

关键路径：

```text
FeishuChannel._on_message
-> GatewayService.handle_inbound
-> FeishuChannel._handle_agent_run
-> _send_thinking_card
-> get_agent_runtime().run_turn(turn_id)
-> _update_channel_message / _update_card_message
```

`FeishuRenderer.render_thinking_card()` 当前只渲染静态 thinking card。最终回复仍通过同一张 card 更新。

### 2.2 Runtime 链路

当前存在两类 runtime：

- `AgentRuntime` / `TurnRuntime`：ReAct loop。
- `TaskAgentRuntime`：PlanningRouter -> NodeExecutor -> ResultAggregator。

ReAct runtime 的进度事实散落在：

- runtime policy resolved
- LLM step
- tool requested / running / completed
- Codex approval requested
- finalize success / failure

Task runtime 的进度事实更天然：

- planning started / completed
- plan created
- node started / completed / failed
- aggregation started / completed
- turn completed / failed

---

## 3. 设计目标

### 3.1 目标

1. 新增通用 `ProgressReporter`，作为 runtime 的结构化进度出口。
2. 让 runtime 只 emit 事实事件，不依赖飞书、Web UI 或其他渠道。
3. 将飞书进度展示封装为 `FeishuProgressSink`，负责事件合并、节流、脱敏、卡片渲染和 PATCH 更新。
4. 优先接入 `TaskAgentRuntime` 的 plan/node 生命周期。
5. 后续接入 ReAct runtime 的 LLM/tool 生命周期和 Codex app-server 事件流。
6. 默认兼容现有链路，未启用进度 sink 时行为不变。

### 3.2 非目标

第一阶段不做：

- 不展示模型原始 `reasoning_content`。
- 不把飞书 API 调用下沉到 runtime。
- 不重构 turns/messages/tool_calls 数据模型。
- 不要求所有工具第一阶段都提供细粒度内部进度。
- 不用多条飞书消息刷屏展示进度。
- 不把完整 event trace 塞进飞书卡片。

---

## 4. 总体架构

目标架构：

```text
FeishuChannel
  send thinking card
  create FeishuProgressSink(card_message_id, chat_id)
  create ProgressReporter([feishu_sink])
  call runtime.run_turn(turn_id, progress=reporter)
  update final answer card

Runtime
  progress.emit(...)
  execute normally
  return TurnResult

ProgressReporter
  fan-out events to sinks
  catch sink failures
  provide Noop fallback

FeishuProgressSink
  merge events into ProgressSnapshot
  throttle card PATCH
  render progress card
```

关键边界：

```text
Runtime 知道 ProgressReporter
Runtime 不知道 Feishu

FeishuChannel 知道 FeishuProgressSink
FeishuProgressSink 不驱动 runtime
```

---

## 5. 模块划分

### 5.1 `app/progress.py`

建议新增通用模块：

```text
app/progress.py
```

职责：

- 定义 `ProgressEvent`
- 定义 `ProgressSink`
- 定义 `ProgressReporter`
- 定义 `NoopProgressReporter`
- 提供安全 emit 语义

建议接口：

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

@dataclass(frozen=True)
class ProgressEvent:
    event_type: str
    turn_id: int | None = None
    conversation_id: int | None = None
    stage: str | None = None
    title: str = ""
    summary: str = ""
    node_id: str | None = None
    tool_name: str | None = None
    status: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

class ProgressSink(Protocol):
    def on_progress(self, event: ProgressEvent) -> None: ...

class ProgressReporter:
    def __init__(self, sinks: list[ProgressSink] | None = None) -> None: ...
    def emit(self, event_type: str, **payload: Any) -> None: ...
    def close(self) -> None: ...
```

约束：

- `emit()` 不向调用方抛出 sink 异常。
- `emit()` 不做渠道渲染。
- `close()` 用于 flush sink，失败只记录日志。

### 5.2 `app/channels/feishu_progress.py`

建议新增飞书进度适配模块：

```text
app/channels/feishu_progress.py
```

职责：

- 定义 `ProgressSnapshot`
- 实现 `FeishuProgressSink`
- 将事件合并为用户可读状态
- 做节流与去重
- 调用 `FeishuChannel` 提供的 card update 能力

`FeishuProgressSink` 不应该直接创建消息，只更新已存在的 thinking card。

建议构造：

```python
FeishuProgressSink(
    message_id=thinking_message_id,
    renderer=feishu_renderer,
    update_card=feishu_channel._update_card_message,
    min_interval_seconds=2.0,
)
```

这样 sink 只依赖一个最小 update 函数，而不是依赖整个 `FeishuChannel`。

### 5.3 `FeishuRenderer`

在现有 renderer 上新增：

```python
render_progress_card(snapshot: ProgressSnapshot) -> FeishuDelivery
```

展示内容建议：

```text
Jarvis 正在处理

当前阶段：执行计划节点 2/4
正在做：检查飞书通道的卡片更新逻辑

已完成：
✓ 理解请求
✓ 生成执行计划
✓ 检查 FeishuChannel

最近进展：
- 找到 thinking card 创建与 PATCH 更新位置
- 已确认 tool_calls 有审计记录
- 正在分析 Codex app-server 事件流
```

约束：

- 最多展示最近 3-5 条进展。
- 最多展示有限数量节点，避免卡片过长。
- 不展示 raw prompt、raw reasoning、完整工具输入。

---

## 6. 事件模型

### 6.1 通用事件

建议第一阶段支持：

```text
turn_started
turn_completed
turn_failed
planning_started
plan_created
node_started
node_completed
node_failed
aggregation_started
aggregation_completed
tool_started
tool_completed
tool_failed
approval_requested
finalizing
```

### 6.2 Task Runtime 事件

`TaskAgentRuntime.run_turn()`：

```text
turn_started
planning_started
plan_created
aggregation_started
aggregation_completed
turn_completed / turn_failed
```

`NodeExecutor.execute()`：

```text
node_started
node_completed
node_failed
```

节点事件 payload：

```python
progress.emit(
    "node_started",
    turn_id=turn_id,
    conversation_id=conversation_id,
    stage="execution",
    node_id=node.id,
    title=node.title,
    summary=f"开始执行 {node.runtime} 节点",
    data={
        "runtime": node.runtime,
        "input_refs": node.input_refs,
    },
)
```

### 6.3 ReAct Runtime 事件

后续接入：

```text
policy_resolved
llm_step_started
llm_step_completed
tool_requested
tool_started
tool_completed
tool_failed
finalizing
```

工具事件 payload 应优先展示工具语义，不展示完整参数：

```python
progress.emit(
    "tool_started",
    turn_id=turn_id,
    stage="tool",
    tool_name=tool_name,
    title=f"调用工具：{tool_name}",
    summary=safe_tool_summary(tool_name, tool_args),
)
```

### 6.4 Codex 事件

Codex app-server 当前已经逐行消费事件。后续可以增加 `on_event` 回调，把 Codex 内部事件映射为安全进度事件：

```text
codex_turn_started
codex_agent_message
codex_command_started
codex_approval_requested
codex_completed
```

限制：

- 不把 Codex raw JSONL 直接展示给飞书用户。
- 不展示完整 diff。
- 命令展示需要截断和脱敏。
- 审批仍走现有 approval card。

---

## 7. 飞书进度合并策略

`FeishuProgressSink` 维护一个 `ProgressSnapshot`：

```python
@dataclass
class ProgressSnapshot:
    title: str
    current_stage: str
    current_action: str
    completed_items: list[str]
    recent_events: list[str]
    node_total: int | None = None
    node_completed: int = 0
    tool_running: str | None = None
    started_at: float = 0.0
    updated_at: float = 0.0
```

合并规则：

- `plan_created` 设置 `node_total`，记录计划摘要。
- `node_started` 更新 `current_action`。
- `node_completed` 追加 completed item，增加 `node_completed`。
- `tool_started` 设置 `tool_running`。
- `tool_completed/tool_failed` 清理 `tool_running`，追加 recent event。
- `approval_requested` 强制 flush，让用户尽快看到等待审批状态。
- `turn_completed/turn_failed` 只做最后一次 flush，最终卡片仍由 FeishuChannel 覆盖。

---

## 8. 节流与可靠性

飞书卡片更新必须节流：

- 默认最小更新间隔：2 秒。
- 状态明显变化可以立即更新：
  - `approval_requested`
  - `turn_failed`
  - `node_failed`
- 每次卡片内容相同则跳过 PATCH。
- sink 异常只记录日志，不影响 agent 执行。

建议 `FeishuProgressSink` 提供：

```python
on_progress(event)
flush(force=False)
close()
```

`ProgressReporter.close()` 在 runtime 返回后调用 sink close，确保最后一次中间态有机会被 flush。

---

## 9. 飞书通道接入方式

`FeishuChannel._handle_agent_run()` 目标形态：

```python
thinking_message_id = self._send_thinking_card(chat_id, text)
progress = NoopProgressReporter()

if thinking_message_id and settings.feishu_progress_updates_enabled:
    sink = FeishuProgressSink(
        message_id=thinking_message_id,
        renderer=self._renderer,
        update_card=self._update_card_message,
    )
    progress = ProgressReporter([sink])

try:
    result = get_agent_runtime().run_turn(turn_id, progress=progress)
finally:
    progress.close()
```

最终回复逻辑保持现状：

```text
progress card 是中间态
final answer card 是最终态
```

如果进度更新失败，仍然应该保留现有行为：

```text
thinking card -> final answer card
```

---

## 10. Runtime 接口兼容

`AgentRuntime` 和 `TaskAgentRuntime` 接口改为可选参数：

```python
def run_turn(self, turn_id: int, progress: ProgressReporter | None = None) -> TurnResult:
    progress = progress or NoopProgressReporter()
```

兼容要求：

- API 入口仍可调用 `run_turn(turn_id)`。
- 测试里已有 fake runtime 不应被迫实现新参数，飞书调用处需要兼容旧签名或同步更新测试 fake。
- 没有 sink 时不改变用户可见行为。

---

## 11. 配置与灰度

建议新增配置：

```text
JARVIS_FEISHU_PROGRESS_UPDATES_ENABLED=false
JARVIS_FEISHU_PROGRESS_MIN_INTERVAL_SECONDS=2.0
JARVIS_FEISHU_PROGRESS_MAX_RECENT_EVENTS=5
```

第一阶段默认关闭飞书动态 PATCH，只落 runtime emit 与 Noop。

灰度顺序：

1. 落 `ProgressReporter`、`NoopProgressReporter`、事件模型。
2. `TaskAgentRuntime` emit plan/node/aggregation 事件，默认 no-op。
3. 实现 `FeishuProgressSink` 和 `render_progress_card()`，配置默认关闭。
4. 本地打开飞书动态更新验证长任务。
5. 接入 ReAct tool 事件。
6. 接入 Codex app-server 安全摘要事件。

---

## 12. 测试策略

### 12.1 单元测试

新增测试覆盖：

- `ProgressReporter.emit()` fan-out。
- sink 异常不会传播。
- `NoopProgressReporter` 无副作用。
- `FeishuProgressSink` 合并事件生成 snapshot。
- 节流逻辑：短时间重复事件不会重复 PATCH。
- `render_progress_card()` 生成 interactive card。

### 12.2 Runtime 测试

Task runtime：

- planning 后 emit `plan_created`。
- node start/finish 事件顺序正确。
- node failed 时 emit `node_failed`。
- aggregation 前后 emit 事件。

ReAct runtime：

- tool started/completed 事件不改变 `tool_calls` 审计。
- tool failed 事件不吞掉原有错误路径。

### 12.3 飞书通道测试

新增或扩展 `tests/test_feishu_channel.py`：

- thinking card 创建后，progress sink 可以更新同一 message id。
- sink update 失败时最终答案仍更新。
- 配置关闭时不触发中间 PATCH。
- 最终回复仍覆盖 progress card。

---

## 13. 安全与隐私

飞书进度卡只展示“工作状态摘要”，不展示“模型思维链”。

禁止展示：

- `reasoning_content`
- raw prompt
- 完整工具参数
- API key、token、cookie
- 大段文件内容
- Codex raw JSONL
- 未截断命令输出

允许展示：

- 当前阶段
- 节点标题
- 工具名
- 安全摘要
- 审批等待状态
- artifact 生成状态

---

## 14. 推荐第一版实现范围

第一版建议只做：

1. `app/progress.py`
2. `TaskAgentRuntime.run_turn(..., progress=None)`
3. `NodeExecutor.execute(..., progress=None)`
4. `FeishuRenderer.render_progress_card()`
5. `app/channels/feishu_progress.py`
6. `FeishuChannel._handle_agent_run()` 在配置打开时注入 sink

第一版不做 Codex 内部事件流。只要 plan/node 能动态展示，就已经能明显改善长任务体验。

---

## 15. 结论

长期方向应是：

```text
ProgressReporter 是 runtime 的观测出口
FeishuProgressSink 是飞书的展示适配器
FeishuChannel 是 thinking card 生命周期 owner
Runtime 不直接依赖飞书
```

这个设计既能解决飞书长任务“黑盒等待”的体验问题，也为后续 Web UI、SSE、CLI 进度条和完整 runtime trace 留出统一扩展口。
