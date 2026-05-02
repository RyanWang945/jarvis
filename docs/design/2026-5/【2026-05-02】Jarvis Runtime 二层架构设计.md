# Jarvis Runtime 二层架构设计

| 项目 | 内容 |
|------|------|
| 作者 | Codex |
| 日期 | 2026-05-02 |
| 状态 | Draft |
| 相关设计 | `【2026-05-02】 context manager设计.md`, `【2026-5-1】Jarvis 长运行多轮对话 ReAct Agent 设计.md` |
| 目标 | 统一上下文与执行 loop，减少主图/子图边界扭曲 |

---

## 1. 背景

当前 Jarvis 的 agent runtime 采用主图/子图结构：

```text
MainGraph:
  prepare -> react -> persist

ReactGraph:
  call_llm -> execute_tools -> call_llm
```

这套结构在第一版实现里是可行的，但它天然会引入一类边界问题：

- 上下文装配在主图和子图之间分裂
- 子图既负责 ReAct loop，又承担部分持久化
- graph state 和业务状态并不完全同构
- 当前 turn 的 working context 与历史 context 没有统一 owner
- 后续如果接入长运行、多代理、memory，会继续放大这种割裂

因此，如果目标变为：

1. 从根上解决主图/子图边界不自然的问题
2. 让上下文和执行 loop 完全统一
3. 为以后长运行、多代理、复杂 memory 铺路

那么需要调整“二层架构”的分层方式。

这里仍然保留二层，但二层不再是“主图 + 子图”，而是：

```text
第一层：Runtime Orchestrator
第二层：Turn Runtime
```

---

## 2. 为什么主图 / 子图分层不再自然

当前主图/子图的主要问题，不是实现细节，而是分层语义本身不稳定。

### 2.1 上下文没有唯一 owner

现在实际上是：

- 主图 `prepare` 负责加载历史和一部分 prompt 注入
- 子图 `call_llm` 负责 trim、system prompt 拼接和工具暴露

这意味着：

- 上下文预算没有唯一真相来源
- 谁决定最终发给模型什么消息并不清晰
- 后续压缩、summary、memory 注入会继续重复分布

### 2.2 执行 loop 被 graph 边界切断

对一个 agent 来说，更自然的执行闭环应当是：

```text
load context
-> call llm
-> execute tool
-> update working state
-> continue or stop
-> finalize
```

但现在这条闭环被拆成：

- 主图负责一部分 lifecycle
- 子图负责一部分 execution
- DB 中途又写入一部分事实

这使得 runtime 很难演化成：

- 长运行
- 可恢复
- 可回放
- 多代理协同

### 2.3 多代理与 memory 会继续放大问题

如果未来直接在主图/子图结构上叠加：

- subagent
- worker
- long-term memory
- turn 内 compaction

最后很容易变成：

- 图里嵌图
- state 里嵌 state
- 持久化语义散落在多个节点
- 上下文压缩策略散落在多个层次

这不是一个适合长期演化的 runtime 形态。

---

## 3. 新二层架构

新的二层不是：

```text
MainGraph / ReactGraph
```

而是：

```text
Runtime Orchestrator / Turn Runtime
```

这两个层次的分界依据不是 graph，而是运行时职责边界：

- 谁负责“调度一个 run”
- 谁负责“执行一个 run”

---

## 4. 第一层：Runtime Orchestrator

### 4.1 定义

`Runtime Orchestrator` 是外层调度与生命周期协调层。

它关注的是：

- 入口事件
- run 生命周期
- 路由与策略
- 恢复、取消、超时
- 多代理编排

它不关注：

- 每一步 prompt 怎么拼
- 每一步 tool call 怎么推进
- 当前 turn 的 working state 如何压缩

### 4.2 职责

这一层负责：

1. Intake
- 接收 Feishu / CLI / API 请求
- 将输入归一化为 conversation / turn / event

2. Conversation / Turn lifecycle
- 创建 conversation
- 创建 turn
- 标记 queued / running / cancelled / completed / failed

3. Routing
- 决定调用哪个 runtime profile
- 决定本轮 skills / policies / model profile

