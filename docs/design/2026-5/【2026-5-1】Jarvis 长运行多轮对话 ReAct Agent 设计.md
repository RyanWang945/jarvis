# Jarvis 长运行多轮对话 ReAct Agent 设计

版本：v3.1 草案
日期：2026-05-01
状态：新架构基线，第一阶段聚焦私聊多轮和群聊多人对话

## 1. 背景

2026-4 版 PRD 把 Jarvis 定位为 CA Agent + Worker 调度系统：主脑把用户请求拆成 WorkOrder，Worker 并行执行，主脑再聚合结果。这个方向能解释一部分本地任务自动化场景，但不适合作为 Jarvis 的长期核心。

Jarvis 的目标不是把自然语言请求拆成工作流，而是成为一个长运行、多轮对话、能在单聊和群聊中持续协作的全能 Agent。它应该在对话中理解上下文、选择工具、等待外部事件、询问用户、恢复长任务，并把代码修改、网页搜索、Deep Research、知识库检索、画图等能力纳入同一个 ReAct runtime。

因此，旧架构中的固定节点图、Task 列表、WorkOrder 调度、统一 risk_gate、aggregate/verify 分层，都不再作为新核心模型延续。可复用的是底层能力代码：skill、部分 worker 执行器、知识库服务、飞书连接、审计和资源锁思路。

## 2. 产品定位

Jarvis 是一个常驻运行的个人/小团队 AI 协作 Agent。

它可以通过 API、CLI、飞书单聊、飞书群聊接收消息，在一个持续存在的 conversation 中进行多轮对话，并通过 ReAct loop 自主调用工具完成任务。

核心价值：

- 长运行：Jarvis 可以处理持续数分钟到数小时的任务，中途挂起、恢复、等待用户、等待工具结果。
- 多轮：每次触发 Jarvis 的用户消息都是一个 turn；普通群聊背景消息只作为 conversation 上下文入库，不创建 turn。
- 群聊协作：Jarvis 能识别频道、群、发送人、被 @ 对象和回复关系，在多人对话中保持边界。
- 自适应：Jarvis 不预先固定 DAG，而是在 ReAct loop 中根据观察结果决定下一步。
- 全能工具化：网页搜索、知识库、Claude Code、Shell、画图、文件、飞书消息都是同级 tool。
- 可审计：第一阶段先审计消息、turn 和 tool_call；长运行阶段再把权限决策、产物、错误和恢复信号完整事件化。

非目标：

- 不把 Jarvis 设计成传统工作流引擎。
- 不要求所有任务预先拆成 Task/WorkOrder。
- 不把 LangGraph checkpoint 当作唯一业务状态来源。
- 不在 Agent 层重复实现 Claude Code 自己已有的交互式代码修改能力。

### 2.1 第一阶段范围

第一阶段目标不是一次性完成长运行 ReAct runtime，而是先把 Jarvis 的对话底座打稳：

- 机器人私聊多轮对话：同一个用户和 Jarvis 的连续消息进入同一个 active conversation，Jarvis 能按 conversation 历史构造上下文并回答追问。
- 群聊多人对话：群内普通消息保留 sender、chat_id、message_id、reply 关系和 mentions；未 @ Jarvis 的消息默认只入库，不触发 turn；@ Jarvis 或回复 Jarvis 消息时创建 turn，并能读取同一 conversation 内的背景讨论。
- 兼容旧入口：`/agent/run`、CLI 和飞书入口在兼容期映射到 conversation/message/turn，但旧 Task/WorkOrder 字段只作为 deprecated 输出保留。
- 先不做长任务恢复、审批卡片、Claude Code job、Deep Research artifact、图片 artifact 和完整事件流；这些进入后续阶段。

第一阶段的成功标准是：Jarvis 可以在私聊里连续追问，在群聊里理解多个人刚刚说过的话，并且所有消息、触发关系和响应都能从数据库查询出来。

## 3. 核心模型

### 3.1 Conversation

Conversation 表示一段持续上下文。

单聊时，一个用户和 Jarvis 可以有一个默认 conversation。群聊时，一个群可以有一个 conversation，也可以按 thread/topic/message root 细分 conversation。

字段建议：

```text
conversation_id
channel: api | cli | feishu | system
channel_conversation_id
conversation_type: direct | group | topic | system
title
status: active | archived | muted
created_at
updated_at
metadata_json
```

### 3.2 User / Participant

User 表示平台上的真实用户。第一版先落 `users` 表，并在 `messages.user_id`、`turns.started_by_user_id` 中引用它。

Participant 表示 user 在某个 conversation 中的成员身份。第一版可以不单独落 `participants` 或 `conversation_members` 表；当需要群权限、角色和可见性控制时再扩展。

```text
user_id
platform
external_user_id
display_name

participant_id / conversation_member_id
conversation_id
user_id
role: owner | member | bot | system
permission_profile
metadata_json
```

群聊中必须保留 sender，不允许只把 chat_id 映射成 thread_id。审批、澄清和权限判断都依赖参与者身份。

### 3.3 Message

Message 是对话事实，不等同于 agent run。

```text
message_id
conversation_id
sender_participant_id
role: user | assistant | tool | system
content_type: text | image | file | mixed
content_text
attachments_json
reply_to_message_id
source_event_id
created_at
metadata_json
```

飞书消息、API 消息、CLI 输入都先归一化为 Message。

### 3.4 Turn

Turn 表示一次用户触发后 Jarvis 的处理过程。一个 conversation 有多个 turn。

```text
turn_id
conversation_id
trigger_message_id
status: queued | running | completed | failed | cancelled
scene: code_task | deep_research | personal_knowledge | casual_chat | image_generation | mixed
risk_profile: read_only | external_api | modify_fs | privileged
started_at
completed_at
summary
error_json
```

旧系统里的 `run_id` 应该改成 turn_id。`thread_id` 应该改成 conversation_id 或兼容映射到 conversation_id。

第一阶段只使用 `queued`、`running`、`completed`、`failed`、`cancelled`。`waiting_user`、`waiting_tool`、`waiting_permission` 属于长运行和事件恢复阶段的扩展状态，补齐 `events`、`jobs`、`permission_requests` 后再启用。

### 3.5 Event

Event 是运行时事实日志。它是恢复、审计和实时推送的基础。

Event 是长运行阶段的核心表。第一阶段暂不落 `events` 表，但 API 和内部命名要避免和后续事件模型冲突。

```text
event_id
conversation_id
turn_id nullable
event_type
actor_type: user | assistant | tool | system | worker
actor_id
payload_json
created_at
```

关键 event_type：

