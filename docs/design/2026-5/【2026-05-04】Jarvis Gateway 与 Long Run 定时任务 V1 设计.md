# Jarvis Gateway 与 Reminder Scheduler V1 设计

日期：2026-05-04

## 结论

Jarvis 第一版不要同时做完整 long-run runtime。V1 只做一件事：把“提醒我”这类任务做成可靠的 Gateway Scheduler。

用户说：

```text
10点提醒我起床
```

Jarvis 应该创建一个持久化 reminder job，到点后通过 Feishu 原会话投递提醒。它不应该在没有日历工具时调用 Codex 临时生成 cron 脚本。Codex 只用于“实现 Jarvis 的定时任务能力”这类开发任务，不用于普通提醒请求。

推荐 V1 架构：

```text
Feishu / CLI / Webhook
  -> Gateway
  -> Conversation Store
  -> Reminder Scheduler
  -> Delivery
```

`agent_tasks`、approval、artifact、isolated research、scheduled agent task 都放到第二阶段。V1 先把可靠提醒系统闭环做扎实。

## 参考实现

### Hermes

Hermes 的 cron 是 Gateway 内置子系统，不是让 agent 生成脚本。它的开发文档把关键文件拆成：

- `cron/jobs.py`：任务模型与 `jobs.json` 原子读写
- `cron/scheduler.py`：due job 检测、执行、repeat 更新
- `tools/cronjob_tools.py`：模型可调用的 `cronjob` 工具
- `gateway/run.py`：Gateway 长运行循环里 tick scheduler
- `hermes_cli/cron.py`：CLI cron 子命令

Hermes 支持 relative delay、interval、cron expression、ISO timestamp。Scheduler 默认每 60 秒 tick，一次 tick 会筛选 due jobs、创建 fresh agent session、注入 skills、运行 prompt、投递结果、更新下一次运行时间。Cron 运行会禁用 `cronjob` 工具，避免递归创建定时任务。

参考：

- https://hermes-agent.nousresearch.com/docs/developer-guide/cron-internals
- https://hermes-agent.nousresearch.com/docs/user-guide/features/cron/

### OpenClaw

OpenClaw 的 cron 也是 Gateway 内置 scheduler。文档明确说：

- Cron runs inside the Gateway process, not inside the model.
- Jobs persist under `~/.openclaw/cron/`，重启不丢。
- Cron execution creates background task records.
- 支持 main session、isolated session、current session、custom session。
- 支持 delivery mode、timezone、retry backoff、run history、session retention、run log pruning。

OpenClaw 对提醒类任务推荐 main session system event；对报告类任务推荐 isolated session fresh turn。Jarvis V1 只实现提醒类 main session delivery；isolated agent job 放到 Phase 2。

参考：

- https://docs.openclaw.ai/automation/cron-jobs
- https://docs.openclaw.ai/cli/cron

## V1 范围

V1 只解决：

1. 用户可以通过 Feishu/CLI 创建一次性提醒。
2. 提醒 job、某次 run、投递记录都持久化。
3. 服务重启后未到期提醒不丢。
4. 到点提醒尽量不重复发送。
5. 用户可以 list/remove reminder。
6. Scheduler claim 是 DB 原子操作，不靠代码层“先查再改”。

V1 不做：

- 完整 long-run task runtime
- `agent_tasks`
- approval 表
- artifact 表
- isolated research/report 定时任务
- recurring cron/every 的完整实现
- 多 worker 分布式调度
- 日历双向同步
- 让 Codex 动态生成定时脚本

Phase 2 再扩展为 scheduled agent task / long-run task runtime。

## Gateway 边界

Gateway 负责：

- 接收外部事件：Feishu、CLI、Webhook。
- 统一事件格式：`InboundEvent`。
- 去重：平台消息 ID、Webhook retry。
- 鉴权与激活策略：DM、群聊 mention、allowlist。
- 创建 conversation/message/turn。
- 定时 tick：扫描 due scheduled jobs。
- 投递提醒：Feishu 文本或卡片。

