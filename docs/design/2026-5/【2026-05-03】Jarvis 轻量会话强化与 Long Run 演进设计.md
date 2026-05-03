# Jarvis 轻量会话强化与 Long Run 演进设计

## 1. 结论

当前 Jarvis 的主链路能跑，且模型很清楚：

- `conversation` 是跨轮上下文容器。
- `turn` 是一次真实执行单元。
- 一个 `turn` 内部是一轮同步 ReAct while loop。
- `messages / turns / tool_calls` 已经能形成可审计事实链。

因此这版设计不建议马上引入 `runs`、任务树、复杂 checkpoint 体系。更合适的第一步是：

1. 保留 `turn = 执行单元`。
2. 强化 `conversation = session state container`。
3. 用强类型 `ConversationSessionState` 管理 session 状态。
4. 让 `/status` 优先展示 Jarvis 当前认知状态。
5. 对 `working_summary` 采取保守更新策略，避免污染长期上下文。

这条路线更贴合 Codex / Claude Code 的公开设计风格：核心是持续 session、工具循环、权限与状态可观测，而不是上来做重型 workflow。


## 2. 当前架构判断

### 2.1 当前事实

当前执行路径是：

1. `/messages` 写入用户消息，必要时创建 `turn`
2. `/turns/{id}/run` 调 `AgentRuntime.run_turn(turn_id)`
3. `TurnRuntime._prepare()` 加载 conversation 下的历史消息
4. `TurnRuntime.invoke()` 在一个同步 while loop 中执行
5. 每步执行 `call_llm()`，必要时执行 `execute_tools()`
6. 最终写入 assistant message，并完成或失败该 turn

对应代码：

- [runtime.py](/E:/pythonProject/jarvis/app/agent_react/runtime.py:136)
- [react_graph.py](/E:/pythonProject/jarvis/app/agent_react/react_graph.py:113)
- [context_manager.py](/E:/pythonProject/jarvis/app/agent_react/context_manager.py:117)

### 2.2 当前数据库事实

当前核心表：

- `conversations`
- `messages`
- `turns`
- `tool_calls`

对应代码：

- [models.py](/E:/pythonProject/jarvis/app/persistence/models.py:19)
- [conversation_store.py](/E:/pythonProject/jarvis/app/persistence/conversation_store.py:88)

现有关系是合理的：

- `conversations`：会话边界
- `messages`：可见对话事实
- `turns`：一次 Jarvis 响应生命周期
- `tool_calls`：一次 turn 内部的工具执行审计

这套结构第一阶段不需要推翻。


## 3. 重新思考后的核心边界

### 3.1 conversation 的职责应该变重

`conversation` 不应只是消息桶。对 Jarvis 的目标而言，它应该承载 session state：

- 当前模式：`chat / coding / research`
- 当前目标
- 当前工作摘要
- 当前等待状态
- 最近一次 turn 状态
- 最近一次 assistant 摘要
- 最近一次可靠更新来自哪个 turn

这让 Jarvis 能回答一个关键问题：

> 我现在认为自己在做什么？

### 3.2 turn 继续作为执行单元

当前阶段不建议引入独立 `run` 表。

原因：

1. 当前 `turn` 已经完整表达一次执行。
2. 工具调用审计已经挂在 `turn_id` 下。
3. `/turns/{id}/run` 已经是稳定入口。
4. 直接引入 `run` 会带来大量迁移和概念重叠。

因此第一阶段仍然保持：

- `conversation = session state`
- `turn = execution unit`
- `tool_call = execution audit`

### 3.3 working_summary 与 active_summary 必须分清

这是本设计最重要的修正。

`working_summary`：

- 是 Jarvis 当前工作状态
- 短、保守、可读
- 只记录已确认事项、当前目标、下一步
- 不应频繁自动改写

`active_summary`：

- 是长历史压缩摘要
- 用于 token 窗口管理
- 可以通过摘要流程定期更新
- 主要解决“历史太长”的问题

两者不能混用。

第一阶段先做 `working_summary`，不强制落 `active_summary` 字段。


## 4. ConversationSessionState

### 4.1 存储方式

第一阶段仍然存到 `conversations.metadata`，避免立即改表。

但代码侧必须有强类型模型，不能散落读写裸 `dict`。

建议新增：

```text
app/agent_react/session_state.py
```

### 4.2 强类型模型

建议初版字段：

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

SessionMode = Literal["chat", "coding", "research"]
WaitingFor = Literal["user", "approval", "tool", "external"] | None


@dataclass(frozen=True)
class ConversationSessionState:
    version: int = 1
    session_mode: SessionMode = "chat"
    session_goal: str | None = None
    working_summary: str | None = None
    waiting_for: WaitingFor = None
    last_turn_id: int | None = None
    last_turn_status: str | None = None
    last_assistant_summary: str | None = None
    updated_by_turn_id: int | None = None


