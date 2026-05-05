# Jarvis 多模型切换与 Agent Loop Provider Adapter 设计

日期：2026-05-04

## 背景

希望支持通过 `/model` 在会话中切换模型。用户输入 `/model` 后，Jarvis 弹出模型选择卡片，用户点击模型后，后续对话使用所选模型。

但这不是简单把 `settings.deepseek_model` 改成一个可变字符串。当前 Jarvis 的 agent loop、意图识别和 token usage 展示都隐含假设模型接口与 DeepSeek/OpenAI-compatible 返回结构一致。实际使用 Kimi、DeepSeek V4、Gemini 等模型时，返回字段、工具调用格式、reasoning 字段、JSON mode 支持和 usage 字段都可能不同。

因此，多模型切换应先做 LLM provider adapter，再做 `/model` UI。

## 当前现状

### Agent loop

当前关键链路：

```text
react_graph.call_llm
  -> ChatClient.chat
  -> raw provider response
  -> _llm_response_to_ai_message
  -> LangChain AIMessage
  -> execute_tools
```

当前主要假设：

1. 所有 provider 都支持 `/chat/completions`。
2. 返回结构都有 `choices[0].message`。
3. tool calls 都是 OpenAI 标准格式：
   - `tool_calls[].id`
   - `tool_calls[].type == "function"`
   - `tool_calls[].function.name`
   - `tool_calls[].function.arguments` 是 JSON string
4. `usage` 字段可直接从 body 上读取。
5. `reasoning_content` 可以从 assistant message 上读取并在后续请求中回传。
6. `response_format={"type": "json_object"}` 可用于 classifier。

这些假设对 DeepSeek 当前接入基本可用，但不应扩散到 Kimi 或其他 provider。

### 意图识别

当前 `turn_classifier._llm_classification` 直接构造 `ChatClient`：

```text
api_key=settings.deepseek_api_key
base_url=settings.deepseek_base_url
model=settings.deepseek_model
response_format={"type": "json_object"}
timeout=min(llm_timeout_seconds, 10)
```

这说明 classifier 当前强绑定 DeepSeek。多模型切换后，classifier 也需要走统一模型解析，但 timeout 和 JSON mode 能力应按节点单独处理。

### 模型展示

当前 token usage footer 已经会展示模型名，例如：

```text
---
- 模型：`deepseek-v4-flash`
- Token：输入 `6893` / 输出 `568` / 合计 `7461`
```

这要求底层 response 能稳定提供 normalized model name 和 normalized token usage。

## 问题

Kimi 与 DeepSeek V4 的返回值并不完全一致。若直接支持 `/model` 切换，agent loop 会出现以下风险：

1. tool call 解析失败，导致模型想调用工具但 Jarvis 看不到。
2. tool call arguments 不是 JSON string 时被解析成 `{}`。
3. provider 不接受 `reasoning_content` 回传，导致下一轮 LLM 请求失败。
4. provider 不支持或不稳定支持 `response_format=json_object`，导致 classifier 失败。
5. usage 字段结构不同，token footer 不准确或缺失。
6. finish reason、content 空值、tool call 空 content 等边界行为不同。

这些都是 agent loop 级别的问题，不是 `/model` 命令本身能解决的。

## 设计目标

1. 支持 conversation 级模型切换。
2. agent loop 不直接依赖某个 provider 的 raw response。
3. classifier、agent step 等 LLM 节点都通过统一 LLM 初始化入口创建 client。
4. provider 差异收口在 adapter 中。
5. `/model` 只切换经过能力校验的 `ModelProfile`。
6. 保留后续节点级模型配置空间，例如 agent step 用强模型，classifier 用快模型，planner 用更强的规划模型。
7. 明确区分 LLM provider 和 Turn loop provider：LLM provider 负责“怎么调用模型”，Turn loop provider 负责“怎么推进一个 turn”。
8. 为后续核心 ReAct loop 切换、plan 节点切换预留稳定接口，但第一版不实现多个 loop。

## 非目标

第一版不做：

1. 每个用户独立的全局模型偏好。
2. 多模型自动 fallback。
3. 成本路由或智能模型选择。
4. 自动 benchmark。
5. 模型市场或管理后台。
6. 多个核心 turn loop 并存运行。
7. 完整 plan-execute runtime。

