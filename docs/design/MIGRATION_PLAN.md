# Jarvis v1.0 → v2.0 改造项目计划

版本：v2.0
目标：将当前单图串行架构改造为 CA Agent + Worker 并行架构，适配 LangGraph 1.0+
状态：基于当前代码基线的迁移指南

---

## 1. 当前代码基线评估

### 1.1 已具备的基础（可直接复用）

| 模块 | 当前状态 | 复用度 |
|------|---------|--------|
| `app/agent/events.py` | `AgentEvent` 模型完整 | 80%，需扩展 `worker_complete`/`worker_failed` 类型 |
| `app/agent/state.py` | `AgentState`、`Task`、`PendingAction` 定义完整 | 50%，需重构加入 Worker 相关字段 |
| `app/agent/nodes.py` | `ingest_event`、`contextualize`、`risk_gate`、`summarize`、`blocked` 逻辑可用 | 60%，`plan`→`strategize` 改造，`execute`/`verify` 需移除或迁移 |
| `app/agent/graph.py` | `StateGraph` 构建完整 | 30%，需从线性图重构为事件循环图 |
| `app/agent/runner.py` | `GraphRunner` + `SqliteSaver` | 30%，需升级为支持多次 invoke + interrupt 的 `ThreadManager` |
| `app/skills/` | `ShellSkill`、`EchoSkill`、base 协议 | 90%，Skill 层无需改动，只需外加 Worker 封装 |
| `app/tools/` | `ToolRegistry`、`ToolSpec`、Function Calling | 80%，`strategize` 可直接复用 |
| `app/llm/deepseek.py` | `DeepSeekClient` 已支持 tool calling | 90%，`plan_tasks` 可改为 `strategize` 的底层调用 |
| `app/api/agent.py` | `POST /agent/run` | 50%，API 接口不变，内部调用方式需改为 `ThreadManager` |
| `app/config.py` | Pydantic Settings | 100%，无需改动 |

### 1.2 核心差距

| 差距项 | 当前 | 目标 |
|--------|------|------|
| 执行模型 | 单图串行，invoke 一次跑完 | 事件循环，多次 invoke + interrupt |
| LangGraph 版本 | 0.x（`next_node` + 条件边） | 1.0+（`interrupt()` + `Command` + `astream`） |
| 决策与执行 | 混在一起（`execute` 节点直接调 Skill） | 分离（CA Agent 决策，Worker 执行） |
| 并行能力 | 无 | Thread Worker 可并行 |
| 异步等待 | 无（同步阻塞） | `interrupt()` 挂起，Worker 回调唤醒 |
| Worker 抽象 | 无 | `WorkOrder`/`WorkResult` + `WorkerClient` 协议 |
| 回调补偿 | 无 | 业务 DB 扫描补偿 |
| 资源锁 | 仅设计文档定义 | 需实现锁继承和 Worker 级锁 |

---

## 2. 改造总体策略

**不推翻重写，渐进式桥接**。

Phase 1 的核心思路：**保留现有 Skill 代码，用 `InlineWorkerClient` 包装它们，让 CA Agent 图跑起来**。此时没有真并行，但架构接口已经对齐。现有测试用例只需少量调整即可通过。

---

## 3. Phase 1：CA Agent 骨架 + Inline Worker（兼容层）

**目标**：CA Agent 事件循环可跑通，Worker 用 Inline 兼容现有代码。

### Step 1：依赖升级（已满足）

**文件**：`pyproject.toml`

**改动**：
- 升级 `langgraph` 到 `>=1.1.8`（含 `interrupt`、`Command`）
- 确认 `langgraph-checkpoint-sqlite` 兼容版本
- 如需流式支持，确认 `asyncio` 基础设施就绪

**验收**：当前项目已满足，`langgraph==1.1.8`、`langgraph-checkpoint-sqlite==3.0.3` 可用。

### Step 2：数据模型扩展（2-3 小时）

**文件**：`app/agent/events.py`、`app/tools/specs.py`（新增 WorkOrder/WorkResult）

