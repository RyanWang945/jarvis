# Jarvis 架构设计文档

版本：v2.0
状态：CA Agent + Worker 架构基线
来源：基于 MVP 架构演进，引入上下文感知主脑（CA Agent）与并行 Worker 执行层

## 1. 架构目标

Jarvis 的架构目标是支持长时间运行的本地 AI Agent。系统需要在自然语言输入、任务规划、本地工具执行、状态持久化、风险授权和知识沉淀之间建立清晰边界。

与 v1.0 的核心变化：

- **引入 CA（Context-Aware）Agent**：作为上下文感知主脑，负责高层逻辑、上下文管理、任务分发和状态聚合。CA Agent 不直接执行 Skill，只做决策。
- **引入 Worker 执行层**：Worker 是具体的执行单元，可并行运行。Worker 无状态、无决策权，只接收 `WorkOrder`、执行、返回 `WorkResult`。
- **LangGraph 1.0+ 适配**：全面使用 `interrupt()`、`Command` 对象和 `astream` 流式执行模型。
- **事件循环驱动**：CA Agent 从"单次跑完"的流水线，变成"事件驱动的事件循环"，可多次 invoke、挂起、恢复。

核心目标（不变）：

- 所有输入统一成事件。
- 所有任务经过资源调度。
- 所有关键节点可 checkpoint。
- 所有工具调用可审计。
- 所有高危动作可拦截。
- 所有完成状态可验证。

## 2. 总体架构

```text
Feishu / Cron / File Watcher / CLI / API
          |
          v
Event Gateway
          |
          v
Session Dispatcher
          |
          v
+----------------+       +------------------+
|  CA Agent      |       |  Worker Pool     |
|  (LangGraph)   |<----->|  - Shell Worker  |
|  - Strategize  |       |  - Coder Worker  |
|  - Dispatch    |       |  - Research W.   |
|  - Monitor     |       |  - Obsidian W.   |
|  - Summarize   |       +------------------+
+----------------+               |
          ^                      |
          |                      v
          |            Skill Execution Layer
          |                      |
          +----------------------+
                   WorkResult
          |
          v
Persistence & Memory
```

### 架构分层说明

| 层级 | 组件 | 职责 |
|------|------|------|
| 入口层 | Event Gateway | 统一外部输入为 `AgentEvent` |
| 调度层 | Session Dispatcher | 资源识别、锁管理、thread 路由 |
| 决策层 | CA Agent (LangGraph) | 意图理解、计划生成、Worker 调度、结果聚合 |
| 执行层 | Worker Pool | 并行执行具体任务，有独立生命周期 |
| 能力层 | Skill Execution | Shell、Claude Code、Obsidian 等具体能力 |
| 持久层 | Persistence | Checkpoint、业务 DB、审计、Obsidian Vault |

## 3. 模块划分

### 3.1 Event Gateway

职责不变：

- 接收飞书消息、飞书互动卡片回调、定时任务触发、CLI/API 输入。
- 将外部输入转为 `AgentEvent`。

新增事件类型（供 Worker 回调和系统恢复）：

```python
AgentEventType = Literal[
    "user_message",
    "schedule",
    "approval",
    "system_resume",
    "task_cancel",
    "task_status_query",
    "worker_complete",   # 新增：Worker 完成回调
    "worker_failed",     # 新增：Worker 失败回调
]
```

### 3.2 Session Dispatcher

职责扩展：

- 识别事件意图。
- 判断事件涉及的资源。
- 创建或复用 `thread_id`。
- 管理资源锁（支持父子 thread 锁继承）。
- **Worker 完成事件路由**：将 `worker_complete` 事件投递到对应 CA thread。

资源锁策略（支持 Worker）：

| 场景 | 策略 |
| --- | --- |
| 同仓库写任务 | 必须持有写锁；Worker 继承 CA thread 的锁 |
| 同仓库只读任务 | 可持有读锁并发；Worker 可申请独立读锁 |
| 不同仓库写任务 | 允许并发，不同 CA thread 各自持有 |
| Critical 操作 | 不管资源是否空闲，都必须授权 |

`ResourceLock` 扩展：

```python
class ResourceLock(TypedDict):
    resource_key: str
    thread_id: str
    ca_thread_id: str       # 新增：所属 CA thread
    worker_id: str | None   # 新增：Worker 实例 ID
    mode: Literal["read", "write"]
    owner_event_id: str
    created_at: str
    expires_at: str | None
```

### 3.3 CA Agent（LangGraph Core）

职责变化：

