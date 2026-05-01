# Jarvis LangGraph Agent 设计文档（2026-04-21）

版本：v2.0
状态：CA Agent + Worker 架构，LangGraph 1.0+ 适配
日期：2026-04-21
目标：定义 Jarvis 本地 Long-run Agent 的 LangGraph 核心设计，适配 CA+Worker 分离架构，充分利用 LangGraph 1.0+ 的 `interrupt`、`Command` 和流式执行能力。

## 1. 设计目标

Jarvis 的 Agent 核心不是一个持续占用上下文的聊天机器人，而是一个**事件驱动的上下文感知决策引擎（CA Agent）**，配合**可并行的无状态 Worker 执行层**。

核心目标：

1. 所有输入统一转为 `AgentEvent`（含 Worker 回调事件）。
2. 每个 CA Agent 运行在独立或复用的 `thread_id` 下。
3. LangGraph 负责 CA Agent 的状态流转和 checkpoint，Worker 生命周期在 LangGraph 外管理。
4. 业务数据库负责查询、审计、授权、资源锁、Worker 状态。
5. 常驻服务负责接收事件、定时触发、恢复任务、Worker 结果路由。
6. 飞书、CLI、HTTP API、Scheduler、Worker 回调都只是不同事件来源。

高层流程：

```text
CLI / API / Scheduler / Feishu / Worker Callback
        |
        v
AgentEvent
        |
        v
Session Dispatcher
        |
        v
CA Agent (LangGraph Thread)
        |
        v
WorkOrder ──► Worker Pool
        |
        v
WorkResult ──► Event Bus ──► CA Agent
        |
        v
Checkpoint + Task DB + Audit Log
```

## 2. Long-run 模型

Long-run 不依赖一个永不结束的大模型上下文，而依赖四个层次：

```text
1. 常驻服务层
   FastAPI / APScheduler / background worker / Event Bus

2. 调度层
   Event Queue / Session Dispatcher / Resource Lock / Worker Registry

3. CA Agent 决策层
   LangGraph thread + interrupt + checkpoint

4. Worker 执行层
   Inline / Thread / Subgraph Worker + Skill
```

常驻服务长期运行。CA Agent 的 LangGraph thread 可以**短暂运行、挂起、恢复、失败或结束**。Worker 在 LangGraph 外独立运行，完成后通过 Event Bus 唤醒 CA Agent。

进程重启后，系统通过业务数据库和 LangGraph checkpoint 恢复未完成 CA thread，并补偿可能丢失的 Worker 回调。

## 3. LangGraph 1.0+ 关键特性使用

### 3.1 `interrupt()` 替代 `NodeInterrupt`

LangGraph 1.0 引入 `interrupt()` 函数，用于在节点中安全挂起 graph，等待外部输入后恢复。

```python
from langgraph.types import interrupt

def monitor(state: AgentState) -> dict[str, Any]:
    if state["active_workers"]:
        # 安全挂起，等待 Worker 完成事件
        interrupt({
            "type": "wait_workers",
            "active_workers": list(state["active_workers"].values()),
        })
    return {"status": "monitoring"}
```

**恢复方式**：

```python
from langgraph.types import Command

# 外部收到 worker_complete 事件后
graph.invoke(
    Command(resume={"event_type": "worker_complete", "order_id": "...", "result": {...}}),
    config={"configurable": {"thread_id": thread_id}},
)
```

`interrupt()` 的优势：
- 比 `NodeInterrupt` 更语义化，明确表达"等待人类/外部系统输入"。
- 自动写入 checkpoint，恢复时精确回到中断节点。
- 支持携带 `resume` payload，外部事件可以作为参数注入。

### 3.2 `Command` 对象：动态路由 + Resume

LangGraph 1.0 的 `Command` 不只是 resume，还能用于**节点间的动态导航**。

```python
from langgraph.types import Command

def aggregate(state: AgentState) -> Command:
    # 收到 Worker 结果，判断是否需要重新规划
    if _needs_replan(state):
        return Command(goto="strategize", update={"status": "strategizing"})
    if _all_done(state):
        return Command(goto="summarize", update={"status": "completed"})
    return Command(goto="monitor", update={"status": "monitoring"})
```

