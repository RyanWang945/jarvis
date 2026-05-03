# Jarvis Agent 测试体系设计

日期：2026-05-03

## 1. 背景

Agent 测试不能只测试最终自然语言回复。

Jarvis 当前已经具备 ReAct runtime、工具调用、会话持久化、飞书通道、session state、skills 和 coder delegation。对这类系统来说，真正需要稳定验证的是：

1. runtime 状态机是否正确推进。
2. 上下文是否被正确装配给模型。
3. 工具调用是否被正确选择、执行、拒绝和审计。
4. 用户可见消息、turn 状态和 tool_calls 事实链是否一致。
5. 真实 LLM 场景下行为质量是否可评估、可回归。

因此 Jarvis 的 agent 测试应分成两类闭环：

- **pytest 闭环**：验证工程确定性，默认 CI 必跑。
- **eval 闭环**：验证真实 LLM 行为质量，默认不进 CI，以 opt-in 或 nightly 方式运行。

## 2. 测试目标

Jarvis agent 测试要回答的问题不是“模型这句话写得像不像”，而是：

- 它看到了正确上下文吗？
- 它是否按预期触发 turn？
- 它是否调用了正确工具？
- 它是否被 policy 拦住了该拦的高风险行为？
- 每一步是否落到了 `messages`、`turns`、`tool_calls`？
- 工具失败、拒绝、取消后，用户和系统是否能知道发生了什么？
- 真实 LLM 任务中，它的工具选择、成本、准确性和稳定性是否可观测？

## 3. 总体分层

### 3.1 确定性单元测试

目标：测纯逻辑，不依赖真实 LLM，不依赖真实外部服务。

覆盖对象：

- `ContextManager`
  - conversation history 转 LangChain messages。
  - trigger message boundary。
  - system prompt 注入。
  - selected skills 注入。
  - session state 注入。
  - token budget 裁剪。
  - assistant tool_call 与对应 tool result 不能被拆散。

- `session_state`
  - metadata round-trip。
  - 非法值容错。
  - status 渲染。
  - model-facing 渲染不能泄漏 debug-only 字段。

- `tools.runtime`
  - shell 只读命令白名单。
  - shell 多命令拒绝。
  - 高风险命令拒绝。
  - coder delegation proposal gate。
  - workdir 存在性和 workspace 边界。

- channel / renderer
  - Markdown 到飞书卡片。
  - interactive 失败后 text fallback。
  - thinking card 更新。

判断方式：

- 普通 assert。
- 快速、稳定、默认 CI 必跑。
- 不检查自然语言措辞，除非该文本是明确协议或固定状态文案。

当前示例：

- `tests/test_session_state.py`
- `tests/test_tools_shell.py`
- `tests/test_feishu_channel.py`
- `tests/test_agent_context_manager_edges.py`

### 3.2 Scripted LLM Runtime 测试

目标：测 ReAct loop 和 runtime 持久化，不测模型能力。

做法：

- monkeypatch `ChatClient.chat`。
- 使用脚本化 LLM 响应控制每一步：
  - 第一次返回 tool call。
  - 第二次读取 tool result 后返回最终回答。
  - 或连续多轮 tool call。
  - 或返回非法参数、重复工具调用、需要被拒绝的工具调用。

断言重点：

- `turn.status`
- user / assistant / tool message 顺序。
- `tool_calls.status`
- `tool_calls.assistant_message_id`
- `tool_calls.provider_tool_call_id`
- `tool_calls.step_index`
- tool output 是否进入下一轮模型上下文。
- 工具失败、拒绝、取消是否被审计。

这层是 Jarvis agent 工程测试的主力。

当前基础设施：

- `tests/helpers/agent_harness.py`
  - `create_agent_test_client()`
  - `create_dm_turn(...)`
  - `ScriptedChat`
  - `tool_call(...)`
  - `tool_response(...)`
  - `final_response(...)`

当前示例：

- `tests/test_conversation_runtime.py`
- `tests/test_tools_coder.py`
- `tests/test_tools_obsidian_wiki_runtime.py`
- `tests/test_agent_runtime_edges.py`

### 3.3 工具集成测试

目标：验证工具真实执行契约，但隔离外部依赖。

分类：

- 文件类工具
  - 使用 `tmp_path` 或临时 workspace。
  - 验证文件内容、路径边界、覆盖行为、拒绝行为。

- shell 工具
  - policy 测试覆盖危险命令拒绝。
  - 执行测试只跑安全单命令。

- Obsidian wiki
  - 使用临时 vault。
  - 测 draft / apply / query / maintain。

- Tavily / HTTP 工具
  - mock HTTP response。
  - 不在默认测试里打真实网络。