**改动**：
- `events.py`：扩展 `AgentEventType`，增加 `"worker_complete"`、`"worker_failed"`
- 新增 `app/workers/base.py`：
  - `WorkOrder`（Pydantic BaseModel）
  - `WorkResult`（Pydantic BaseModel）
  - `WorkerClient` Protocol

**验收**：模型可序列化/反序列化，类型检查通过。

### Step 3：AgentState 重构（2-3 小时）

**文件**：`app/agent/state.py`

**改动**：
- `AgentState` 新增字段：
  - `dispatch_queue: list[dict[str, Any]]`
  - `active_workers: dict[str, str]`（task_id -> order_id）
  - `worker_results: dict[str, Any]`
- `Task` 新增字段：
  - `worker_type: str | None`
  - `order_id: str | None`
  - `result_summary: str | None`
- `AgentRunStatus` 新增 `"strategizing"`、`"dispatching"`、`"monitoring"`

**注意**：`initial_state` 需初始化新字段。

**验收**：单元测试通过，`initial_state` 不报错。

### Step 4：Worker 抽象层（2-3 小时）

**文件**：新增 `app/workers/__init__.py`、`app/workers/inline.py`

**改动**：
- `app/workers/inline.py` 实现 `InlineWorkerClient`：
  - `dispatch(order: WorkOrder) -> str`：直接调用对应 Skill，同步返回
  - `poll(order_id: str) -> WorkResult | None`：Inline 模式下直接返回结果
- `app/workers/shell.py`：封装 `ShellSkill` 为 Worker（`ShellWorker`）
- `app/workers/coder.py`：预留 `CoderWorker`（返回未实现）

**关键代码**：

```python
class InlineWorkerClient:
    def dispatch(self, order: WorkOrder) -> str:
        if order.worker_type == "shell":
            result = ShellSkill().run(SkillRequest(...))
        elif order.worker_type == "echo":
            result = EchoSkill().run(SkillRequest(...))
        else:
            result = SkillResult(ok=False, summary="Worker not implemented")
        self._results[order.order_id] = WorkResult.from_skill_result(order, result)
        return order.order_id

    def poll(self, order_id: str) -> WorkResult | None:
        return self._results.get(order_id)
```

**验收**：`ShellWorker` 接收 `WorkOrder` 返回 `WorkResult`，exit_code/stdout 正确。

### Step 5：节点重构（核心，4-6 小时）

**文件**：`app/agent/nodes.py`（大改）

**新增/改造节点**：

| 节点 | 动作 | 说明 |
|------|------|------|
| `ingest_event` | 改造 | 增加解析 `worker_complete` payload，写入 `worker_results` |
| `contextualize` | 保留 | 基本不变 |
| `plan` | **改名** `strategize` | 输出从 `task_list` 变为 `dispatch_queue` + `task_list` |
| `select_task` | **删除** | 不再需要，调度逻辑移到 `dispatch` |
| `prepare_action` | **删除** | 不再需要，Worker 参数直接在 `strategize` 生成 |
| `execute` | **删除** | 执行逻辑迁移到 `InlineWorkerClient` |
| `verify` | **删除** | 验证是 Worker 内部的事 |
| `dispatch` | **新增** | 遍历 `dispatch_queue`，调 `InlineWorkerClient.dispatch()`，更新 `active_workers` |
| `monitor` | **新增** | Inline 模式下 `active_workers` 会立即完成，直接进 `aggregate` |
| `aggregate` | **新增** | 将 `worker_results` 映射回 `task_list`，判断下一步 |
| `risk_gate` | 改造 | 从"执行前审批"变成"dispatch 前审批" |
| `summarize` | 保留 | 基本不变 |
| `blocked` | 保留 | 增加取消 active_workers 逻辑 |

**`strategize` 的实现**：

复用现有 `_llm_plan` 逻辑，但输出改为 `WorkOrder` 列表：

