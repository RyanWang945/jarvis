# Jarvis Context Manager 设计

| 项目 | 内容 |
|------|------|
| 作者 | Codex |
| 日期 | 2026-05-02 |
| 状态 | Draft |
| 定位 | `Turn Runtime` 内部的上下文能力设计 |
| 前置设计 | `【2026-05-02】Jarvis Runtime 二层架构设计.md` |
| 相关代码 | `app/agent_react/agent_graph.py`, `app/agent_react/react_graph.py` |

---

## 1. 文档定位

这份文档不再把“主图 / 子图 + ContextManager”当成最终架构目标。

它的前提是：

1. Jarvis 先完成 runtime 二层改造
2. 二层边界从 `MainGraph / ReactGraph` 收敛为：

```text
第一层：Runtime Orchestrator
第二层：Turn Runtime
```

在这个前提下，`ContextManager` 的定位是：

```text
Turn Runtime 内部的上下文管理组件
```

也就是说，这份设计解决的是：

- Turn Runtime 内部如何统一管理模型上下文
- 如何做当前 turn 的 working context 装配
- 如何接入 conversation summary / tool output pruning / compaction

它不解决：

- 外层 Orchestrator 如何调度 turn
- 多代理生命周期如何管理
- checkpoint 如何恢复

这些属于上一份“Runtime 二层架构设计”的范围。

---

## 2. 为什么要在二层改造之后再做 ContextManager

如果在 `MainGraph / ReactGraph` 边界还没收敛之前就做完整的 `ContextManager`，会有两个问题：

1. `ContextManager` 容易变成“主图做一半、子图做一半”的包装层  
结果仍然是：
   - 主图负责历史加载
   - 子图负责最终裁剪
   - 上下文 owner 看似统一，实际仍然分裂

2. 后续一旦把 `ReactGraph` 收缩为普通 execution loop，`ContextManager` 还要重写一次  
这会造成重复设计和重复迁移。

因此正确顺序应当是：

```text
先做 runtime 二层改造
再做 Turn Runtime 内部的 context 能力改造
```

也就是：

1. 先把“谁负责执行一个 turn”收敛出来
2. 再把“执行 turn 的组件如何管理上下文”收敛出来

---

## 3. 设计目标

在新二层架构下，`ContextManager` 的目标是：

1. 成为 `Turn Runtime` 内唯一的模型上下文 owner
2. 统一历史上下文、当前 turn working context、runtime policy 注入
3. 为 conversation summary、in-turn compaction、tool output pruning 提供落点
4. 让执行 loop 不再关心 prompt 拼接细节
5. 为后续 long-term memory 接入预留稳定边界

它不负责：

- tool 执行
- turn 生命周期推进
- DB 持久化事务编排
- subagent 调度

---

## 4. 在新二层中的位置

在新的架构里，应当是：

```text
Runtime Orchestrator
  -> invoke Turn Runtime

Turn Runtime
  + ContextManager
  + ToolRuntime
  + ModelClient
```

其中：

- `Runtime Orchestrator` 只负责调度和生命周期
- `Turn Runtime` 负责执行闭环
- `ContextManager` 属于 `Turn Runtime` 的内部能力

也就是说：

```text
谁负责执行 turn
谁就负责维护 turn 的 context
```

---

## 5. ContextManager 的职责边界

### 5.1 负责什么

`ContextManager` 负责：

- 从持久化消息与 runtime state 构造可见上下文
- 组装 system prompt / skill prompt / runtime policy
- 加载 active conversation summary
- 选择 recent history 与 current turn working set
- 对超长 tool output 做模型可见层的裁短
- 在真正调用模型前将上下文渲染为 `LLMMessage`
- 在需要时触发 in-turn compaction

### 5.2 不负责什么

`ContextManager` 不负责：

- 工具执行
- 数据库写入
- turn success / failure / cancel 状态推进
- artifact 存储
- 长期记忆检索策略本身