- **维护全局上下文**：通过 `context_summary` 压缩历史，避免 messages 无限膨胀。
- **拆解高层计划**：不是拆解成"执行步骤"，而是拆解成"WorkOrder 清单"。
- **调度 Worker**：将 WorkOrder 分发给 Worker Pool，非阻塞。
- **聚合 Worker 结果**：收到 `worker_complete` 后，判断目标是否达成。
- **处理授权中断**：对高危 WorkOrder 进行审批控制。
- **失败后重新规划**：不是简单重试，而是让 LLM 根据失败信息重新 strategize。
- **生成阶段摘要**：写入 `context_summary` 和 Obsidian。

节点设计（v2.0）：

| 节点 | 职责 | 调用 LLM |
| --- | --- | --- |
| `ingest_event` | 接收事件，更新 messages | 否 |
| `contextualize` | 收集上下文，更新 `context_summary` | 否（MVP）/ 是（后续） |
| `strategize` | 理解意图，生成高层计划（WorkOrder 清单） | **是** |
| `dispatch` | 将 WorkOrder 分发给 Worker Pool | 否 |
| `monitor` | 检查 active_workers 状态，决定等待或继续 | 否 |
| `risk_gate` | 对高危 WorkOrder 做审批判断 | 否 |
| `aggregate` | 收到 Worker 结果，更新 task_list，判断是否需要重新 strategize | **是**（复杂场景） |
| `summarize` | 生成最终摘要 | 否 |
| `blocked` | 阻塞终止 | 否 |

**关键变化**：没有 `execute` 和 `verify` 节点了。执行和验证是 Worker 内部的事。CA Agent 只关心"Worker 是否完成了 WorkOrder"。

### 3.4 Worker 执行层

Worker 是独立的执行单元，不是 LangGraph 节点。Worker 的生命周期：

```text
receive WorkOrder
  |
  v
internal plan (可选，Coder Worker 内部有自己的 plan-execute-verify)
  |
  v
execute Skill(s)
  |
  v
verify (Worker 内部验证)
  |
  v
return WorkResult ──► Event Bus ──► CA Agent
```

Worker 类型：

| Worker | 职责 | Skill 映射 | 并行度 |
|--------|------|-----------|--------|
| `ShellWorker` | 执行本地 shell 命令 | Shell Skill | 多实例 |
| `CoderWorker` | 调用 Claude Code CLI 做代码任务 | Coder Skill | 单实例（资源锁限制） |
| `ResearchWorker` | 读文件、查笔记、做调研 | File Skill + Obsidian Skill | 多实例 |
| `ObsidianWorker` | 写入 Markdown 复盘 | Obsidian Skill | 单实例（避免写冲突） |

Worker 统一接口：

```python
from typing import Any, Literal
from pydantic import BaseModel

RiskLevel = Literal["low", "medium", "high", "critical"]

class WorkOrder(BaseModel):
    worker_type: Literal["shell", "coder", "research", "obsidian"]
    order_id: str
    ca_thread_id: str         # 所属 CA thread
    instruction: str          # 自然语言指令
    tool_name: str | None     # 指定工具（可选）
    tool_args: dict[str, Any] # 工具参数
    workdir: str | None
    resource_key: str | None
    risk_level: RiskLevel
    dod: str | None           # 完成定义
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

Worker 实现形态（渐进）：

| 阶段 | 实现 | 说明 |
|------|------|------|
| Phase 1 | Inline Worker | 同步内联调用，兼容当前代码 |
| Phase 2 | Thread Worker | `ThreadPoolExecutor` 异步执行 |
| Phase 3 | Subgraph Worker | Worker 是独立 LangGraph thread，有自己的 checkpoint |

### 3.5 Skill 执行层

Skill 职责不变：封装具体能力。但调用方从 CA Agent 变成了 Worker。

```python
class SkillRequest(TypedDict):
    skill: str
    action: str
    workdir: str | None
    args: dict[str, Any]
    risk_level: RiskLevel
    timeout_seconds: int

class SkillResult(TypedDict):
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    artifacts: list[str]
    summary: str