```text
message.received
turn.started
llm.delta
llm.completed
tool.call_requested
tool.call_started
tool.output_delta
tool.completed
tool.failed
permission.requested
permission.granted
permission.denied
user.clarification_requested
user.clarification_received
artifact.created
turn.waiting
turn.resumed
turn.completed
turn.failed
```

审批、澄清、worker 完成不再伪装成同一种 LangGraph resume，而是明确的事件。

### 3.6 Tool Call

ToolCall 是 ReAct loop 中一次工具调用。

```text
tool_call_id
turn_id
conversation_id
tool_name
input_json
status: requested | running | completed | failed | cancelled
risk_level
started_at
completed_at
result_json
error_json
```

ToolCall 可以同步完成，也可以启动 Job 后挂起。

第一阶段的 ToolCall 只记录同步或短耗时工具调用。需要权限等待、用户澄清或长 job 挂起时，再扩展 `waiting_permission`、`waiting_user` 和 `waiting_tool` 类状态。

### 3.7 Job

Job 表示长时间运行或外部执行单元。

```text
job_id
tool_call_id
conversation_id
turn_id
job_type: claude_code | shell | deep_research | image_generation | ingestion | custom
status: queued | running | waiting_user | completed | failed | cancelled
resource_key
started_at
completed_at
heartbeat_at
metadata_json
```

Job 是运行时执行概念，不应该污染 Message 和 Turn 的语义。

### 3.8 Artifact

Artifact 表示 Jarvis 产物。

```text
artifact_id
conversation_id
turn_id
tool_call_id nullable
kind: report | image | diff | file | log | citation_bundle | dataset
title
mime_type
uri
summary
metadata_json
created_at
```

Deep Research 报告、Claude Code diff、生成图片、网页引用、日志文件都应作为 artifact 暴露。

## 4. Runtime 设计

### 4.1 新入口流程

```text
Channel/API/CLI
  -> normalize input to Message
  -> persist Message
  -> decide whether this message triggers Jarvis
  -> if triggered: create Turn and enqueue AgentRuntime
  -> if not triggered: keep as conversation context only
  -> return message_id / turn_id
```

API 不应该默认同步等待完整执行。默认返回 `turn_id`，调用方通过事件流或查询接口获取进展。

第一阶段可以先不启动完整异步事件流：HTTP、飞书、CLI 入口先完成 Message 入库、Turn 创建和短耗时回复；当消息不触发 Jarvis 时只返回 `message_id`、`conversation_id` 和 `turn_id = null`。后续接入长运行时，再把默认行为切成“立即返回 turn_id，后台 runtime 继续执行”。

### 4.2 ReAct Loop

每个 turn 运行一个 ReAct loop：

```text
load conversation context
load relevant memory and recent events
build model input
LLM decides next response/tool call
if answer: append assistant message, complete or continue
if tool call: execute tool or create job
if ask_user: send clarification, mark waiting_user
if permission required: create permission request, mark waiting_user
if long job: mark waiting_tool and return control
on event resume: reload state and continue loop
```

这里的 plan 是 LLM 的内部过程和可选输出，不是持久化 DAG。可以记录 reasoning summary 和 tool call sequence，但不要强制转成 Task 图。

### 4.2.1 tool_calls 持久化与 proposal gate

`tool_calls` 的设计目标不是保存一份“消息回放摘要”，而是保存一次真实工具执行事实。第一版没有独立 `events` 表时，`tool_calls` 需要直接承担审计、测试、恢复判断三类职责：

- 审计：回答这个 turn 调了什么工具、输入是什么、何时开始、何时结束、结果如何。
- 测试：验证 ReAct loop 是否真的按预期调用了目标工具，而不是只从最终回复反推。
- 恢复：系统重启后扫描 `running` / `requested` 的 `tool_calls`，判断是否需要标记失败、取消或人工恢复。

现状：

- `prepare` 会加载整个 conversation 历史消息，图内 state 同时包含“历史消息”和“本 turn 新增消息”。
- runtime 在图执行结束后扫描最终 `messages`，再反向补写 `tool_calls`。
- `delegate_to_claude_code` 这类高权限工具虽然由 LLM 通过 tool call 触发，但执行前还要经过 runtime policy gate，而不是模型一调用就直接执行。

问题：

- 如果 `tool_calls` 在图结束后通过扫描消息补写，历史 tool call 和本 turn 新 tool call 很容易混淆，审计记录不再是执行事实，而变成消息推断。
- `tool_calls` 表当前字段表达的是生命周期事实：`status`、`started_at`、`finished_at`、`error_message`。如果落库时机放在执行完成之后，这些字段很难保持严格语义。
- proposal 类工具的“被拒绝”是 runtime 的治理结果，不应该在审计上伪装成一次成功执行。

解决方案：

- `tool_calls` 必须在 `execute_tools` 节点内持久化，不再由 runtime 在图结束后统一回扫消息补记。
- 每个 tool call 的推荐时序为：创建 `requested` 记录 -> 开始执行时更新为 `running` -> 执行结束后更新为 `completed` / `failed` / `cancelled` / `rejected`。
- proposal gate 属于 runtime policy。它负责判断高权限工具是否满足执行前提；如果不满足，应记录为一次被 runtime 拒绝的工具调用，而不是跳过审计。
- ReAct state 需要显式区分 `history_messages` 和 `new_messages`，或者保留 `initial_message_count` 之类的增量边界，避免图外逻辑误把历史消息再次记账。
- 最终 assistant reply 应单独作为 turn 输出持久化；`tool_calls` 只表达工具执行事实，不承担“推断最终答复”的职责。

关系要求：

- `conversation` 是顶层会话容器，`tool_calls` 不直接挂在 `conversation` 上，而是通过 `turn` 和 `message` 间接归属到某个 conversation。
- `turn` 表示 Jarvis 对某次触发消息的一次响应生命周期；一次 `tool_call` 必须属于且只属于一个 `turn`。
- `messages` 保存用户、assistant、tool 的可见对话事实；`tool_calls` 保存对应的结构化执行事实。
- assistant 发起工具调用时，应该同时产生一条 assistant message 和零到多条 `tool_calls` 记录；后续 tool result message 只是把执行结果回灌给模型，不替代 `tool_calls` 审计。
- 因此 `tool_calls` 的结构关系不应只靠 `turn_id` 和 `tool_name` 弱关联，而应能追溯到“由哪条 assistant message 发起、在该 message 中是哪一个 tool call”。

### 4.3 挂起和恢复

Turn 可以因为以下原因挂起：