```python
def strategize(state: AgentState) -> dict[str, Any]:
    # 复用 DeepSeekClient.plan_tasks，但解析为 WorkOrder 而不是 Task
    planned_calls = client.plan_tasks(...)
    tasks = []
    dispatch_queue = []
    for item in planned_calls:
        order_id = str(uuid4())
        task = Task(..., worker_type=item.tool_name, order_id=order_id)
        order = WorkOrder(
            worker_type=_map_tool_to_worker(item.tool_name),
            order_id=order_id,
            ...
        )
        tasks.append(task)
        dispatch_queue.append(order.model_dump())
    return {"task_list": tasks, "dispatch_queue": dispatch_queue, "status": "strategizing"}
```

**`dispatch` 的实现**：

```python
def dispatch(state: AgentState) -> dict[str, Any]:
    client = get_inline_worker_client()
    active_workers = dict(state["active_workers"])
    task_list = [t.copy() for t in state["task_list"]]

    for order_dict in state["dispatch_queue"]:
        order = WorkOrder(**order_dict)
        # 风险预检
        if order.risk_level in {"high", "critical"}:
            # 不 dispatch，标记 waiting，进入 risk_gate
            continue
        order_id = client.dispatch(order)
        # 更新 task 状态
        for task in task_list:
            if task["order_id"] == order_id:
                task["status"] = "running"
        active_workers[order_dict["id"]] = order_id  # 需要 task_id -> order_id 映射

    return {"active_workers": active_workers, "task_list": task_list, "dispatch_queue": []}
```

**注意**：Inline 模式下 Worker 是同步的，`dispatch` 返回时 Worker 已经完成。但为了保持架构一致性，`monitor` 节点仍然检查 `active_workers`，然后立刻发现它们已完成。

**`aggregate` 的实现**：

```python
def aggregate(state: AgentState) -> Command:
    task_list = _merge_worker_results(state)
    if _all_success(state):
        return Command(goto="summarize", update={"task_list": task_list, "status": "completed"})
    if _has_failed_and_can_retry(state):
        return Command(goto="strategize", update={"task_list": task_list, "status": "strategizing"})
    return Command(goto="blocked", update={"task_list": task_list, "status": "blocked"})
```

**验收**：一条简单任务（如 `python --version`）可走完完整循环。

### Step 6：Graph 重构（2-3 小时）

**文件**：`app/agent/graph.py`

**改动**：
- 删除 `select_task`、`prepare_action`、`execute`、`verify` 节点注册
- 新增 `strategize`、`dispatch`、`monitor`、`aggregate` 节点
- 边结构改为事件循环：

```python
def build_ca_graph(checkpointer=None):
    graph = StateGraph(AgentState)
    graph.add_node("ingest_event", ingest_event)
    graph.add_node("contextualize", contextualize)
    graph.add_node("strategize", strategize, retry=RetryPolicy(max_attempts=3))
    graph.add_node("dispatch", dispatch)
    graph.add_node("risk_gate", risk_gate)
    graph.add_node("monitor", monitor)
    graph.add_node("aggregate", aggregate)
    graph.add_node("summarize", summarize)
    graph.add_node("blocked", blocked)

    graph.add_edge(START, "ingest_event")
    graph.add_edge("ingest_event", "contextualize")
    graph.add_edge("contextualize", "strategize")
    graph.add_edge("strategize", "dispatch")
    graph.add_edge("dispatch", "risk_gate")
    graph.add_conditional_edges("risk_gate", route_after_risk_gate, {
        "monitor": "monitor",
        "blocked": "blocked",
    })
    graph.add_conditional_edges("monitor", route_after_monitor, {
        "aggregate": "aggregate",
        "interrupt": END,  # monitor 挂起后结束当前 invoke
    })
    graph.add_conditional_edges("aggregate", aggregate, {
        "strategize": "strategize",
        "summarize": "summarize",
        "blocked": "blocked",
    })
    graph.add_edge("summarize", END)
    graph.add_edge("blocked", END)
    return graph.compile(checkpointer=checkpointer)
```

**注意**：Inline 模式下 `monitor` 不会真的挂起（因为 Worker 已同步完成），但代码结构要保留 `interrupt()` 的语义，为 Phase 2 做准备。

