# jarvis-strategize 节点能力升级

## 1. 现状与问题

当前 `strategize` 节点虽然是 LLM 节点，但其输入的 `tools` 列表被上游 `classify_intent` 节点的 `allowed_tools` **严格过滤**。`classify_intent` 本身是一个纯规则分类器，仅通过关键词 in-string 匹配将用户意图归入 4 种扁平类型（`explicit_shell`、`code_write`、`search_summary`、`simple_chat`）。

### 1.1 当前 `classify_intent` 节点的处理逻辑

代码位于 `app/agent/nodes.py:1073`（`_classify_intent` 函数），处理流程如下：

```python
def _classify_intent(state: AgentState) -> IntentDecision:
    payload = _payload(state)
    instruction = str(payload.get("instruction") or "")
    command = _clean_optional(payload.get("command"))
    workdir = _clean_optional(payload.get("workdir"))

    if command:
        return _intent_decision("explicit_shell", ...)

    if workdir and _looks_like_code_write(instruction):
        return _intent_decision("code_write", ...)

    if _looks_like_search_summary(instruction):
        return _intent_decision("search_summary", ...)

    return _intent_decision("simple_chat", ...)  # 兜底
```

其中 `_looks_like_code_write` 与 `_looks_like_search_summary` 为纯关键词匹配：

| 意图 | 判断条件 |
|------|---------|
| `explicit_shell` | 用户显式传入了 `--command` |
| `code_write` | `workdir` 非空 **且** instruction 中同时包含任意 action_terms（`写、新增、修改、fix、create` 等）和任意 object_terms（`脚本、代码、file、commit` 等） |
| `search_summary` | instruction 中包含任意 search_terms（`搜索、查一下、research` 等）**且** 不包含 code_context_terms（`test 失败、bug、workdir` 等） |
| `simple_chat` | 以上均未命中 |

分类完成后，`_allowed_tools_for_intent` 根据意图类型返回固定的工具白名单，写入 `state["allowed_tools"]`。后续 `strategize` 节点只能在此白名单内选择工具。

这导致三个核心问题：

1. **视野受限**：LLM 规划器只能看到当前意图白名单下的工具，无法处理复合意图。例如"搜索 LangGraph 文档并写一个示例"会被错误归类为单一意图，搜索工具对规划器不可见。
2. **语义缺失**：规则分类无法处理上下文承接、隐含意图、多步骤需求。用户说"再加个去重功能"，分类器看不到上文"写爬虫"，导致工具选择错误。
3. **过度依赖 Claude Code**：`coder` skill 直接调用 Claude Code CLI 处理几乎所有代码类任务，jarvis 退化为启动器，失去了对执行过程的可见性和编排权。

## 2. 设计目标

将 `strategize` 升级为**端到端 LLM 规划节点**，由它根据用户输入、历史上下文和候选能力集合做决策。`classify_intent` 不再用单一意图白名单决定工具视野，仅保留为观测标签和轻量快速路由。

这里不能简单地把所有工具无条件暴露给 LLM。历史上 `allowed_tools` 的存在，是为了防止 planner 把代码修改任务错误路由到通用 shell。升级后的边界应是：

- **宽进**：候选工具集合可以覆盖复合意图，例如搜索 + 代码修改。
- **严出**：`strategize` 在生成 WorkOrder 前必须执行 deterministic eligibility guard，校验 workdir、显式命令、文件修改权限和风险等级。

同时明确 Claude Code 的定位：它是 jarvis 工具生态中的一个**代码执行器**，不是同级大脑。jarvis 负责"什么时候调什么"，Claude Code 负责仓库文件编辑、代码审查、提交推送等需要代码上下文的执行场景。

为后续接入公司平台（飞书、飞书文档等）预留扩展位，这些平台能力以标准工具形式注册，由 `strategize` 统一调度。

## 3. 架构变更

### 3.1 图结构简化

```
改造前：
ingest_event -> contextualize -> classify_intent(规则) -> strategize(受限LLM) -> dispatch

改造后：
ingest_event -> contextualize -> classify_intent(观测/快速路径) -> strategize(候选能力 + LLM) -> dispatch
```