- 等待用户澄清。
- 等待权限确认。
- 等待 Claude Code、Deep Research、画图、文件解析等长任务。
- 等待外部 API rate limit 或回调。

恢复统一通过事件驱动：

```text
POST /turns/{turn_id}/events
```

或内部 event bus：

```text
worker.completed
permission.granted
message.received
```

Runtime 根据 event_type 决定如何恢复，而不是把所有恢复塞进 `Command(resume=...)`。

### 4.4 上下文管理

上下文分三层：

- Recent messages：最近 N 条原始对话。
- Working memory：当前 turn 和未完成 job 的摘要。
- Long-term memory：知识库、历史报告、项目记忆、用户偏好。

Context builder 负责选择输入模型的材料：

```text
conversation metadata
users / participants
recent messages
open turns/jobs
relevant artifacts
retrieved memory
permission scope
resource locks
```

旧 `context_summary` 不应只是展示字符串，它必须成为模型输入和恢复状态的一部分。

## 5. API 设计

### 5.1 Conversation API

```http
POST /conversations
GET /conversations/{conversation_id}
GET /conversations/{conversation_id}/messages
GET /conversations/{conversation_id}/turns
```

`POST /conversations` 支持绑定外部 channel：

```json
{
  "channel": "feishu",
  "channel_conversation_id": "oc_xxx",
  "conversation_type": "group",
  "title": "研发群"
}
```

### 5.2 Message API

```http
POST /conversations/{conversation_id}/messages
```

请求：

```json
{
  "sender": {
    "channel_user_id": "ou_xxx",
    "display_name": "Ryan"
  },
  "content_type": "text",
  "content_text": "@Jarvis 查一下最近 OpenAI Agents SDK 的变化",
  "attachments": [],
  "reply_to_message_id": null,
  "metadata": {
    "mentions": ["jarvis"],
    "raw_channel_event_id": "..."
  }
}
```

响应：

```json
{
  "conversation_id": "...",
  "message_id": "...",
  "turn_id": "...",
  "should_respond": true,
  "trigger_type": "mention",
  "status": "queued"
}
```

如果这是群聊背景消息且未触发 Jarvis，响应中的 `turn_id` 为 `null`，`should_respond` 为 `false`，`status` 为 `stored`。

### 5.3 Turn API

第一版：

```http
GET /turns/{turn_id}
POST /turns/{turn_id}/cancel
```

长运行和事件流阶段再增加：

```http
GET /turns/{turn_id}/events
POST /turns/{turn_id}/events
```

`GET /turns/{turn_id}/events` 可以先做轮询，后续改为 SSE。

提交澄清：

```json
{
  "event_type": "user.clarification_received",
  "payload": {
    "message_id": "...",
    "text": "只看官方文档和 changelog"
  }
}
```

提交审批：

```json
{
  "event_type": "permission.granted",
  "payload": {
    "permission_request_id": "...",
    "scope": "tool_call",
    "expires_at": "2026-05-01T12:00:00Z"
  }
}
```

### 5.4 Artifact API

Artifact API 是 Deep Research、画图和 Claude Code 产物阶段的扩展接口，不进入第一版 5 表模型。

```http
GET /artifacts/{artifact_id}
GET /turns/{turn_id}/artifacts
```

Artifact API 必须支持：

- Markdown/HTML research report。
- 图片生成结果。
- Claude Code 修改摘要和 diff。
- 日志和 stdout/stderr。
- citation bundle。

### 5.5 兼容 API

旧接口保留一段时间：

```http
POST /agent/run
GET /agent/runs/{thread_id}
POST /agent/approve
POST /agent/reject
```

兼容层行为：

- `thread_id` 映射到 `conversation_id`。
- 每次 `/agent/run` 创建新的 message 和 turn。
- 响应补充 `turn_id`。
- `tasks` 字段保留但标记 deprecated。

## 6. 工具体系

### 6.1 工具原则

所有能力都是 tool：

- `web.search`
- `kb.search`
- `kb.ingest`
- `claude_code.run`
- `shell.run`
- `image.generate`
- `ask_user`
- `feishu.send_message`
- `file.read`
- `file.write`
- `artifact.create`

工具必须声明：

```text
name
description
input_schema
output_schema
risk_profile
requires_permission
supports_streaming
supports_cancellation
can_run_long
resource_key_policy
```

### 6.2 Deep Research

Deep Research 不应该是固定工作流，而是一个高阶 tool 或 mode。

实现方式：

- 作为 `deep_research.run` tool，内部可以运行自己的 ReAct loop。
- 可以调用 `web.search`、`kb.search`、`file.write`、`artifact.create`。
- 输出必须是 artifact，而不是简单 summary。

Deep Research 的事件：

```text
research.plan_created
research.source_found
research.note_added
research.claim_verified
research.report_drafted
artifact.created
```

### 6.3 Claude Code

Claude Code 有两种角色：

- 代码任务主执行器：用户要求修改代码时，Jarvis 调用 `claude_code.run`，让 Claude Code 在仓库内完成修改、测试、总结。
- 研究辅助工具：Deep Research 需要写脚本分析数据时，调用受限的 Claude Code 或 shell。

Jarvis 不应该把 Claude Code 当成一次性黑盒 skill 后立即 summarize。它需要：

- 记录 Claude Code 启动、输出、等待确认、完成事件。
- 将 Claude Code 的问题转发给用户。
- 收集 changed files、diff、verification、commit id 等 artifact。
- 由 resource lock 控制同仓库写操作串行。

### 6.4 网页搜索

网页搜索是基础 tool，不等于 Deep Research。

要求：

- 返回结构化结果：title、url、snippet、published_at、source_type。
- 支持引用追踪。
- 允许 ReAct loop 多次搜索、改写 query、交叉验证。
- 搜索质量由模型在后续 loop 中评估，而不是 `result.ok=True` 即成功。

### 6.5 知识库

知识库现有 HTTP API 偏管理面。新 runtime 需要工具面：

- `kb.search`：按 query 检索个人知识库。
- `kb.ingest_file`：把文件纳入知识库，可产生长 job。
- `kb.remember`：把对话结论或报告保存为长期记忆。

知识库结果必须进入 context builder，而不是只作为独立接口返回。

### 6.6 画图

画图是 artifact-first tool。

`image.generate` 输入：

```json
{
  "prompt": "...",
  "style": "...",
  "size": "1024x1024",
  "reference_artifact_ids": []
}
```

输出：

```json
{
  "artifact_id": "...",
  "mime_type": "image/png",
  "uri": "...",
  "summary": "..."
}
```

群聊中返回图片时，channel adapter 负责把 artifact 转成飞书图片或文件消息。

