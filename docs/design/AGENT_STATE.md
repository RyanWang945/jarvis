# AgentState 字段说明

本文档详细说明 `app/agent/state.py` 中定义的状态结构，包括 `AgentState` 及其子类型（`Task`、`PendingAction`、`IntentDecision`、`PlanStep`、`WorkPlan`）的字段含义。

---

## 类型别名

| 别名 | 取值 |
|------|------|
| `TaskStatus` | `"pending"`, `"running"`, `"waiting"`, `"success"`, `"failed"`, `"blocked"`, `"cancelled"` |
| `RiskLevel` | `"low"`, `"medium"`, `"high"`, `"critical"` |
| `ActionKind` | `"echo"`, `"shell"`, `"coder"`, `"file"`, `"obsidian"`, `"github"` |
| `ActionStatus` | `"ready"`, `"waiting_approval"`, `"approved"`, `"rejected"` |
| `IntentKind` | `"code_write"`, `"code_review"`, `"search_summary"`, `"explicit_shell"`, `"test_only"`, `"simple_chat"`, `"unknown"` |
| `AgentRunStatus` | `"created"`, `"contextualizing"`, `"planning"`, `"strategizing"`, `"dispatching"`, `"monitoring"`, `"running"`, `"verifying"`, `"waiting_approval"`, `"blocked"`, `"completed"`, `"failed"`, `"cancelled"` |

---

## AgentState

LangGraph 图执行的全局状态对象，贯穿一次 Agent 运行的完整生命周期。

| 字段 | 类型 | 说明 |
|------|------|------|
| `thread_id` | `str` | 当前运行线程的唯一标识，用于区分不同对话/运行实例。 |
| `messages` | `list[Any]`（通过 `add_messages` 合并） | 对话消息列表，LangGraph 自动按消息增量合并。 |
| `event` | `dict[str, Any]` | 触发当前运行的原始事件（`AgentEvent` 序列化后的字典）。 |
| `task_list` | `list[Task]` | 本次运行需要完成的任务列表。 |
| `current_task_id` | `str \| None` | 当前正在执行的任务 ID。 |
| `status` | `AgentRunStatus` | Agent 运行的整体状态。 |
| `resource_key` | `str \| None` | 当前运行关联的资源锁键，用于并发控制。 |
| `pending_action` | `PendingAction \| None` | 当前待处理/待审批的动作。 |
| `dispatch_queue` | `list[dict[str, Any]]` | 已生成但尚未分派的工单队列。 |
| `work_orders` | `dict[str, dict[str, Any]]` | 已分派的工单映射，key 为 order_id。 |
| `approved_order_ids` | `list[str]` | 已被审批通过的工单 ID 列表。 |
| `active_workers` | `dict[str, str]` | 正在执行的 Worker 映射，key 为 worker_type，value 为状态标识。 |
| `worker_results` | `dict[str, dict[str, Any]]` | Worker 执行结果，key 为 worker_type 或 order_id。 |
| `pending_approval_id` | `str \| None` | 正在等待人工审批的审批请求 ID。 |
| `error_count` | `int` | 累计错误次数，用于失败重试或熔断判断。 |
| `last_error` | `str \| None` | 最近一次错误信息摘要。 |
| `intent` | `IntentDecision \| None` | 初始意图识别结果。 |
| `observation_intent` | `IntentDecision \| None` | 观察/复盘阶段的二次意图识别结果。 |
| `work_plan` | `WorkPlan \| None` | 显式多工单编排的工作计划。 |
| `allowed_tools` | `list[str]` | 当前运行被允许使用的工具列表。 |
| `candidate_tools` | `list[str]` | 候选工具列表，供决策使用。 |
| `plan_steps` | `list[dict[str, Any]]` | 规划阶段生成的原始步骤列表。 |
| `planner_raw_output` | `dict[str, Any] \| None` | Planner LLM 的原始输出，保留用于调试。 |
| `failure_kind` | `str \| None` | 失败类型标记，用于分类处理和恢复策略。 |
| `context_summary` | `str \| None` | 上下文摘要，用于长对话的上下文压缩。 |
| `final_summary` | `str \| None` | 运行结束后的最终总结输出。 |
| `next_node` | `str \| None` | LangGraph 条件边中指定的下一个节点名称。 |

---

## Task

