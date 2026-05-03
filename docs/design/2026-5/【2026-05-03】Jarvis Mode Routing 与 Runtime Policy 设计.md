# Jarvis Mode Routing 与 Runtime Policy 设计

## 1. 结论

Jarvis 需要引入一层轻量 `mode routing`，但不应该因此变成复杂多 agent swarm。

推荐终态是：

```text
Conversation / Session State
-> DeepSeek V4 Flash Intent Router
-> turn_type + session_mode_update + target_resources + requested_capabilities
-> RuntimePolicy
-> Context / Skills / Tools / Budget / Writeback
-> Turn Runtime
```

核心判断：

- `session_mode` 是 conversation 的长期会话模式。
- `turn_type` 是当前 turn 的执行类型。
- 意图识别第一阶段应使用 DeepSeek V4 Flash 做轻量 LLM router，而不是继续堆关键词规则。
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

`RuntimePolicy` 是由 `TurnClassification + ConversationSessionState` 映射出的执行策略。

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

`RuntimePolicy` 的输入不应只剩 `session_mode + turn_type`。新版输入应是结构化路由结果：

```python
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class TargetResource:
    type: Literal["repository", "knowledge_base", "conversation", "external_service"]
    id: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class TurnClassification:
    turn_type: str
    session_mode_update: str | None = None
    target_resources: list[TargetResource] = field(default_factory=list)
    requested_capabilities: list[str] = field(default_factory=list)
    routing_basis: Literal["explicit", "contextual", "inferred", "fallback"] = "fallback"
    model_confidence: float | None = None
    reason: str = ""
    source: str = "llm_router"
```

这样 `turn_type` 不再承担所有语义。`turn_type=chat` 也可以因为 `requested_capabilities=["web.search"]` 获得搜索工具，`session_mode=coding` 也不会强行污染本轮工具集合。

关键原则：

- Router 输出用户目标需要的能力标签，不输出具体工具名。
- Router 不直接说“需要 tavily/codex”，只说 `web.search`、`code.inspect` 这类能力。
- `model_confidence` 只用于日志和调试，不参与权限判断。
- 真正的工具、权限、预算由 `RuntimePolicy` 做确定性映射。

## 4. DeepSeek V4 Flash Intent Router

### 4.1 为什么不是纯规则

纯关键词规则对 Jarvis 不够好：

- 用户说法变化很大。
- research / chat / summary 边界经常依赖语义。
- 关键词容易误判，比如“研究一下这个 bug”可能是 coding。
- deep research 的触发常常是“帮我系统梳理”“对比几个方案”“给我一份报告”，不一定包含“研究”。
- 代码任务后的话题切换容易被历史 mode 污染，例如“先不看项目了，查查最新金价”不应该继续走 coding policy。

因此 `turn_classifier` 不应继续扩展关键词表。关键词规则只保留命令、安全边界和失败兜底，其余非命令消息交给轻量 LLM router。

推荐模型：

```text
deepseek-v4-flash
```

选择理由：

- 任务只需要短 JSON 分类，不需要强推理。
- 成本低，适合每个用户消息都调用。
- 中文语义、话题切换、上下文 mode 判断比关键词稳定。
- 延迟可控，可以放在 conversation ingest 阶段。

### 4.2 分类器职责

分类器本质上是 router，只做调度，不回答用户。

输入：

```json
{
  "message": "用户当前消息",
  "session_mode": "research",
  "session_goal": "可选",
  "working_summary": "可选",
  "active_repo_id": "nltk",
  "registered_repositories": [
    {"repo_id": "jarvis", "name": "Jarvis"},
    {"repo_id": "nltk", "name": "NLTK Smoke Repo"}
  ],
  "last_turn_type": "coding",
  "last_turn_status": "completed",
  "recent_user_intent_digest": "上一轮在 review nltk 项目"
}
```

输出：

```json
{
  "turn_type": "chat",
  "session_mode_update": "chat",
  "target_resources": [],
  "requested_capabilities": ["web.search"],
  "routing_basis": "explicit",
  "model_confidence": 0.92,
  "reason": "user switches away from code and asks for current market information"
}
```

约束：

- 输出固定 JSON。
- 不允许调用工具。
- 不允许生成用户可见回答。
- reason 必须短。
- `requested_capabilities` 必须使用能力标签，不允许输出工具名。
- `target_resources` 只放用户明确提到或上下文强相关的资源。
- `routing_basis` 比 `model_confidence` 更重要，policy 不应依赖模型自评置信度。
- 输出必须短，禁止长链路推理。
- temperature 建议为 0 或接近 0。
- max output tokens 建议控制在 200 以内。
- Router 失败、JSON 无效或能力标签非法时回退 `chat`，但不要继承高风险工具。

