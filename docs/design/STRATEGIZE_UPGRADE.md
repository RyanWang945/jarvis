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

将 `strategize` 升级为**端到端 LLM 规划节点**，由它直接根据用户输入、历史上下文、全部可用工具做决策。`classify_intent` 不再限制工具视野，仅保留为轻量快速路由。

同时明确 Claude Code 的定位：它是 jarvis 工具生态中的一个**深度代码执行器**，不是同级大脑。jarvis 负责"什么时候调什么"，Claude Code 负责"复杂多文件代码重构"这一细分场景。

为后续接入公司平台（飞书、飞书文档等）预留扩展位，这些平台能力以标准工具形式注册，由 `strategize` 统一调度。

## 3. 架构变更

### 3.1 图结构简化

```
改造前：
ingest_event -> contextualize -> classify_intent(规则) -> strategize(受限LLM) -> dispatch

改造后：
ingest_event -> contextualize -> [classify_intent(可选/快速路径)] -> strategize(端到端LLM) -> dispatch
```

`classify_intent` 职责降级：

| 场景 | 行为 |
|------|------|
| 输入以 `/` 开头 | 解析为 Slash 命令，直接路由，不走 LLM |
| 输入包含 `--command` 等显式参数 | 规则快速决策，减少 token 消耗 |
| 其他所有请求 | **不做工具过滤**，只将分类结果作为 `observation_label` 写入 state，用于日志和报表 |

### 3.2 strategize 节点升级

**输入**：
- `messages`：完整对话历史
- `instruction`：当前用户指令
- `context`：workdir、git 状态、resource_key 等
- `all_tools`：**全部可用工具描述**（不再过滤）

**LLM Prompt 核心结构**：
```text
You are Jarvis, a planning agent. Based on the user's request and available tools,
output a sequence of tool calls to fulfill the request.

Available tools:
- coder: Deep code refactoring via Claude Code CLI. Use ONLY when task involves
  multi-file edits, complex refactoring, or requires deep codebase understanding.
- shell: Execute shell commands. Use for simple builds, tests, single-file ops.
- search: Web or local search. Use for information gathering.
- feishu_message: Send message to Feishu user/group.
- feishu_doc: Read/write Feishu documents.
- answer_echo: Direct reply when no external action needed.

Rules:
- Break complex requests into atomic steps.
- Prefer specialized tools over general ones (e.g., use shell for "pytest", not coder).
- Use coder only when file editing scope > 1 file or requires semantic understanding.
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
1. `strategize` 调用 `get_jarvis_llm().plan_tasks(...)` 获取规划
2. 校验每个 `capability_name` 是否在注册表中
3. 为每个 step 生成 `Task` + `WorkOrder`，写入 `dispatch_queue`
4. 设置 `next_node` = `dispatch`（正常）或 `blocked`（校验失败）

### 3.3 工具选择策略（当前全量，未来 RAG）

**当前阶段**：jarvis 工具数量极少（coder、shell、search、answer_echo 等），`strategize` 直接将**全部工具描述**放入 prompt 即可。此阶段无需复杂检索，简单直接，延迟最低。

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
| ≤ 15 个 | 全量放入 prompt（当前做法） |
| 15-50 个 | RAG 召回 Top 8 |
| > 50 个 | 层级分类 + RAG 召回 |

此方案作为预留设计，当前版本暂不需要实现，但 `strategize` 的接口应支持传入 "候选工具列表" 而非强制全量，以便未来无缝切换。

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

## 4. 工具层级与 Claude Code 定位

### 4.1 工具分级

| 层级 | 工具示例 | 使用时机 | 归属 |
|------|---------|---------|------|
| **通用轻量工具** | `shell.command`, `shell.test`, `search.web`, `answer.echo` | 单步命令、信息查询、简单回复 | jarvis 自有 worker |
| **平台集成工具** | `feishu_message.send`, `feishu_doc.read`, `feishu_doc.write` | 跨系统交互 | jarvis 自有 worker |
| **深度代码工具** | `coder` (Claude Code CLI) | 多文件重构、复杂语义编辑 | 外包给 Claude Code |

### 4.2 Claude Code 调用原则

`strategize` 的 prompt 中必须明确约束：

```
Use coder ONLY when:
- Task requires editing 2+ files
- Task requires understanding cross-file relationships
- Task is a complex refactoring (rename, extract interface, etc.)
- User explicitly asks for "deep refactor" or "help me redesign"

Use shell instead when:
- Running tests, lint, build
- Single file read/write with clear scope
- Git operations that don't need semantic understanding
```

**目的**：防止"写个 hello world"也走 Claude Code，造成成本和延迟浪费。

### 4.3 可观测性增强

Claude Code 当前输出是纯文本 stdout。要求 `coder` skill 在调用时追加结构化输出参数：

```python
command = provider_command + [
    "--print",
    "--output-format", "json",  # 如果 Claude Code 支持；否则 prompt 内要求
    # ... existing args
]
```

同时在 prompt 中要求 Claude Code 在回复末尾附加：

```json
[JARVIS_STRUCT]
{
  "files_modified": ["src/main.py", "src/utils.py"],
  "tests_run": "pytest tests/",
  "test_result": "passed",
  "commit_created": "abc1234"
}
```

`aggregate` 节点解析该 JSON，替代当前的纯文本解析。

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
| `intent` | 保留但仅作标签，不参与路由 |
| `allowed_tools` | **废弃**，strategize 直接读取全部工具 |
| `plan_steps` | 由 `strategize` LLM 直接生成，不再由 `classify_intent` 产出 |

新增：

| 字段 | 说明 |
|------|------|
| `planner_raw_output` | LLM 原始 JSON，用于调试和审计 |
| `observation_intent` | `classify_intent` 的输出标签（只读） |

## 7. 降级与容错

| 故障场景 | 策略 |
|----------|------|
| LLM 规划超时 | 返回 `blocked`，提示用户重试 |
| LLM 输出非法 JSON | 捕获异常，最多重试 2 次，仍失败则 `blocked` |
| 选了不存在工具 | `strategize` 内部校验失败，返回 `blocked` |
| 缓存命中但工具已更新 | 缓存 key 包含工具 schema 版本号，自动失效 |
| 飞书等平台服务不可用 | Worker 返回失败，`aggregate` 评估是否重试或 `blocked` |

## 8. 实施路径

**Phase 1：解耦 classify_intent**
- 移除 `allowed_tools` 过滤逻辑
- `classify_intent` 仅保留 Slash 命令和显式参数路由
- 验证现有测试用例不挂

**Phase 2：strategize Prompt 升级**
- 重写 `plan_tasks` prompt，暴露全部工具
- 引入复合意图和 step-by-step 规划
- 输出格式改为结构化 JSON

**Phase 3：Claude Code 约束**
- 在 prompt 中增加 coder 使用边界
- `coder` skill 增加结构化输出要求
- 统计 coder 调用频率，验证是否过度使用

**Phase 4：平台工具注册**
- 实现 `feishu_message`、`feishu_doc` 等 skill
- 注册到 capability_registry
- 编写多步骤编排测试用例

**Phase 5：监控与调优**
- 收集 `planner_raw_output` 做离线分析
- 根据误调工具的案例微调 prompt
- 优化缓存命中率

---

**核心原则**：jarvis 的价值是**编排**——知道什么时候调 Claude Code 改代码，什么时候调 shell 跑命令，什么时候调飞书发消息。`strategize` 升级后，LLM 成为这个编排大脑，而不再是受规则束缚的有限规划器。