这些能力不应被混入 `ContextManager`，否则它会迅速膨胀成新的 God object。

---

## 6. ContextManager 要管理的三层上下文

在新的 Turn Runtime 内，建议明确区分三层上下文：

### 6.1 Conversation Summary

用途：

- 压缩旧历史
- 缩短模型窗口占用

来源：

- `conversation_context_summary` active 记录

特点：

- 表示已压缩的 conversation 历史
- 不等于 working memory
- 不等于 long-term memory

### 6.2 Recent History

用途：

- 保留最近几轮原始上下文
- 维持对话连续性

来源：

- active summary 之后的 recent messages / recent turn blocks

特点：

- 仍是原始消息
- 保持结构完整
- 优先保留最近的完整交互片段

### 6.3 Current Turn Working Context

用途：

- 支撑当前 turn 内的 ReAct 执行闭环

来源：

- 当前 turn 的 user 输入
- 当前 turn 内已发生的 tool calls / tool results / intermediate assistant outputs

特点：

- 是最需要被严格保护的上下文
- 后续 in-turn compaction 主要针对这一层
- 这一层不应被 conversation summary 替代

---

## 7. 推荐接口

在新架构下，`ContextManager` 更适合围绕 `Turn Runtime` 的 working state 设计，而不是围绕 graph node 设计。

推荐接口：

```python
class ContextManager:
    def build_visible_context(
        self,
        runtime_state: TurnRuntimeState,
    ) -> list[BaseMessage]:
        ...

    def render_for_model(
        self,
        runtime_state: TurnRuntimeState,
    ) -> list[LLMMessage]:
        ...

    def maybe_compact_in_turn_context(
        self,
        runtime_state: TurnRuntimeState,
    ) -> TurnRuntimeState:
        ...
```

这里的核心变化是：

- 输入不再是“主图传进来的 messages + runtime_config”
- 输入应尽量直接是 `TurnRuntimeState`

这能保证 `ContextManager` 真正依附于 `Turn Runtime`，而不是依附于某个 graph node。

---

## 8. 对 TurnRuntimeState 的要求

如果 `ContextManager` 要工作，`Turn Runtime` 必须有统一的 runtime state。

至少需要这些字段：

```python
class TurnRuntimeState(TypedDict):
    turn_id: int
    conversation_id: int
    selected_skills: list[str]
    allowed_tools: list[str]
    model: str
    risk_level: str

    active_summary: str | None
    recent_history_messages: list[BaseMessage]
    working_messages: list[BaseMessage]

    step_count: int
    status: str
    final_reply: str | None
    error: str | None
```

说明：

- `active_summary`：conversation 级压缩摘要
- `recent_history_messages`：active summary 之后仍保留的原始历史
- `working_messages`：当前 turn 内 working set

这三个字段应由 `ContextManager` 统一消费，而不是再分散到多个 graph state 中。

---

## 9. 第一版能力范围

在“先做二层改造，再做 context 能力改造”的顺序下，第一版 `ContextManager` 只建议做这些能力：

### 9.1 可见上下文统一装配

统一负责：

- base system prompt
- skill prompt
- runtime policy
- active summary
- recent history
- current turn working messages

### 9.2 删除 message-count 级硬裁剪

明确废弃旧逻辑：

- `_trim_messages()`

改为：

- 基于 runtime state 的统一上下文装配
- 必要时做 token-aware 裁剪

### 9.3 工具输出裁短

第一版最务实、最有收益的能力之一是：

- 工具结果完整落库
- 回给模型前可以裁短

这样可以显著降低当前 turn working context 的膨胀速度。

### 9.4 active summary 接入

第一版只做：

- 读取 `conversation_context_summary` 的 active 记录
- 将其作为 history compaction 结果注入上下文

不做：

- 摘要链拼接
- 多 active summary 合并
- 自动重写历史摘要树

---

## 10. 摘要持久化与读取约定

### 10.1 表结构前提