`Command` 替代了 v0.x 的 `next_node` + 条件边模式，更直观地表达"这个节点做完后去哪"。

### 3.3 `astream` 与事件流

CA Agent 使用 `astream` 替代 `invoke`，让常驻服务可以观察到节点级事件：

```python
async for event in graph.astream(
    state,
    config={"configurable": {"thread_id": thread_id}},
    stream_mode="values",
):
    # event 是 AgentState 的快照
    # 常驻服务可以据此更新业务 DB、发送飞书通知
    await update_task_status(thread_id, event["status"])
```

对于 Worker 完成回调场景，仍使用 `invoke` + `Command(resume=...)`，因为回调是离散事件。

### 3.4 `RetryPolicy`

节点级重试策略，用于处理瞬态错误（如 LLM API 超时）：

```python
from langgraph.pregel import RetryPolicy

graph.add_node("strategize", strategize, retry=RetryPolicy(max_attempts=3))
```

注意：`RetryPolicy` 是**节点级**的，适合 LLM 调用；Worker 失败的重试由 CA Agent 的 `aggregate` 逻辑控制。

## 4. 核心数据结构

### 4.1 AgentEvent

```python
from typing import Any, Literal, TypedDict

class AgentEvent(TypedDict):
    event_id: str
    event_type: Literal[
        "user_message",
        "schedule",
        "approval",
        "system_resume",
        "task_cancel",
        "task_status_query",
        "worker_complete",   # Worker 完成回调
        "worker_failed",     # Worker 失败回调
    ]
    source: Literal["api", "cli", "scheduler", "feishu", "system", "worker"]
    thread_id: str | None
    user_id: str | None
    timestamp: str
    payload: dict[str, Any]
```

Worker 回调的 payload 示例：

```python
{
    "order_id": "ord-xxx",
    "worker_type": "shell",
    "ok": True,
    "summary": "pytest passed",
    "stdout": "...",
    "stderr": "",
    "artifacts": [],
}
```

### 4.2 Task

```python
TaskStatus = Literal[
    "pending",      # CA Agent 已生成，未 dispatch
    "dispatched",   # 已发给 Worker，Worker 未开始
    "running",      # Worker 正在执行
    "waiting",      # 等待审批或外部条件
    "success",
    "failed",
    "blocked",
    "cancelled",
]

class Task(TypedDict):
    id: str
    parent_id: str | None
    title: str
    description: str
    resource_key: str | None
    status: TaskStatus
    dod: str
    verification_cmd: str | None
    retry_count: int
    max_retries: int
    worker_type: str | None       # shell / coder / research / obsidian
    order_id: str | None          # 关联 WorkOrder ID
    result_summary: str | None    # WorkResult 摘要
```

### 4.3 WorkOrder 与 WorkResult

```python
from pydantic import BaseModel

class WorkOrder(BaseModel):
    worker_type: str
    order_id: str
    ca_thread_id: str
    instruction: str
    tool_name: str | None
    tool_args: dict[str, Any]
    workdir: str | None
    resource_key: str | None
    risk_level: str
    dod: str | None
    verification_cmd: str | None
    max_retries: int = 0
    timeout_seconds: int = 300

class WorkResult(BaseModel):
    order_id: str
    ca_thread_id: str
    ok: bool
    summary: str
    stdout: str = ""
    stderr: str = ""
    artifacts: list[str] = []
    retry_count: int = 0
    error_message: str | None = None
```

### 4.4 AgentState（CA Agent）

```python
from typing import Annotated, Any, Literal, TypedDict
from langgraph.graph.message import add_messages

AgentRunStatus = Literal[
    "created",
    "contextualizing",
    "strategizing",
    "dispatching",
    "monitoring",
    "waiting_approval",
    "blocked",
    "completed",
    "failed",
    "cancelled",
]

class AgentState(TypedDict):
    thread_id: str
    messages: Annotated[list[Any], add_messages]
    event: dict[str, Any]
    resource_key: str | None

    # 任务状态
    task_list: list[Task]
    dispatch_queue: list[dict[str, Any]]
    active_workers: dict[str, str]       # task_id -> order_id
    worker_results: dict[str, Any]       # order_id -> WorkResult

    status: AgentRunStatus

    # 授权和错误
    pending_approval_id: str | None
    error_count: int
    last_error: str | None

    # 记忆和压缩
    context_summary: str | None
    final_summary: str | None
```