```

### 3.6 Persistence & Memory

职责扩展：

- 保存 CA Agent checkpoint（LangGraph）。
- 保存 Worker 状态（如果 Worker 是独立 thread）。
- 保存任务业务状态。
- 保存资源锁。
- 保存授权请求。
- 保存工具调用审计。
- 写入 Obsidian 复盘。

存储：

| 存储 | 用途 | MVP |
| --- | --- | --- |
| SQLite checkpoint | CA Agent 恢复 | 是 |
| SQLite task tables | 任务、锁、授权、审计 | 是 |
| SQLite worker_state | Worker 运行状态（Phase 3） | 否 |
| Obsidian Vault | 长期复盘和知识沉淀 | 是 |
| ChromaDB | 向量检索 | 否 |

## 4. 核心数据结构

### 4.1 Task（重构）

Task 不再直接映射到 Skill 调用，而是映射到 WorkOrder：

```python
class Task(TypedDict):
    id: str
    parent_id: str | None
    title: str
    description: str
    resource_key: str | None
    status: Literal["pending", "dispatched", "running", "waiting", "success", "failed", "blocked", "cancelled"]
    dod: str
    verification_cmd: str | None
    retry_count: int
    max_retries: int
    worker_type: str | None       # 新增：由哪个 Worker 执行
    order_id: str | None          # 新增：关联的 WorkOrder ID
    result_summary: str | None    # 新增：WorkResult 摘要
```

### 4.2 AgentState（CA Agent）

```python
from typing import Annotated, Any, Literal, TypedDict
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    thread_id: str
    messages: Annotated[list[Any], add_messages]
    event: dict[str, Any]
    resource_key: str | None

    # 任务状态
    task_list: list[Task]
    dispatch_queue: list[dict[str, Any]]   # 新增：待分发的 WorkOrder
    active_workers: dict[str, str]          # 新增：task_id -> order_id
    worker_results: dict[str, Any]          # 新增：order_id -> WorkResult

    status: Literal["created", "contextualizing", "strategizing", "dispatching",
                    "monitoring", "waiting_approval", "blocked", "completed", "failed"]

    # 授权和错误
    pending_approval_id: str | None
    error_count: int
    last_error: str | None

    # 记忆和压缩
    context_summary: str | None
    final_summary: str | None

    # 路由辅助（LangGraph 1.0+ Command 支持动态路由，可减少 next_node 依赖）
    next_node: str | None
```

### 4.3 ApprovalRequest

```python
class ApprovalRequest(TypedDict):
    approval_id: str
    thread_id: str
    task_id: str
    order_id: str | None      # 新增：关联 WorkOrder
    command: str
    workdir: str
    risk_level: Literal["high", "critical"]
    reason: str
    status: Literal["pending", "approved", "rejected", "expired"]
    created_at: str
    expires_at: str | None
```

## 5. 关键流程

### 5.1 CA Agent 主循环（事件驱动）

```text
外部事件注入
  │
  ▼
ingest_event
  │
  ▼
contextualize ──► 更新 context_summary
  │
  ▼
strategize ──► LLM 生成 WorkOrder 清单，写入 dispatch_queue
  │
  ▼
dispatch ──► 遍历 dispatch_queue，为每个 WorkOrder 启动 Worker
  │          更新 active_workers
  │
  ▼
monitor ──► 检查 active_workers
  │
  ├── 还有 Worker 在跑 ──► interrupt() ──► END（等待 worker_complete 事件）
  │
  ├── 全部完成，目标未达成 ──► strategize（重新规划）
  │
  ├── 全部完成，目标达成 ──► summarize ──► END
  │
  └── 需要审批 ──► risk_gate ──► interrupt() ──► END
```

**关键点**：CA Agent 会多次 `invoke`。每次外部事件（用户输入、Worker 完成、审批结果）都会触发一次 `invoke`。`interrupt()` 用于在中间状态安全挂起。

### 5.2 Worker 执行与回调

```text
Worker 收到 WorkOrder
  │
  ▼
内部执行（Skill 调用）
  │
  ▼
生成 WorkResult
  │
  ▼
写入审计日志
  │
  ▼
通过 Event Bus 发送 worker_complete 事件
  │
  ▼
Session Dispatcher 路由到对应 CA thread
  │
  ▼
