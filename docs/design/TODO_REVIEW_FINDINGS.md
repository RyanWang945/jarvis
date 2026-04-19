# Review TODOs

版本：v2.0 baseline 后续修复清单
来源：Claude Code review + 本地复核
状态：部分完成，剩余项继续跟踪

## 当前基线说明

当前提交 `2bfccf5 feat: establish CA agent inline worker baseline` 已建立 CA Agent + Inline Worker 的第一版骨架，但它只应视为 v2 架构 baseline，不应视为完整 Phase 1 完成。

已完成：

- CA Agent 图替代旧单图串行路径。
- `WorkOrder` / `WorkResult` / `WorkerClient` 契约。
- `InlineWorkerClient` 封装 Echo/Shell Skill。
- `strategize -> dispatch -> monitor -> aggregate` 基础链路。
- 高危 WorkOrder 在 dispatch 前进入 `waiting_approval`。
- LangGraph SQLite checkpoint 已接入。

仍未完成：

- Worker 回调补偿自动化和边界策略。
- Thread Worker 并行执行。
- 失败后的重新规划闭环。

本轮已推进：

- `dispatch` / `monitor` 语义分离。
- `wait_approval` 最小 `interrupt()` / `Command(resume=...)` 闭环。
- 业务 DB schema、Repository、run/task/work_order/work_result/approval/audit 基础写入。
- Approval approve/reject 后更新业务表状态。
- 审批通过后复用原始 WorkOrder，保留 risk / verification / timeout。
- `runs.thread_id` 去重更新，WorkResult artifacts 已落库。
- Worker resume 已支持 `worker_failed` 事件归一化。
- run inspection API 已返回 work_orders、work_results、approval history。
- 已实现最小 Worker 回调补偿：扫描已落库 work_results 并 replay resume。

## P0：修复 dispatch / monitor 语义分离

### 问题

当前 `dispatch` 节点在启动 Worker 后立即 `poll`：

```python
client.dispatch(order)
result = client.poll(order.order_id)
```

这导致 `dispatch` 同时负责启动 Worker 和收集结果，`monitor` 节点形同二次确认。

### 判断

问题成立。即使 Phase 1 使用 Inline Worker，节点职责也应该保持与后续 Thread Worker 一致：

- `dispatch` 只启动 Worker，并记录 `active_workers`。
- `monitor` 统一 poll Worker 结果，并写入 `worker_results`。

注意：InlineWorkerClient 本身仍是同步执行 Skill，所以 `dispatch` 在 Inline 模式下仍会阻塞到 Skill 完成；但这不影响节点职责拆分，且能为 ThreadWorkerClient 平滑替换留接口。

### 状态

已完成。

### 完成内容

- 修改 `dispatch`：移除 dispatch 后立即 `poll` 的逻辑。
- 修改 `monitor`：负责所有 `poll()` 和 `worker_results` 写入。
- 增加测试：验证 `dispatch` 后有 `active_workers`，结果由 `monitor` 汇总。

## P0：引入 interrupt / resume 最小闭环

### 问题

当前事件循环仍是同步 invoke：

- `GraphRunner.run_event()` 只调用一次 `graph.invoke()`。
- `monitor` 没有使用 `interrupt()`。
- `wait_approval` 走向 `END`，不是真正挂起。
- 没有 `Command(resume=...)` 恢复入口。

### 判断

问题成立。当前系统能返回 `waiting_approval`，但不具备文档目标中的 long-run event loop 能力。

### 状态

最小闭环已完成。剩余工作是 Worker 回调恢复、服务启动恢复扫描，以及 CLI 授权入口。

### 完成内容

- 将 Runner 升级为 `ThreadManager` 或新增等价接口。
- `wait_approval` 使用 `interrupt()` 挂起审批。
- 增加本地 approval resume 方法，使用 `Command(resume=...)` 恢复。
- `monitor` 在存在未完成 Worker 时使用 `interrupt()` 挂起。
- API 层增加 approve/reject 入口。

## P0：业务数据库与 Repository

### 问题

当前只有 LangGraph checkpoint，没有业务表。审批恢复、状态查询、审计和 Worker 回调补偿缺少事实来源。

### 判断

问题成立。checkpoint 不能替代业务 DB。

### 状态