## 7. 权限和风险控制

旧 fixed risk_gate 废弃，改为分层权限。

### 7.1 Permission Scope

权限作用域：

```text
conversation
turn
tool_call
resource
participant
```

示例：

- 用户允许本 turn 修改 `E:\pythonProject\jarvis`。
- 用户允许本 conversation 中 Jarvis 读取网页。
- 用户拒绝当前 `git push` tool call。
- 群聊中只有 owner 可以批准写仓库操作。

### 7.2 风险策略

风险判断在 tool execution 前发生，但不是固定图节点。

默认策略：

- read_only：直接执行并审计。
- external_api：按 conversation 配置决定是否提示。
- modify_fs：需要资源锁；首次或高影响操作需要授权。
- privileged：必须明确授权。

高风险操作包括：

- `git push`
- 删除目录
- force push
- 修改全局配置
- 生产部署
- 发送群公告或外部消息给大量用户

### 7.3 审批交互

审批是一个 tool/event 交互：

```text
permission.requested -> waiting_user -> permission.granted/denied -> resume
```

拒绝不等于 turn failed。ReAct loop 可以换方案、解释无法执行，或继续完成低风险部分。

## 8. 并发和资源锁

### 8.1 并发边界

并发发生在三个层级：

- 不同 conversation 可以并行。
- 同一 conversation 的不同 turn 可以排队或有限并行。
- 同一 turn 内只读 tool 可以并行，写 tool 需要资源约束。

### 8.2 Per Conversation Lock

替换实例级大锁：

- conversation 级短锁：保护状态加载、事件追加、turn 状态变更。
- resource 级锁：保护仓库/目录/外部对象的写操作。
- tool/job 自己负责长时间运行，不持有全局锁。

### 8.3 MySQL + SQLite checkpoint 持久化策略

业务状态以 MySQL 表为准。LangGraph checkpoint 只作为 runtime 恢复辅助，不作为业务事实源。

V1 选择：

- Agent runtime 业务库使用 MySQL 8.x。
- LangGraph checkpoint 使用 SQLite。
- 知识库当前 SQLite 数据继续保留。

选择 MySQL 的原因：

- 用户更熟悉 MySQL，排查和维护成本更低。
- V1 核心表简单，主要是 conversation/message/turn/tool_call，不依赖 PostgreSQL 特有能力。
- MySQL 8.x 的 InnoDB、事务、行锁、JSON 字段足够支撑第一版多轮对话和工具审计。
- SQLite 只保留在 checkpoint 和知识库侧，避免 agent runtime 主业务状态继续遇到 SQLite 写锁问题。

SQLite checkpoint 约束：

- checkpoint 独立文件，不和知识库 SQLite、旧 business DB 混用。
- 开启 WAL、busy_timeout、短事务。
- 不把 checkpoint 当业务事实源；业务查询、状态展示、恢复判断以 MySQL 的 `turns`、`messages`、`tool_calls` 为准。
- 第一版单进程运行可以接受；如果后续出现 checkpoint 锁竞争或多实例需求，再评估 PostgreSQL checkpoint。

存储边界：

```text
MySQL database: jarvis

tables
  users
  conversations
  messages
  turns
  tool_calls

SQLite file: langgraph_checkpoints.sqlite
  LangGraph checkpoint tables

SQLite file: knowledge.db
  existing knowledge base tables
```

runtime 通过 `kb.search` / `kb.ingest` / `kb.remember` tool 访问 KnowledgeBaseService，不直接读写知识库表。后续如需迁移知识库元数据，可以单独迁移，不阻塞 v3 runtime。

## 9. 数据库表设计 V1

本节是第一版多轮对话存储设计。第一版保持简单，只落 5 张核心业务表：

```text
users
conversations
messages
turns
tool_calls
```

这 5 张表足够支撑：

- 单聊和群聊长期上下文。
- 群聊背景消息入库但不触发 Jarvis。
- 每次 Jarvis 响应用 turn 追踪。
- ReAct 工具调用链路可审计。
- `/clear` 后归档旧上下文并新建上下文。

第一版的产品范围是“对话底座”，不是完整长运行 runtime。它必须把私聊多轮、群聊多人消息、触发规则和响应链路存清楚；长任务恢复、事件流推送、审批、artifact 和资源锁进入后续表扩展。

后续的 `events`、`jobs`、`artifacts`、`permission_requests`、`resource_locks`、`channel_deliveries`、`conversation_members`、`conversation_summaries`、`memories` 都是扩展表，不进入第一刀。

数据库实现使用 MySQL 8.x、InnoDB、utf8mb4。JSON 字段用于 `raw_payload`、`metadata`、`input`、`output` 等扩展数据。

字段名使用 `id` 还是 `*_id` 可以在实现时统一。本文 SQL 使用 `BIGINT UNSIGNED AUTO_INCREMENT` 主键，API 层返回时可以映射为 `conversation_id`、`message_id`、`turn_id`。如果后续需要跨实例 ID 生成，再切换为 UUID/ULID。

### 9.1 设计原则

- `conversation` 是上下文边界。
- `message` 是真实发生的一条消息。
- `turn` 是 Jarvis 被触发后的一次执行边界。
- `tool_call` 是 turn 内部的工具调用审计数据，不是聊天上下文数据。
- 构造多轮上下文时按 `conversation_id` 查 `messages`，不按 `turn_id` 查。
- 调试一次 Jarvis 响应时按 `turn_id` 查 `messages` 和 `tool_calls`。
- 普通群聊消息也要入库，但 `turn_id` 可以为空。
- 触发 Jarvis 的用户消息和 Jarvis 回复应绑定同一个 `turn_id`。

### 9.2 users

`users` 表示平台上的真实用户。飞书用户、Web 用户、CLI 用户都归一化到这里。

```sql
CREATE TABLE jarvis.users (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    platform VARCHAR(32) NOT NULL,
    external_user_id VARCHAR(128) NOT NULL,
    display_name VARCHAR(255),
    avatar_url VARCHAR(1024),
    metadata JSON,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_users_platform_external (platform, external_user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
```

用户不等于 conversation。一个群聊 conversation 可以有多个用户参与；一个用户也可以参与多个 conversation。

### 9.3 conversations

`conversations` 表示长期上下文。私聊、群聊、Web 会话、CLI 会话都可以映射到 conversation。

第一版规则：

```text
一个飞书私聊 = 一个 active conversation
一个飞书群聊 = 一个 active conversation
除非执行 /clear，否则一直复用
/clear 后归档旧 conversation，新建 conversation
```