def load_session_state(metadata: dict[str, Any] | None) -> ConversationSessionState:
    ...


def dump_session_state(state: ConversationSessionState) -> dict[str, Any]:
    ...
```

### 4.3 metadata 结构

建议放在独立 namespace，避免污染已有 metadata：

```json
{
  "session": {
    "version": 1,
    "session_mode": "chat",
    "session_goal": null,
    "working_summary": null,
    "waiting_for": null,
    "last_turn_id": 123,
    "last_turn_status": "completed",
    "last_assistant_summary": "简短摘要",
    "updated_by_turn_id": 123
  }
}
```

不要把 session 字段平铺到 `metadata` 顶层。当前 `metadata` 已经承载 `mentions`、`cleared_from_conversation_id` 等业务信息，继续平铺会增加冲突风险。


## 5. working_summary 更新策略

### 5.1 原则

`working_summary` 是长期上下文入口，必须保守。

更新原则：

- 默认不自动更新
- 只在 turn 成功完成后考虑更新
- 只写确认过的信息
- 不写模型猜测
- 不写工具原始输出
- 不写 reasoning 内容
- 宁可漏写，也不要污染

### 5.2 第一阶段建议

第一阶段只做两类更新：

1. 必写状态字段
2. 谨慎更新工作摘要

必写字段：

- `last_turn_id`
- `last_turn_status`
- `last_assistant_summary`
- `session_mode`
- `updated_by_turn_id`

谨慎字段：

- `session_goal`
- `working_summary`

### 5.3 保守更新器

建议新增：

```python
def build_conservative_session_patch(
    *,
    previous: ConversationSessionState,
    turn_id: int,
    turn_type: str,
    turn_status: str,
    trigger_text: str,
    final_reply: str,
    tool_calls: list,
) -> ConversationSessionState:
    ...
```

规则：

1. `turn_status != "completed"` 时不更新 `working_summary`。
2. `final_reply` 为空时不更新 `working_summary`。
3. 工具全部失败或被拒绝时不更新 `working_summary`。
4. 普通 chat 默认只更新 `last_*` 字段。
5. `research / coding` turn 可以更新 `session_goal`。
6. 只有回复中出现明确结论、已完成事项、下一步时，才更新 `working_summary`。
7. 如果新内容与旧 `working_summary` 冲突，保留旧值，把新内容放到 `last_assistant_summary`。

### 5.4 长度限制

建议限制：

- `last_assistant_summary`: 200 字以内
- `working_summary`: 800 字以内
- `session_goal`: 120 字以内

超过长度直接截断或不写，后续再引入正式 summarizer。


## 6. /status 作为认知状态调试入口

当前 `/status` 偏会话统计和系统参数。对 long run / deep research 来说，它应该优先展示 Jarvis 当前认知状态。

### 6.1 展示顺序

建议 `/status` 输出顺序：

1. 当前 session mode
2. 当前 session goal
3. waiting_for
4. working_summary
5. last_turn_id / last_turn_status
6. last_assistant_summary
7. 消息数、turn 统计
8. LLM provider、model、timeout

### 6.2 调试价值

这个接口用于回答：

- Jarvis 是否认为当前是 research 模式？
- Jarvis 当前目标是什么？
- Jarvis 现在是否在等待用户、审批或外部结果？
- Jarvis 的工作摘要是否被污染？
- 最近一次 turn 是否更新了 session state？

因此 `/status` 应该在 Phase 1 优先接入 session state。


## 7. ContextManager 改造

### 7.1 输入改造

当前：

```python
build_initial_messages(records, trigger_message_id)
```

建议改为：

```python
build_initial_messages(
    records,
    trigger_message_id,
    *,
    session_state: ConversationSessionState | None = None,
)
```

### 7.2 注入方式

如果存在 `session_goal` 或 `working_summary`，在 system prompt 之后注入一个额外 `SystemMessage`：

```text
Conversation session state:
- Mode: research
- Goal: ...
- Working summary: ...
```

注意：

- 不注入 `last_assistant_summary`，它主要用于调试，不应默认影响模型。
- 不注入 `waiting_for`，除非未来支持真正暂停/恢复。
- 不注入未验证的 metadata。


## 8. Store 改造

### 8.1 最小接口

在 `ConversationStore` 协议和 `MySQLConversationStore` 中新增：

```python
def get_conversation(self, conversation_id: int) -> ConversationRecord | None: ...