Gateway 不负责：

- reminder intent detection。
- LLM 推理 loop。
- Codex 子任务执行。
- 研究任务执行。
- 代码修改。

提醒创建属于 Conversation/Turn/Agent 语义层：用户消息正常创建 turn，模型通过 `scheduled_task` 工具创建、查看、取消提醒。这样“10 分钟后提醒我上课，现在先改 xx 项目的代码”可以在同一个 agent loop 里先调用 `scheduled_task`，再继续调用代码工具。

到点提醒本身只写 `scheduled_job_runs` 和 `message_deliveries`，不默认追加为普通聊天上下文。后续如果需要“我刚才提醒过你什么”的记忆，再由专门的 reminder history 查询提供。

推荐内部事件：

```python
class InboundEvent:
    platform: str
    external_chat_id: str
    external_message_id: str | None
    chat_type: str
    sender_id: str
    sender_name: str | None
    text: str
    mentions: list[str]
    reply_to_external_message_id: str | None
    raw_payload: dict
    received_at: str
```

Gateway 返回：

```python
class GatewayResult:
    status: str  # ignored / stored / queued / duplicate / reset
    conversation_id: int
    message_id: int | None
    turn_id: int | None
    immediate_reply: str | None
    delivery_kind: str  # none / text / card
```

## 数据模型

V1 使用 MySQL，和现有 conversation/tool_calls 审计链路一致。

```text
scheduled_jobs
- id
- conversation_id
- created_by_user_id
- name
- prompt
- schedule_kind: at
- schedule_expr
- timezone
- next_run_at
- last_run_at
- lifecycle_status: active / paused / completed / cancelled
- run_count
- delivery_mode: origin
- delivery_target_json
- metadata_json
- created_at
- updated_at

scheduled_job_runs
- id
- job_id
- status: queued / running / completed / failed / skipped
- scheduled_for
- started_at
- finished_at
- output_summary
- error_message
- metadata_json

message_deliveries
- id
- source_type: scheduled_job_run
- source_id
- platform
- external_chat_id
- delivery_key
- status: pending / sent / failed
- external_message_id
- error_message
- created_at
- sent_at

scheduler_locks
- lock_name
- owner
- expires_at
```

`scheduled_jobs.lifecycle_status` 只描述计划本体是否启用、是否结束、是否取消；不要用 `running` 表示执行中。执行中的是某一次 `scheduled_job_runs`。

`scheduled_job_runs.status` 描述某一次执行：

- `queued`：run 已创建，待执行。
- `running`：run 正在投递。
- `completed`：投递完成。
- `failed`：投递失败。
- `skipped`：被取消、过期或幂等去重跳过。

`message_deliveries.delivery_key` 必须有唯一索引。提醒类任务建议：

```text
scheduled_job_run:{run_id}:origin
```

这能覆盖提醒最怕的崩溃窗口：

```text
1. Scheduler 到点
2. 发送飞书提醒成功
3. 服务在更新 DB 前崩溃
4. 重启后发现 job 仍然 due
5. delivery_key 去重，避免重复发送或至少能看出投递状态
```

V1 目标不是严格 exactly-once delivery，而是做到可审计、可去重、可恢复判断。

建议唯一索引：

```text
scheduled_job_runs: unique(job_id, scheduled_for)
message_deliveries: unique(delivery_key)
```

## 模型工具

新增工具：`scheduled_task`。

不要把底层 cron expression 直接暴露给模型。模型负责理解用户意图，后端 parser 负责时间规范化、校验和置信度判断。

V1 schema：