```sql
CREATE TABLE jarvis.conversations (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    platform VARCHAR(32) NOT NULL,
    external_chat_id VARCHAR(128) NOT NULL,
    chat_type VARCHAR(32) NOT NULL,
    title VARCHAR(255),
    owner_user_id BIGINT UNSIGNED NULL,
    created_by_user_id BIGINT UNSIGNED NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    clear_generation INT UNSIGNED NOT NULL DEFAULT 0,
    cleared_from_conversation_id BIGINT UNSIGNED NULL,
    metadata JSON,
    last_message_at DATETIME(6),
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_conversations_generation (platform, external_chat_id, clear_generation),
    KEY idx_conversations_platform_chat (platform, external_chat_id),
    KEY idx_conversations_status_last_message (status, last_message_at),
    CONSTRAINT fk_conversations_owner_user FOREIGN KEY (owner_user_id) REFERENCES jarvis.users(id) ON DELETE SET NULL,
    CONSTRAINT fk_conversations_created_by FOREIGN KEY (created_by_user_id) REFERENCES jarvis.users(id) ON DELETE SET NULL,
    CONSTRAINT fk_conversations_cleared_from FOREIGN KEY (cleared_from_conversation_id) REFERENCES jarvis.conversations(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
```

`chat_type` 建议值：`dm`、`group`、`web`、`cli`、`system`。

私聊时 `owner_user_id` 通常是对话用户。群聊时 `owner_user_id` 可以为空，`created_by_user_id` 表示谁触发创建了这个 conversation。

`clear_generation` 从 0 开始，每次 `/clear` 归档旧 conversation 并新建 generation + 1 的 active conversation。实现上用事务保证同一 `(platform, external_chat_id)` 只有最新 generation 为 active。

### 9.4 messages

`messages` 表存储 conversation 下所有真实消息。

关键原则：

```text
所有消息都属于 conversation。
只有触发 Jarvis 或由 Jarvis 产生的消息，才需要绑定 turn。
普通群聊背景消息 turn_id 可以为空。
```

```sql
CREATE TABLE jarvis.messages (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    conversation_id BIGINT UNSIGNED NOT NULL,
    turn_id BIGINT UNSIGNED NULL,
    sender_type VARCHAR(32) NOT NULL,
    user_id BIGINT UNSIGNED NULL,
    role VARCHAR(32) NOT NULL,
    content TEXT,
    content_type VARCHAR(32) NOT NULL DEFAULT 'text',
    external_message_id VARCHAR(128),
    reply_to_message_id BIGINT UNSIGNED NULL,
    raw_payload JSON,
    token_count INTEGER,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_messages_external (conversation_id, external_message_id),
    KEY idx_messages_conversation_created (conversation_id, created_at),
    KEY idx_messages_turn_created (turn_id, created_at),
    KEY idx_messages_user_created (user_id, created_at),
    CONSTRAINT fk_messages_conversation FOREIGN KEY (conversation_id) REFERENCES jarvis.conversations(id) ON DELETE CASCADE,
    CONSTRAINT fk_messages_turn FOREIGN KEY (turn_id) REFERENCES jarvis.turns(id) ON DELETE SET NULL,
    CONSTRAINT fk_messages_user FOREIGN KEY (user_id) REFERENCES jarvis.users(id) ON DELETE SET NULL,
    CONSTRAINT fk_messages_reply_to FOREIGN KEY (reply_to_message_id) REFERENCES jarvis.messages(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
```

`messages.turn_id` 和 `turns.trigger_message_id` 存在循环引用。实际 migration 可以先建表和普通索引，再用 `ALTER TABLE` 分步补充其中一个外键。

`sender_type` 建议值：`user`、`assistant`、`system`、`tool`。

`role` 是 LLM 上下文角色，建议值：`user`、`assistant`、`system`、`tool`。

`content` 建议存纯文本或 Markdown，便于检索、摘要和上下文拼接。富文本、飞书卡片、原始事件 JSON 存 `raw_payload`。

普通群聊背景消息示例：

```text
Alice: 我觉得这个设计太复杂了       turn_id = null
Bob: 可以先简化                    turn_id = null
Ryan: @Jarvis 总结一下刚才讨论      turn_id = t1
Jarvis: 总结如下...                turn_id = t1
```

### 9.5 turns

`turns` 表示 Jarvis 的一次执行，不是上下文边界。

一次 turn 通常包含：

```text
触发 Jarvis 的用户消息
Jarvis 的最终回复消息
本次工具调用链路
执行状态
错误信息
运行元数据
```

```sql
CREATE TABLE jarvis.turns (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    conversation_id BIGINT UNSIGNED NOT NULL,
    trigger_message_id BIGINT UNSIGNED NULL,
    trigger_type VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'running',
    turn_type VARCHAR(32) NOT NULL DEFAULT 'chat',
    started_by_user_id BIGINT UNSIGNED NULL,
    started_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    completed_at DATETIME(6),
    error_message TEXT,
    metadata JSON,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    KEY idx_turns_conversation_created (conversation_id, created_at),
    KEY idx_turns_status_updated (status, updated_at),
    KEY idx_turns_trigger_message (trigger_message_id),
    CONSTRAINT fk_turns_conversation FOREIGN KEY (conversation_id) REFERENCES jarvis.conversations(id) ON DELETE CASCADE,
    CONSTRAINT fk_turns_trigger_message FOREIGN KEY (trigger_message_id) REFERENCES jarvis.messages(id) ON DELETE SET NULL,
    CONSTRAINT fk_turns_started_by FOREIGN KEY (started_by_user_id) REFERENCES jarvis.users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
```

`trigger_type` 建议值：

```text
dm_message
mention
reply_to_bot
command
scheduled
manual
unknown
```

`status` 建议值：

```text
queued
running
completed
failed
cancelled
```

后续做长任务挂起时再扩展 `waiting_user`、`waiting_tool`、`waiting_permission`。

`turn_type` 建议值：

```text
chat
command
task
research
coding
summary
image_generation
```

### 9.6 tool_calls

`tool_calls` 表记录某个 turn 内部的一次工具调用。

原则：

- 一行 `tool_calls` 代表一次真实工具执行尝试，而不是从 `messages` 派生出来的摘要。
- `tool_calls` 的写入边界在 runtime 的 `execute_tools` 节点内，而不是 turn 结束后的统一补录。
- proposal gate 的拒绝结果也属于可审计事实。拒绝不等于 turn failed，但应该体现在 `tool_calls` 上。
- `tool_calls` 不是孤立表。它必须能和 `conversations -> turns -> messages` 这条事实链路对齐。

关系：