这些可以等 provider adapter 稳定后再做。

## 核心设计

### ModelProfile

新增模型 profile 概念：

```python
@dataclass(frozen=True)
class ModelProfile:
    id: str
    label: str
    provider: str
    model: str
    base_url: str
    api_key: str | None
    supports_tools: bool
    supports_json_object: bool
    supports_reasoning_content: bool
    supports_usage: bool
```

示例：

```json
{
  "id": "deepseek-v4-flash",
  "label": "DeepSeek V4 Flash",
  "provider": "deepseek",
  "model": "deepseek-v4-flash",
  "supports_tools": true,
  "supports_json_object": true,
  "supports_reasoning_content": true,
  "supports_usage": true
}
```

```json
{
  "id": "kimi-k2",
  "label": "Kimi K2",
  "provider": "kimi",
  "model": "kimi-k2",
  "supports_tools": true,
  "supports_json_object": true,
  "supports_reasoning_content": false,
  "supports_usage": true
}
```

### LLMNode

不同 LLM 节点有不同能力要求和 timeout：

```python
class LLMNode(str, Enum):
    AGENT_STEP = "agent_step"
    INTENT_CLASSIFIER = "intent_classifier"
    PLANNER = "planner"
    PLAN_REPAIR = "plan_repair"
    VERIFIER = "verifier"
    CONTEXT_COMPRESSION = "context_compression"
    SUMMARY = "summary"
```

第一版只接入：

1. `agent_step`
2. `intent_classifier`

`planner`、`plan_repair`、`verifier` 第一版只作为配置和接口预留，不在默认 runtime 中启用。

命名上不建议继续使用 `agent_loop` 表达 LLM 节点。`agent_loop` 容易和核心执行 loop 混淆。实际一次模型调用更准确地叫 `agent_step`：

```text
Turn loop = 负责推进 turn 的执行策略
agent_step = Turn loop 内部的一次 LLM 决策节点
```

### NodePolicy

每个 LLM 节点应有独立 policy，而不是只靠 profile 自己声明能力。

```python
@dataclass(frozen=True)
class LLMNodePolicy:
    node: LLMNode
    requires_tools: bool = False
    prefers_json_object: bool = False
    allows_reasoning_content: bool = False
    timeout_seconds: float | None = None
    fallback_profile_id: str | None = None
```

建议第一版内置：

```python
NODE_POLICIES = {
    LLMNode.AGENT_STEP: LLMNodePolicy(
        node=LLMNode.AGENT_STEP,
        requires_tools=True,
        allows_reasoning_content=True,
        timeout_seconds=settings.llm_timeout_seconds,
    ),
    LLMNode.INTENT_CLASSIFIER: LLMNodePolicy(
        node=LLMNode.INTENT_CLASSIFIER,
        prefers_json_object=True,
        timeout_seconds=min(settings.llm_timeout_seconds, 10),
        fallback_profile_id=settings.default_classifier_profile,
    ),
    LLMNode.PLANNER: LLMNodePolicy(
        node=LLMNode.PLANNER,
        prefers_json_object=True,
        timeout_seconds=settings.llm_timeout_seconds,
    ),
}
```

`ModelRouter` resolve 出 profile 后，必须用 `NodePolicy` 做能力校验：

1. `requires_tools=true` 时，profile 必须 `supports_tools=true`。
2. `prefers_json_object=true` 且 profile 不支持 JSON mode 时，request builder 不传 `response_format`，但 parser 必须走容错 JSON 提取。
3. `fallback_profile_id` 存在时，节点可回退到稳定模型；没有 fallback 时 fail closed。
4. `allows_reasoning_content=false` 或 profile 不支持 reasoning 时，请求中必须剥离 reasoning 字段。

### ModelRouter

统一解析当前节点应该使用哪个模型：

```text
node override
  -> conversation active_model_profile
  -> settings default_model_profile
  -> hardcoded fallback
```

conversation metadata 第一版：

```json
{
  "active_model_profile": "deepseek-v4-flash"
}
```

后续扩展：

```json
{
  "active_model_profile": "deepseek-v4-flash",
  "model_overrides": {
    "agent_step": "kimi-k2",
    "intent_classifier": "deepseek-v4-flash",
    "planner": "deepseek-v4-pro",
    "plan_repair": "deepseek-v4-pro",
    "verifier": "gpt-5.5"
  }
}
```

