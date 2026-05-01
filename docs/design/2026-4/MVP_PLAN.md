# Jarvis MVP 开发计划（2026-04-21）

版本：v2.0
状态：CA Agent + Worker 架构，分阶段实施
日期：2026-04-21
目标：先交付一个可本地运行、支持 CA Agent 事件循环、Worker 抽象、状态持久化、风险拦截的 Jarvis Agent。飞书接入后置到集成阶段。

## 1. 路线调整说明

v2.0 的核心架构变化：

1. **引入 CA Agent + Worker 分离架构**：CA Agent 负责决策和上下文管理，Worker 负责执行。
2. **LangGraph 1.0+ 升级**：使用 `interrupt()`、`Command`、`astream` 替代旧的状态机模式。
3. **事件循环驱动**：CA Agent 不再是单次跑完的流水线，而是可多次 invoke、挂起、恢复的事件循环。
4. **Worker 渐进式实现**：Phase 1 用 Inline Worker 兼容现有代码，Phase 2 引入真并行 Thread Worker，Phase 3 引入 Subgraph Worker。

## 2. MVP 交付目标

MVP 需要证明以下核心闭环成立：

1. 用户通过本地 CLI 或 HTTP API 发起任务。
2. 系统将输入转为标准内部事件 `AgentEvent`。
3. CA Agent 创建任务会话，用 LangGraph 维护 `AgentState`。
4. CA Agent 拆解意图、生成 WorkOrder、调度 Worker。
5. Worker 执行 Skill、返回结果。
6. CA Agent 聚合结果，判断目标达成或重新规划。
7. 高危命令在 dispatch 前被识别并进入授权等待。
8. 执行结果可查询、可审计、可归档。
9. 进程重启后可恢复未完成 CA thread。
10. Worker 回调丢失可通过业务 DB 补偿。
11. 后续可无缝接入飞书作为外部入口。

## 3. 里程碑拆分

### M0：项目基础与配置

状态：已完成。

目标：建立可运行的工程骨架。

| 编号 | 任务 | 产出 | 优先级 | 状态 |
| --- | --- | --- | --- | --- |
| M0-1 | 确认 Python 项目结构 | `app/`、`tests/`、`docs/` 基础结构 | P0 | Done |
| M0-2 | 建立配置系统 | 环境变量和 `.env` 配置 | P0 | Done |
| M0-3 | 建立日志系统 | 控制台日志和文件日志 | P0 | Done |
| M0-4 | 建立本地开发启动脚本 | `uv run jarvis` / Uvicorn 启动 | P0 | Done |
| M0-5 | 增加健康检查接口 | `GET /health` | P0 | Done |

v2.0 新增：

| 编号 | 任务 | 产出 | 优先级 | 状态 |
| --- | --- | --- | --- | --- |
| M0-6 | 升级 LangGraph 到 1.0+ | `langgraph>=1.1.8`（含 `interrupt`、`Command`） | P0 | Done |
| M0-7 | 引入异步基础设施 | `asyncio` 兼容的 FastAPI、HTTPX | P1 | Pending |

---

### Phase 1：CA Agent + Inline Worker（单线程兼容层）

目标：建立 CA Agent 事件循环骨架，Worker 用 Inline 模式兼容现有 Skill 代码。此阶段 Worker 是同步的，不并行，但接口已抽象为 `WorkOrder`/`WorkResult`。

#### M1：CA Agent 最小骨架

| 编号 | 任务 | 产出 | 优先级 | 状态 |
| --- | --- | --- | --- | --- |
| P1-M1-1 | 扩展 `AgentEvent` | 增加 `worker_complete`、`worker_failed` 类型 | P0 | Done |
| P1-M1-2 | 定义 `WorkOrder`、`WorkResult` | Worker 通信契约 | P0 | Done |
| P1-M1-3 | 重构 `AgentState` | 增加 `dispatch_queue`、`active_workers`、`worker_results` | P0 | Done |
| P1-M1-4 | 实现 CA Agent 核心节点 | `ingest_event`、`contextualize`、`strategize`、`dispatch`、`monitor`、`aggregate`、`summarize` | P0 | Done |
| P1-M1-5 | 构建事件循环 Graph | 使用 LangGraph 1.0+ `add_edge`、`add_conditional_edges` | P0 | Done |
| P1-M1-6 | 实现 `InlineWorkerClient` | 同步调用 Skill，返回 `WorkResult` | P0 | Done |