- `conversations`：会话容器，提供跨 turn 的上下文边界。
- `turns`：一次 Jarvis 响应生命周期；`tool_calls.turn_id` 说明这次工具调用属于哪个响应。
- `messages`：可见对话事实；assistant message 负责表达“模型决定调用了什么工具”，tool message 负责把执行结果回灌到对话。
- `tool_calls`：结构化执行事实；负责回答“这次调用何时开始、何时结束、执行还是被拒绝、输入输出是什么”。

建议查询链路：

- 查某次响应发生了哪些工具调用：按 `turn_id` 查 `tool_calls`。
- 查某个工具调用是由哪次模型决策发起：通过 `assistant_message_id` 回查 `messages.id`。
- 查某次模型决策里有几个工具调用：按 `assistant_message_id` 聚合 `tool_calls`。

```sql
CREATE TABLE jarvis.tool_calls (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    turn_id BIGINT UNSIGNED NOT NULL,
    tool_name VARCHAR(128) NOT NULL,
    assistant_message_id BIGINT UNSIGNED NULL,
    provider_tool_call_id VARCHAR(128),
    step_index INTEGER UNSIGNED NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'running',
    input JSON,
    output JSON,
    error_message TEXT,
    started_at DATETIME(6),
    finished_at DATETIME(6),
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_tool_calls_turn_provider_call (turn_id, provider_tool_call_id),
    KEY idx_tool_calls_turn_created (turn_id, created_at),
    KEY idx_tool_calls_assistant_message (assistant_message_id),
    KEY idx_tool_calls_status_created (status, created_at),
    KEY idx_tool_calls_tool_name_created (tool_name, created_at),
    CONSTRAINT fk_tool_calls_turn FOREIGN KEY (turn_id) REFERENCES jarvis.turns(id) ON DELETE CASCADE,
    CONSTRAINT fk_tool_calls_assistant_message FOREIGN KEY (assistant_message_id) REFERENCES jarvis.messages(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
```

`status` 建议值：`requested`、`running`、`completed`、`failed`、`cancelled`、`rejected`。

字段说明：

- `assistant_message_id`：指向发起这次工具调用的 assistant message。这个字段把结构化审计记录正式挂回 `messages` 事实链，避免 `tool_calls` 成为悬空表。
- `provider_tool_call_id`：保存模型 provider 返回的 tool call id。它不是业务主键，而是“该 assistant message 内部的调用标识”，用于对账、排查和幂等辅助。
- `step_index`：保存本 turn 内第几次进入工具执行阶段，便于审计 ReAct loop 顺序，也方便恢复和测试断言。

新增字段在审计上的好处：

- `assistant_message_id`
  - 能明确回答“这次工具调用是由哪条 assistant message 发起的”。
  - 审计时可以把 assistant 的原始决策文本、tool call 列表、后续 tool result 放到一条完整链路里看。
  - 测试时不再只能按 `turn_id + tool_name` 做弱匹配。
- `provider_tool_call_id`
  - 能明确回答“这是不是同一条 assistant message 里的同一个 tool call”。
  - 对同名工具多次调用、重复回放、幂等防重都有帮助。
  - 排查 provider 输出和数据库记录不一致时，可以直接对账。
- `step_index`
  - 能明确回答“这次调用处于 turn 的第几轮 ReAct 决策”。
  - 比只看 `created_at` 更稳定，特别适合测试断言和故障回放。
  - 一条 assistant message 里可以有多个 tool call，`step_index` 用来表达轮次，`provider_tool_call_id` 用来表达同轮内的具体调用。

现状：

- 当前实现已经有 `tool_calls` 表，但写入发生在 turn 结束后，由 runtime 扫描整段 `messages` 再补写。
- 这种方式默认假设消息列表只包含本 turn 新增内容，但实际图 state 往往还包含 conversation 历史。
- proposal gate 的拒绝文本如果只作为 `ToolMessage` 回给模型，而不写入结构化状态，就会丢失“是谁拒绝、为什么拒绝”的审计价值。

问题：

- 同一个历史 tool call 可能在后续 turn 中被重复补录。
- 同一次工具调用如果执行失败、被 gate 拒绝或被取消，最终仍可能在审计表里表现为 `completed`。
- 没有 `assistant_message_id` 时，`tool_calls` 和 `messages` 的关系是断开的，无法精确知道是哪次模型决策发起了这次调用。
- 没有 `provider_tool_call_id` 时，测试和审计只能按 `tool_name` 和时间顺序做弱匹配，稳定性差。
- 没有 `step_index` 时，只能按时间猜测 ReAct loop 的顺序，不适合做精确断言。

解决方案：

- 在 `execute_tools` 节点收到 `AIMessage.tool_calls` 后，逐条立即创建 `tool_calls` 记录。
- 创建记录时同步写入 `turn_id`、`assistant_message_id`、`provider_tool_call_id`、`step_index`。
- proposal gate 拒绝时，更新该条记录为 `rejected`，并把拒绝原因写入 `error_message`；同时向模型返回对应 `ToolMessage`，让 ReAct loop 自行换方案或解释。
- 工具真正开始执行后，将状态更新为 `running`；执行成功写 `completed + output`，执行异常写 `failed + error_message`，turn 被取消则写 `cancelled`。
- `runtime` 不再扫描全量 `messages` 回填 `tool_calls`。图外只消费结构化结果，不再负责推断工具执行历史。

`input` / `output` 不要无脑存完整内容。不建议完整存：

```text
大文件内容
完整 PDF 解析结果
超长网页正文
完整 embedding 向量
大量检索结果全文
敏感 token、cookie、api key
过长 shell stdout
```

建议存摘要或引用：

```json
{
  "query": "reranker 部署方案",
  "index": "jarvis_wiki",
  "top_k": 10,
  "hit_ids": ["doc_1", "doc_2", "doc_3"]
}
```

### 9.7 上下文构造规则

构造上下文时，按 `conversation_id` 取历史消息，不按 `turn_id` 取。

```sql
SELECT *
FROM jarvis.messages
WHERE conversation_id = $1
ORDER BY created_at DESC
LIMIT 50;
```

因为用户可能会在群聊里说：

```text
@Jarvis 总结一下上面讨论
```

此时上面的讨论消息可能都是 `turn_id = null`，但它们仍然属于同一个 conversation，Jarvis 应该可以读取。

### 9.8 Turn 查询规则

如果要查看某一次 Jarvis 响应链路，按 `turn_id` 查。

```sql
SELECT *
FROM jarvis.messages
WHERE turn_id = $1
ORDER BY created_at ASC;
```