router 的返回值不应只是 client，还应包含本次调用实际使用的 profile 和 node policy，方便 footer、审计、测试断言：

```python
@dataclass(frozen=True)
class ResolvedLLM:
    node: LLMNode
    profile: ModelProfile
    policy: LLMNodePolicy
    client: LLMClient
```

### TurnLoopProvider

LLM provider 和核心执行 loop 必须分开。

```python
class TurnLoopProvider(str, Enum):
    REACT = "react"
    PLAN_EXECUTE = "plan_execute"
    RESEARCH = "research"
    CODING_REVIEW = "coding_review"
```

第一版只实现：

```text
react
```

但 `TurnRuntime` 的调用边界应预留成：

```python
class TurnLoop(Protocol):
    def invoke(self, request: TurnLoopRequest) -> TurnLoopResult:
        ...
```

`ReactLoop` 内部继续做当前逻辑：

```text
render context
-> call LLMNode.AGENT_STEP
-> execute tool calls
-> append tool results
-> continue or finalize
```

后续如果引入 plan-execute，不应修改 LLM provider adapter，而是新增一个 loop：

```text
PlanExecuteLoop
  -> call LLMNode.PLANNER
  -> execute planned step
  -> call LLMNode.VERIFIER or PLAN_REPAIR
  -> continue or finalize
```

### RuntimeProfile

conversation 级别可以同时记录默认模型和 loop provider：

```json
{
  "active_model_profile": "deepseek-v4-flash",
  "runtime_profile": {
    "loop_provider": "react",
    "model_overrides": {
      "agent_step": "kimi-k2",
      "planner": "deepseek-v4-pro"
    }
  }
}
```

第一版可以先不暴露 `/loop` 命令，也不允许用户切换 loop。只在 metadata 和代码接口上保留字段：

```text
missing loop_provider -> react
unknown loop_provider -> fail closed or fallback react with warning
unsupported loop_provider -> fail closed
```

### ProviderAdapter

新增 provider adapter，把 raw response 统一转成内部结构：

```python
@dataclass(frozen=True)
class NormalizedToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class NormalizedLLMResponse:
    content: str
    tool_calls: tuple[NormalizedToolCall, ...]
    reasoning_content: str | None
    usage: TokenUsage | None
    model: str
    finish_reason: str | None
    raw: dict[str, Any]
```

adapter 职责：

1. 解析 raw response。
2. 标准化 tool calls。
3. 标准化 usage。
4. 识别 provider-specific reasoning 字段。
5. 按 profile 能力构造 provider-compatible request。
6. 按 node policy 决定是否传 `response_format`、tools、tool_choice、reasoning_content。

agent step、planner、classifier 等 LLM 节点只消费 `NormalizedLLMResponse`，不再直接读 provider raw 字段。

注意：`ProviderAdapter` 不负责选择模型，也不负责决定 turn loop。模型选择属于 `ModelRouter`，turn 推进属于 `TurnLoopProvider`。

## Provider 差异点

### Tool Calls

内部统一成：

```json
{
  "id": "call_xxx",
  "name": "delegate_to_codex",
  "args": {
    "instruction": "..."
  }
}
```

adapter 需要处理：

1. `function.arguments` 是 JSON string。
2. `function.arguments` 已经是 dict。
3. `function.arguments` 是空字符串。
4. `tool_calls` 缺少 id 时生成稳定 fallback id。
5. provider 返回非标准 tool call 字段时做兼容。

### Reasoning Content

DeepSeek thinking mode 可能返回 `reasoning_content`，并要求后续请求带回。

Kimi 不应默认接收 DeepSeek 的 `reasoning_content` 字段。否则可能请求失败。

因此：

```text
supports_reasoning_content=true  -> request builder 可以带 reasoning_content
supports_reasoning_content=false -> request builder 必须剥离 reasoning_content
```

### JSON Mode

classifier 目前依赖：

```python
response_format={"type": "json_object"}
```

但不同 provider 对 JSON mode 的支持和严格性不同。

策略：