**验收**：图编译通过，无循环引用错误。

### Step 7：Runner 升级为 ThreadManager（3-4 小时）

**文件**：`app/agent/runner.py`（大改或新增 `app/agent/manager.py`）

**改动**：
- `GraphRunner` 重命名为 `ThreadManager`
- `run_event` 从同步单次 invoke 改为支持多次 invoke + resume
- 增加 `resume_worker(result: WorkResult)` 方法（Phase 1 先留接口）
- Inline 模式下，`run_event` 内部流程：

```python
class ThreadManager:
    async def run_event(self, event: AgentEvent) -> AsyncIterator[AgentState]:
        thread_id = event.thread_id or str(uuid4())
        state = initial_state(event, thread_id)
        config = {"configurable": {"thread_id": thread_id}}

        # 第一次 invoke
        async for snapshot in self._graph.astream(state, config=config, stream_mode="values"):
            yield snapshot

        # Phase 1 Inline 模式下，一次 invoke 就会结束
        # Phase 2 时，这里会变成循环，处理 interrupt 和 resume
```

**验收**：`POST /agent/run` 返回正确的 `thread_id`、状态、摘要。

### Step 8：API 层适配（1 小时）

**文件**：`app/api/agent.py`

**改动**：
- 将 `GraphRunner` 替换为 `ThreadManager`
- 由于 Phase 1 还是同步返回，API 接口不变

**验收**：端到端测试通过。

### Step 9：测试迁移（2-3 小时）

**文件**：`tests/test_agent.py`

**改动**：
- 更新测试预期状态（`"strategizing"` 替代 `"planning"`）
- 验证 `dispatch_queue` 和 `task_list` 的联动
- 保留 `test_health.py` 不变

**验收**：`pytest` 全部通过。

---

## 4. Phase 2：Thread Worker + 并行 + 中断恢复

**目标**：Worker 真正异步并行，CA Agent 可 interrupt 挂起，Worker 回调唤醒。

### Step 10：LangGraph `interrupt()` 接入（2-3 小时）

**文件**：`app/agent/nodes.py`（`monitor` 节点）

**改动**：

```python
from langgraph.types import interrupt

def monitor(state: AgentState) -> dict[str, Any]:
    if state["active_workers"]:
        # 检查 Worker 是否已完成（通过 poll）
        client = get_thread_worker_client()
        all_done = all(client.poll(oid) is not None for oid in state["active_workers"].values())
        if not all_done:
            interrupt({"type": "wait_workers", "active_workers": state["active_workers"]})
    return {"status": "monitoring"}
```

### Step 11：ThreadWorkerClient 实现（3-4 小时）

**文件**：新增 `app/workers/threaded.py`

**改动**：
- `ThreadWorkerClient` 使用 `ThreadPoolExecutor`
- `dispatch` 非阻塞，返回 `order_id`
- `poll` 查询 `Future` 状态
- Worker 完成后写入业务 DB，然后回调 Event Bus

```python
class ThreadWorkerClient:
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._futures: dict[str, Future] = {}
        self._event_bus = get_event_bus()

    def dispatch(self, order: WorkOrder) -> str:
        future = self._executor.submit(_run_worker, order)
        self._futures[order.order_id] = future
        # 添加完成回调
        future.add_done_callback(lambda f: self._on_done(order, f))
        return order.order_id

    def _on_done(self, order: WorkOrder, future: Future):
        result = future.result()
        # 写入业务 DB
        save_work_result(result)
        # 发布事件到 Event Bus
        self._event_bus.publish("worker_complete", result.model_dump())
```

### Step 12：Event Bus + Dispatcher 改造（3-4 小时）

**文件**：新增 `app/services/event_bus.py`，改造 `app/services/dispatcher.py`

**改动**：
- `EventBus`：简单的内存队列 + 订阅者模式
- `DispatcherService`：订阅 `worker_complete` 事件，路由到对应 CA thread
- Dispatcher 收到事件后调用 `ThreadManager.resume_worker()`