设计原则：

- `messages` 用于对话和事件历史，但**不作为唯一事实来源**。
- `task_list` 是任务状态的事实来源。
- `dispatch_queue` + `active_workers` + `worker_results` 构成 Worker 调度闭环。
- `context_summary` 用于长任务上下文压缩，避免 `messages` 无限膨胀。
- `pending_approval_id` 连接 LangGraph `interrupt()` 和业务授权表。

## 5. Graph 节点设计

### 5.1 ingest_event

职责：

- 将 `AgentEvent` 写入 state。
- 如果是 `worker_complete` / `worker_failed`，提取 WorkResult 写入 `worker_results`。
- 初始化或更新运行状态。

是否调用模型：否。

输入：`AgentEvent`

输出：

- `state.event`
- `state.worker_results`（Worker 回调时）
- `state.status = "created"`（新事件）或保持当前状态（回调事件）

### 5.2 contextualize

职责：

- 收集任务上下文。
- 读取已有任务状态。
- 读取资源信息。
- 读取已有 `context_summary`。
- 确定或补全 `resource_key`。

MVP 先只收集：

- 用户 instruction。
- 当前工作目录。
- 已有 task list。
- payload 中显式传入的 `resource_key`。

是否调用模型：MVP 否，后续可选（让 LLM 做上下文压缩）。

### 5.3 strategize

职责：

- **理解用户意图**（不是简单拆解，而是理解目标）。
- 根据当前 `task_list`、`worker_results`、`context_summary` 生成高层计划。
- 输出 `WorkOrder` 清单，写入 `dispatch_queue`。
- 为每个 WorkOrder 生成 DoD 或验证命令。

MVP 策略：

- 使用 LLM 作为 Strategist。
- 将可用 Worker 类型以 `tools` / function calling 形式提供给 LLM。
- LLM 自主决定生成哪些 WorkOrder（调用哪个 Worker、参数是什么）。
- 如果 payload 包含 `workdir` 或 `verification_cmd`，作为上下文传给 LLM，并在 WorkOrder 上补齐。
- 如果已有 Worker 失败，LLM 在 `strategize` 时可以看到 `worker_results`，决定重试、换方案或放弃。

降级策略：

- 规则式 fallback 仅作为测试、离线调试或模型不可用时的降级。

输出：

- `state.dispatch_queue: list[WorkOrder]`
- `state.status = "strategizing"`
- 更新 `state.task_list`（新增 pending 任务）

### 5.4 dispatch

职责：

- 遍历 `dispatch_queue`。
- 对每个 WorkOrder 做风险预检。
- **low/medium**：直接通过 Worker Client 启动 Worker，更新 `active_workers`。
- **high/critical**：暂不启动，标记任务为 `waiting`，进入 `risk_gate`。
- 清空 `dispatch_queue`（已处理的）。

是否调用模型：否。

输出：

- `state.active_workers`
- `state.task_list`（更新为 `dispatched` 或 `waiting`）
- `state.dispatch_queue = []`

### 5.5 risk_gate

职责：

- 对 `waiting` 状态的 WorkOrder 做确定性风险判断。
- 决定直接 dispatch、等待授权或阻塞。

规则：

- low / medium：直接 dispatch。
- high / critical：创建 `ApprovalRequest`，interrupt 挂起。
- rejected：标记任务 `blocked`。

特殊规则：`git push` 一律需授权。

风险判断必须是确定性逻辑，不能完全交给大模型。

### 5.6 monitor

职责：

- 检查 `active_workers`。
- 如果还有 Worker 在跑：`interrupt()` 挂起，等待 `worker_complete` 事件。
- 如果全部完成：路由到 `aggregate`。

LangGraph 1.0+ 实现：