基础 DB 和 Repository 已完成并接入 Runner 持久化。WorkOrder/WorkResult 的核心字段已覆盖，包括审批后的原始风险元数据和 artifacts。最小 Worker 回调补偿已接入 `ThreadManager.recover_unfinished()`。剩余工作是更细粒度审计、服务启动自动恢复，以及把 dispatch / Skill 调用点直接纳入审计。

### 完成内容

- 设计并初始化 SQLite 业务表：
  - `runs`
  - `tasks`
  - `work_orders`
  - `work_results`
  - `approvals`
  - `audit_logs`
- 实现 Repository：
  - Task Repository
  - WorkOrder/WorkResult Repository
  - Approval Repository
  - Audit Repository
- 在 Runner 汇总写入关键业务状态。

### 剩余待办

- 服务启动时自动触发未完成 thread 扫描。
- 扩展 Worker 回调丢失补偿策略：超时、重复回调、多 Worker 部分完成。
- 将 dispatch / Skill 调用点直接写入 audit，替代仅在 Runner 汇总写入。

## P0：恢复查询与 Worker 结果补偿

### 问题

业务 DB 已有 run/task/order/result 数据，但查询 API 只返回部分状态；CA Agent 在 `monitoring` 中断后，如果 Worker 结果已落库但回调丢失，缺少恢复入口。

### 状态

最小闭环已完成。剩余工作是服务启动自动调用、补偿策略扩展，以及 CLI 查询入口。

### 完成内容

- `ThreadManager.inspect_run()` 返回 run、tasks、work_orders、work_results、approvals、audit_logs。
- `/agent/runs/{thread_id}` 返回完整 run inspection，未找到时返回 404。
- `ThreadManager.recover_unfinished()` 扫描未完成 runs，对 `dispatched` 且已有 `work_results` 的 WorkOrder 合成 `worker_complete` / `worker_failed` resume。
- `/agent/recover` 提供手动恢复入口。
- 增加测试覆盖重启后等待审批查询、API 运行明细查询、Worker result 补偿恢复。

## P0：审批后 WorkOrder 保真

### 问题

审批通过后如果重新构造 WorkOrder，容易丢失 `verification_cmd`、`timeout_seconds`，并错误降级 `risk_level`。

### 状态

已完成。

### 完成内容

- 审批通过后从 `work_orders[order_id]` 取回原始 WorkOrder。
- 使用 `approved_order_ids` 表示该 order 已授权，dispatch 仅绕过本次风险拦截，不修改原始风险等级。
- 增加测试覆盖审批后仍执行 verification，并持久化 high risk、verification command、timeout。

## P1：aggregate 失败后重新规划

### 问题

当前 `aggregate` 遇到失败直接进入 `blocked`：

```python
"next_node": "blocked" if failed else "summarize"
```

但 graph 已支持 `aggregate -> strategize`。

### 判断

问题成立，但不能简单把失败都路由回 `strategize`。必须保留失败历史，并用 `retry_count / max_retries` 控制重试。

### 待办

- `aggregate` 根据失败类型和 retry 上限决定：
  - 全部成功：`summarize`
  - 可重试：`strategize`
  - 不可重试：`blocked`
- 将失败 `WorkResult`、stderr、summary 带入下一次 strategize 上下文。
- 避免重新规划时无声覆盖旧任务历史。

## P1：修正文档进展状态

### 问题

文档中 Phase 1 验收项曾被标记为已完成，但从架构语义看，当前只是 baseline。

### 判断

需要调整，避免误导后续开发。

### 待办

- 在 `MVP_PLAN.md` 中明确：
  - P1-M1 baseline 已完成。
  - 完整 Phase 1 仍缺 `interrupt/resume`、业务 DB、审批恢复。
- 在 `MIGRATION_PLAN.md` 中将 Phase 1 验收拆成：
  - Baseline 已完成。
  - Full Phase 1 Pending。

## 推荐处理顺序

1. 修复 dispatch / monitor 语义分离。
2. 修正文档进展状态。
3. 实现 approval 的 `interrupt()` / `Command(resume=...)` 最小闭环。
4. 实现 SQLite 业务 DB 与 Repository。
5. 实现 aggregate 失败重规划。
6. 再进入 ThreadWorkerClient 和真正并行。