沿用前一版已经确定的表：

```sql
conversation_context_summary
```

它用于保存：

- 多次压缩历史
- 当前 active summary

### 10.2 ContextManager 的读取规则

在新架构下，不再把“`prepare` 读取摘要”当成默认前提。

改为：

```text
Turn Runtime 初始化时加载 summary 元数据
ContextManager 负责消费 active summary
```

也就是说：

- `Runtime Orchestrator` 不负责理解摘要内容
- `Turn Runtime` 负责把 active summary 纳入可见上下文
- `ContextManager` 负责决定它在 prompt 中的呈现方式

### 10.3 业务约束保持不变

仍然保持：

- 不使用数据库外键
- 由业务层保证引用完整性
- 同一个 conversation 同时只能有一条 `is_active = 1`

这些约束与新二层架构兼容，不需要改变。

---

## 11. 与 ToolRuntime 的关系

`ContextManager` 与 `ToolRuntime` 必须清晰解耦。

建议边界如下：

### 11.1 ToolRuntime 负责

- 允许哪些工具暴露给模型
- 真正执行工具
- 记录 tool_call / tool_result 审计
- 处理 tool timeout / failure

### 11.2 ContextManager 负责

- 工具结果如何进入模型可见上下文
- 是否裁短
- 是否参与 in-turn compaction

这能避免一个常见问题：

```text
工具执行逻辑和模型上下文逻辑搅在一起
```

---

## 12. 当前代码的过渡映射

在完全完成 runtime 二层改造之前，现有代码可以作为过渡映射理解：

- `agent_graph.py` 暂时近似扮演外层 orchestrator 壳
- `react_graph.py` 暂时近似扮演 turn runtime 的一部分

在这个过渡期里，`ContextManager` 可以先吸收这些旧逻辑：

- `_records_to_lc_messages`
- `_inject_selected_skills`
- `_lc_messages_to_llm`
- `_SYSTEM_PROMPT` 拼接
- `_trim_messages` 的替代逻辑

但要明确：

这只是迁移期映射，不是最终归属。

最终归属应为：

```text
Turn Runtime
  + ContextManager
```

而不是：

```text
MainGraph
  + 一半 ContextManager

ReactGraph
  + 另一半 ContextManager
```

---

## 13. 推荐实施顺序

### Phase 1：先做 runtime 二层改造

目标：

- 收敛 `Runtime Orchestrator / Turn Runtime` 边界
- 让 turn execution loop 有统一 owner

这一步完成前，不建议继续扩展复杂 context 规则。

### Phase 2：接入 ContextManager

目标：

- 把模型上下文组装收口到 Turn Runtime 内部
- 去掉旧的 `_trim_messages()`
- 接入 active summary
- 接入 tool output pruning

### Phase 3：扩展高级 context 能力

目标：

- token-aware compaction
- in-turn step compaction
- long-term memory injection
- 更稳定的 history / working / memory 分层

---

## 14. 第一版不做什么

在这份文档的范围内，第一版不做：

- 重新定义 Orchestrator 生命周期
- 多代理调度系统
- 完整 event sourcing
- 长期记忆引擎
- 多层摘要树
- 全自动摘要重写器

第一版只处理：

```text
Turn Runtime 内部的上下文 owner 收敛
```

---

## 15. 结论

基于新的 runtime 二层架构，`ContextManager` 不应再被设计成“主图 / 子图结构上的辅助 service”，而应被设计成：

```text
Turn Runtime 内部的上下文管理组件
```

因此这份设计的关键结论是：

1. `ContextManager` 的前置条件是先完成二层改造
2. `ContextManager` 的最终归属是 `Turn Runtime`
3. `ContextManager` 的主要职责是统一：
   - active summary
   - recent history
   - current turn working context
4. 旧的主图 / 子图映射只是一种迁移过渡，不应继续被当成目标架构

所以当前正确的实施顺序应当是：

```text
先做二层改造
再做 context 能力改造
```