`classify_intent` 职责降级：

| 场景 | 行为 |
|------|------|
| 输入以 `/` 开头 | 解析为 Slash 命令，直接路由，不走 LLM |
| 输入包含 `--command` 等显式参数 | 规则快速决策，减少 token 消耗 |
| 其他所有请求 | 不再产出硬白名单，只将分类结果作为 `observation_intent` 写入 state，用于日志、报表和候选召回 |

### 3.2 strategize 节点升级

**输入**：
- `messages`：完整对话历史
- `instruction`：当前用户指令
- `context`：workdir、git 状态、resource_key 等
- `candidate_tools`：由 capability metadata、上下文和轻量规则召回的候选工具

**LLM Prompt 核心结构**：
```text
You are Jarvis, a planning agent. Based on the user's request and available tools,
output a sequence of tool calls to fulfill the request.

Candidate tools:
- coder: Repository development worker via Claude Code CLI. Use for file edits,
  repository review, commits/pushes explicitly requested by the user, or workflows
  that need codebase understanding.
- shell.test: Run known low-risk test commands such as pytest.
- shell.command: Execute an explicit command supplied by the caller. Do not invent
  shell commands for code editing.
- search: Web or local search. Use for information gathering.
- feishu_message: Send message to Feishu user/group.
- feishu_doc: Read/write Feishu documents.
- answer_echo: Direct reply when no external action needed.

Rules:
- Break complex requests into atomic steps.
- Prefer specialized tools over general ones (e.g., use shell for "pytest", not coder).
- Use coder for repository file edits, even if the edit is small, when semantic
  repository context matters.
- Use shell.command only when the user supplied the exact command.
- If the request is ambiguous, set needs_clarification=true.
```

**输出（结构化 JSON）**：
```json
{
  "confidence": 0.92,
  "needs_clarification": false,
  "clarification_question": null,
  "plan_steps": [
    {
      "step_id": "step-1",
      "capability_name": "search",
      "tool_args": {"query": "LangGraph checkpointer usage"},
      "reason": "Need to understand latest API before writing code"
    },
    {
      "step_id": "step-2",
      "capability_name": "coder",
      "tool_args": {"instruction": "Add a SQLite checkpointer example..."},
      "reason": "Multi-file example creation requires deep editing"
    }
  ]
}
```

**内部处理**：
1. `strategize` 调用 `select_candidate_capabilities(state)` 召回候选能力
2. `strategize` 调用 `get_jarvis_llm().plan_tasks(...)` 获取规划
3. 校验每个 `capability_name` 是否在注册表中，并执行 eligibility guard
4. 为每个 step 生成 `Task` + `WorkOrder`，写入 `dispatch_queue`
5. 设置 `next_node` = `dispatch`（正常）或 `blocked`（校验失败）

### 3.3 工具选择策略（当前候选召回，未来 RAG）

**当前阶段**：jarvis 工具数量极少（coder、shell、search、answer_echo 等），但仍不应无条件暴露通用 shell。当前采用轻量候选召回：

| Capability | 候选条件 | 执行期 guard |
|------------|----------|--------------|
| `answer.echo` | 总是候选 | 无副作用 |
| `search.tavily` | 搜索/调研/带来源语义命中；复合任务可与 coder 同时候选 | capability 必须存在且 exposed |
| `coder.claude_code` | 有 `workdir` 且涉及仓库、文件、代码、commit/push、审查或多步骤工作 | 必须有 workdir；高危操作仍走 approval |
| `shell.test` | 测试语义命中，或显式命令是受控测试命令 | 只能执行 schema 允许的测试命令 |
| `shell.command` | 用户显式传入 `--command` 或等价结构化命令参数 | 必须有 caller supplied command；planner 不能凭空生成 |

此阶段无需复杂检索，但 `strategize` 的接口应接收“候选工具列表”，而不是依赖全量工具或单一 intent 白名单。

**未来扩展讨论**：当接入飞书、飞书文档、公司内部平台后，工具数量可能达到 20-50 个甚至更多。此时全量放入 prompt 会导致：
- Token 成本激增
- 模型选择 paralysis，长尾工具被忽略
- 延迟显著增加