- coder tool
  - 默认 mock backend / subprocess。
  - 验证参数、policy、审计、返回结构。
  - 真实仓库测试 opt-in。

断言重点：

- 输入 schema。
- 输出结构。
- 失败可读性。
- workspace 污染。
- audit record。

当前示例：

- `tests/test_tools_write_file.py`
- `tests/test_tools_shell.py`
- `tests/test_tools_tavily.py`
- `tests/test_tools_obsidian_wiki.py`
- `tests/test_tools_coder_real_repo.py`

### 3.4 端到端通道测试

目标：验证用户入口到 runtime 的完整链路，但仍使用 Scripted LLM。

覆盖场景：

- DM 消息创建 turn。
- 群聊背景消息只入库，不触发 turn。
- mention 触发 turn。
- 回复 bot 消息触发 turn。
- `/clear` 创建新 conversation generation。
- `/cancel` 取消 running turn。
- `/status` 展示 session state。
- 飞书 thinking card 到 final card 更新。

断言重点：

- `conversation_id`
- `message_id`
- `turn_id`
- `should_respond`
- `trigger_type`
- `messages.role`
- `messages.turn_id`
- `turn.status`

当前示例：

- `tests/test_conversation_runtime.py`
- `tests/test_feishu_channel.py`

### 3.5 恢复与长运行测试

目标：验证 Long Run Agent 的可恢复性和副作用安全。

后续应覆盖：

- 进程重启后 conversation history 可恢复。
- running turn 超时后标记为 failed / waiting_manual_recovery。
- 已完成 tool_call 恢复时不能重复执行副作用工具。
- cancelled turn 不再生成 assistant reply。
- 同一 external_message_id 幂等。
- 长任务挂起、等待用户确认、恢复继续。
- 多 agent / subagent 场景下主 agent 不重复消费子 agent trace。

第一阶段事实源：

- `messages`
- `turns`
- `tool_calls`

后续如果引入 `events` 表：

- runtime replay 和恢复测试应以 `events` 为主事实源。
- `messages`、`turns`、`tool_calls` 作为查询投影和用户可见状态。

### 3.6 真实 LLM 行为评测

目标：评估真实模型能力和 prompt/tool 组合效果。

这不是默认 pytest，而是 opt-in eval。

适合任务集：

- 简单问答：不应调用工具。
- 当前信息：应调用搜索工具。
- 代码修改：应调用 coder 工具。
- 高风险请求：应拒绝或请求授权。
- 多轮追问：应读取历史上下文。
- 工具失败：应如实说明并给出下一步。
- wiki 归档：应先 draft，再 apply 或等待确认。
- 长上下文：应保留关键事实，避免引用被裁剪的旧信息。

输出内容：

- 原始 query。
- 模型输入摘要。
- assistant tool call trace。
- tool result。
- final answer。
- latency。
- token usage。
- cost。
- 自动评分。
- 人工评分。

真实 LLM eval 不应该只返回 pass/fail，而应保存 trace 和指标，便于回归分析。

## 4. 测试基础设施

### 4.1 pytest helper

当前已引入：

```text
tests/helpers/agent_harness.py
```

职责：

- 清理 runtime 全局缓存。
- 创建 `TestClient`。
- 创建 DM turn。
- 安装 scripted `ChatClient.chat`。
- 构造 OpenAI-compatible tool call response。

建议后续扩展：

- `create_group_message(...)`
- `create_group_mention_turn(...)`
- `assert_message_roles(...)`
- `assert_tool_call_chain(...)`
- `assert_turn_completed(...)`
- `assert_turn_failed(...)`
- `ScriptedChat.assert_called_with_tools(...)`
- `ScriptedChat.last_model_messages`

### 4.2 pytest markers

建议在 `pyproject.toml` 增加 markers：

```toml
[tool.pytest.ini_options]
markers = [
    "unit: pure deterministic unit tests",
    "runtime: scripted LLM runtime tests",
    "integration: safe local integration tests",
    "real_llm: opt-in tests that call real LLM providers",
    "real_coder: opt-in tests that run real coder backend",
    "slow: slow tests excluded from default local loop",
]
```

默认本地闭环：

```powershell
uv run pytest tests/test_agent_context_manager_edges.py tests/test_agent_runtime_edges.py tests/test_conversation_runtime.py tests/test_tools_coder.py tests/test_tools_obsidian_wiki_runtime.py tests/test_session_state.py -q
```

默认 CI：

- unit
- runtime
- safe integration

默认不跑：

- real_llm
- real_coder
- slow

### 4.3 Eval 数据集

建议结构：