4. Long-running supervision
- 恢复 unfinished turns
- 处理 cancel
- 处理 timeout
- 管理 worker / subagent 生命周期

5. Persistence coordination
- 写高层业务状态
- 写 event / artifact metadata
- 管理 checkpoint 恢复边界

### 4.3 不负责什么

这一层不负责：

- 历史消息如何裁剪
- tool output 如何回填模型
- ReAct step 如何推进
- 最终 prompt 如何形成
- turn 内部压缩如何触发

如果这些逻辑泄漏到 Orchestrator，说明分层已经再次失真。

---

## 5. 第二层：Turn Runtime

### 5.1 定义

`Turn Runtime` 是单个 turn 或单个 agent run 的完整执行闭环。

它是 runtime 内真正的“agent loop”。

### 5.2 职责

这一层负责：

1. Context assembly
- 加载 active summary
- 加载 recent messages
- 构造当前 turn working context

2. Execution loop
- 调用 LLM
- 解析 tool calls
- 执行工具
- 回填结果
- 判断是否继续

3. In-turn compaction
- 压缩或裁短旧 tool output
- 压缩当前 turn 内旧 step
- 控制当前 working set 的 token 预算

4. Turn result generation
- 生成 final reply
- 生成 reasoning / audit 信息
- 输出失败或取消结果

5. Runtime-local state machine
- step_count
- pending tool calls
- final_reply
- running / completed / failed / cancelled

### 5.3 这一层的核心原则

这一层必须统一：

- 上下文
- 执行 loop
- 工具调用
- 压缩策略
- turn 内状态推进

也就是说：

```text
谁负责执行 turn
谁就负责维护 turn 的 context
```

---

## 6. 两层之间如何协作

### 6.1 协作边界

更自然的调用关系应该是：

```text
Runtime Orchestrator
  -> create / resume turn
  -> build runtime profile
  -> invoke Turn Runtime
  -> consume result / events / artifacts
```

而 Turn Runtime 内部是：

```text
load visible context
-> call llm
-> execute tool
-> update working state
-> maybe compact
-> continue or stop
-> return result
```

### 6.2 返回结果

Turn Runtime 应该返回结构化结果，而不是只返回一串 messages。

例如：

```python
class TurnRunResult(TypedDict):
    status: str
    reply: str
    working_messages: list[BaseMessage]
    step_count: int
    was_compacted: bool
    emitted_events: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    error: str | None
```

这样 Orchestrator 才能只做协调，而不是被迫理解 turn 内部细节。

---

## 7. 上下文管理放在哪一层

`ContextManager` 应当属于 `Turn Runtime`，而不是 `Runtime Orchestrator`。

原因很直接：

- 它管理的是模型可见上下文
- 它参与 turn 内 working set 的裁剪
- 它和 tool result 的回填策略强相关
- 它属于 turn 执行闭环的内部能力

因此未来结构应更像：

```text
Turn Runtime
  + ContextManager
  + ToolRuntime
  + ModelClient
```

而不是：

```text
Orchestrator
  + ContextManager
```

---

## 8. 多代理如何放进这套架构

### 8.1 基本原则

多代理不应当继续做成：

- 图里再嵌子图
- 一个 runtime 内再嵌另一个 runtime 的局部 state

更自然的方式是：

- Orchestrator 负责 spawn sub-runtime
- 每个 subagent 是独立的 Turn Runtime 实例
- 通过 event / artifact / message 交互

### 8.2 为什么这样更自然

这样做的好处：

- 每个 agent 拥有独立上下文窗口
- 每个 agent 拥有独立 working state
- 每个 agent 拥有独立 compaction 策略
- 主 agent 不需要理解子 agent 的内部 ReAct trace

这比“在一个大 graph 里再嵌很多 graph 节点”稳定得多。

---

## 9. Memory 如何放进这套架构

这套二层架构下，memory 也应该按语义分层，而不是按 graph 分层。

至少要区分三种：

1. Conversation summary
- 目的：窗口压缩
- 所属：Turn Runtime 可见

