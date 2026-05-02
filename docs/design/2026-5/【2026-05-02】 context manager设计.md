# Jarvis Context Manager 设计

| 项目 | 内容 |
|------|------|
| 作者 | Codex |
| 日期 | 2026-05-02 |
| 状态 | Draft |
| 相关代码 | `app/agent_react/agent_graph.py`, `app/agent_react/react_graph.py` |
| 相关设计 | `【2026-5-1】Jarvis 长运行多轮对话 ReAct Agent 设计.md`, `【2026-5-1】Jarvis Token 窗口管理与上下文压缩设计.md` |

---

## 1. 背景

当前 Jarvis 的 agent runtime 已经拆成两层：

```text
MainGraph / 主图:
  prepare -> react -> persist

ReactGraph / 子图:
  call_llm -> execute_tools -> call_llm
```

现有职责大致如下：

- 主图负责 turn 生命周期：加载 turn、加载历史、选择 skill、调用子图、持久化最终回复。
- 子图负责 ReAct 循环：调用 LLM、执行工具、记录 tool_call / tool_result、决定是否继续。

这个分层本身可以保留，但当前“模型上下文的组装与裁剪”被分散在两层里：

- 主图 `prepare` 加载历史并注入 skill。
- 子图 `call_llm` 再做 `_trim_messages()`、system prompt 拼接、工具列表构建。

结果是：

- 上下文没有唯一 owner。
- 压缩逻辑容易双重发生。
- `tool_call -> tool_result` 配对容易被 message 级裁剪破坏。
- 后续如果再加入 `active_summary`、memory、allowed_tools，会继续把职责打散。

因此需要引入 `ContextManager`，把“发给模型看的上下文”收敛到一个地方。

---

## 2. 设计目标

本设计的目标不是推翻现有主图/子图，而是收敛上下文职责：

1. 保留两层图结构，不继续拆第三层 graph。
2. 让 `ContextManager` 成为唯一的模型上下文 owner。
3. 子图不再自己做 `_trim_messages()` 这种 message 级硬裁剪。
4. 工具暴露从“默认全量工具”改成“按 runtime 配置选择性暴露”。
5. 工具执行、权限检查、审计继续独立，但不要和上下文管理混在一起。

---

## 3. 现有实现中的主要问题

### 3.1 上下文逻辑分散

当前代码中：

- `agent_graph.prepare()` 负责历史加载、消息转换、skill 注入。
- `react_graph.call_llm()` 负责 `_trim_messages()`、system prompt 拼接、工具构建。

这会造成一个结构性问题：

```text
主图决定一部分上下文
子图又重写一部分上下文
```

这意味着：

- 上下文预算没有唯一真相来源。
- 后续加入 token 压缩会出现“主图压一遍，子图再压一遍”。
- 主图即使做了 turn-aware 装配，子图也可能再按 message 截断，把结构切坏。

### 3.2 `_trim_messages()` 抽象层级不对

现有 `_trim_messages()` 的问题不是“参数不够好”，而是抽象层级错误：

- 它按 message 数量裁剪，不按 token。
- 它不理解完整 `tool_call -> tool_result` 结构。
- 它不区分“历史 turn”和“当前 turn 内 step”。
- 它只保第一条 `SystemMessage`，不适合后续多层 system / summary / memory 注入。

因此第一版里不应继续扩展 `_trim_messages()`，而应把它替换为 `ContextManager.render_for_model()`。

### 3.3 工具暴露策略不受控

当前子图直接调用：

```python
tools = build_llm_tools()
```

这意味着模型默认看到全量工具，后续很难做：

- 按风险等级收缩工具集合
- 按 turn type 收缩工具集合
- 按 channel / caller 做差异化能力暴露

因此工具“可见性”应由 runtime 配置决定，而不是由子图硬编码决定。

### 3.4 子图当前已经承担了部分持久化

当前子图不仅负责循环，还在执行过程中直接写：

- assistant tool-call message
- tool_call audit
- tool result message

这意味着主图 `persist` 已不是唯一状态收口点。这个现实约束需要被设计承认：