```text
tests/fixtures/agent_eval/
  basic_qa.jsonl
  search.jsonl
  coder.jsonl
  safety.jsonl
  memory.jsonl
  wiki.jsonl
```

单条样本建议字段：

```json
{
  "id": "search_latest_pricing_001",
  "category": "search",
  "messages": [
    {"role": "user", "content": "帮我查一下最新模型价格并总结"}
  ],
  "expected_tools": ["tavily_search"],
  "forbidden_tools": ["delegate_to_claude_code", "shell_run_command"],
  "success_criteria": [
    "uses search tool",
    "cites sources",
    "does not invent prices"
  ],
  "risk_level": "low"
}
```

### 4.4 Eval runner

建议新增：

```text
scripts/run_agent_eval.py
```

职责：

- 加载 eval jsonl。
- 创建隔离 conversation。
- 执行真实 LLM runtime。
- 保存 trace。
- 计算自动指标。
- 输出 Markdown 报告。

建议输出目录：

```text
data/eval_runs/YYYY-MM-DD_HH-mm-ss_agent_eval/
  run.json
  traces/
  report.md
```

### 4.5 Trace Schema

建议 eval trace 至少包含：

```json
{
  "case_id": "search_latest_pricing_001",
  "conversation_id": 123,
  "turn_id": 456,
  "status": "completed",
  "messages": [],
  "tool_calls": [],
  "metrics": {
    "latency_ms": 12000,
    "llm_calls": 2,
    "tool_calls": 1,
    "input_tokens": 3000,
    "output_tokens": 500,
    "estimated_cost_usd": 0.01
  },
  "scores": {
    "tool_selection": 1.0,
    "policy": 1.0,
    "answer_quality": 0.8
  }
}
```

## 5. 评估指标

### 5.1 工程正确性指标

- Turn completion rate
  - completed / total。

- Turn failure rate
  - failed / total。

- Tool call audit completeness
  - 有 tool call 的 assistant message 是否都有 `tool_calls` 记录。
  - `assistant_message_id` 是否不为空。
  - `provider_tool_call_id` 是否被保存。
  - `step_index` 是否连续。

- Tool call terminal status rate
  - tool call 是否最终进入 completed / failed / rejected / cancelled。

- Idempotency pass rate
  - 重复 external message 是否不会重复创建 turn 或重复执行副作用。

- Cancellation correctness
  - cancelled turn 是否不再生成 assistant reply。

- Recovery correctness
  - crash / restart 后是否能恢复可见状态。

- Workspace pollution count
  - 是否产生 `.idea/`、`.pytest_cache/`、`__pycache__/`、`.venv/` 等不期望产物。

### 5.2 行为可靠性指标

- Tool selection accuracy
  - expected tool 被调用。
  - forbidden tool 未被调用。

- Unnecessary tool call rate
  - 不需要工具的问题却调用了工具。

- Repeated tool call rate
  - 相同参数重复调用失败工具。

- Search budget violation rate
  - 超过搜索预算。

- Max-step hit rate
  - ReAct loop 命中最大步数。

- High-risk false positive
  - 明确代码任务被错误拒绝 coder。

- High-risk false negative
  - 非代码任务错误调用 coder。

- Context use accuracy
  - 多轮追问是否使用了正确历史。

- Clarification appropriateness
  - 信息不足时是否合理追问，而不是盲目执行。

### 5.3 结果质量指标

- Answer correctness
  - 事实是否正确。

- Source faithfulness
  - 是否忠实于工具结果和来源。

- Instruction following
  - 是否满足用户明确约束。

- Completeness
  - 是否覆盖问题要点。

- Conciseness
  - 是否避免无关背景和过度解释。

- User language match
  - 是否使用用户当前语言回复。

- Actionability
  - 对代码、运维、研究任务是否给出可执行结论。

- Markdown / channel render quality
  - 最终输出在飞书卡片中是否可读。

### 5.4 成本与性能指标

- Latency per turn。
- LLM calls per turn。
- Tool calls per turn。
- Input tokens。
- Output tokens。
- Cost per successful task。
- Retry count。
- Tool execution duration。

### 5.5 长期稳定性指标

- Same task pass rate across model versions。
- Regression count by capability。
- Flaky rate。
- Eval drift after prompt changes。
- Eval drift after tool changes。
- Recovery success after simulated crash。

## 6. 测试用例设计原则

### 6.1 默认测试不依赖真实 LLM

默认 pytest 必须稳定。

LLM 行为使用 `ScriptedChat` 控制。真实 LLM 只用于 eval。

### 6.2 测事实链，不测措辞

优先断言：

- message role 顺序。
- turn status。
- tool call audit。
- raw_payload。
- provider tool call id。
- step index。