业界主流方案是 **Query Rewriting + RAG 召回 + LLM 精选**：

```
用户输入 + 上下文
    ↓
AI 扩写（Query Rewriting）
    → "通知一下大家" 扩写为 "发送消息到群组或私聊，通知团队成员"
    ↓
混合检索（BM25 + 向量相似度）
    → 召回 Top 8-10 最相关工具
    ↓
strategize LLM（只看到这 8-10 个工具）
    → 最终选择
```

**扩写目的**：用户输入通常模糊，直接检索容易漏召。通过 LLM 轻量调用生成多条扩写 query（覆盖不同语义角度），提升召回率。

**混合检索**：工具元数据（名称、描述、keywords、example_inputs）预先建立向量索引。检索时 BM25 匹配精准关键词，向量检索覆盖语义相似度，最后用 Reranker 排序。

**层级分类补充**：给工具打大类标签（communication / code / deploy），先粗分类再细检索，进一步缩小搜索空间。

**实施建议**：
| 工具数量 | 策略 |
|---------|------|
| ≤ 15 个 | 轻量候选召回 + eligibility guard（当前做法） |
| 15-50 个 | RAG 召回 Top 8 |
| > 50 个 | 层级分类 + RAG 召回 |

此方案作为预留设计，当前版本暂不需要实现，但 `strategize` 的接口已经应支持传入 "候选工具列表"，以便未来无缝切换。

### 3.4 混合路由策略

不是所有请求都需要 LLM：

| 请求类型 | 处理器 | 说明 |
|----------|--------|------|
| Slash 命令 (`/commit`, `/status`) | `classify_intent` 规则路由 | 零 token 开销 |
| `--command` 显式 shell | 规则路由 | 确定性执行 |
| 简单 echo/问候 | 规则路由或低置信度缓存 | 避免 LLM 浪费 |
| 其他所有请求 | `strategize` LLM 规划 | 真正需要智能的部分 |

缓存机制：对常见指令（如"跑测试"、"提交代码"）做 `(instruction_hash) -> plan` 缓存，命中率高的请求直接跳过 LLM。

### 3.5 置信度与澄清机制

当 LLM 返回 `confidence < 0.7` 或 `needs_clarification == true` 时：

```python
return {
    "status": "clarifying",
    "final_summary": clarification_question,
    "next_node": "blocked",  # 或新增 clarify 节点走 interrupt
}
```

用户通过 API/CLI 回复后，新消息追加到 `messages`，重新进入 `strategize`。

### 3.6 WorkPlan 顺序编排

当用户明确要求“拆成多个 work order”并提供 numbered steps 时，Jarvis 保留 WorkPlan 作为硬约束：

- `classify_intent` 只负责识别 numbered steps 并创建顺序 WorkPlan。
- `planner_type=rule_based` 时，每个 step 沿用主 intent 的默认 capability，保持稳定降级行为。
- `planner_type=llm` 时，每个 step 初始标记为 `__planner__`；执行到该 pending step 时，`strategize` 只把当前 step 和候选工具交给 LLM，让 LLM 为该 step 选择一个 capability。
- `strategize` 每次只把一个 pending step 转成 WorkOrder；`aggregate` 标记成功后再回到 `strategize` 处理下一个 step。
- 如果单个 step 的 planner 结果需要澄清、置信度过低、没有工具调用或选择了不合规工具，该 run 进入 `blocked`，不会静默跳过 required step。

这样可以同时满足两件事：用户显式要求的步骤数量不会被 planner 压缩成一个大任务；每个 step 又可以独立选择 search、coder、shell.test、平台工具等不同 capability。

## 4. 工具层级与 Claude Code 定位

### 4.1 工具分级

| 层级 | 工具示例 | 使用时机 | 归属 |
|------|---------|---------|------|
| **通用轻量工具** | `shell.command`, `shell.test`, `search.web`, `answer.echo` | 单步命令、信息查询、简单回复 | jarvis 自有 worker |
| **平台集成工具** | `feishu_message.send`, `feishu_doc.read`, `feishu_doc.write` | 跨系统交互 | jarvis 自有 worker |
| **代码执行工具** | `coder` (Claude Code CLI) | 仓库文件编辑、代码审查、commit/push workflow、复杂语义编辑 | 外包给 Claude Code |