```python
from langgraph.types import interrupt

def monitor(state: AgentState) -> dict[str, Any]:
    if state["active_workers"]:
        interrupt({
            "type": "wait_workers",
            "active_workers": list(state["active_workers"].values()),
        })
    return {"status": "monitoring"}
```

路由：

- 有 active worker：挂起（外部事件恢复后回到 `ingest_event`）。
- 全部完成：进入 `aggregate`。

### 5.7 aggregate

职责：

- 将 `worker_results` 中的结果映射回 `task_list`。
- 更新每个 Task 的 `status` 和 `result_summary`。
- 判断整体目标是否达成。
- 决定：进入 `summarize`、回到 `strategize`、或进入 `blocked`。

是否调用模型：MVP 否（用规则判断），后续复杂场景可接入 LLM 做决策。

输出：

- 更新 `state.task_list`
- 清空已处理的 `state.worker_results`
- 返回 `Command(goto=...)` 决定下一步

### 5.8 summarize

职责：

- 生成最终任务摘要。
- 写入 `final_summary`。
- 生成 Markdown 复盘。
- 更新业务数据库任务结果。

是否调用模型：MVP 否，后续可接入 LLM 生成自然语言摘要。

### 5.9 blocked

职责：

- 标记任务不可继续。
- 记录阻塞原因。
- 释放资源锁或进入人工处理状态。
- 取消所有 `active_workers`（发送 cancel 信号）。

## 6. 边设计

### 6.1 主流程

```text
START
  |
  v
ingest_event
  |
  v
contextualize
  |
  v
strategize
  |
  v
dispatch
  |
  v
risk_gate (条件边)
```

### 6.2 risk_gate 条件边

```text
risk_gate
  |-- monitor          # low / medium / approved
  |-- interrupt        # high / critical and not approved（挂起）
  |-- blocked          # rejected
```

### 6.3 monitor 与 aggregate 的循环

```text
monitor
  |-- interrupt        # 还有 Worker 在跑（挂起）
  |-- aggregate        # 全部 Worker 完成

aggregate
  |-- strategize       # 目标未达成，需要重新规划
  |-- summarize        # 目标达成
  |-- blocked          # 失败且不可修复
```

### 6.4 终止边

```text
summarize -> END
blocked   -> END
```

### 6.5 事件恢复后的重新进入

外部事件（Worker 完成、用户审批、新消息）通过 `Command(resume=...)` 恢复 CA Agent：

```text
[外部事件] ──► graph.invoke(Command(resume=payload), config)
                    |
                    v
               ingest_event（接收事件）
                    |
                    v
               contextualize
                    |
                    v
               [根据事件类型路由]
                    |
                    ├── worker_complete ──► aggregate
                    |
                    ├── approval approved ──► dispatch
                    |
                    ├── approval rejected ──► blocked
                    |
                    └── user_message ──► strategize（重新规划）
```

## 7. Worker 回调与 LangGraph 集成

### 7.1 Worker 完成时序

```text
Worker 完成
  |
  v
Worker Client 写入业务 DB（WorkResult）
  |
  v
Worker Client 发布事件到 Event Bus
  |
  v
Session Dispatcher 路由到对应 CA thread
  |
  v
常驻服务调用 graph.invoke(Command(resume=...))
  |
  v
CA Agent 从 monitor 的 interrupt 处恢复
  |
  v
ingest_event 解析 resume payload，写入 worker_results
  |
  v
aggregate 处理结果
```

### 7.2 恢复时序示例（Python 伪代码）

```python
from langgraph.types import Command

async def on_worker_complete(order_id: str, result: WorkResult):
    thread_id = result.ca_thread_id

    # 恢复 CA Agent
    async for event in ca_graph.astream(
        Command(resume={
            "event_type": "worker_complete",
            "payload": result.model_dump(),
        }),
        config={"configurable": {"thread_id": thread_id}},
        stream_mode="values",
    ):
        # 流式更新 UI / 飞书 / 业务 DB
        await update_business_state(thread_id, event)

        # 如果 CA Agent dispatch 了新 Worker，启动它们
        for order in event.get("dispatch_queue", []):
            await worker_client.dispatch(order)
```

### 7.3 回调丢失的补偿机制

进程重启时：

