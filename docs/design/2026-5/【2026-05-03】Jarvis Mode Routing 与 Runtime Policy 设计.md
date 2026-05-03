# Jarvis Mode Routing 与 Runtime Policy 设计

## 1. 结论

Jarvis 需要引入一层轻量 `mode routing`，但不应该因此变成复杂多 agent swarm。

推荐终态是：

```text
Conversation / Session State
-> LLM Intent Classifier
-> turn_type + session_mode
-> RuntimePolicy
-> Context / Skills / Tools / Budget / Writeback
-> Turn Runtime
```

核心判断：

- `session_mode` 是 conversation 的长期会话模式。
- `turn_type` 是当前 turn 的执行类型。
- `RuntimePolicy` 是真正影响执行行为的配置层。
- deep research 第一阶段不是独立子 agent，而是 `research` mode 下的研究型执行策略。
- 未来可以支持 specialist subagent，但必须由 Jarvis supervisor 统一调度，不做自由群聊式 agent swarm。

## 2. 为什么需要 mode

如果没有 `mode / turn_type`，所有任务都会走同一套 ReAct loop，并暴露同一批工具、skills 和 context。

这会带来几个问题：

- 普通聊天可能看到过多高风险工具。
- deep research 没有阶段、证据、来源和报告结构。
- coding 任务和普通问答混用同一套工具策略，权限边界不清晰。
- long run 场景下 Jarvis 无法解释自己当前在继续研究、继续编码，还是普通对话。
- `/status` 无法展示 Jarvis 当前认知状态和下一步执行策略。

因此 `mode` 是必要的，但它不是复杂任务树。它第一阶段只是 runtime policy selector。

## 3. 概念边界

### 3.1 session_mode

`session_mode` 属于 conversation 级状态，表示 Jarvis 当前认为这个会话处在什么长期模式。

建议枚举：

```text
chat
research
coding
```

特点：

- 保存在 `ConversationSessionState`。
- 会跨 turn 延续。
- 用于“继续”“下一步”“展开”等短指令的上下文判断。
- 不应被每个 turn 随意覆盖。

示例：

```text
用户：帮我调研 Claude Code 和 Codex 的架构差异
=> session_mode = research

用户：继续
=> session_mode 仍是 research
=> 当前 turn 多半也是 research
```

### 3.2 turn_type

`turn_type` 属于 turn 级执行属性，表示当前这一轮应该按什么类型执行。

建议枚举：

```text
chat
research
coding
summary
command
image_generation
```

特点：

- 写入 `turns.turn_type`。
- 创建 turn 前后确定。
- 用于 runtime 选择 policy。
- 可以和 `session_mode` 不完全一致。

示例：

```text
当前 session_mode = research
用户：先别调研了，简单回答一下这个概念
=> turn_type = chat
=> session_mode 可保持 research
```

### 3.3 RuntimePolicy

`RuntimePolicy` 是由 `turn_type + session_mode + session_state` 映射出的执行策略。

它不应该由 `ContextManager` 或 `react_graph` 各自猜测，而应该成为明确对象。

建议结构：

```python
@dataclass(frozen=True)
class RuntimePolicy:
    mode: str
    allowed_tools: list[str]
    forced_skills: list[str]
    context_sections: list[str]
    max_steps: int
    search_budget: int | None
    writeback_strategy: str
```

## 4. LLM Intent Classifier

### 4.1 为什么不是纯规则

纯关键词规则对 Jarvis 不够好：

- 用户说法变化很大。
- research / chat / summary 边界经常依赖语义。
- 关键词容易误判，比如“研究一下这个 bug”可能是 coding。
- deep research 的触发常常是“帮我系统梳理”“对比几个方案”“给我一份报告”，不一定包含“研究”。

因此可以引入轻量 LLM classifier。

### 4.2 分类器职责

分类器只做调度，不回答用户。

输入：

```json
{
  "message": "用户当前消息",
  "session_mode": "research",
  "session_goal": "可选",
  "working_summary": "可选",
  "last_turn_status": "completed"
}
```

输出：

```json
{
  "turn_type": "research",
  "session_mode_update": "research",
  "confidence": 0.86,
  "reason": "user asks for multi-source comparison and architecture review"
}
```

约束：

- 输出固定 JSON。
- 不允许调用工具。
- 不允许生成用户可见回答。
- reason 必须短。
- 低置信度回退 `chat`。

### 4.3 硬规则优先

LLM classifier 不处理所有情况。硬规则仍然优先。

硬规则包括：

```text
/status -> command
/cancel -> command
/clear -> command
/research -> research
明显图片生成命令 -> image_generation
```

其余情况交给 LLM classifier。

推荐流程：

```text
if hard_command:
    return command/research/etc.

classification = llm_classify(...)

if classification.confidence < 0.65:
    return chat

return classification.turn_type
```

## 5. mode 对执行的影响

`mode / turn_type` 主要影响五件事：

- tools 注入
- skills 选择
- context/header 组装
- token budget / step budget
- finalize writeback 策略

### 5.1 chat policy

```text
allowed_tools:
  - tavily_search
  - obsidian_wiki_query
  - business_knowledge_search

context:
  - base system prompt
  - session state
  - recent messages

budget:
  - short/medium step budget

writeback:
  - last_turn_id
  - last_turn_status
  - last_assistant_summary
```

原则：

- 默认少工具。
- 优先直接回答。
- 需要事实或当前信息时再搜索。

### 5.2 research policy