验收标准：

- 一条文本任务可走完 `ingest -> strategize -> dispatch(Inlined) -> monitor -> aggregate -> summarize`。
- `strategize` 生成 `WorkOrder`，`dispatch` 调用 `InlineWorkerClient`。
- 返回 `thread_id`、状态和任务摘要。

#### M2：本地任务模型与 SQLite 持久化

| 编号 | 任务 | 产出 | 优先级 | 状态 |
| --- | --- | --- | --- | --- |
| P1-M2-1 | 设计 SQLite 业务表 | `tasks`、`work_orders`、`work_results`、`approvals`、`audit_logs`、`runs` | P0 | Done |
| P1-M2-2 | 实现数据库初始化 | ThreadManager 初始化业务 DB | P0 | Done |
| P1-M2-3 | 实现 Task Repository | 任务创建、更新、查询 | P0 | Done |
| P1-M2-4 | 实现 WorkOrder/Result Repository | Worker 派发和结果记录 | P0 | Done |
| P1-M2-5 | 实现 Audit Repository | Skill 调用和关键状态审计 | P0 | Done |
| P1-M2-6 | 接入 LangGraph SQLite checkpoint | 节点状态可恢复 | P0 | Done |

验收标准：

- 每次任务运行都有数据库记录。
- CA Agent 关键状态能 checkpoint。
- 可通过 API 或 CLI 查询任务状态。
- 审计日志记录每次 Worker 调用。

当前进展：

- 已有业务 DB schema、Repository、run/task/work_order/work_result/approval/audit 的基础写入。
- 已有 `/agent/runs` 和 `/agent/runs/{thread_id}` 查询入口。
- WorkOrder 审批后保留原始 risk / verification / timeout；WorkResult 已持久化 artifacts。
- `runs.thread_id` 已按单 thread 单 run 模型去重更新。
- 仍需把审计粒度下沉到每次 dispatch / Skill 调用，并补 Worker 回调补偿。

#### M3：Skill 执行层（复用 + 抽象）

| 编号 | 任务 | 产出 | 优先级 | 状态 |
| --- | --- | --- | --- | --- |
| P1-M3-1 | 定义 Skill 统一接口 | `SkillRequest`、`SkillResult`（保持兼容） | P0 | Done |
| P1-M3-2 | 实现 Echo Skill | 用于测试 Graph 和任务流 | P0 | Done |
| P1-M3-3 | 实现 Shell Skill | 本地命令执行 | P0 | Done |
| P1-M3-4 | 实现命令超时 | 防止进程卡死 | P0 |
| P1-M3-5 | 实现输出截断 | 防止日志和上下文膨胀 | P0 |
| P1-M3-6 | 实现 `ShellWorker` | 封装 Shell Skill 为 Worker | P0 |
| P1-M3-7 | 实现 Obsidian/File Skill | 写入本地 Markdown 复盘 | P1 |
| P1-M3-8 | 实现 Skill Registry | 新 Skill 可注册接入，不改 Worker executor | P0 | Done |
| P1-M3-9 | 实现 Tavily Search Skill | 外部 `tavily_search` 工具 | P1 | Done |
| P1-M3-10 | 实现 Image Generation Skill | 画图工具，产物写入 artifacts | P1 | Pending |

验收标准：

- Skill 调用结果结构化返回。
- Shell 命令包含工作目录、超时、退出码、stdout/stderr。
- `ShellWorker` 接收 `WorkOrder`，返回 `WorkResult`。

当前进展：

- 已新增 `SkillRegistry`，`execute_work_order()` 通过 skill 名分发，避免继续在 executor 中手写 if/elif。
- 已新增外部 Skill bootstrap/loader，支持 `data/skills/`、`~/.jarvis/skills/`、`JARVIS_SKILL_PATH`。
- 已接入外部 Tavily `tavily_search` skill，配置项为 `JARVIS_TAVILY_API_KEY`。
- 内置 `web_search` 已移除，搜索能力统一走外部 skill 包。
- 后续画图工具应按同一模式实现 `ImageGenerationSkill`，图片写入 `data/artifacts/images/...`，路径放入 `SkillResult.artifacts`。