```json
{
  "name": "scheduled_task",
  "description": "Create, list, or remove Jarvis reminder tasks. The model provides natural-language time fields; Jarvis parses and validates the concrete schedule.",
  "parameters": {
    "type": "object",
    "properties": {
      "action": {
        "type": "string",
        "enum": ["create", "list", "remove"]
      },
      "job_id": { "type": "integer" },
      "task_type": {
        "type": "string",
        "enum": ["reminder"]
      },
      "title": { "type": "string" },
      "prompt": { "type": "string" },
      "time_text": {
        "type": "string",
        "description": "Natural-language time expression from the user, such as 今天10点, tomorrow at 9, in 20 minutes."
      },
      "timezone": { "type": "string" },
      "delivery_mode": {
        "type": "string",
        "enum": ["origin"]
      }
    },
    "required": ["action"]
  }
}
```

后端解析结果示例：

```json
{
  "schedule_kind": "at",
  "schedule_expr": "2026-05-04T10:00:00+08:00",
  "next_run_at": "2026-05-04T02:00:00Z",
  "timezone": "Asia/Shanghai",
  "confidence": 0.98
}
```

低置信度时不创建 job，而是让 Gateway 要求用户确认。例如“晚上提醒我”没有具体时间，应追问；“10点提醒我起床”在用户默认 timezone 存在时可以直接创建。

Policy：

- `reminder` turn 默认允许 `scheduled_task`。
- `scheduled_task` V1 只支持 create/list/remove。
- Cron/every/recurring 先不进入 V1 工具 schema。
- 如果模型想调用 Codex 实现一个提醒脚本，runtime policy 应拒绝，并提示使用 `scheduled_task`。

## 时间语义

时间解析由 `scheduler.parser` 负责，不由模型直接生成 UTC 时间。

规则：

- timezone 优先级：用户显式 timezone > conversation/user profile timezone > `JARVIS_DEFAULT_TIMEZONE` > server local timezone。
- timestamp 入库统一使用 UTC。
- 展示和自然语言解析使用用户 timezone。
- 只说“10 点”：如果今天 10:00 未过去，默认今天；否则默认明天。
- “20 分钟后”：用收到消息的时间作为 base time。
- “明早 8 点”：解析为下一天 08:00。
- “早上/上午/下午/晚上”需要明确映射；低置信度时追问。
- 群聊默认投递到原 chat/thread；“私聊提醒我”后续再支持。

V1 不支持 recurring。对“每天 10 点提醒我起床”，可以回复：“当前只支持一次性提醒，要不要先帮你设置下一次 10 点的提醒？”Phase 2 再支持 recurring。

## 用例：10 点提醒我起床

用户在飞书 DM 说：

```text
10点提醒我起床
```

执行路径：

```text
Feishu message
  -> Gateway parse InboundEvent
  -> conversation/message/turn
  -> AgentRuntime
  -> LLM calls scheduled_task.create(time_text="10点", prompt="提醒用户起床")
  -> scheduler.parser normalizes time
  -> insert scheduled_jobs lifecycle_status=active
  -> assistant reply: 已设置，今天/明天 10:00 提醒你起床
  -> Gateway scheduler tick
  -> atomic scheduler lock acquired
  -> due job creates scheduled_job_runs
  -> create message_deliveries pending with unique delivery_key
  -> delivery to origin chat: 提醒：起床
  -> message_deliveries sent
  -> scheduled_job_runs completed
  -> one-shot scheduled_jobs lifecycle_status=completed
```

简单 reminder 会创建普通 turn，由 agent 通过工具完成；到点 delivery 不创建新的普通 turn，也不创建 `agent_tasks`。

## Scheduler Claim

Scheduler claim 必须是 DB 原子操作。

V1 使用全局锁：

```sql
UPDATE scheduler_locks
SET owner = :owner, expires_at = :expires_at
WHERE lock_name = 'scheduler'
  AND expires_at < NOW();
```

影响行数为 1 才算拿到锁。初始化时插入一行 `lock_name='scheduler'`。不要使用这种伪安全流程：

```text
select due jobs
for job in jobs:
    create run
    update job
```

正确 tick 流程：