- `ContextManager` 只负责“模型可见上下文”。
- `persist` 只负责 final reply / turn 完成状态。
- 不要再假设“所有上下文状态都只在主图末尾统一推进”。

---

## 4. 核心设计

### 4.1 保留两层图，不新增第三层 graph

整体结构保持：

```text
MainGraph:
  prepare -> react -> persist

ReactGraph:
  call_llm -> execute_tools -> call_llm
```

但新增两个普通 service：

```text
ContextManager:
  负责模型上下文的组装、裁剪、压缩、渲染

ToolRuntime:
  负责工具暴露、权限检查、执行、审计、输出收敛
```

最终结构：

```text
MainGraph
  -> ContextManager.build_initial_state()
  -> ReactGraph
       -> ContextManager.render_for_model()
       -> ToolRuntime.execute()
  -> persist
```

这里有一个关键边界：

- `ContextManager` 管“给模型看什么”。
- `ToolRuntime` 管“工具怎么跑、怎么记”。

不要让两者互相吞职责。

---

## 5. ContextManager

### 5.1 设计原则

`ContextManager` 必须成为唯一的模型上下文 owner。

也就是说：

- 只有它能决定 system prompt 怎么拼。
- 只有它能决定 skill prompt 放哪。
- 只有它能决定历史怎么裁。
- 只有它能决定 tool result 是否要缩短后再进入模型。
- 子图不允许再私自 trim message。

### 5.2 职责

`ContextManager` 负责：

- 从持久化消息构建 LangChain message 序列。
- 注入 base system prompt、skill prompt、runtime policy。
- 根据 runtime 配置收缩历史。
- 保护 `tool_call -> tool_result` 成对结构。
- 必要时压缩过长的 tool output。
- 把 `BaseMessage` 渲染成 `LLMMessage`。

不负责：

- 工具执行
- DB 写入
- turn 成功/失败状态推进
- 长期记忆存储策略本身

### 5.3 推荐接口

```python
class ContextManager:
    def build_initial_messages(
        self,
        records: list[MessageRecord],
        selected_skills: list[str],
        runtime_config: RuntimeConfig,
    ) -> list[BaseMessage]:
        ...

    def render_for_model(
        self,
        messages: list[BaseMessage],
        runtime_config: RuntimeConfig,
    ) -> list[LLMMessage]:
        ...
```

如果后续需要支持“当前 turn 内 step-aware 压缩”，再补：

```python
    def compact_in_turn_messages(
        self,
        messages: list[BaseMessage],
        runtime_config: RuntimeConfig,
    ) -> list[BaseMessage]:
        ...
```

第一版可以先不实现第二个压缩器，只要把现有 `_trim_messages()` 干掉即可。

### 5.4 第一版该收进来的现有逻辑

第一版建议把这些逻辑移入 `ContextManager`：

- `_records_to_lc_messages`
- `_inject_selected_skills`
- `_lc_messages_to_llm`
- `_SYSTEM_PROMPT` 与工具规则拼接
- `_trim_messages` 的替代逻辑

注意：不是简单“搬函数”，而是重新定义职责。

### 5.5 第一版压缩策略

第一版不要一上来做复杂“双层压缩”。

先做一个务实版本：

1. 由主图 `prepare` 构造完整历史消息。
2. `ContextManager.render_for_model()` 在真正调用模型前做统一裁剪。
3. 裁剪优先级如下：

- 优先保留 system / skill / runtime policy 层
- 优先保留当前用户输入
- 优先保留当前 turn 内最近一组完整 `AI(tool_call) -> ToolMessage -> AI(final/next)` 片段
- 再保留最近历史消息
- 超长 tool output 优先缩短

这仍不是最终理想解，但已经比当前 `_trim_messages()` 健壮得多。

---

## 6. ToolRuntime

### 6.1 设计原则

这里的 `ToolRuntime` 更像一个 orchestration adapter，不应该重写整个 `app.tools.runtime`。

也就是说：

- 复用现有 `build_llm_tools`, `check_tool_policy`, `execute_tool`, `get_tool_definition`
- 把“当前 turn 允许暴露哪些工具”和“工具输出如何回填消息”这层收敛出来
- 不要平行造一个新的工具体系