#### M4：风险分级与本地授权（LangGraph 1.0+ interrupt）

| 编号 | 任务 | 产出 | 优先级 | 状态 |
| --- | --- | --- | --- | --- |
| P4-M4-1 | 定义命令风险规则 | low/medium/high/critical | P0 | Done |
| P4-M4-2 | 实现 `risk_gate` 节点 | 执行前风险判断 | P0 | In Progress |
| P4-M4-3 | 实现 `interrupt()` 审批挂起 | 高危动作进入 `waiting_approval` | P0 | Done |
| P4-M4-4 | 实现本地 `ApprovalRequest` | 授权请求生命周期 | P0 | Done |
| P4-M4-5 | 实现授权 API/CLI | 本地允许或拒绝授权 | P0 | Done |
| P4-M4-6 | 实现 `Command(resume=...)` 恢复 | 授权后 Graph 继续 | P0 | Done |
| P4-M4-7 | 实现拒绝后的阻塞状态 | 拒绝后不执行危险动作 | P0 | Done |

验收标准：

- `git push`、递归删除、生产部署类命令会被拦截。
- 未授权前不 dispatch Worker。
- 本地授权允许后 CA Agent 能继续（`Command(resume=...)`）。
- 拒绝后任务进入 `blocked`。
- interrupt 状态可 checkpoint，恢复后重新进入 `risk_gate`。

当前进展：审批最小闭环已跑通；审批 API 已支持 `approval_id` 校验；风险判断仍内嵌在 `dispatch`，还未拆成独立 `risk_gate` 节点。

#### M5：恢复、查询与归档

| 编号 | 任务 | 产出 | 优先级 | 状态 |
| --- | --- | --- | --- | --- |
| P1-M5-1 | 启动时扫描未完成 CA thread | 恢复入口 | P0 | Done |
| P1-M5-2 | 恢复 LangGraph checkpoint | 从最后成功节点继续 | P0 | Done |
| P1-M5-3 | 处理等待授权任务 | 重启后仍能查询并授权 | P0 | Done |
| P1-M5-4 | 补偿 Worker 回调丢失 | 扫描 `work_results` 表，合成事件注入 | P0 | In Progress |
| P1-M5-5 | 实现任务状态查询 | API/CLI 返回任务摘要 | P0 | Done |
| P1-M5-6 | 实现任务结果报告 | 本地 JSON/Markdown 报告 | P0 | Done |
| P1-M5-7 | 实现复盘模板 | 写入 `data/notes` 或 Obsidian Vault | P0 | Done |

验收标准：

- 重启后能发现未完成 CA thread。
- 等待授权任务不会丢失。
- Worker 完成但回调丢失时可补偿恢复。
- 完成任务能生成结构化报告和 Markdown 复盘。

当前进展：

- `ThreadManager.inspect_run()` 可返回 run、tasks、work_orders、work_results、approvals、audit_logs。
- `/agent/runs/{thread_id}` 已返回完整运行明细；`/agent/recover` 可触发恢复扫描。
- 已支持重启后查询等待授权任务。
- 已支持最小 Worker 结果补偿：`dispatched` WorkOrder 若已有 `work_results`，会合成 `worker_complete` / `worker_failed` resume graph。
- 剩余工作：补偿策略更细化、重复回调/多 Worker 部分完成边界、Worker 超时取消。

---

### Phase 2：并行 Worker + 资源锁

目标：引入真正的并行执行能力。`ThreadWorkerClient` 使用 `ThreadPoolExecutor` 异步执行，多个 Worker 可同时跑。

#### M6：Thread Worker 并行执行

| 编号 | 任务 | 产出 | 优先级 | 状态 |
| --- | --- | --- | --- | --- |
| P2-M6-1 | 实现 `ThreadWorkerClient` | `ThreadPoolExecutor` 异步执行 | P0 | Done |
| P2-M6-2 | 实现 `monitor` + `interrupt()` 异步等待 | CA Agent 在 Worker 跑时挂起 | P0 | Done |
| P2-M6-3 | 实现 Worker 完成事件路由 | `EventBus` + `DispatcherService` 投递回调 | P0 | Done |
| P2-M6-4 | 实现 `Command(resume=worker_result)` | Worker 完成后唤醒 CA Agent | P0 | Done |
| P2-M6-5 | 实现 Worker 超时取消 | `cancel()` 终止超时的 Worker | P1 | Pending |
| P2-M6-6 | 实现 `aggregate` 的 LLM 决策 | 复杂场景接入 LLM 判断是否重新规划 | P1 | In Progress |