```sql
SELECT *
FROM jarvis.tool_calls
WHERE turn_id = $1
ORDER BY created_at ASC;
```

这用于：

```text
调试一次响应
查看工具调用链路
分析失败原因
做评测样本
回放执行过程
```

### 9.9 私聊场景示例

```text
Ryan: 帮我总结一下 Jarvis 的数据库设计
Jarvis: 总结如下...
```

落库：

```text
conversation c1:
  chat_type = dm
  owner_user_id = Ryan

turn t1:
  conversation_id = c1
  trigger_type = dm_message
  started_by_user_id = Ryan
  status = completed

messages:
  m1:
    conversation_id = c1
    turn_id = t1
    sender_type = user
    user_id = Ryan
    role = user
    content = 帮我总结一下 Jarvis 的数据库设计

  m2:
    conversation_id = c1
    turn_id = t1
    sender_type = assistant
    role = assistant
    content = 总结如下...
```

### 9.10 群聊场景示例

```text
Alice: 这个方案是不是太复杂了？
Bob: 可以先简化，turn 表保留就行。
Ryan: @Jarvis 总结一下刚才讨论，并给出最终设计。
Jarvis: 总结如下...
```

落库：

```text
conversation c2:
  chat_type = group
  owner_user_id = null

messages:
  m1:
    conversation_id = c2
    turn_id = null
    sender_type = user
    user_id = Alice
    role = user
    content = 这个方案是不是太复杂了？

  m2:
    conversation_id = c2
    turn_id = null
    sender_type = user
    user_id = Bob
    role = user
    content = 可以先简化，turn 表保留就行。

  m3:
    conversation_id = c2
    turn_id = t1
    sender_type = user
    user_id = Ryan
    role = user
    content = @Jarvis 总结一下刚才讨论，并给出最终设计。

  m4:
    conversation_id = c2
    turn_id = t1
    sender_type = assistant
    role = assistant
    content = 总结如下...
```

`m1` 和 `m2` 虽然没有 `turn_id`，但仍然能被 Jarvis 作为上下文读取。

### 9.11 /clear 逻辑

当用户执行 `/clear`，不要删除旧 conversation，而是归档：

```text
旧 conversation.status = archived
新建 conversation.status = active
新 conversation.cleared_from_conversation_id = 旧 conversation.id
```

这样可以保留历史、避免误删、方便审计，并让新上下文干净开始。

### 9.12 V1 暂不落表

第一版暂不落这些表：

```text
conversation_members
events
jobs
artifacts
permission_requests
resource_locks
channel_deliveries
conversation_summaries
memories
agent_runs
message_attachments
```

扩展时机：

- 需要群权限和高风险审批时，加 `conversation_members` 和 `permission_requests`。
- 需要长任务后台运行和恢复时，加 `jobs`。
- 需要图片、报告、diff、日志下载时，加 `artifacts`。
- 需要 Feishu 卡片更新、SSE、完整运行事实日志时，加 `events` 和 `channel_deliveries`。
- 需要代码仓库写锁时，加 `resource_locks`。
- 历史过长时，加 `conversation_summaries`。
- 需要长期偏好和事实时，加 `memories`。
- 一个 turn 内需要多次重试或多 agent 协作时，加 `agent_runs`。

### 9.13 LangGraph SQLite checkpoint

LangGraph checkpoint 使用独立 SQLite 文件：

```text
data/langgraph_checkpoints.sqlite
```

约束：

- 业务代码不直接依赖 checkpoint 表结构。
- checkpoint 是 runtime 恢复辅助，不是 Jarvis 业务事实源。
- SQLite checkpoint 文件必须和知识库 SQLite 分离。
- 启用 WAL、busy_timeout。
- checkpoint 写入只发生在 graph/runtime 边界，不在业务事务里混写。
- 如果后续出现 checkpoint 锁竞争，优先减少 checkpoint 写频率或改为按 conversation 分片文件；再评估 PostgreSQL checkpoint。

第一版如果暂不落 `events` 表，则业务恢复以 `turns`、`messages`、`tool_calls` 为准；当进入长运行和事件流阶段，应补上 `events` 表，并把它提升为业务事实源。

### 9.14 旧表处理

旧 agent 表可以删除或归档，因为当前数据都是测试数据：

- `runs` 删除，由 `turns` 替代。
- `tasks` 删除，不再作为核心实体。
- `work_orders` 删除，第一版由 `tool_calls` 替代，长任务阶段再引入 `jobs`。
- `work_results` 删除，第一版由 `tool_calls.output` 替代，产物阶段再引入 `artifacts`。
- `approvals` 删除，权限阶段再引入 `permission_requests`。
- `audit_logs` 删除，第一版由 `tool_calls` 和后续 `events` 替代。
- `resource_locks` 删除，代码写任务阶段按新 schema 重建。

知识库 SQLite 表暂不删除，继续由 KnowledgeBaseService 管理。

## 10. Channel Adapter

Channel adapter 只负责协议适配，不直接决定业务语义。

### 10.1 API Adapter

把 HTTP 请求转换为 user、conversation、message 和 turn。事件流 API 属于后续长运行阶段。

### 10.2 CLI Adapter

CLI 是本地 channel：

- `jarvis chat "..." --conversation-id ...`
- `jarvis status <turn_id>`
- `jarvis trace <turn_id>`
- `jarvis approve <permission_request_id>`

### 10.3 Feishu Adapter

飞书适配必须保留：

- chat_id
- chat_type
- message_id
- sender open_id
- mentions
- reply relation
- raw event id

群聊规则：

- 未 @ Jarvis 的普通消息默认只入库，不触发 turn，除非 conversation 配置为 always-listen。
- @ Jarvis 或回复 Jarvis 消息触发 turn。
- Jarvis 提问时要 @ 目标用户。
- 审批卡片只允许有权限的人操作。

## 11. 状态恢复

第一版恢复不依赖“重新跑节点图”，而是从 `turns`、`messages`、`tool_calls` 重建可见状态。进入长运行和事件流阶段后，再从 `events` 重建完整 runtime 状态。

第一版启动恢复流程：

```text
scan running/waiting turns
scan running tool_calls
mark stale turns/tool_calls as failed or waiting_manual_recovery
notify affected conversations
```

关键原则：

- 已完成 tool_call 不重复执行。
- 有外部副作用的 tool_call 恢复时必须保守。
- 第一版恢复动作写入 assistant/system message 或 tool_call error；后续写入 events。
- 等待用户的 turn 不自动继续。

## 12. 新旧架构映射