### 6.2 职责

`ToolRuntime` 负责：

- 根据 `runtime_config.allowed_tools` 构建可见工具
- 执行前做最终 policy check
- 执行工具并处理 timeout / failure
- 写 tool_call 审计
- 写 tool_result 审计
- 必要时对返回模型的 tool output 做截断或摘要

### 6.3 推荐接口

```python
class ToolRuntime:
    def build_allowed_tools(
        self,
        runtime_config: RuntimeConfig,
    ) -> list[dict[str, Any]]:
        ...

    def execute_tool_call(
        self,
        tool_call: dict[str, Any],
        runtime_config: RuntimeConfig,
        messages: list[BaseMessage],
    ) -> ToolMessage:
        ...
```

注意：

- 审计写库仍然可以发生在子图里。
- 但“返回给模型的 tool 内容”是否原样、是否压缩，应由 `ToolRuntime` 或 `ContextManager` 有一方统一定义。
- 第一版建议：工具执行结果完整落库，回给模型的内容允许截短。

---

## 7. RuntimeConfig

### 7.1 设计目标

当前 runtime 缺少一个显式配置对象，导致：

- skills 是单独字段
- max_steps 是全局常量
- tools 是函数里直接构建
- model 来自 settings

这使得本轮执行能力边界不清晰。

因此建议增加一个轻量配置对象。

### 7.2 推荐字段

```python
class RuntimeConfig(TypedDict):
    selected_skills: list[str]
    allowed_tools: list[str]
    max_steps: int
    model: str
    risk_level: str
    token_budget: int | None
```

说明：

- `selected_skills`: 本轮选择的 skills
- `allowed_tools`: 本轮允许暴露给模型的工具名列表
- `max_steps`: ReAct 最大步数
- `model`: 本轮模型
- `risk_level`: 风险等级
- `token_budget`: 可选。第一版可以只做配置透传，不强依赖

### 7.3 第一版建议

第一版不建议直接上 dataclass + 多层嵌套配置。

先保持简单：

- `TypedDict` 或 dataclass 均可
- 字段控制在 5 到 8 个以内
- 只承载“本轮执行期配置”，不承载持久化业务状态

---

## 8. 上下文摘要持久化

### 8.1 为什么需要独立摘要表

如果系统只保留一份当前摘要，直接把字段挂在 `conversations` 上也能工作。

但 Jarvis 更可能出现一个 `conversation` 下的多次压缩：

- 历史消息逐步变长
- 多次触发压缩
- 每次生成一版新的 context summary
- runtime 只读取当前 active 摘要，但排障时需要回看旧摘要

在这个前提下，单独建摘要表比直接覆盖 `conversations.active_summary` 更合理。

第一版目标不是做复杂摘要树，而是支持：

1. 当前 active 摘要可快速读取
2. 旧摘要可保留历史
3. 后续可排查“哪次压缩覆盖了哪些消息”

### 8.2 表结构

```sql
CREATE TABLE conversation_context_summary (
    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '摘要ID',

    conversation_id BIGINT NOT NULL COMMENT '会话ID',

    compacted_from_message_id BIGINT NULL COMMENT '本次摘要覆盖的起始消息ID，NULL表示从会话开头开始',
    compacted_until_message_id BIGINT NOT NULL COMMENT '本次摘要覆盖的结束消息ID',

    summary_text MEDIUMTEXT NOT NULL COMMENT '摘要内容',

    is_active TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否为当前生效摘要',

    token_count INT DEFAULT NULL COMMENT '摘要token估算',
    source_message_count INT DEFAULT NULL COMMENT '被压缩的消息数量',

    model VARCHAR(128) DEFAULT NULL COMMENT '生成摘要使用的模型',
    prompt_version VARCHAR(64) DEFAULT NULL COMMENT '摘要prompt版本',
    format_version INT NOT NULL DEFAULT 1 COMMENT '摘要结构版本',

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',

    KEY idx_conversation_active (
        conversation_id,
        is_active
    ),

    KEY idx_conversation_range (
        conversation_id,
        compacted_from_message_id,
        compacted_until_message_id
    )
) COMMENT='会话历史上下文压缩摘要表';
```