验收标准：

- 多个无资源冲突的 Worker 可同时执行。
- CA Agent 在 Worker 执行期间处于挂起状态，不占 CPU。
- Worker 完成后自动唤醒 CA Agent。
- 超时 Worker 可被强制取消。

当前进展：

- 已新增 `ThreadWorkerClient`，通过 `JARVIS_WORKER_MODE=thread` 切换。
- Skill 执行逻辑已抽为 `execute_work_order()`，Inline / Thread Worker 复用同一执行路径。
- Thread Worker 可异步 dispatch，`poll()` 在 Future 完成后返回 `WorkResult`。
- 已新增 in-process `WorkerEventBus` 和 `DispatcherService`，Thread Worker Future 完成后自动发布 `worker_complete` / `worker_failed` 并 resume CA thread。
- FastAPI 在 `JARVIS_WORKER_MODE=thread` 时会随 lifespan 启停 DispatcherService。
- aggregate 已接入规则优先的 LLM completion assessment：确定性规则先判断，非客观成功任务才调用 DeepSeek 判断 DoD 是否满足。
- LLM assessment 可返回 `replan`，aggregate 会保留旧任务历史并回到 `strategize` 追加新任务。
- summarize 已接入 LLM final answer synthesis：基于用户原始指令、task 和 worker stdout/stderr 生成面向用户的最终回答，而不是仅返回任务状态摘要。
- 搜索类任务已增加 final answer fallback：当 DeepSeek 因内容风控等原因拒绝合成时，从搜索结果 JSON/Markdown/plain text 中提取摘要片段和来源 URL。
- 剩余工作：Worker 超时取消、把 replan 的失败上下文更完整地传给 LLM planner、搜索 fallback 的摘要质量优化，以及进入 M7 资源锁。

#### M7：资源锁与会话调度

| 编号 | 任务 | 产出 | 优先级 | 状态 |
| --- | --- | --- | --- | --- |
| P2-M7-1 | 定义 `resource_key` 规则 | 目录、仓库、文件等资源标识 | P0 | Done（基础规则） |
| P2-M7-2 | 实现 Resource Lock Repository | 加锁、释放、查询 | P0 | Done |
| P2-M7-3 | 实现 Worker 锁继承 | Worker 可继承 CA thread 的锁 | P0 | Done（thread 级锁） |
| P2-M7-4 | 实现 Session Dispatcher | 输入事件映射到 thread_id，Worker 回调路由 | P0 |
| P2-M7-5 | 实现同资源写任务串行 | 防止同仓库并发写 | P0 | In Progress（冲突拦截已完成，排队待做） |
| P2-M7-6 | 实现任务取消释放锁 | 取消后清理资源占用 | P1 |
| P2-M7-7 | 实现同资源追加事件 | 追加到已有 thread 的任务队列 | P1 |

验收标准：

- 同一资源不会被两个写 Worker 同时占用。
- 不同资源可创建独立 CA thread，Worker 并行执行。
- Worker 继承 CA thread 锁后无需重复申请。
- 任务完成、失败或取消后释放锁。

当前进展：

- 已新增 `resource_locks` 业务表和 `ResourceLockRepository`，支持按 `resource_key` 获取、查询和释放锁。
- `ThreadManager.run_event()` 会在 run 启动时按显式 `resource_key` 或 `workdir` 获取 thread 级资源锁。
- 同资源已有未完成 run 持锁时，新 run 会被阻止进入执行，避免 Thread Worker 并发写同一资源。
- run 进入 `completed`、`blocked` 或 `failed` 后会释放该 thread 持有的资源锁。
- 剩余工作：把冲突策略从“直接 blocked”升级为“排队/追加到已有 thread”，并补任务取消释放锁。

#### M8：Coder Worker 桥接