1. 扫描 SQLite checkpoint，找到处于 `monitoring` 状态的 CA thread。
2. 检查 `active_workers` 中的 `order_id`。
3. 查询业务 DB 中这些 `order_id` 的 WorkResult。
4. 若 WorkResult 已存在（Worker 完成但回调丢失），合成 `worker_complete` 事件注入。
5. 若 WorkResult 不存在，Worker 可能还在跑（进程级 Worker）或已死亡（线程级 Worker），需要重新 dispatch。

## 8. 常驻服务组件

### 8.1 SchedulerService

职责：

- 管理 APScheduler。
- 注册 cron job。
- 到时间生成 `AgentEvent`。
- 投递给 Dispatcher。

### 8.2 DispatcherService

职责：

- 接收所有 `AgentEvent`。
- 识别 `resource_key`。
- 创建或复用 `thread_id`。
- 获取资源锁。
- **Worker 完成事件路由**：根据 `ca_thread_id` 投递到对应 CA thread。
- 调用 `ThreadManager` 启动或恢复 CA Agent。

### 8.3 ThreadManager（替代 GraphRunner）

职责：

- 管理 CA Agent 的 LangGraph thread 生命周期。
- 启动新 CA thread。
- 恢复已有 CA thread（`Command(resume=...)`）。
- 处理授权 resume。
- 处理系统恢复。
- 流式消费 CA Agent 的 `astream` 事件，驱动 Worker dispatch。

接口：

```python
class ThreadManager:
    async def run_event(self, event: AgentEvent) -> AsyncIterator[AgentState]:
        """启动或恢复 CA Agent，流式返回状态更新"""
        ...

    async def resume_worker(self, result: WorkResult) -> AsyncIterator[AgentState]:
        """Worker 完成后恢复 CA Agent"""
        ...

    async def resume_approval(
        self, thread_id: str, approval_id: str, approved: bool
    ) -> AsyncIterator[AgentState]:
        """审批后恢复 CA Agent"""
        ...

    async def recover_unfinished(self) -> list[AgentState]:
        """恢复未完成的 CA thread"""
        ...
```

### 8.4 WorkerClient

职责：

- 接收 `WorkOrder`。
- 启动 Worker（Inline / Thread / Subgraph）。
- 管理 Worker 生命周期（超时、取消）。
- Worker 完成后写入业务 DB，发送事件到 Event Bus。

接口：

```python
class WorkerClient(Protocol):
    async def dispatch(self, order: WorkOrder) -> str:
        """非阻塞派发，返回 order_id"""
        ...

    async def cancel(self, order_id: str) -> bool:
        """取消正在运行的 Worker"""
        ...

    async def poll(self, order_id: str) -> WorkResult | None:
        """查询结果，None 表示还在跑"""
        ...
```

### 8.5 ApprovalService

职责：

- 创建授权请求。
- 查询待授权请求。
- 允许或拒绝授权。
- 恢复等待中的 CA Agent（`Command(resume=...)`）。

### 8.6 TaskRepository

职责：

- 保存业务任务状态。
- 保存 Worker 运行状态。
- 提供查询和审计能力。

## 9. 资源锁设计

`resource_key` 使用规范化绝对路径或稳定资源 ID。

锁策略（支持 Worker）：

| 已有锁 | 新请求 | 策略 |
| --- | --- | --- |
| read | read | 允许并发 |
| read | write | 保守串行（MVP） |
| write | read | 保守串行（MVP） |
| write | write | 串行 |

Worker 锁继承：

- Worker 启动时，如果操作同一 `resource_key`，可以继承 CA thread 的锁。
- 如果 Worker 需要操作不同的 `resource_key`，需独立申请锁。

## 10. Checkpoint 和业务数据库边界

### 10.1 LangGraph Checkpoint

用途：

- 保存 `AgentState`。
- 支持 CA Agent thread 恢复。
- 支持 `interrupt()` 后 resume。

### 10.2 业务数据库

建议表：

```text
runs
tasks
work_orders          # 新增
work_results         # 新增
resource_locks
approvals
audit_logs
scheduled_jobs
```

用途：