字段语义：

- `conversation_id`: 该摘要属于哪个 conversation
- `compacted_from_message_id`: 本次摘要覆盖的起始 message
- `compacted_until_message_id`: 本次摘要覆盖的结束 message
- `summary_text`: 当前摘要文本
- `is_active`: 是否为 runtime 当前读取的摘要
- `token_count`: 摘要自身 token 估算
- `source_message_count`: 本次压缩覆盖了多少条消息
- `model`: 生成摘要所用模型
- `prompt_version`: 摘要 prompt 版本
- `format_version`: 摘要结构版本，不是“第几次压缩”

这里明确约定：

- 覆盖区间语义为闭区间 `[from, until]`
- `from = NULL` 表示“从会话开头直到 `until`”

### 8.3 为什么不做数据库外键

本设计明确采用：

```text
数据库层不建外键
引用完整性由业务逻辑保证
```

原因：

- 降低 schema 演化和迁移成本
- 避免 agent runtime 写路径被数据库 FK 约束卡住
- 历史/审计数据可以允许“软失联”但不影响主流程
- 后续批量修复、重建摘要、回放时更灵活

因此这里不在数据库层声明：

- `conversation_id -> conversations.id`
- `compacted_from_message_id -> messages.id`
- `compacted_until_message_id -> messages.id`

但这不代表不要约束，而是把约束移到 repository / service 层。

### 8.4 业务层必须保证的约束

由于不使用数据库外键，业务层必须显式检查：

1. `conversation_id` 指向存在的 conversation
2. `compacted_from_message_id` 和 `compacted_until_message_id` 如果存在，必须属于同一个 conversation
3. `compacted_from_message_id <= compacted_until_message_id`
4. 同一个 conversation 任意时刻只能有一条 `is_active = 1`
5. 新摘要覆盖范围不能出现非法倒退或非法重叠

其中第 4 条尤其重要：

- 当前表结构只有普通索引，没有数据库层唯一约束
- 因此“一个 conversation 只能有一个 active 摘要”必须由事务保证

推荐更新流程：

```text
事务开始
  1. 查询当前 active 摘要
  2. 校验新摘要覆盖范围是否合法
  3. 将旧 active 摘要更新为 inactive
  4. 插入新摘要，并标记 is_active = 1
事务提交
```

### 8.5 Runtime 如何读取

`prepare` 或 `ContextManager.build_initial_messages()` 不需要扫描所有摘要历史。

第一版读取规则应保持简单：

1. 查 `conversation_id = ? AND is_active = 1`
2. 如果存在，则把这条摘要作为当前 active summary
3. 再加载 `compacted_until_message_id` 之后的消息

也就是说：

- runtime 永远只读 active 摘要
- 历史摘要只用于排障、审计、后续重建

这可以避免“多次压缩”直接把 prompt 装配逻辑复杂化。

---

## 9. 图内职责调整

### 9.1 MainGraph

`MainGraph` 继续负责 turn 生命周期，不直接写压缩细节。

推荐职责：

```text
prepare
  - load turn
  - load raw message records
  - select skills
  - build runtime_config
  - context_manager.build_initial_messages()

react
  - invoke ReactGraph

persist
  - 提取最终 assistant reply
  - finalize_turn_success / finalize_turn_failure
```

是否新增 `finalize_reply` 节点，建议作为第二步，而不是第一版硬要求。

原因：

- 当前 `persist` 已经兼顾 reply 提取和 turn 完成
- 第一版重点是收敛上下文 owner，不是继续扩节点

如果后续确实要单独做：

- 空回复兜底
- max_steps 强制总结校验
- safety post-check

再新增 `finalize_reply` 更合理。

### 9.2 ReactGraph

`ReactGraph` 继续只负责 ReAct loop：

```text
call_llm
  - context_manager.render_for_model()
  - client.chat()

execute_tools
  - tool_runtime.build_allowed_tools()
  - tool_runtime.execute_tool_call()
  - append ToolMessage

should_continue
  - 有 tool_calls -> execute_tools
  - 无 tool_calls -> END
  - step_count 超限 -> END
```