谨慎断言大段自然语言。

### 6.3 工具副作用必须隔离

默认测试：

- 不打真实网络。
- 不修改真实仓库业务文件。
- 不调用真实 coder backend。
- 不执行危险 shell。

真实副作用测试必须 opt-in。

### 6.4 每个失败路径都要可审计

以下路径都应写入明确状态：

- policy rejected。
- tool failed。
- LLM failed。
- turn cancelled。
- max steps reached。
- malformed tool args。
- unknown tool。

### 6.5 测试模块按能力拆分

建议结构：

```text
tests/
  helpers/
    agent_harness.py
  test_agent_context_manager_edges.py
  test_agent_runtime_edges.py
  test_conversation_runtime.py
  test_tools_coder.py
  test_tools_obsidian_wiki_runtime.py
  test_session_state.py
```

后续如果 runtime 测试继续增长，可再拆：

```text
tests/agent_runtime/
  test_turn_lifecycle.py
  test_tool_audit.py
  test_tool_budget.py
  test_cancellation.py
  test_recovery.py
  test_context.py
```

## 7. 当前已落地内容

已新增：

- `tests/helpers/agent_harness.py`
- `tests/test_agent_runtime_edges.py`
- `tests/test_agent_context_manager_edges.py`
- `tests/fixtures/agent_eval/smoke.jsonl`
- `scripts/run_agent_eval.py`
- `tests/test_agent_eval_runner.py`

覆盖：

- 多工具同一 assistant message 内调用时，审计记录共享同一个 `step_index`。
- 工具执行抛异常时，`tool_calls` 被标记 failed，tool message 仍写入上下文。
- `tavily_search` 单 turn 预算超过后，第 3 次调用被 rejected。
- token budget 裁剪时保留 assistant tool_call 与对应 tool result。
- 最小 E2E smoke eval 数据集，包括 basic QA、search、wiki memory、coding、safety 五类。
- E2E eval runner 支持 fixture 加载、真实 runtime 执行、trace 保存、自动评分和 Markdown report。
- E2E eval runner 默认带安全闸：不设置 `JARVIS_RUN_AGENT_EVAL=1` 时不会调用真实 LLM/runtime。

E2E eval dry-run：

```powershell
uv run python scripts\run_agent_eval.py --dry-run
```

真实 E2E smoke 示例：

```powershell
$env:JARVIS_RUN_AGENT_EVAL="1"
uv run python scripts\run_agent_eval.py --limit 1
```

带外部能力的 case 需要显式允许：

```powershell
$env:JARVIS_RUN_AGENT_EVAL="1"
uv run python scripts\run_agent_eval.py --allow-requires tavily
```

输出目录：

```text
data/eval_runs/YYYY-MM-DD_HH-mm-ss_agent_eval/
  run.json
  report.md
  traces/
```

当前验证命令：

```powershell
uv run pytest tests/test_agent_context_manager_edges.py tests/test_agent_runtime_edges.py tests/test_agent_eval_runner.py tests/test_conversation_runtime.py tests/test_tools_coder.py tests/test_tools_obsidian_wiki_runtime.py tests/test_session_state.py -q
```

当前结果：

```text
37 passed
```

## 8. 后续路线

### Phase 1：完善 pytest runtime 覆盖

建议补充：

1. malformed tool arguments。
2. unknown tool。
3. max steps force final response。
4. cancellation mid-loop。
5. LLM exception。
6. tool result 过长截断。
7. repeated failed tool call 防护。
8. session state 在多轮 turn 后更新策略。

### Phase 2：拆分 runtime 测试目录

当 `test_conversation_runtime.py` 继续膨胀后，将其拆到：

```text
tests/agent_runtime/
```

每个文件只负责一种能力。

### Phase 3：引入 eval runner

新增真实 LLM eval：

- 固定任务集。
- trace 保存。
- 自动指标。
- Markdown 报告。

默认不进 CI。

### Phase 4：Long Run 恢复测试

在引入 events / job / artifact 后，补充：

- crash replay。
- pending permission。
- stale running recovery。
- no duplicate side effect。
- subagent result integration。

## 9. 结论

Jarvis 的 agent 测试应遵循以下边界：

- pytest 负责工程确定性。
- eval 负责真实行为质量。
- 默认测试不依赖真实 LLM。
- runtime 测试以 `messages`、`turns`、`tool_calls` 事实链为核心。
- 工具测试验证契约和安全边界。
- 长运行测试以恢复和副作用幂等为核心。

这样后续开发 agent runtime、skills、coder、wiki、飞书通道和长运行能力时，每个改动都可以落到明确、稳定、可回归的测试闭环里。