def update_conversation_session(
    self,
    conversation_id: int,
    session_state: ConversationSessionState,
) -> None: ...
```

实现上仍然写 `conversations.metadata.session`。

### 8.2 更新方式

MySQL 侧推荐使用 JSON merge patch，而不是整块覆盖 metadata。

目标：

- 保留已有 metadata
- 只更新 `$.session`
- 避免覆盖 `cleared_from_conversation_id` 等已有字段


## 9. TurnRuntime 改造

### 9.1 prepare 阶段

`_prepare()` 增加：

1. 读取 conversation
2. 从 `conversation.metadata` 加载 `ConversationSessionState`
3. 传入 `ContextManager.build_initial_messages()`
4. 把 session state 放入 `TurnRuntimeState`

### 9.2 finalize 阶段

`_finalize()` 增加：

1. 提取最终 reply
2. 查询本 turn 的 tool_calls
3. 生成保守 session patch
4. 写回 conversation session

失败和取消时：

- 更新 `last_turn_id`
- 更新 `last_turn_status`
- 清空或保留 `waiting_for` 需要按状态判断
- 不更新 `working_summary`


## 10. Deep Research 的轻量落点

当前阶段不单独做 research runtime。

建议先让 research 体现在 session state 上：

- `turn_type == "research"` 时，把 `session_mode` 设置为 `research`
- `session_goal` 记录用户研究目标
- `working_summary` 只记录确认过的研究进展
- `/status` 能看到当前 research 状态

这已经能明显提升 deepresearch 的连续性和可调试性。

等出现以下需求，再考虑更重的模型：

- 一个研究需要跨进程后台跑
- 需要阶段性流式事件
- 需要中途挂起再恢复
- 需要生成可下载报告、图片、表格等产物

那时再引入：

- `events`
- `artifacts`
- 或独立 `runs`


## 11. 分阶段实施

### Phase 0：文档与测试基线

1. 明确 `turn` 仍是执行单元。
2. 明确 `conversation` 是 session state 容器。
3. 补充 tests 中 `/status` 当前行为的覆盖。

### Phase 1：Session State 与 Status

目标：先把状态模型和观测面建立起来。

改造项：

1. 新增 `app/agent_react/session_state.py`
2. 新增 `ConversationSessionState`
3. `MySQLConversationStore` 增加 `update_conversation_session()`
4. `InMemoryConversationStore` 同步增加测试实现
5. `/status` 优先展示 session state

这一阶段不把 `working_summary` 注入模型。

### Phase 2：Context 注入

目标：让 Jarvis 在当前 turn 中感知 session state。

改造项：

1. `TurnRuntime._prepare()` 加载 session state
2. `ContextManager.build_initial_messages()` 接收 session state
3. 只注入 `session_mode / session_goal / working_summary`
4. 增加测试确认注入内容可被模型看到

### Phase 3：保守写回

目标：让 turn 完成后可以更新 session state。

改造项：

1. 实现 `build_conservative_session_patch()`
2. `_finalize()` 成功时写回 session state
3. 失败、取消时只更新状态字段
4. 增加污染防护测试

### Phase 4：active_summary

当上下文长度成为实际问题时再做：

```sql
ALTER TABLE conversations
  ADD COLUMN active_summary TEXT NULL,
  ADD COLUMN active_summary_token_count INT NOT NULL DEFAULT 0,
  ADD COLUMN summarized_until_message_id BIGINT UNSIGNED NULL;
```

`active_summary` 用于历史压缩，不替代 `working_summary`。


## 12. 当前文档对旧设计的修正

这版相对旧稿有几个关键调整：

1. session 字段放入 `metadata.session`，不是平铺在 `metadata` 顶层。
2. `ConversationSessionState` 是 Phase 1 必做，不是可选项。
3. `/status` 是 Phase 1 必做，用来调试 Jarvis 当前认知状态。
4. `working_summary` 第一阶段不急着注入和更新，先建立模型和可观测面。
5. `working_summary` 与 `active_summary` 分离，前者是工作状态，后者是历史压缩。
6. 不在当前阶段引入 `runs / tasks / events / artifacts`。


## 13. 最小验收标准

Phase 1 完成后，应满足：

1. `ConversationSessionState` 可以从空 metadata 正确加载默认值。
2. session state 可以写回 `conversations.metadata.session`。
3. `/status` 展示 session mode、goal、waiting、working summary、last turn 状态。
4. 现有 conversation runtime 测试继续通过。

Phase 2 完成后，应满足：

1. session state 能注入模型上下文。
2. 不存在 session state 时，模型上下文与当前行为保持一致。
3. skills 注入和 system prompt 顺序不被破坏。

Phase 3 完成后，应满足：

1. completed turn 更新 `last_turn_*`。
2. failed/cancelled turn 不更新 `working_summary`。
3. chat turn 默认不污染 `working_summary`。
4. research/coding turn 只在规则允许时更新 `working_summary`。


## 14. 最终判断

对当前 Jarvis，最稳的路线不是马上上 `run`，也不是把 `turn` 推翻，而是先把 `conversation` 变成真正的 session 容器。

这能在最小改造下解决三个真实问题：

1. Jarvis 缺少可读的当前认知状态。
2. long run 缺少跨 turn 的工作记忆。
3. deepresearch 缺少保守、可调试的持续上下文。

因此本设计建议按 `ConversationSessionState -> /status -> context 注入 -> 保守写回` 的顺序推进。