| 编号 | 任务 | 产出 | 优先级 |
| --- | --- | --- | --- |
| P2-M8-1 | 定义 Coder Worker 接口 | 封装 Claude Code CLI | P0 | Done |
| P2-M8-2 | 封装 Claude Code CLI | 可配置启用 | P1 |
| P2-M8-3 | 封装 Codex CLI | 已取消，统一使用 Claude Code | P1 | Cancelled |
| P2-M8-4 | 限定工作目录 | 不允许越过资源边界写入 | P0 |
| P2-M8-5 | 接入验证命令 | 修改后必须运行 DoD | P0 |
| P2-M8-6 | 生成变更摘要 | 返回改动说明和验证结果 | P0 |

验收标准：

- CA Agent 可以 dispatch Coder Worker 处理代码任务。
- Coder Worker 只能在指定工作目录运行。
- 代码修改后必须执行验证命令或进入人工验收状态。

---

### Phase 3：Subgraph Worker + 完整生态

目标：Worker 自身也具备 plan-execute-verify 能力，作为独立 LangGraph 子图运行。引入飞书集成和长期记忆。

#### M9：Subgraph Worker

| 编号 | 任务 | 产出 | 优先级 |
| --- | --- | --- | --- |
| P3-M9-1 | 设计 Worker 内部子图 | Coder Worker 的 plan-execute-verify 子图 | P1 |
| P3-M9-2 | 实现 Worker 独立 checkpoint | Worker 状态可独立恢复 | P1 |
| P3-M9-3 | 实现 Worker 审批上报 | Worker 内部高危操作上报 CA Agent | P1 |
| P3-M9-4 | 实现 Worker 结果聚合 | 子图完成后汇总返回 CA Agent | P1 |

#### M10：飞书集成

| 编号 | 任务 | 产出 | 优先级 |
| --- | --- | --- | --- |
| P3-M10-1 | 实现飞书 Webhook | 文本消息和卡片回调 | P1 |
| P3-M10-2 | 飞书事件转 `AgentEvent` | 复用本地事件模型 | P1 |
| P3-M10-3 | 实现 Feishu Skill 文本发送 | 发送任务状态和结果 | P1 |
| P3-M10-4 | 实现 Feishu 授权卡片 | 允许/拒绝高危命令 | P1 |
| P3-M10-5 | 飞书状态查询指令 | 查询任务、锁、待授权项 | P1 |
| P3-M10-6 | 飞书端到端联调 | 真实机器人环境验证 | P1 |

验收标准：

- 飞书只是外部入口，不改变 CA Agent 核心流程。
- 飞书消息能转为与本地入口一致的 `AgentEvent`。
- 飞书授权回调能恢复等待中的本地任务。

#### M11：长期记忆与知识沉淀

| 编号 | 任务 | 产出 | 优先级 |
| --- | --- | --- | --- |
| P3-M11-1 | 实现 `context_summary` 压缩 | LLM 生成阶段摘要 | P1 |
| P3-M11-2 | 实现 Obsidian 双向同步 | 读取背景知识、写入复盘 | P1 |
| P3-M11-3 | 引入 ChromaDB | 向量检索历史任务 | P2 |

---

## 4. 建议排期

| 周期 | 重点 | 里程碑 | 可裁剪项 |
| --- | --- | --- | --- |
| Week 1 | P1-M1、P1-M2 | CA Agent 事件循环骨架、AgentState、Inline Worker、SQLite 任务状态 | P1-M2-6 checkpoint 可延后 |
| Week 2 | P1-M3、P1-M4 | Skill 抽象为 Worker、interrupt 审批、Command resume | P1-M3-7 Obsidian Skill |
| Week 3 | P1-M5、P2-M6 | 恢复与补偿、Thread Worker 并行、事件路由 | P2-M6-6 LLM aggregate |
| Week 4 | P2-M7、P2-M8 | 资源锁、Coder Worker、测试与文档 | Claude Code-only |
| Week 5+ | P3-M10、P3-M11 | 飞书集成、长期记忆 | 全部可按需拆分 |

## 5. 任务依赖

```text
P1: M0 -> M1 -> M2 -> M3 -> M4 -> M5
P2:           M5 -> M6 -> M7 -> M8
P3:                 M8 -> M9/M10/M11
```

关键路径：