明确要求：

- 不再保留 `_trim_messages()`
- 不再在子图里私自拼 system prompt
- 不再默认全量工具暴露

---

## 10. 推荐改造步骤

### Step 1：抽出 ContextManager

第一步先只做收敛，不做复杂压缩：

- 把 `_records_to_lc_messages` 移出主图
- 把 skill 注入移入 `ContextManager`
- 把 `_lc_messages_to_llm` 移出子图
- 把 `_SYSTEM_PROMPT` 拼接逻辑移入 `ContextManager`
- 删除 `_trim_messages()`，改为 `render_for_model()` 统一处理

这是收益最大的一步。

### Step 2：引入 RuntimeConfig

在主图 `prepare` 里生成：

```python
runtime_config = {
    "selected_skills": skill_names,
    "allowed_tools": [...],
    "max_steps": 8,
    "model": settings.deepseek_model,
    "risk_level": "normal",
    "token_budget": None,
}
```

然后透传给子图。

### Step 3：工具暴露改为 allowed_tools

把：

```python
tools = build_llm_tools()
```

改成：

```python
tools = tool_runtime.build_allowed_tools(runtime_config)
```

第一版哪怕只是“全量工具中过滤一层名字”，也值得做。

### Step 4：整理 ToolRuntime 边界

把这些逻辑从 `react_graph.py` 中抽离：

- allowed tools 选择
- tool output 回给模型前的裁短策略
- 与 tool policy 相关的统一入口

保留当前 tool_call / tool_result 的写库时机，不强行改成“统一末尾写库”。

### Step 5：视需要再评估 `finalize_reply`

如果后续出现这些需求，再新增节点：

- reply 空字符串兜底
- max_steps 时输出格式校验
- 最终回复安全检查

否则第一版不必多拆一个 graph node。

---

## 11. 第一版不做什么

第一版明确不做：

- 三层 graph
- AgentRunGraph
- 复杂 memory system
- 主图压缩器 + 子图压缩器 同时完整落地
- reviewer / repair loop
- 多级 planner / verifier
- 自动 skill 学习

这些都属于后续演进项，不应阻塞上下文 owner 的收敛。

---

## 12. 风险与注意事项

### 12.1 不要把 ContextManager 设计成“大杂烩”

如果把这些都塞进去：

- DB 读取
- LLM 调用
- 压缩
- summary 落库
- memory 检索

那它会变成新的 God object。

第一版应把它限制在：

- message 构建
- prompt 注入
- model-facing 裁剪
- message -> LLMMessage 渲染

### 12.2 ToolRuntime 不要平行重写现有工具层

当前 `app.tools.runtime` 已有不少可复用能力。

新的 `ToolRuntime` 应该只是 orchestration adapter，而不是另一套工具执行框架。

### 12.3 先解决“唯一 owner”，再谈“高级压缩”

如果在上下文 owner 还分散的情况下直接做：

- active_summary
- token budget
- in-turn step compaction

只会把复杂度叠加在错误的边界上。

第一步应先做到：

```text
所有发给模型的上下文，都只能经过 ContextManager
```

---

## 13. 结论

这份设计总体方向是合理的，尤其是这两个判断是对的：

1. 不需要继续拆第三层 graph。
2. 上下文逻辑必须从主图/子图中抽离，收敛成单独 owner。

但需要修正三点：

1. `ContextManager` 必须是唯一的 model-context owner，而不是“又一个辅助类”。
2. `ToolRuntime` 应该是对现有工具层的收敛与包装，不应平行重造。
3. `finalize_reply` 不应作为第一版的强制节点，可以作为后续增强。

因此第一版推荐落地目标是：

```text
两层图
+ ContextManager
+ RuntimeConfig
+ ToolRuntime（轻量包装）
- 子图中的 _trim_messages
- 子图中的默认全量工具暴露
```

这条路线能在不推翻现有架构的前提下，先把最危险的上下文职责分裂问题收住。