```text
1. 原子获取 scheduler lock
2. 查询 next_run_at <= now 且 lifecycle_status=active 的 one-shot jobs
3. INSERT scheduled_job_runs，依赖 unique(job_id, scheduled_for) 防重复
4. 创建 message_deliveries pending，依赖 unique(delivery_key) 防重复投递
5. 标记 run running
6. 调用 Feishu delivery
7. 成功：delivery sent，run completed，job completed
8. 失败：delivery failed，run failed，job 保持 active 或进入 retry 策略
9. 释放 lock 或等待 lock 过期
```

V1 可以 `max_concurrent_scheduled_runs=1`。后续再升级到每个 job 独立 claim，或使用 `SELECT ... FOR UPDATE SKIP LOCKED`。

## 如果没有日历提醒工具，是否调用 Codex 生成代码

不应该。

对用户的“提醒我”请求，Jarvis 应该使用内置 `scheduled_task`。如果没有这个工具，正确行为是告诉用户“当前还没有启用提醒能力”，而不是让 Codex 生成临时脚本。

原因：

- 临时脚本无法统一展示、取消、暂停、重试。
- 脚本生命周期不受 Gateway 管理，重启/迁移容易丢。
- 无法统一 delivery 到 Feishu 原会话。
- 无法做权限、审计、去重和失败通知。
- 多个临时脚本会形成不可维护的隐性状态。

## 与现有代码的落点

V1 新增模块：

```text
app/gateway/events.py
app/gateway/service.py
app/gateway/policies.py
app/gateway/delivery.py
app/scheduler/models.py
app/scheduler/service.py
app/scheduler/parser.py
app/scheduler/runtime.py
app/tools/scheduled_task.py
```

V1 暂不新增：

```text
app/agent_tasks/store.py
app/agent_tasks/runtime.py
```

优先改造：

- `app/channels/feishu.py` 只负责 parse event 与 delivery。
- `GatewayService.handle_inbound_event()` 只负责普通 message ingest，并返回是否需要运行 agent。
- `scheduled_task` 工具注册到 tool definitions，并受 runtime policy 控制；`conversation_id/platform/external_chat_id` 由 runtime 注入，不暴露给模型填写。
- Reminder scheduler 可以在 FastAPI lifespan 启动单线程 worker。

## V1 验收标准

1. 飞书 DM 输入“10点提醒我起床”，Jarvis 回复已设置。
2. 用户能通过自然语言或 `scheduled_task` 工具查看该提醒。
3. 到点后 Jarvis 在原飞书聊天发送提醒。
4. 服务重启后未到期提醒仍存在。
5. one-shot 提醒成功后 `scheduled_jobs.lifecycle_status=completed`。
6. `scheduled_jobs` 不使用 `running` 表示执行中。
7. `scheduled_job_runs` 记录本次执行状态。
8. `message_deliveries` 记录投递幂等状态。
9. 同一个 `scheduled_job_run` 不会重复向同一个 origin 发送。
10. Scheduler claim 是原子的，两个 worker 同时 tick 不会重复创建同一个 due run。
11. 简单 reminder 不创建 `agent_tasks`；到点投递不污染普通 conversation context。
12. 用户能取消未触发的 reminder。

## 实施顺序

第一步：

- 建 `scheduled_jobs` / `scheduled_job_runs` / `message_deliveries` / `scheduler_locks` 表。
- 实现 `SchedulerService.create/list/remove/tick`。
- 实现 `scheduler.parser` 的一次性时间解析。
- 支持 `at` one-shot reminder。

第二步：

- 新增 `scheduled_task` 工具。
- Runtime policy 默认允许普通 turn 使用 `scheduled_task`。
- Tool runtime 自动注入 conversation/platform/chat 上下文。
- Feishu DM 支持“10点提醒我起床”。

第三步：

- Gateway worker 定时 tick。
- Delivery 幂等记录。
- 自然语言 list/remove reminder。

Phase 2：

- recurring `every` / `cron`。
- `agent_tasks`。
- scheduled agent task。
- isolated research/report。
- approval 和 artifact。
- 日历集成工具，但只作为外部同步，不替代 Jarvis Scheduler。