### Step 13：ThreadManager 完整实现（3-4 小时）

**文件**：`app/agent/runner.py`

**改动**：
- `run_event` 支持 `astream`
- 新增 `resume_worker(result: WorkResult)`：

```python
async def resume_worker(self, result: WorkResult) -> AsyncIterator[AgentState]:
    config = {"configurable": {"thread_id": result.ca_thread_id}}
    async for snapshot in self._graph.astream(
        Command(resume={"event_type": "worker_complete", "payload": result.model_dump()}),
        config=config,
        stream_mode="values",
    ):
        yield snapshot
```

### Step 14：资源锁实现（3-4 小时）

**文件**：新增 `app/persistence/locks.py`

**改动**：
- `ResourceLockRepository`：SQLite 表操作
- 锁策略：悲观锁，支持 Worker 继承 CA thread 锁
- `dispatch` 节点在启动 Worker 前检查锁

### Step 15：回调补偿机制（2-3 小时）

**文件**：`app/agent/runner.py`（`recover_unfinished`）

**改动**：
- 扫描 checkpoint 中 `monitoring` 状态的 thread
- 检查 `active_workers` 的 `order_id`
- 查询 `work_results` 表
- 若结果已存在，合成 `worker_complete` 事件注入

---

## 5. Phase 3：Subgraph Worker + 完整生态

**目标**：Worker 自身具备 plan-execute-verify 能力，飞书集成，长期记忆。

### Step 16：Subgraph Worker（高优先级可选）

**文件**：新增 `app/workers/subgraph.py`

**改动**：
- `SubgraphWorkerClient`：每个 Worker 是独立 LangGraph thread
- Worker 内部有自己的 checkpoint
- Worker 完成后通过 Event Bus 回调

### Step 17：飞书集成

**文件**：`app/services/feishu.py`、`app/api/routes.py`

**改动**：
- Webhook 接收飞书消息，转为 `AgentEvent`
- 飞书授权卡片回调，转为 `approval` 事件
- Feishu Skill 发送消息和卡片

### Step 18：长期记忆

**文件**：`app/agent/nodes.py`（`contextualize` 节点）

**改动**：
- `contextualize` 接入 LLM，做上下文压缩
- 生成 `context_summary`，定期清理 `messages`
- Obsidian 双向同步

---

## 6. 文件变更清单

### 新增文件

```text
app/workers/__init__.py
app/workers/base.py              # WorkOrder, WorkResult, WorkerClient
app/workers/inline.py            # InlineWorkerClient（Phase 1）
app/workers/threaded.py          # ThreadWorkerClient（Phase 2）
app/workers/shell.py             # ShellWorker 封装
app/workers/coder.py             # CoderWorker 封装
app/workers/obsidian.py          # ObsidianWorker 封装
app/services/event_bus.py        # Event Bus（Phase 2）
app/persistence/locks.py         # 资源锁 Repository（Phase 2）
app/persistence/worker_state.py  # Worker 状态持久化（Phase 2）
```

### 大幅修改文件

```text
app/agent/state.py               # AgentState 重构
app/agent/nodes.py               # 节点大改（删除 execute/verify，新增 strategize/dispatch/monitor/aggregate）
app/agent/graph.py               # 图结构重构
app/agent/runner.py              # GraphRunner -> ThreadManager
app/api/agent.py                 # 适配 ThreadManager
```

### 小幅修改文件

```text
app/agent/events.py              # 扩展事件类型
app/llm/deepseek.py              # plan_tasks 可复用，可能改名为 strategize
app/tools/specs.py               # ToolCallPlan 可复用
tests/test_agent.py              # 测试预期更新
pyproject.toml                   # LangGraph 版本升级
```

### 无需修改文件

```text
app/skills/base.py               # Skill 协议不变
app/skills/shell.py              # Skill 实现不变
app/skills/echo.py               # Skill 实现不变
app/config.py                    # 配置系统不变
app/api/routes.py                # health check 不变
app/logging_config.py            # 日志不变
```

---

## 7. 风险与回滚策略