### 4.2 Claude Code 调用原则

`strategize` 的 prompt 中必须明确约束：

```
Use coder when:
- Task requires editing repository files
- Task requires understanding file relationships or existing code
- Task is a code review, refactor, README/doc update inside a repo, or commit/push workflow
- User explicitly asks for "deep refactor" or "help me redesign"

Use shell instead when:
- Running known test/lint/build commands through shell.test
- Executing an exact command supplied by the caller through shell.command

Do not use shell.command to invent file-editing commands.
```

**目的**：防止代码任务被错误路由到 shell，同时避免"跑 pytest"这类受控验证任务都走 Claude Code。

### 4.3 可观测性增强

Claude Code 当前输出是纯文本 stdout。当前 `coder` skill 已经追加 `[JARVIS_POSTFLIGHT]`，由 Jarvis 自己采集 git status、branch、commit 等信息。后续应优先增强这个 deterministic postflight，而不是完全依赖 Claude 文本自报。

建议增强字段：

```json
[JARVIS_POSTFLIGHT]
{
  "files_modified": ["src/main.py", "src/utils.py"],
  "diff_stat": "...",
  "commit": "abc1234",
  "working_tree_clean": false
}
```

可选地，如果 Claude Code CLI 支持结构化输出，可追加：

```python
command = provider_command + [
    "--print",
    "--output-format", "json",  # 如果 Claude Code 支持；否则 prompt 内要求
    # ... existing args
]
```

同时在 prompt 中要求 Claude Code 在回复末尾附加弱信号：

```json
[JARVIS_STRUCT]
{
  "files_modified": ["src/main.py", "src/utils.py"],
  "tests_run": "pytest tests/",
  "test_result": "passed",
  "commit_created": "abc1234"
}
```

`aggregate` 节点解析这些结构化片段时，应优先信任 Jarvis postflight，Claude 末尾 JSON 只作为测试命令和人工摘要的补充。

## 5. 公司平台接入预留（飞书/飞书文档）

公司平台能力以**标准 Skill/Tool** 形式注册到 `capability_registry`，对 `strategize` LLM 完全透明。

### 5.1 新增工具定义

```python
# app/skills/feishu.py
class FeishuMessageSkill:
    name = "feishu_message"
    action = "send"
    worker_type = "feishu"
    risk_level = "low"

class FeishuDocSkill:
    name = "feishu_doc"
    action = "read"  # or "write"
    worker_type = "feishu"
    risk_level = "medium"  # write 场景
```

### 5.2 多系统编排示例

用户指令："整理昨天的会议纪要，发到飞书文档，并通知项目组"

`strategize` 规划：

| Step | Tool | Args |
|------|------|------|
| 1 | `search.local` | 查找本地会议记录文件 |
| 2 | `answer.echo` | LLM 自己总结要点（或调用摘要模型） |
| 3 | `feishu_doc.write` | 写入飞书文档 |
| 4 | `feishu_message.send` | 发送文档链接到项目群 |

Claude Code 不参与此流程，因为不涉及代码编辑。

### 5.3 异步与长时任务

飞书消息发送、文档协作等可能是异步的。`monitor` 节点的 `interrupt(wait_workers)` 机制天然支持：worker 完成后通过回调 `resume`，图继续执行后续步骤。

## 6. State 结构变更

`AgentState` 中以下内容废弃或降级：

| 字段 | 变更 |
|------|------|
| `intent` | 保留但仅作观测标签，不直接决定最终工具 |
| `allowed_tools` | 降级为兼容字段；不再作为 LLM planner 的硬白名单 |
| `plan_steps` | 由 `strategize` LLM 直接生成，不再由 `classify_intent` 产出 |

新增：

| 字段 | 说明 |
|------|------|
| `observation_intent` | `classify_intent` 的输出标签（只读） |
| `candidate_tools` | 本轮传给 planner 的候选 capability 名称 |
| `planner_raw_output` | LLM 原始 JSON，用于调试和审计 |