- 查询任务状态。
- 查询待授权项。
- 审计 Worker 调用。
- 恢复时发现未完成任务和 Worker 结果。
- 补偿回调丢失。

## 11. 本地 MVP 最小闭环（CA+Worker Phase 1）

Phase 1 使用 Inline Worker，Worker 其实是同步调用，但接口已抽象为 `WorkOrder`/`WorkResult`。

```text
POST /agent/run
{
  "instruction": "运行 python --version",
  "command": "python --version"
}
```

Graph 流程：

```text
ingest_event
contextualize
strategize ──► 生成 WorkOrder（shell worker）
dispatch ──► InlineWorkerClient.run(...) ──► 同步返回 WorkResult
monitor ──► 无 active_workers，直接进入 aggregate
aggregate ──► 更新 task_list
summarize
```

返回：

```json
{
  "thread_id": "...",
  "status": "completed",
  "summary": "...",
  "tasks": []
}
```

Phase 1 的目标是**验证 CA Agent 的事件循环图可以跑通**，Worker 层用 Inline 兼容现有代码。

## 12. 目录结构建议（v2.0）

```text
app/
  agent/
    __init__.py
    events.py
    state.py
    nodes.py              # CA Agent 节点（strategize, dispatch, monitor, aggregate）
    graph.py              # LangGraph 构建
    runner.py             # ThreadManager
  workers/
    __init__.py
    base.py               # WorkOrder, WorkResult, WorkerClient 协议
    inline.py             # InlineWorkerClient（Phase 1）
    threaded.py           # ThreadWorkerClient（Phase 2）
    shell.py              # ShellWorker 实现
    coder.py              # CoderWorker 实现
  skills/
    __init__.py
    base.py               # SkillRequest, SkillResult
    echo.py
    shell.py
    file.py
  services/
    __init__.py
    dispatcher.py
    approvals.py
    scheduler.py
    event_bus.py          # 新增：内部事件总线
  persistence/
    __init__.py
    database.py
    tasks.py
    approvals.py
    audit.py
    worker_state.py       # 新增：Worker 状态持久化
  api/
    routes.py
    agent.py
    approvals.py
  config.py
  main.py
tests/
  agent/
  workers/                # 新增
  skills/
  persistence/
```

## 13. 实现顺序（v2.0）

建议按以下顺序实现：

1. 定义 `AgentEvent`（扩展 Worker 回调类型）、`WorkOrder`、`WorkResult`。
2. 定义 CA Agent `AgentState`（含 `dispatch_queue`、`active_workers`、`worker_results`）。
3. 实现最小 CA Agent Graph：`ingest_event -> contextualize -> strategize -> dispatch -> monitor -> aggregate -> summarize`。
4. 实现 `InlineWorkerClient`，把现有 Skill 调用封装成 Worker 接口。
5. 增加 `POST /agent/run`，验证 Inline 模式端到端。
6. 接入 LangGraph 1.0+ `interrupt()` 和 `Command(resume=...)`，验证事件恢复。
7. 增加 SQLite checkpoint 和业务 DB。
8. 增加 `risk_gate` + `interrupt()` 审批流程。
9. 实现 `ThreadWorkerClient`（`ThreadPoolExecutor`），验证 Worker 并行。
10. 增加 SchedulerService。
11. 增加资源锁和 Dispatcher。
12. 增加 CoderWorker（封装 Claude Code CLI）。
13. 增加飞书入口和通知。

## 14. 待 Review 问题

1. `strategize` 节点是否需要在每次 Worker 完成后都重新调用 LLM？还是只在失败时调用？
2. `aggregate` 节点在 MVP 是否用规则判断足够，还是直接接入 LLM？
3. Worker 超时后，CA Agent 如何决策：重试、换 Worker 类型、还是放弃？
4. `interrupt()` 的 resume payload 是否统一成 `AgentEvent` 格式，还是保持独立结构？
5. Phase 2 的 `ThreadWorkerClient` 是否需要独立的 SQLite 表记录 Worker 状态，还是复用 `work_results`？
6. CoderWorker 内部是否需要自己的 LangGraph 子图（plan-execute-verify），还是直接封装 CLI？