2. Working memory
- 目的：当前任务推进
- 所属：Turn Runtime 内部 working state

3. Long-term memory
- 目的：跨会话持久知识
- 所属：Orchestrator 或独立 memory service 检索接入

这三者不要再混成一个“summary_text”。

否则后续复杂 memory 很快会重新污染上下文边界。

---

## 10. Persistence 与恢复策略

### 10.1 核心原则

checkpoint 只负责恢复，不负责业务事实源。

也就是说：

- checkpoint 可以继续存在
- 但业务状态应由自己的 persistence / event store 维护
- runtime 恢复不应直接等价于“业务事实”

### 10.2 推荐方向

即使第一版不做完整 event sourcing，也建议逐步把这些视作一等事件：

- `user_message_received`
- `assistant_tool_requested`
- `tool_completed`
- `assistant_replied`
- `turn_failed`
- `turn_cancelled`
- `context_compacted`

这样好处是：

- 恢复路径清晰
- 排障路径清晰
- 多代理和长运行更容易扩展

---

## 11. 与当前主图 / 子图方案的关系

### 11.1 不是立刻推翻现有实现

这份设计不是要求立刻删除现有 `MainGraph / ReactGraph`。

更现实的演进路径是：

1. 先用 `ContextManager` 收敛上下文 owner
2. 再把 ReAct 子图收缩为普通 Python loop
3. 最后把 LangGraph 退到更外层，或者只保留在 Orchestrator 层

### 11.2 现阶段可以接受的过渡形态

短期内可以接受：

```text
LangGraph 继续保留
但只作为外层 orchestration 壳
Turn Runtime 内部不再继续 graph 化
```

这已经比现在的主图/子图边界自然很多。

---

## 12. 推荐演进步骤

### Phase 1：边界收敛

目标：不改 runtime 总体形态，先收住上下文分裂。

- 引入 `ContextManager`
- 引入 `RuntimeConfig`
- 去掉子图 `_trim_messages()`
- 工具暴露改成 `allowed_tools`

### Phase 2：统一 Turn Runtime

目标：把上下文和执行 loop 收到一个统一 runtime 内。

- 合并 `AgentState` / `ReActState` 为统一 turn runtime state
- 把 ReAct 子图收缩为普通 execution loop
- 明确 turn 内 working context 与历史 context 分层

### Phase 3：外层 Orchestrator 化

目标：让上层只负责调度，不再插手 turn 内 prompt。

- 把当前主图收缩成 Runtime Orchestrator
- 恢复、取消、超时、worker 管理提升到外层
- checkpoint 只保留为恢复能力

### Phase 4：长运行 / 多代理 / memory 演进

目标：在稳定 runtime 边界上继续扩展能力。

- subagent 独立 runtime 化
- 引入 long-term memory service
- 引入 event / artifact 更清晰的结果边界
- 内建化 compaction

---

## 13. 第一版不做什么

在这份新二层架构目标下，第一版仍然不做：

- 多级 runtime DAG
- 图里嵌图的多代理编排
- 完整 event sourcing 重写
- 自动 memory 学习引擎
- 多种 runtime profile 同时重构

第一版的重点仍然是：

```text
把执行 loop 和上下文 owner 收到同一个层级
```

---

## 14. 结论

在“统一上下文和执行 loop、为长运行/多代理/memory 铺路”的目标下，Jarvis 仍然应该保留二层架构，但二层不应再按“主图 / 子图”来划分。

更自然的分层是：

1. `Runtime Orchestrator`
- 负责外层调度、生命周期、恢复、路由、多代理管理

2. `Turn Runtime`
- 负责单个 turn 的上下文、ReAct loop、工具调用、压缩、最终回复

这意味着：

- `ContextManager` 属于 Turn Runtime
- 工具执行 loop 属于 Turn Runtime
- checkpoint 只做恢复，不做业务事实源
- 多代理通过独立 runtime 实例扩展，而不是 graph 内继续嵌套

如果后续朝这个方向演进，当前 `ContextManager` 设计可以作为第一步，但它只是边界收敛，不是最终 runtime 形态。