| 风险 | 影响 | 缓解策略 |
|------|------|---------|
| LangGraph 1.0+ API 不稳定 | 中断/Command 行为与文档不符 | 先用最小图验证 interrupt + resume，再集成到完整图 |
| Phase 1 改造过大导致测试崩 | 现有功能不可用 | 保留 v1.0 分支，Phase 1 每步都跑通测试再下一步 |
| Thread Worker 并发导致资源冲突 | 文件损坏 | Phase 2 必须等资源锁实现后才能上 Thread Worker |
| Worker 回调丢失 | CA Agent 永久挂起 | Phase 2 必须实现回调补偿后才能上 Thread Worker |
| `AgentState` 结构变更导致 checkpoint 不兼容 | 恢复失败 | 变更 `AgentState` 时同时更新 `initial_state`，旧 checkpoint 可废弃 |
| LLM strategize 成本过高 | 每次 Worker 完都调 LLM | Phase 1 aggregate 先用规则判断，仅在失败时调 LLM |

**回滚策略**：
- 保留 `master` 分支作为 v1.0 基线。
- 在 `feat/ca-worker` 分支上进行改造。
- Phase 1 完成后若发现架构问题，可回退到 v1.0，仅损失 Phase 1 工作量。
- Skill 层代码（`app/skills/`）始终不变，是回滚的安全垫。

---

## 8. 验收检查清单

### Phase 1 验收

- [x] `pytest` 全部通过（含改造后的 test_agent）
- [x] `POST /agent/run` 返回 `thread_id`、`status=completed`、摘要
- [x] `ShellWorker` 接收 `WorkOrder`，返回正确的 `WorkResult`
- [x] 高危命令（如 `git push`）进入 `blocked` 或 `waiting_approval`
- [x] `AgentState` 包含 `dispatch_queue`、`active_workers`
- [x] SQLite checkpoint 可记录 CA Agent 状态

当前第一版状态：已完成 CA Agent + Inline Worker 垂直切片，并移除旧的单图串行兼容路径。下一阶段进入 SQLite 业务表、Repository、审计日志和查询 API。

### Phase 2 验收

- [ ] 两个无冲突的 `ShellWorker` 可同时执行
- [ ] CA Agent `monitor` 节点可 `interrupt()` 挂起
- [ ] Worker 完成后自动唤醒 CA Agent
- [ ] Worker 回调丢失后，重启可补偿恢复
- [ ] 同资源写任务不会并发执行
- [ ] Worker 超时后可被取消

---

## 9. 建议的每日开发节奏

| 天数 | 任务 | 产出 |
|------|------|------|
| Day 1 | Step 1 + Step 2 | 依赖升级，数据模型扩展 |
| Day 2 | Step 3 + Step 4 | AgentState 重构，InlineWorkerClient |
| Day 3 | Step 5（上） | strategize + dispatch 节点 |
| Day 4 | Step 5（下） + Step 6 | monitor + aggregate + graph 重构 |
| Day 5 | Step 7 + Step 8 + Step 9 | ThreadManager，API 适配，测试通过 |
| Day 6-7 | 缓冲/联调 | Phase 1 验收 |

---

## 10. 关键决策点

在改造过程中，以下决策需要在对应阶段做出：

1. **Day 1**：`strategize` 是否复用现有的 `_llm_plan` 逻辑？**建议**：复用，只改输出格式为 `WorkOrder`。
2. **Day 2**：`InlineWorkerClient` 是否放在 `app/workers/` 还是 `app/agent/`？**建议**：`app/workers/`，保持层边界。
3. **Day 3**：`aggregate` 在 Phase 1 是否接入 LLM？**建议**：不接入，用规则判断，降低复杂度。
4. **Day 4**：`monitor` 在 Phase 1 是否保留 `interrupt()` 代码？**建议**：保留，但 Inline 模式下不会触发，为 Phase 2 留接口。
5. **Day 5**：是否保留 v1.0 的 `plan` 节点作为 fallback？**建议**：保留 `rule_based_plan` 逻辑，封装在 `strategize` 内部作为降级。