这些字段会写入 audit log，并出现在 JSON/Markdown report 中，便于离线分析 planner 误选、候选召回缺失和 WorkPlan 执行偏差。

## 7. 降级与容错

| 故障场景 | 策略 |
|----------|------|
| LLM 规划超时 | 返回 `blocked`，提示用户重试 |
| LLM 输出非法 JSON | 捕获异常，最多重试 2 次，仍失败则 `blocked` |
| 选了不存在工具 | `strategize` 内部校验失败，返回 `blocked` |
| 缓存命中但工具已更新 | 缓存 key 包含工具 schema 版本号，自动失效 |
| 飞书等平台服务不可用 | Worker 返回失败，`aggregate` 评估是否重试或 `blocked` |

## 8. 实施路径

### 8.1 当前实现状态

截至 2026-04-22，已落地：

| 阶段 | 状态 | 说明 |
|------|------|------|
| Phase 1：解耦 classify_intent | Done | `strategize` 不再用 `allowed_tools` 作为 LLM 硬白名单；改为候选 capability 召回 + eligibility guard。 |
| Phase 2：strategize Prompt / PlannerDecision | Done | `JarvisLLM.plan_decision()` 已接入；`plan_tasks()` 保持兼容；支持 confidence、clarification、raw output。 |
| Phase 2：LLM WorkPlan step capability | Done | LLM 模式下 numbered WorkPlan step 初始为 `__planner__`，执行到该 step 时由 planner 单独选择 capability。 |
| Phase 3：Claude Code postflight | Partial | `coder` 已输出 `[JARVIS_POSTFLIGHT]`，包含 `files_modified`、`diff_stat`、commit、branch、worktree 状态；尚未解析 Claude 自报的 `[JARVIS_STRUCT]`。 |
| Phase 4：平台工具注册 | Not started | 飞书消息、飞书文档等 skill 尚未实现。 |
| Phase 5：监控与调优 | Partial | `candidate_tools`、`planner_raw_output`、`work_plan_snapshot` 已进入 audit log 和 JSON/Markdown report；尚未实现缓存和离线分析任务。 |

当前测试结果：

```text
uv run pytest -q
69 passed, 1 skipped
```

### 8.2 后续实施项

**Phase 1：解耦 classify_intent（Done）**
- 移除 `strategize` 中基于 `allowed_tools` 的硬白名单过滤
- 新增候选 capability 召回函数
- 新增执行期 eligibility guard，保留 shell/coder 安全边界
- 验证现有测试用例不挂

**Phase 2：strategize Prompt 升级（Done）**
- 重写 `plan_tasks` prompt，暴露候选工具
- 引入复合意图和 step-by-step 规划
- 输出格式演进为 `PlannerDecision`，承载 confidence、clarification 和 raw output
- LLM WorkPlan step 逐步选择 capability，而不是全部继承主 intent

**Phase 3：Claude Code 约束（Partial）**
- 在 prompt 中增加 coder 使用边界
- `coder` skill 增加 deterministic postflight：`files_modified`、`diff_stat`、commit、branch、worktree 状态
- 后续：如果 Claude Code CLI 支持稳定 JSON 输出，再解析 `[JARVIS_STRUCT]` 作为补充信号
- 统计 coder 调用频率，验证是否过度使用

**Phase 4：平台工具注册（Not started）**
- 实现 `feishu_message`、`feishu_doc` 等 skill
- 注册到 capability_registry
- 编写多步骤编排测试用例

**Phase 5：监控与调优（Partial）**
- 收集 `planner_raw_output` 做离线分析（audit/report 已记录，离线分析任务未实现）
- 根据误调工具的案例微调 prompt
- 优化缓存命中率

---

**核心原则**：jarvis 的价值是**编排**——知道什么时候调 Claude Code 改代码，什么时候调 shell 跑受控命令，什么时候调搜索或飞书。`strategize` 升级后，LLM 成为编排大脑；规则层降级为候选召回和安全资格校验，而不是用单一 intent 白名单束缚 planner。