1. profile 支持 `supports_json_object` 时，传 `response_format`。
2. 不支持时，不传 `response_format`，但 prompt 中继续要求 compact JSON only。
3. `parse_json_content` 保持“提取第一个 JSON object”的容错。

### Token Usage

不同 provider 可能使用：

1. `prompt_tokens`
2. `completion_tokens`
3. `total_tokens`
4. `input_tokens`
5. `output_tokens`

adapter 输出统一 `TokenUsage | None`。没有可靠 usage 时返回 None，不强行估算。

### Finish Reason

保留 `finish_reason`，后续可用于判断：

1. tool_calls
2. length
3. stop
4. content_filter

第一版只记录，不改变行为。

## ChatClient 改造

当前 `ChatClient.chat()` 返回 dict。建议拆成两层：

```text
ProviderChatClient
  -> 负责 HTTP request/response

ProviderAdapter
  -> 负责 request build + response normalize

ModelRouter
  -> 负责选择 profile + adapter
```

接口可以是：

```python
class LLMClient:
    def chat(
        self,
        messages: list[LLMMessage],
        *,
        node: LLMNode,
        response_format: dict[str, str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> NormalizedLLMResponse:
        ...
```

为了降低改动，第一版也可以保持 `ChatClient` 类名不变，但让它内部返回 normalized response，或者新增 `NormalizedChatClient` 并逐步迁移。

## `/model` 命令设计

`/model` 应该在 command 层拦截，不进入 agent loop。

### 文本命令

支持：

```text
/model
/model deepseek-v4-flash
/model kimi-k2
```

行为：

1. `/model`：展示可选模型列表。
2. `/model <id>`：直接切换。
3. 无效 id：返回可选列表和当前模型。

### 飞书交互卡片

`/model` 返回 interactive card：

1. 当前模型。
2. 模型列表。
3. 每个模型一个按钮或 select option。
4. 点击后通过 card action 更新 conversation metadata。
5. 原卡片更新为“已切换到 xxx”。

card action payload 示例：

```json
{
  "action": "set_model_profile",
  "conversation_id": 2635,
  "profile_id": "kimi-k2"
}
```

### `/status`

`/status` 应展示：

```text
LLM: active_model_profile
Loop: react
Agent step: xxx
Intent classifier: xxx
Planner: xxx
```

第一版 loop 固定为 `react`。agent step 与 classifier 可以先使用同一个 active profile，但 `/status` 的展示结构应提前按节点拆开，避免后续再改用户可见格式。

## 推荐落地顺序

### Phase 1：Provider Adapter 基础

1. 新增 `app/llm/model_profiles.py`。
2. 新增 `app/llm/provider_adapters.py`。
3. 定义 `NormalizedLLMResponse`、`NormalizedToolCall`、`TokenUsage`。
4. 实现 `DeepSeekAdapter`，保持当前行为不变。
5. 实现 `KimiAdapter`，至少处理 tool calls、usage、无 reasoning_content。
6. agent step 改为消费 normalized response。
7. 保留当前 DeepSeek 默认配置，确保行为不变。

### Phase 2：ModelRouter 与 NodePolicy

1. 新增 `ModelRouter.resolve(node, conversation_metadata)`。
2. 新增 `LLMNodePolicy`。
3. agent step 通过 router 创建 client。
4. classifier 通过 router 创建 client。
5. profile capability + node policy 决定是否传 `response_format`。
6. profile capability + node policy 决定是否回传 `reasoning_content`。
7. 预留 `planner`、`plan_repair`、`verifier` 节点配置，但第一版不启用。

### Phase 3：TurnLoopProvider 预留

1. 新增 `TurnLoopProvider` enum。
2. 新增 `TurnLoop` protocol 或等价内部接口。
3. 当前 `TurnRuntime` 只注册 `react` loop。
4. 不暴露用户级 loop 切换命令。
5. unknown / unsupported loop provider fail closed。

### Phase 4：`/model` 文本命令

1. command handler 支持 `/model`。
2. conversation metadata 写入 `active_model_profile`。
3. `/status` 展示当前模型、loop、节点模型。
4. 测试默认模型、切换模型、无效模型。

### Phase 5：飞书卡片

1. renderer 新增 `render_model_selection_card`。
2. card action 支持 `set_model_profile`。
3. 点击后更新 conversation metadata。
4. 更新原卡片展示结果。