描述 Agent 运行中的一个具体任务单元。

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | `str` | 任务唯一标识。 |
| `title` | `str` | 任务标题。 |
| `description` | `str` | 任务详细描述。 |
| `status` | `TaskStatus` | 任务当前状态。 |
| `resource_key` | `str \| None` | 资源锁键，防止并发冲突。 |
| `dod` | `str \| None` | Definition of Done，完成标准定义。 |
| `verification_cmd` | `str \| None` | 任务完成后用于验证的命令。 |
| `tool_name` | `str \| None` | 指定使用的工具名称。 |
| `tool_args` | `dict[str, Any]` | 工具调用参数。 |
| `worker_type` | `str \| None` | 执行此任务的 Worker 类型。 |
| `order_id` | `str \| None` | 关联的工单 ID。 |
| `retry_count` | `int` | 当前已重试次数。 |
| `max_retries` | `int` | 最大允许重试次数。 |
| `result_summary` | `str \| None` | 任务执行结果摘要。 |

---

## PendingAction

描述一个已生成但尚未执行（或等待审批）的动作。

| 字段 | 类型 | 说明 |
|------|------|------|
| `action_id` | `str` | 动作唯一标识。 |
| `capability_name` | `str \| None` | 所属能力名称。 |
| `kind` | `ActionKind` | 动作类型（如 shell、coder 等）。 |
| `skill` | `str` | 使用的技能名称。 |
| `provider` | `str \| None` | 服务提供商。 |
| `action` | `str` | 具体动作名称。 |
| `args` | `dict[str, Any]` | 动作参数。 |
| `command` | `str \| None` | 实际要执行的命令文本。 |
| `workdir` | `str \| None` | 执行时的工作目录。 |
| `risk_level` | `RiskLevel` | 风险等级。 |
| `reason` | `str` | 执行此动作的原因/依据。 |
| `status` | `ActionStatus` | 动作当前状态（ready、waiting_approval 等）。 |
| `order_id` | `str \| None` | 关联工单 ID。 |

---

## IntentDecision

意图识别阶段的输出结果，决定 Agent 的后续行为。

| 字段 | 类型 | 说明 |
|------|------|------|
| `kind` | `IntentKind` | 识别出的意图类型。 |
| `confidence` | `float` | 置信度分数（0~1）。 |
| `confidence_source` | `str` | 置信度来源说明（如哪个模型或规则判断）。 |
| `reason` | `str` | 意图识别的推理依据。 |
| `allowed_tools` | `list[str]` | 该意图下允许使用的工具白名单。 |
| `requires_workdir` | `bool` | 是否需要指定工作目录。 |
| `plan_steps` | `list[dict[str, Any]]` | 与该意图相关的初步步骤。 |

---

## PlanStep

工作计划中的一个步骤。

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | `str` | 步骤唯一标识。 |
| `title` | `str` | 步骤标题。 |
| `instruction` | `str` | 步骤执行指令。 |
| `capability_name` | `str` | 所需能力名称。 |
| `status` | `str` | 步骤状态。 |
| `order_id` | `str \| None` | 关联工单 ID。 |
| `result_summary` | `str \| None` | 步骤执行结果摘要。 |

---

## WorkPlan

显式多工单编排的工作计划。

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | `str` | 计划唯一标识。 |
| `goal` | `str` | 计划总体目标描述。 |
| `status` | `str` | 计划整体状态。 |
| `requires_multiple_work_orders` | `bool` | 是否需要多个工单才能完成。 |
| `steps` | `list[PlanStep]` | 计划包含的步骤列表。 |

---

## AgentEvent

触发 Agent 运行的事件对象，由 `AgentEvent` Pydantic 模型定义，序列化后存入 `AgentState.event`。

| 字段 | 类型 | 说明 |
|------|------|------|
| `event_id` | `str` | 事件唯一标识（默认 UUID）。 |
| `event_type` | `AgentEventType` | 事件类型。 |
| `source` | `AgentEventSource` | 事件来源（api、cli、scheduler、feishu 等）。 |
| `thread_id` | `str \| None` | 关联线程 ID。 |
| `user_id` | `str \| None` | 触发用户 ID。 |
| `timestamp` | `str` | 事件时间戳（ISO 格式）。 |
| `payload` | `dict[str, Any]` | 业务载荷，通常包含 `instruction`、`command`、`workdir`、`resource_key` 等。 |