```text
allowed_tools:
  - tavily_search
  - business_knowledge_search
  - obsidian_wiki_query
  - write_file

context:
  - base system prompt
  - session state
  - research protocol
  - research state
  - evidence digest
  - recent messages

budget:
  - larger max_steps
  - explicit search_budget

writeback:
  - last_turn_id
  - last_turn_status
  - last_assistant_summary
  - research_phase
  - source/evidence summary
  - open_questions
```

原则：

- 研究必须有来源意识。
- 结论应区分事实、观点和推断。
- 工具结果要进入 evidence ledger。
- 最终报告应能引用证据。

### 5.3 coding policy

```text
allowed_tools:
  - shell_inspect
  - delegate_to_claude_code
  - delegate_to_codex
  - shell_run_command

context:
  - base system prompt
  - session state
  - coding protocol
  - repo/task context
  - recent messages

budget:
  - medium/large step budget

writeback:
  - last_turn_id
  - last_turn_status
  - last_assistant_summary
  - coder artifacts
  - changed files summary
```

原则：

- 高权限工具必须经过 policy gate。
- Jarvis 是 supervisor，coder 是 worker tool。
- 不把 coding worker 设计成和用户平等对话的子 agent。

## 6. 普通 ReAct 与 Deep Research 的关系

当前 Jarvis 已经有普通 ReAct：

```text
LLM
-> tool calls
-> tool results
-> LLM
-> final answer
```

它适合：

- 普通问答
- 简单搜索
- 知识库查询
- 小范围总结
- 单步工具任务

Deep Research 不应该只是“普通 ReAct 多调用几次工具”。它需要研究状态：

```text
clarify
-> plan
-> collect
-> read
-> synthesize
-> verify
-> report
```

第一阶段不需要新建复杂任务树。建议做成 research-flavored ReAct：

```text
turn_type = research
-> load ResearchSessionState
-> inject research protocol
-> run current ReAct loop
-> extract/update sources and evidence
-> writeback research state
```

也就是说：

- ReAct loop 仍然是执行机制。
- Research state 是外层约束和记忆。
- Evidence ledger 是研究质量保障。

## 7. ResearchSessionState

第一版可以挂在 conversation metadata 下，或作为 `ConversationSessionState` 的扩展字段。

建议最小字段：

```python
@dataclass(frozen=True)
class ResearchSessionState:
    question: str | None = None
    phase: str | None = None
    plan: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    evidence_count: int = 0
    last_report_path: str | None = None
```

阶段建议：

```text
clarify
plan
collect
read
synthesize
verify
report
```

## 8. Evidence Ledger

Deep Research 的关键不是“多搜索”，而是“证据可追踪”。

最小表结构建议：

```text
research_sources
  id
  conversation_id
  turn_id
  title
  url
  source_type
  publisher
  published_at
  created_at

research_evidence
  id
  conversation_id
  turn_id
  source_id
  claim
  excerpt
  relevance
  confidence
  created_at
```

第一阶段也可以先不建表，把 source/evidence digest 写入 turn metadata 或 conversation metadata。但如果要支撑真正 deep research，最终应独立建表。

## 9. 未来 Specialist / Subagent 边界

Jarvis 终态可以支持多个 specialist，但不建议做自由群聊式多 agent。

推荐形态：

```text
Jarvis Supervisor
  - 对用户负责
  - 维护 session state
  - 做 routing
  - 合并结果

Specialist Runtimes / Tools
  - ResearchRuntime
  - CodingRuntime / Codex / ClaudeCode adapter
  - KnowledgeRuntime
  - ReportWriter
  - Verifier
```

拆 specialist 的条件：

- 工具集明显不同。
- 上下文需要隔离。
- 输入输出契约清晰。
- 可以并行。
- 权限边界不同。
- 失败需要隔离。

不满足这些条件时，不拆。

## 10. 实施顺序

建议按以下顺序实现：

1. 实现 `TurnClassification` 和轻量 LLM classifier。
2. 把 ingest 阶段的 `_turn_type(content)` 替换为 classifier 结果。
3. 引入 `RuntimePolicy`。
4. 让 `ContextManager` 按 policy 组装 header。
5. 让 `react_graph.build_llm_tools()` 支持 `allowed_tools`。
6. 增加 `research` policy，不改变普通 chat 的行为。
7. 增加 `ResearchSessionState` 和 `/status` 展示。
8. 增加 evidence ledger 的最小持久化。
9. 增加 `/research status`、`/research reset`、`/research report`。
10. 根据需要再拆 specialist runtime。

## 11. 非目标

第一阶段不做：

- 复杂任务树。
- 多 agent 自由对话。
- 自动无限 long run。
- 每轮自动覆盖 `working_summary`。
- 未经 policy gate 的高权限工具执行。

## 12. 当前代码落点

现有代码中与本设计相关的位置：

- `app/api/agent.py`：ingest、创建 turn、当前 `_turn_type(content)`。
- `app/agent_react/runtime.py`：turn runtime、session writeback。
- `app/agent_react/context_manager.py`：固定 context header、session state 注入。
- `app/agent_react/react_graph.py`：LLM/tool ReAct loop。
- `app/tools/runtime.py`：tool policy gate。
- `app/tools/definitions.py`：工具定义。
- `app/agent_react/session_state.py`：`ConversationSessionState`。

下一步最小改造应优先落在：

```text
app/agent_react/turn_classifier.py
app/agent_react/runtime_policy.py
app/api/agent.py
app/agent_react/context_manager.py
app/agent_react/react_graph.py
```