1. CA Agent `AgentState` 和事件循环 Graph。
2. SQLite 任务状态和 checkpoint。
3. `InlineWorkerClient` 抽象层。
4. `interrupt()` + `Command(resume=...)` 审批恢复。
5. `ThreadWorkerClient` 并行执行。
6. 资源锁和会话调度。
7. 恢复、补偿、归档。
8. Coder Worker 桥接。
9. 飞书集成。

## 6. 验收用例

### Case 1：本地普通任务（Inline）

输入：通过 API 提交"总结当前项目结构"。
期望：

- 创建 `AgentEvent`。
- CA Agent `strategize` 生成 WorkOrder。
- `InlineWorkerClient` 同步执行。
- CA Agent `aggregate` 更新任务状态。
- 返回任务摘要。
- 写入审计日志。

### Case 2：并行任务（Thread Worker）

输入：提交"在 repoA 跑测试，同时在 repoB 查最近提交"。
期望：

- CA Agent 生成两个 WorkOrder（`shell` + `shell`）。
- `ThreadWorkerClient` 同时启动两个 Worker。
- CA Agent `interrupt()` 挂起等待。
- 两个 Worker 完成后分别回调。
- CA Agent 被唤醒，`aggregate` 汇总结果。

### Case 3：高危命令本地授权

输入：CA Agent `strategize` 生成 `git push` WorkOrder。
期望：

- `dispatch` 发现 high/critical。
- CA Agent `interrupt()` 挂起，生成 `ApprovalRequest`。
- 未授权前不 dispatch Worker。
- 本地允许后 `Command(resume=...)` 恢复，dispatch Worker。
- 本地拒绝后进入 `blocked`。

### Case 4：断点恢复 + 回调补偿

输入：任务执行中手动停止服务，Worker 已完成但回调未处理。
期望：

- 系统发现未完成 CA thread（`monitoring` 状态）。
- 扫描 `work_results` 表，发现已有结果。
- 合成 `worker_complete` 事件注入 CA Agent。
- CA Agent 从 `monitor` 恢复，进入 `aggregate`。

### Case 5：同资源并发

输入：

1. 提交对 A 仓库的写任务。
2. 在任务运行时再提交另一个 A 仓库写任务。

期望：

- 第二个写事件注入同一 CA thread。
- CA Agent `strategize` 生成新 WorkOrder，但 dispatch 时因锁冲突排队。
- 第一个 Worker 完成后释放锁，第二个 Worker 才能 dispatch。

## 7. MVP 完成定义

Phase 1（Inline Worker）完成定义：

1. 本地 API 或 CLI 可提交任务。
2. `AgentEvent`、`WorkOrder`、`WorkResult`、`AgentState` 模型稳定。
3. CA Agent 事件循环可端到端完成。
4. SQLite 可记录任务、Worker、授权、审计和运行状态。
5. 至少一个 `ShellWorker` 任务可完成。
6. 高危命令可在 dispatch 前拦截并通过本地授权恢复。
7. 进程重启后可恢复或查询未完成 CA thread。
8. 完成任务可生成 Markdown 复盘。
9. 核心测试用例通过。

Phase 2（Thread Worker）完成定义：

1. 多个 Worker 可并行执行。
2. CA Agent 可在 Worker 执行时挂起，完成后唤醒。
3. Worker 回调丢失可补偿。
4. 资源锁能阻止同资源并发写 Worker。
5. Coder Worker 可处理代码任务。

飞书集成不再作为 Phase 1/2 的完成前置条件。

## 8. 飞书后置集成原则

飞书接入时必须遵守：

1. 飞书只产生 `AgentEvent`，不侵入 CA Agent 核心。
2. 飞书授权只更新 `ApprovalRequest`，不直接执行命令。
3. 飞书通知只消费任务状态和审计记录。
4. 本地 API/CLI 入口必须保留，便于测试和排障。

## 9. 版本兼容性

v2.0 实施策略：

- **不一次性推翻重写**：Phase 1 保留现有 Skill 代码，通过 `InlineWorkerClient` 桥接。
- **状态模型渐进扩展**：`AgentState` 新增字段，`Task` 新增 `worker_type`/`order_id`。
- **Graph 重构**：从线性图变为事件循环图，但节点函数可复用部分逻辑（如 `risk_gate`）。
- **测试兼容**：现有单元测试逐步迁移，先保证 Inline 模式通过，再上 Thread 模式。