| 旧概念 | 新概念 | 处理方式 |
| --- | --- | --- |
| thread_id | conversation_id | 保留兼容别名 |
| run_id | turn_id | 改为每次用户触发一个 turn |
| Task | 可选计划摘要或 tool_call 序列 | 不作为核心持久实体 |
| WorkOrder | Job 或 ToolExecution | 仅长任务/外部执行需要 |
| risk_gate | permission tool + policy engine | 废弃固定节点 |
| clarify node | ask_user tool | 工具化 |
| monitor node | event-driven job watcher | 废弃轮询+interrupt 组合 |
| aggregate/verify | ReAct loop 自我评估 + tool result handling | 合并进 runtime |
| final_summary | assistant message + artifacts | 不只是一段 summary |

## 13. 迁移计划

### Phase 0：冻结旧工作流扩展

目标：停止在旧 `nodes.py` 上继续堆功能。

动作：

- 标记 `/agent/run` 和 Task 返回为兼容接口。
- 保留现有测试作为回归基线。
- 新能力只进入新 runtime。

### Phase 1：私聊多轮和群聊多人对话底座

目标：先打通 MySQL persistence、conversation/message/turn/tool_call，并让机器人私聊多轮对话和群聊多人上下文可用。

动作：

- 新建 MySQL database `jarvis` 和 V1 核心表：`users`、`conversations`、`messages`、`turns`、`tool_calls`。
- 配置独立 SQLite checkpoint 文件 `langgraph_checkpoints.sqlite`。
- 新增 `/conversations`、`/messages`、`/turns` API。
- API、CLI、Feishu 输入统一归一化为 Message。
- 私聊规则：同一用户默认复用一个 active conversation；用户每次发给 Jarvis 的消息都创建 turn。
- 群聊规则：同一群默认复用一个 active conversation；普通背景消息只入库，`turn_id = null`；@ Jarvis 或回复 Jarvis 消息时创建 turn。
- 上下文构造按 `conversation_id` 读取最近消息，不按 `turn_id` 读取。
- `/agent/run` 兼容层创建 message 和 turn，响应补充 `turn_id`，旧 `tasks` 字段标记 deprecated。

验收：

- 私聊可以连续发 3 条消息，第三条追问能读取前两条上下文；数据库能查到同一个 conversation 下的 3 个 user message、3 个 assistant message 和 3 个 turn。
- 群聊里 Alice、Bob 的普通讨论消息能入库且 `turn_id = null`；Ryan @Jarvis 后创建 turn，Jarvis 回答时能读取 Alice、Bob 的背景消息。
- 群聊消息能记录 sender、chat_id、chat_type、external_message_id、mentions、reply relation 和 raw event id。
- 未 @ Jarvis 的群聊消息不会触发 runtime，也不会产生 turn。
- `/agent/run` 可以通过兼容层创建 conversation、message 和 turn，并返回兼容的 `thread_id` 与新的 `turn_id`。

### Phase 2：最小 ReAct Runtime

目标：替换固定工作流的核心执行路径，让 Phase 1 的对话底座真正由新 runtime 回答。

动作：

- 实现 ContextBuilder。
- 实现 AgentRuntime loop。
- 工具接入：`ask_user`、`web.search`、`kb.search`、`artifact.create`、`echo`。
- 先用 `tool_calls` 记录短耗时工具调用；完整 `events` 表和 SSE 进入长运行阶段。

验收：

- 用户可以多轮追问。
- Jarvis 可以基于 conversation 历史回答私聊追问和群聊总结。
- Jarvis 可以主动 ask_user；第一阶段可以把用户补充作为新 message 继续同一 conversation，后续再升级为同一 turn 的挂起恢复。
- 搜索结果作为 tool_call output 进入上下文，最终回答带来源。

### Phase 3：长任务和 Claude Code

目标：支持代码修改和长时间 job。

动作：

- 实现 Job 表和 JobRunner。
- 接入 `claude_code.run`。
- 实现 resource lock。
- 实现 permission policy。
- 实现 job output event 和 artifact。

验收：

- 用户让 Jarvis 修改仓库，系统请求必要授权，启动 Claude Code job。
- Claude Code 完成后返回 changed files、diff summary、verification artifact。
- 同一仓库写任务串行，不同仓库可并行。

### Phase 4：Deep Research 和画图

目标：支持复杂研究和多模态产物。

动作：

- 实现 `deep_research.run`。
- 实现 `image.generate`。
- 报告和图片统一走 artifact。
- Feishu adapter 支持发送 artifact。

验收：

- Deep Research 可以多次搜索、交叉验证、生成报告 artifact。
- 用户可以要求 Jarvis 画图，返回图片 artifact，并在飞书中展示。

### Phase 5：下线旧工作流

目标：删除旧核心。

动作：

- `/agent/run` 内部完全走新 runtime。
- 删除或归档 `graph.py`、`nodes.py`、旧 Task/WorkOrder 调度状态。
- 只保留必要 skill/worker 执行代码。

## 14. 关键设计决策

1. Jarvis 的核心实体是 conversation 和 turn，不是 task。
2. ReAct loop 是主执行模型，固定 DAG 不是主执行模型。
3. 长期目标是所有输入、输出、工具调用和恢复信号都事件化；第一阶段先以 `messages`、`turns`、`tool_calls` 作为可查询事实源，事件表进入长运行阶段。
4. Tool call 是 Agent 行为记录；Job 是长任务执行记录；Artifact 是用户可消费产物。
5. 权限是 runtime policy，不是固定图节点。
6. 群聊能力必须从 API 和数据模型开始支持，不能只靠 channel 临时拼字段。
7. Deep Research、Claude Code、网页搜索、画图都应作为工具进入同一 runtime。
8. v3 Agent runtime V1 主业务库使用 MySQL；知识库 SQLite 短期保留并通过工具接口隔离。
9. 第一版业务事实源是 `messages`、`turns`、`tool_calls`；长运行和事件流阶段补上 `events` 并提升为完整 runtime 事实源。
10. LangGraph checkpoint 使用独立 SQLite 文件，业务代码不直接依赖 checkpoint 表结构。

## 15. 开放问题

- topic/thread 是否需要独立 conversation：第一阶段单聊按 user、群聊按 chat_id 复用 active conversation；topic 按 root message 细分留到后续。
- 群聊中哪些用户拥有审批写仓库权限？
- Claude Code 是否继续走 `--print` 非交互模式，还是改为可流式、可转发问题的交互模式？
- 图片生成使用哪一个 provider，artifact 存本地文件还是对象存储？
- LangGraph 是否继续用于 ReAct loop，还是先实现轻量自有 runtime？
- 事件流扩展阶段使用轮询还是 SSE？