CA Agent 被唤醒，进入 ingest_event ──► aggregate
```

### 5.3 同仓库插队（Worker 并行场景）

1. `thread_A` 的 CA Agent 派发 Worker 修改 `repoA`。
2. `thread_A` 持有 `repoA` 写锁，Worker 继承该锁。
3. 用户发送同仓库追加需求。
4. Dispatcher 发现 `repoA` 锁已被 `thread_A` 占用。
5. 新事件注入 `thread_A`。
6. CA Agent 在 `monitor` 或 `aggregate` 节点被唤醒，读取新事件，进入 `strategize` 重新生成 WorkOrder。

### 5.4 高危授权（Worker 场景）

1. CA Agent 的 `strategize` 生成高危 WorkOrder（如 `git push`）。
2. `dispatch` 节点发现风险等级为 high/critical。
3. CA Agent 进入 `risk_gate`，生成 `ApprovalRequest`，`interrupt()` 挂起。
4. Feishu Skill 发送授权卡片（由 CA Agent 调用，不是 Worker）。
5. 用户点击允许。
6. Webhook 收到回调，转成 `approval` 事件注入 CA thread。
7. CA Agent `resume`，`risk_gate` 通过，进入 `dispatch` 真正启动 Worker。

**区别**：v1.0 是执行前审批；v2.0 是**派发前审批**，Worker 只执行已审批的 WorkOrder。

### 5.5 断点恢复

1. 进程启动时扫描未完成 CA thread。
2. 读取 SQLite checkpoint。
3. 检查 `active_workers`：若 Worker 在进程崩溃前已完成，读取业务 DB 中的 WorkResult，合成 `worker_complete` 事件注入。
4. 检查 `pending_approval_id`：若正在等待授权，恢复后重新发送提醒。
5. 对可恢复 CA thread 从 `monitor` 或 `ingest_event` 节点继续。

## 6. 安全设计

### 6.1 命令风险分级（Worker 场景）

风险判断仍然在 CA Agent 层完成，Worker 执行的是**已审批的 WorkOrder**：

| 风险等级 | 判断时机 | 默认策略 |
| --- | --- | --- |
| Low | `strategize` 生成 WorkOrder 时 | 直接 dispatch Worker |
| Medium | `strategize` 生成 WorkOrder 时 | 直接 dispatch Worker，记录日志 |
| High | `dispatch` 前 | CA Agent 根据配置决定是否生成 ApprovalRequest |
| Critical | `dispatch` 前 | **必须人工授权**，Worker 才能收到 WorkOrder |

### 6.2 Worker 安全约束

- Worker 只能在 `workdir` 范围内操作。
- Worker 超时强制终止。
- Worker 输出截断，防止日志膨胀。
- Worker 进程级错误不得影响 CA Agent 主进程。

### 6.3 任务恢复安全

- 进程重启后恢复 CA thread 时，若 `active_workers` 中有未完成的 Worker，需检查 Worker 实际状态（可能已完成但回调丢失）。
- 对已有外部副作用的 WorkOrder（如已成功的 push）通过 `order_id` 幂等键避免重复执行。

## 7. 技术选型

| 类别 | 技术 | 说明 |
| --- | --- | --- |
| 运行平台 | Windows（MVP），后续兼容 macOS | Python 跨平台 |
| 编排框架 | LangGraph 1.0+ | 使用 `interrupt()`、`Command`、`astream` |
| Web 网关 | FastAPI + Uvicorn | 对外暴露事件接口 |
| 定时任务 | APScheduler | Cron 表达式触发内部事件 |
| 状态持久化 | SQLite + langgraph-checkpoint-sqlite | CA Agent checkpoint |
| 业务数据库 | SQLite | 任务、锁、授权、审计、Worker 状态 |
| Worker 执行 | ThreadPoolExecutor（Phase 2）/ asyncio（Phase 3） | 并行执行 |
| 长期知识库 | Obsidian Markdown | Vault 目录由配置指定 |
| 代码 Agent | Claude Code CLI | 封装为 CoderWorker |
| 通知入口 | 飞书 OpenAPI | 机器人 + 个人机器人 |

## 8. 架构风险

| 风险 | 影响 | 缓解策略 |
| --- | --- | --- |
| Worker 失控 | Worker 卡死或资源泄漏 | 超时、Worker 进程隔离、CA Agent 主动 cancel |
| Worker 回调丢失 | CA Agent 永远等待 | Worker 完成时写入业务 DB，恢复时扫描补偿 |
| CA Agent 上下文膨胀 | `messages` 无限增长 | `context_summary` 压缩，定期丢弃历史 messages |
| 并发 Worker 资源冲突 | 同一目录被多个 Worker 写 | 资源锁继承 + CA Agent 调度时排他 |
| 第三方 CLI 卡死 | Worker 阻塞 | Worker 内部超时、输出截断 |
| LangGraph interrupt 误用 | 状态机陷入不可恢复 | 所有 interrupt 必须有明确的 resume 路径和超时 |

## 9. 版本兼容性

v2.0 架构与 v1.0 代码的关系：

- `AgentEvent`、`Task`、`ApprovalRequest` 结构扩展，但核心字段兼容。
- `SkillRequest`、`SkillResult` 不变，Skill 层可直接复用。
- `nodes.py` 中的 `execute` 和 `verify` 逻辑可迁移到 `ShellWorker`。
- `graph.py` 需要重构，从线性图变成事件循环图。
- `runner.py` 需要从单次 `invoke` 改成支持多次 invoke + interrupt resume 的 `ThreadManager`。