### 4.3 Router Prompt 约束

Router prompt 应直接说明 Jarvis 的能力边界：

```text
You are Jarvis' routing model. Do not answer the user.
Return compact JSON only.

Decide the current turn execution type and capability needs.
Prefer topic switch when the user explicitly says they stop/leave the previous task.
Return capabilities as labels, not tool names.
Use code.inspect/code.edit/code.test only when the user asks to inspect, modify, test, commit, review, or reason about a registered code repository.
Use web.search when the user asks for latest/current/recent/time-sensitive facts.
Use target_resources only when the message clearly refers to a registered repository, knowledge base, conversation, or external service.
```

关键点：

- 不让 router 生成正文。
- 不让 router 做复杂计划。
- 不把“项目”“研究”这类词本身当作决定性信号。
- 允许 router 识别“离开当前 coding/research 上下文”的话题切换。
- 不让 router 选择具体工具。工具选择属于 `RuntimePolicy`。

### 4.4 硬规则优先，但范围必须收窄

LLM router 不处理所有情况。硬规则仍然优先，但只覆盖确定性强的情况。

硬规则包括：

```text
/status -> command
/cancel -> command
/clear -> command
/research -> research
/coding -> coding
/chat -> chat
显式 repo id 且明确 code action -> coding + target_resources=[repository:<repo_id>]
```

不建议继续维护大规模中文关键词分类表。如下规则应迁移到 LLM router：

```text
调研 / 查一下 / 最新 / 项目 / review / 总结 / 继续
```

原因是这些词都需要上下文 disambiguation。

### 4.5 推荐流程

推荐流程：

```text
if hard_command:
    return command/research/etc.

classification = deepseek_v4_flash_route(...)

if classification.invalid_json:
    return safe_chat_fallback

if classification.has_unknown_capability:
    return safe_chat_fallback

if classification.routing_basis == "inferred" and classification.requests_high_risk_capability:
    return safe_chat_fallback

return classification
```

`safe_chat_fallback` 的含义：

- `turn_type = chat`
- `session_mode_update = null`
- `target_resources = []`
- `requested_capabilities = []`
- `routing_basis = fallback`
- 不注入 coder / shell 等高权限工具
- 可以注入低风险知识库工具，是否注入 web search 由后续策略决定

## 5. mode 对执行的影响

`mode / turn_type / requested_capabilities / target_resources` 主要影响五件事：

- tools 注入
- skills 选择
- context/header 组装
- token budget / step budget
- finalize writeback 策略

### 5.1 chat policy