### Phase 6：节点级配置与 fallback

1. 支持 `model_overrides.agent_step`。
2. 支持 `model_overrides.intent_classifier`。
3. 支持 `model_overrides.planner`。
4. 支持 classifier fallback。
5. 支持 provider 超时重试。
6. 后续真正引入 plan-execute loop 时，再启用 planner 节点。

## 测试计划

### Adapter 测试

1. DeepSeek 标准 tool calls 能转成 normalized tool calls。
2. Kimi tool calls 能转成 normalized tool calls。
3. arguments 是 dict/string/空字符串都能处理。
4. usage 字段不同命名能统一。
5. reasoning_content 只在支持的 provider 上保留。

### Agent loop 测试

1. DeepSeek profile 下行为与现在一致。
2. Kimi profile 下工具调用可执行。
3. 不支持 tools 的 profile 不应出现在 agent step 可选列表。
4. 模型返回空 content + tool_calls 时不失败。
5. 模型返回 content + tool_calls 时不丢内容。

### Classifier 测试

1. 支持 JSON mode 时传 `response_format`。
2. 不支持 JSON mode 时不传 `response_format`。
3. provider 超时时 fallback 分类仍可用。
4. classifier 使用 conversation 当前 active model。

### NodePolicy / Router 测试

1. `agent_step` 选择不支持 tools 的 profile 时 fail closed 或回退。
2. `intent_classifier` 使用短 timeout。
3. `planner` override 能被 resolve，但默认 runtime 不调用 planner。
4. profile 不支持 reasoning 时，请求中不包含 reasoning_content。

### TurnLoopProvider 测试

1. metadata 缺少 loop provider 时默认 `react`。
2. metadata 为 `react` 时走当前 ReAct loop。
3. metadata 为未知 loop provider 时 fail closed 或明确 fallback。
4. 第一版不暴露 loop 切换命令。

### `/model` 测试

1. `/model` 返回模型列表。
2. `/model <valid>` 写入 metadata。
3. `/model <invalid>` 返回错误和候选列表。
4. `/status` 展示 active model。
5. 飞书 card action 能切换模型。

## 风险

### 风险 1：模型能力不一致

不是所有模型都适合 agent loop。必须通过 `supports_tools` 过滤，否则模型可能无法稳定调用工具。

### 风险 2：reasoning 字段污染其他 provider

DeepSeek 特有字段不能传给 Kimi。request builder 必须按 profile capability 清理消息。

### 风险 3：classifier 依赖 JSON mode

如果切到不支持 JSON mode 的模型，classifier 可能不稳定。第一版可以强制 classifier 仍使用默认稳定模型，后续再开放节点级切换。

### 风险 4：conversation 切换后历史消息格式不兼容

历史 assistant message 可能带 DeepSeek reasoning metadata。切到 Kimi 后，request builder 应剥离不兼容字段，而不是直接透传。

### 风险 5：模型展示与实际请求不一致

footer 中的模型名应来自 normalized response 的 model 字段。如果 provider 不返回 model，则使用 profile model。

### 风险 6：LLM provider 与 loop provider 混淆

Kimi、DeepSeek、OpenAI 这类 provider 只应该描述模型协议差异。ReAct、plan-execute、research 这类 loop 是 turn 执行策略。两者如果混在一起，后续会出现“为了切 planner 模型而改 agent loop”或“为了换 research loop 而改 provider adapter”的问题。

### 风险 7：第一版过度设计

第一版只应实现一个 loop：`react`。`TurnLoopProvider`、`planner`、`plan_repair`、`verifier` 是接口预留和配置预留，不应在第一版引入复杂 plan-execute 状态机。

## 当前结论

多模型切换是一次 agent loop 级别改造，不建议先做 `/model` UI。

正确顺序是：

```text
Provider Adapter
  -> ModelRouter
  -> NodePolicy
  -> agent step / classifier 接入
  -> TurnLoopProvider 接口预留（只实现 react）
  -> /model 文本命令
  -> 飞书选择卡片
  -> 节点级模型配置
```

只有先把 Kimi、DeepSeek V4 的返回差异收口到 adapter，`/model` 才是稳定功能，而不是一个会触发隐性 runtime bug 的开关。