```text
allowed_tools:
  - obsidian_wiki_query
  - business_knowledge_search
  - tavily_search  # only when requested_capabilities contains web.search

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
- 如果 router 输出 `requested_capabilities=["web.search"]`，即使当前 session 原本是 coding，也必须允许 `tavily_search`，因为当前 turn 已经切出 coding。

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
  - delegate_to_codex

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
- 第一阶段 code 任务只暴露 `delegate_to_codex`，不要把 shell 作为常规 coding turn 工具暴露给主模型。
- shell 更适合 Jarvis 自身维护、诊断、测试当前服务，不适合作为飞书用户 code 任务的默认工具。

### 5.4 能力标签与 policy 关系

`turn_type` 负责决定当前 turn 的主执行形态，`requested_capabilities` 负责表达本轮需要哪些能力，`target_resources` 负责表达这些能力作用在哪些资源上。

建议第一版能力标签：

```text
web.search
kb.read
kb.write
code.inspect
code.edit
code.test
research.deep
image.generate
```

标签含义：

- `web.search`：需要外部实时或公开网络信息。
- `kb.read`：需要读取个人/业务知识库。
- `kb.write`：需要写入个人/业务知识库。
- `code.inspect`：需要查看、review、理解仓库代码。
- `code.edit`：需要修改仓库代码。
- `code.test`：需要运行或分析测试。
- `research.deep`：需要研究协议、来源意识、证据整理。
- `image.generate`：需要图片生成。

示例：

```json
{
  "turn_type": "chat",
  "session_mode_update": "chat",
  "target_resources": [],
  "requested_capabilities": ["web.search"],
  "routing_basis": "explicit"
}
```

对应策略：

```text
RuntimePolicy.mode = chat
allowed_tools = [tavily_search, obsidian_wiki_query, business_knowledge_search]
context_sections = [base, session_state, recent_messages]
```

再如：

```json
{
  "turn_type": "coding",
  "session_mode_update": "coding",
  "target_resources": [
    {"type": "repository", "id": "nltk"}
  ],
  "requested_capabilities": ["code.inspect"],
  "routing_basis": "explicit"
}
```

对应策略：

```text
RuntimePolicy.mode = coding
allowed_tools = [delegate_to_codex]
context_sections = [base, session_state, coding_protocol, repo_context, recent_messages]
```

这能避免一个常见错误：只因为 `session_mode=coding`，就让下一轮“查最新国际金价”继续拿不到搜索工具。

混合任务示例：

```json
{
  "turn_type": "coding",
  "session_mode_update": "coding",
  "target_resources": [
    {"type": "repository", "id": "nltk"}
  ],
  "requested_capabilities": ["code.inspect", "web.search"],
  "routing_basis": "explicit",
  "reason": "Review repository and compare with latest release notes"
}
```

对应策略：

```text
RuntimePolicy.mode = coding
allowed_tools = [delegate_to_codex, tavily_search]
context_sections = [base, session_state, coding_protocol, repo_context, search_note, recent_messages]
```

注意：这是给 ReAct loop 暴露工具，不是写死“先 search 再 codex”。Loop 在 policy 允许的工具集合内自主决定调用顺序。

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

1. 扩展 `TurnClassification`，新增 `requested_capabilities`、`target_resources`、`routing_basis`。
2. 引入 DeepSeek V4 Flash router，替换非命令 turn 的关键词分类。
3. 收窄硬规则，只保留 slash command、安全 gate、显式 repo code action。
4. 把 ingest 阶段的 `_turn_type(content)` 替换为 router 结果。
5. `RuntimePolicy` 改为接收 `classification + session_state`，不再只看 `turn_type + session_mode`。
6. 让 `ContextManager` 按 policy 组装 header。
7. 让 `react_graph.build_llm_tools()` 支持 `allowed_tools`。
8. 增加 classification/policy 日志，至少记录 `turn_type`、`session_mode_update`、`target_resources`、`requested_capabilities`、`routing_basis`、`allowed_tools`。
9. 增加 `research` policy，不改变普通 chat 的行为。
10. 增加 `ResearchSessionState` 和 `/status` 展示。
11. 增加 evidence ledger 的最小持久化。
12. 增加 `/research status`、`/research reset`、`/research report`。
13. 根据需要再拆 specialist runtime。

第一批验收测试应覆盖：

- `review 下 nltk 项目的代码` -> `turn_type=coding`、`requested_capabilities=["code.inspect"]`、`target_resources=[repository:nltk]`、只暴露 `delegate_to_codex`。
- `不看项目了，查查最新国际金价` -> `turn_type=chat`、`session_mode_update=chat`、`requested_capabilities=["web.search"]`、暴露 `tavily_search`。
- `review nltk 并结合最新 release note 看兼容风险` -> `requested_capabilities=["code.inspect","web.search"]`、暴露 `delegate_to_codex` 和 `tavily_search`。
- `继续` 在 research session 中 -> 可以继承 `session_mode=research`，但仍由 router 判断是否需要 research protocol。
- `/status` -> hard rule command，不调用 router。
- router 失败或 JSON 无效 -> safe chat fallback，不暴露 coder/shell。

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
- `app/agent_react/runtime_policy.py`：由 classification/session state 生成 allowed tools、context sections、budget。
- `app/tools/runtime.py`：tool policy gate。
- `app/tools/definitions.py`：工具定义。
- `app/agent_react/session_state.py`：`ConversationSessionState`。
- `app/agent_react/turn_classifier.py`：改造成 DeepSeek V4 Flash intent router，规则只做 hard command 和安全兜底。

下一步最小改造应优先落在：

```text
app/agent_react/turn_classifier.py
app/agent_react/runtime_policy.py
app/api/agent.py
app/agent_react/context_manager.py
app/agent_react/react_graph.py
```

其中 `turn_classifier.py` 的改造优先级最高。当前线上暴露出来的问题不是 `tavily_search` 未注册，而是分类层把话题切换误判成 coding，导致 policy 没有给搜索工具。继续修补关键词会让系统越来越不可预测，应该尽快切到 LLM router。

