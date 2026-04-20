# Jarvis Tool Routing 设计文档

版本：v2.1
状态：方案设计
日期：2026-04-21
范围：`jarvis-cli` / API 输入到 `WorkOrder` 的工具选择、代码任务路由、Shell 安全边界和失败重规划

## 1. 背景

当前 Jarvis 已具备 CA Agent + Worker 架构，主要执行能力包括：

- `run_shell_command`：执行本地 shell 命令。
- `run_tests`：执行项目测试命令。
- `delegate_to_claude_code`：委托 Claude Code 处理仓库级代码修改任务。
- `tavily_search`：执行搜索并返回结构化结果。

在真实 `jarvis-cli` 测试中，用户输入：

```powershell
uv run jarvis-cli run "在nltk项目中写一个快排的脚本，用python就可以" --workdir E:\pythonProject\jarvis\data\workspaces\nltk --resource-key nltk-live-test
```

期望行为：

- 这是一个仓库内代码创建任务，应交给 `delegate_to_claude_code`。
- 因 coder worker 是高风险执行能力，应进入审批。
- 审批后 Claude Code 在 `workdir` 中创建 Python 脚本，可选验证、提交、推送。

实际行为：

- LLM planner 选择了 `run_shell_command`。
- planner 生成了 Unix shell 命令 `pwd && ls -la`。
- Windows 下 `ShellSkill` 使用 `subprocess.run(..., shell=True)`，默认由 `cmd.exe` 执行，导致 `pwd` 不可识别。
- 任务直接 `blocked`，没有自动切换到 coder。

这个问题说明：**工具选择不能完全交给 LLM 自由决定**。Jarvis 需要在 LLM planner 前后增加确定性路由、工具可用性过滤和错误重规划策略。

## 2. 设计目标

1. 对仓库级代码修改任务，稳定路由到 `delegate_to_claude_code`。
2. 对搜索总结任务，稳定路由到 `tavily_search`。
3. 对显式命令执行任务，才允许使用 `run_shell_command`。
4. 对 Windows shell 语法错误和工具误选，支持自动重规划或给出明确诊断。
5. 不引入通过 CLI 参数强制指定 worker 的设计；目标是让 Agent 自动准确选择工具。
6. 保留现有 LangGraph interrupt / approval / checkpoint / business DB 机制。
7. 为真实测试仓库 `git@github.com:RyanWang945/nltk.git` 建立 live integration 测试路径。

## 3. 外部方案参考

### 3.1 LangGraph / DeepAgents

LangGraph 和 DeepAgents 的 human-in-the-loop 方案强调：

- 对敏感工具调用配置 `interrupt_on`。
- 工具调用命中策略后暂停，由用户 approve / edit / reject。
- 必须依赖 checkpointer 持久化中断状态。
- 多个 tool call 的审批决策需要按顺序匹配。

对 Jarvis 的启发：

- 高风险 worker 不应执行后再补救，必须在 dispatch 前审批。
- 审批对象应是结构化 `WorkOrder`，而不是自然语言描述。
- 后续可支持 edit approval：用户可以修改 tool args 后继续。

### 3.2 LangGraph Supervisor / Handoff

LangGraph supervisor 模式将中央 agent 约束为“只分派，不自己做事”。不同 specialist agent 负责不同能力，例如 research、math、coding。

对 Jarvis 的启发：

- CA Agent 应成为 supervisor，而不是直接暴露所有底层工具给单个 planner。
- `coder`、`search`、`shell` 应被视为 specialist worker。
- supervisor 的第一步是选择 worker 类型，而不是直接生成任意 tool call。

### 3.3 OpenAI Agents SDK

OpenAI Agents SDK 将 handoffs 和 guardrails 作为一等概念：

- handoff 让任务交给 specialist。
- tool guardrails 在每次工具调用前后执行。
- 如果 workflow 中包含 manager / handoff，不应只依赖 agent-level guardrails。

对 Jarvis 的启发：

- `ToolSpec` 需要增加可机读的 policy metadata。
- 工具执行前应有统一 `ToolPolicy` / `WorkOrderPolicy`，而不是散落在 prompt 中。
- 对 shell/coder/search 应分别配置输入 guardrail 和输出 guardrail。

### 3.4 Claude Code

Claude Code 的权限系统包含 permission modes、allow/deny/ask rules、hooks 和 runtime callback。

关键点：

- `bypassPermissions` 会批准未被 deny/hook 拦截的工具调用，不能仅靠 `allowedTools` 限制能力。
- hooks 可在工具执行前确定性 allow / deny / ask。
- `acceptEdits` 更适合受控的文件编辑场景。
- `plan` mode 可用于先让 Claude 规划，暂不执行。

对 Jarvis 的启发：

- Coder worker 不应长期依赖 `bypassPermissions` 作为默认模式。
- 应引入 project-local Claude hooks 或 deny rules，限制工作目录外写入、危险 Bash、未经请求的 push。
- 后续可以拆成 `coder_plan` 和 `coder_execute` 两阶段。

### 3.5 SWE-agent

SWE-agent 的核心经验是 Agent-Computer Interface（ACI）：不要让 coding agent 面对裸 shell，而是提供适合 LLM 的文件查看、搜索、编辑、lint、测试反馈。

对 Jarvis 的启发：

- 外层 planner 不应尝试用 shell 完成代码编辑。
- 仓库级任务应交给专门 coder worker。
- coder worker 内部应拥有更适合代码任务的 ACI 和验证反馈。
- 空输出、错误输出、测试结果应结构化返回，便于 CA Agent 判断。

### 3.6 AutoGPT

AutoGPT Forge / Platform 采用 components / blocks 组合能力。每个组件或 block 有明确输入输出和职责。

对 Jarvis 的启发：

- Jarvis 的 Skill / Worker 应按能力模块组合。
- 复杂任务应编排多个 worker block，而不是让 LLM 一次性自由选择所有底层工具。

## 4. 当前问题拆解

### 4.1 LLM Planner 误选低风险 Shell

当前 `JarvisLLM.plan_tasks()` 的 system prompt 包含：

```text
Prefer the lowest-risk tool that can complete the task
```

这会让模型倾向选择 `run_shell_command`，即使任务本质是代码修改。

问题：

- 低风险优先是安全策略，不是任务能力策略。
- shell 低风险不代表适合代码修改。
- planner 看到所有 exposed tools，缺少 tool eligibility 限制。

### 4.2 Shell Tool 职责过宽

`run_shell_command` 当前被 LLM 视为通用本地执行入口。模型可自己生成探索命令。

问题：

- 对“写脚本/改文件/新增 feature”任务，shell 容易生成平台不兼容命令。
- `ShellSkill` 使用 Windows `cmd.exe` 时，Unix 命令会失败。
- shell 失败后没有区分“命令本身失败”和“工具选择错误”。

### 4.3 Coder Worker 缺少稳定自动路由

当前只能通过 LLM planner 选择 `delegate_to_claude_code`。用户没有稳定的 CLI 方式指定“这就是代码任务”。

问题：

- 复杂任务测试不稳定。
- 用户必须在 prompt 里写“必须使用 delegate_to_claude_code”。
- Agent 没有先判断任务类型，导致代码任务仍可能误走 shell。

### 4.4 失败后 Replan 不够语义化

Shell 失败后，如果 stderr 是：

```text
'pwd' 不是内部或外部命令
```

系统应识别为 Windows shell 语法错误和工具误选。但当前直接 blocked。

## 5. 目标架构

```text
User / CLI / API
    |
    v
AgentEvent
    |
    v
Intent Router
    |
    +-- code_write  ---------> Coder Planner / delegate_to_claude_code
    +-- search_summary ------> Search Planner / tavily_search
    +-- explicit_shell ------> Shell Planner / run_shell_command
    +-- test_only -----------> Test Planner / run_tests
    +-- simple_chat ---------> Echo
    |
    v
Tool Eligibility Filter
    |
    v
WorkOrder Builder
    |
    v
Risk Gate / Approval
    |
    v
Worker
    |
    v
Completion Assessment
    |
    v
Wrong-tool Replan / Final Summary
```

核心原则：

1. **先判断任务类型，再暴露工具**。
2. **Shell 默认只执行显式命令**。
3. **代码修改任务默认走 coder**。
4. **搜索总结任务默认走 search**。
5. **LLM 可以参与规划，但不能越过 eligibility policy**。

## 6. 任务意图模型

新增内部类型：

```python
IntentKind = Literal[
    "code_write",
    "code_review",
    "search_summary",
    "explicit_shell",
    "test_only",
    "simple_chat",
    "unknown",
]
```

新增结构：

```python
class IntentDecision(TypedDict):
    kind: IntentKind
    confidence: float
    confidence_source: Literal["rule", "llm", "fallback"]
    reason: str
    allowed_tools: list[str]
    requires_workdir: bool
    plan_steps: list[dict[str, Any]]
```

`plan_steps` 用于表达复合任务。例如“搜索 LangGraph 最新用法，然后在项目里实现 demo”不应被压成单一 intent，而应拆成：

```json
[
  {"kind": "search_summary", "allowed_tools": ["tavily_search"]},
  {"kind": "code_write", "allowed_tools": ["delegate_to_claude_code"]}
]
```

MVP 可先只执行单 step；多 step 编排作为后续扩展，但数据结构需要提前留出口。

## 7. Hybrid Intent Router

### 7.1 输入

- `instruction`
- `command`
- `verification_cmd`
- `workdir`
- `resource_key`
- 历史失败上下文，例如前一次 worker 类型、stderr、failure kind

### 7.2 规则优先级

1. 用户显式 `--command` 优先进入 `explicit_shell`。
2. 有 `workdir` 且高置信度命中代码修改模式，进入 `code_write`。
3. 高置信度命中搜索总结模式，进入 `search_summary`。
4. 高置信度命中测试模式且无代码修改信号，进入 `test_only`。
5. 低置信度或多意图任务，进入轻量 LLM intent classifier。
6. LLM classifier 仍低置信度时进入 `unknown`，但只暴露安全工具子集。

### 7.3 双轨分类策略

Intent Router 不应是纯关键词规则。关键词只处理高置信度 case，避免把“查一下为什么 test 失败”误判成纯搜索，或把“fix 一下这个命令参数”误判成代码修改。

规则分类返回：

```python
IntentDecision(kind="code_write", confidence=0.95, confidence_source="rule", ...)
```

如果规则分类 confidence 低于阈值，例如 `0.8`，调用低成本 LLM intent classifier。该 classifier 只输出结构化 intent，不直接生成工具调用。

LLM classifier 输入：

```json
{
  "instruction": "...",
  "workdir": "...",
  "has_explicit_command": false,
  "candidate_intents": ["code_write", "search_summary", "test_only", "explicit_shell", "simple_chat"],
  "response_schema": {
    "kind": "one candidate intent",
    "confidence": "0.0-1.0",
    "reason": "short reason",
    "plan_steps": []
  }
}
```

LLM classifier 输出仍需经过 Tool Eligibility Filter，不能直接选择任意工具。

### 7.4 代码修改高置信度模式

中文：

- 写
- 新增
- 修改
- 实现
- 修复
- 生成
- 创建
- 脚本
- 代码
- 文件
- feature
- 提交
- 推送
- commit
- push

英文：

- add
- create
- modify
- implement
- fix
- script
- feature
- file
- code
- commit
- push

注意：单个关键词不应直接触发高置信度。建议规则是：

- `workdir` 存在。
- 命中代码修改动作词，例如“写/新增/修改/实现/创建/add/create/implement”。
- 同时命中代码对象词，例如“脚本/代码/文件/feature/script/file/code”。

示例：

- “写一个快排脚本” -> 高置信度 `code_write`。
- “查一下为什么 test 失败” -> 低置信度，进入 LLM intent classifier。
- “fix 这个命令参数” -> 低置信度，进入 LLM intent classifier。

### 7.5 搜索总结高置信度模式

中文：

- 搜索
- 查一下
- 查找
- 调研
- 总结
- 带来源
- 引用

英文：

- search
- research
- summarize
- sources
- citations
- latest

注意：搜索 intent 与代码诊断 intent 有重叠。若 instruction 同时包含 `workdir` 和“test 失败/报错/bug/代码”，不应直接判为 `search_summary`。

### 7.6 示例

输入：

```text
在nltk项目中写一个快排的脚本，用python就可以
```

上下文：

```text
workdir=E:\pythonProject\jarvis\data\workspaces\nltk
```

输出：

```json
{
  "kind": "code_write",
  "confidence": 0.95,
  "reason": "instruction asks to write a Python script inside a repository",
  "allowed_tools": ["delegate_to_claude_code"],
  "requires_workdir": true
}
```

## 8. Tool Eligibility Matrix

| Intent | Allowed Tools | 默认 Worker | 说明 |
| --- | --- | --- | --- |
| `code_write` | `delegate_to_claude_code` | `coder` | 新增/修改文件、实现 feature、提交推送 |
| `code_review` | `delegate_to_claude_code` 或未来 readonly coder | `coder` | 代码审查、定位问题，可先 plan mode |
| `search_summary` | `tavily_search` | `tavily-search` | 搜索、调研、总结、带来源 |
| `explicit_shell` | `run_shell_command` | `shell` | 用户显式提供命令 |
| `test_only` | `run_tests` | `shell` | 跑测试命令，不编辑文件 |
| `simple_chat` | `echo` | `echo` | 无副作用回应 |
| `unknown` | 当前 exposed tools 子集 | 由 LLM 决定 | 需记录低置信度 |

原则：

- `run_shell_command` 不应出现在 `code_write` 的 allowed tools 中。
- `delegate_to_claude_code` 不应出现在简单搜索任务中。
- `tavily_search` 不应执行本地文件操作。

## 9. CLI 设计

### 9.1 保持自然语言入口

```powershell
uv run jarvis-cli run "在 nltk 项目中写一个快排脚本" `
  --workdir E:\pythonProject\jarvis\data\workspaces\nltk `
  --resource-key nltk-live-quicksort
```

行为：

- CLI 不提供由用户直接指定 worker 或 tool 的子命令/标志。
- Agent 必须通过 `classify_intent` 自动识别这是 `code_write`。
- `Tool Eligibility Filter` 只向 planner 暴露 `delegate_to_claude_code`。
- 仍进入风险审批。

### 9.2 显式命令仍使用 `--command`

```powershell
uv run jarvis-cli run "查看当前目录" `
  --workdir E:\pythonProject\jarvis\data\workspaces\nltk `
  --command "dir"
```

行为：

- 只有用户显式传 `--command` 时进入 `explicit_shell`。
- Agent 不应自行生成 shell 命令完成代码修改任务。

### 9.3 搜索总结自然路由

```powershell
uv run jarvis-cli run "搜索 LangGraph interrupt/resume 并总结，带来源"
```

行为：

- Agent 自动识别为 `search_summary`。
- `Tool Eligibility Filter` 只向 planner 暴露 `tavily_search`。
- final answer synthesis 仍执行 untrusted data 防护。

### 9.4 不新增强制 Worker 参数

本轮迭代不设计任何由用户参数直接指定 worker 或 tool 的接口。

原因：

- 目标是让 Agent 准确理解任务并自动选择工具。
- 强制参数会绕过工具路由问题，降低对 planner 和 router 的真实验证价值。
- `--command` 例外，因为它代表用户显式提供了要执行的 shell 命令，而不是强制指定 worker。

## 10. Planner Prompt 调整

即使有 intent router，planner prompt 仍需调整。

当前问题语句：

```text
Prefer the lowest-risk tool that can complete the task
```

建议改为：

```text
Choose only from the tools provided for this intent. Do not substitute a shell command for repository file edits. Use shell only when the user explicitly provided a command or the tool list contains only shell tools.
```

如果 `allowed_tools=["delegate_to_claude_code"]`，则 prompt 中无需再描述 shell。

## 11. Shell Guardrail

### 11.1 输入限制

`run_shell_command` 仅允许以下来源：

- 用户显式 `--command`
- 内部 verification command
- 未来受控系统命令

不允许：

- LLM planner 自己生成 shell 命令来探索仓库。
- 用 shell 处理代码文件创建、编辑、提交、推送。

### 11.2 Windows 语法保护

Shell Guardrail 优先前移到 `strategize` 或 `validate_work_order` 阶段，而不是等 `ShellSkill.run()` 执行失败后再处理。

如果 planner 生成了 shell tool call，需要先判断来源：

- 来源是用户显式 `--command`：允许进入 shell，并记录 audit。
- 来源是 verification command：允许进入 shell，但仍做风险分级。
- 来源是 LLM 自行生成，且原始 intent 是 `code_write`：拒绝该 tool call，改为 coder route。

对于 Windows 平台，Unix 命令黑名单只能作为诊断补充，不作为主要防线。典型模式包括：

- `pwd`
- `ls -la`
- `cat `
- `rm -rf`
- `touch `
- `grep `
- `mkdir -p`
- `cp `
- `mv `

示例诊断：

```text
The planner generated a Unix shell command for a Windows shell. This task appears to be a repository editing task, so Jarvis will replan with the coder worker.
```

### 11.3 Shell 执行环境策略

可选方案：

1. 保持 `cmd.exe`，但加强 prompt 和 guardrail。
2. 改为 PowerShell 作为默认 shell。
3. 将 shell command 拆成 `shell_kind=cmd|powershell|bash`。
4. 对常见只读命令做平台映射，例如 `pwd -> cd`、`ls -la -> dir`。

推荐阶段 1：不做大规模 shell 迁移，不维护庞大黑名单；先严格 enforce “shell 只执行用户显式命令或内部 verification”。平台映射仅用于未来只读诊断场景。

## 12. Wrong-tool Replan

新增失败分类：

```python
FailureKind = Literal[
    "wrong_tool",
    "platform_shell_mismatch",
    "command_failed",
    "verification_failed",
    "worker_error",
    "approval_rejected",
]
```

识别规则：

- 当前 worker 是 `shell`。
- 原始 intent 是 `code_write` 或指令命中代码修改关键词。
- stderr 包含 `not recognized` / `不是内部或外部命令`。

处理：

1. 将当前 task 标记为 `cancelled` 或 `failed`。
2. 写入 `last_error` 和 replan context。
3. 生成新的 `delegate_to_claude_code` WorkOrder。
4. 进入 approval。

## 13. Coder Worker 权限策略

当前 `CoderSkill` 使用：

```text
--permission-mode bypassPermissions
--allowedTools Read,Write,Edit,MultiEdit,Bash(git:*),...
```

风险：

- Claude Code 文档指出 `allowedTools` 不限制 `bypassPermissions`，未被 deny/hook 拦截的工具仍会被批准。
- 即使对测试仓库，也应有最低安全基线，避免把测试路径变成默认不受控路径。

建议阶段：

| 阶段 | 策略 | 说明 |
| --- | --- | --- |
| S1 | `bypassPermissions` + 最低 deny rules / hooks | 保持 live test 可跑通，同时阻止工作目录外写入和明显危险 Bash |
| S2 | 项目级 `.claude/settings.local.json` 策略模板 | 区分测试仓库、普通本地仓库和敏感仓库 |
| S3 | 默认切换 `acceptEdits` | 文件编辑自动批准，其他操作仍受控 |
| S4 | 引入 `coder_plan` / `coder_execute` | 先 plan，审批后执行 |

## 14. 数据结构变更

### 14.1 AgentEvent payload

本轮不新增强制工具字段，也不通过 CLI 参数强制 worker。可选新增字段仅用于记录路由结果或上游系统提示，不作为强制执行依据：

```python
intent_hint: str | None  # optional, advisory only
```

`intent_hint` 只能来自可信系统入口或未来上游 dispatcher，不作为用户 CLI 强制 worker 的接口。

### 14.2 AgentState

新增字段：

```python
intent: dict[str, Any] | None
allowed_tools: list[str]
plan_steps: list[dict[str, Any]]
failure_kind: str | None
```

### 14.3 ToolSpec

建议新增 metadata：

```python
intent_kinds: list[str]
requires_explicit_user_command: bool = False
can_modify_files: bool = False
requires_workdir: bool = False
platform_constraints: list[str] = []
```

示例：

```python
ToolSpec(
    name="delegate_to_claude_code",
    intent_kinds=["code_write", "code_review"],
    can_modify_files=True,
    requires_workdir=True,
)
```

## 15. 节点变化

### 15.1 新增 `classify_intent`

位置：

```text
ingest_event -> contextualize -> classify_intent -> strategize
```

职责：

- 读取 payload、workdir、历史失败上下文。
- 执行高置信度规则分类。
- 对低置信度 case 调用轻量 LLM intent classifier。
- 输出 `intent` 和 `allowed_tools`。
- 写入 audit logs，记录 `kind`、`confidence`、`confidence_source`、`reason`。

### 15.2 修改 `strategize`

职责变化：

- 只向 LLM 提供 `allowed_tools` 子集。
- 如果 LLM 返回不在 allowed_tools 中的工具，拒绝并 fallback 到默认工具。
- `planner_type=rule_based` 也必须先经过 `classify_intent`，再根据 intent 构造 ToolCallPlan。
- `ToolRegistry.list()` 增加 `intent_kinds` 过滤参数，避免在 `strategize` 中手写过滤逻辑。

### 15.3 修改 `aggregate`

职责变化：

- 识别 wrong-tool / platform-shell-mismatch。
- 可重规划时生成 coder WorkOrder 或回到 `strategize`。

## 16. 实施计划

### Phase A：Intent Router + Tool Eligibility 骨架（P0）

目标：

- 新增 `classify_intent` 节点。
- 新增 `IntentDecision` 数据结构。
- 扩展 `ToolSpec` intent metadata。
- 扩展 `ToolRegistry.list(intent_kinds=...)`。

验收：

- 用户输入“写快排脚本”自动识别为 `code_write`。
- `strategize` 只向 planner 暴露 `delegate_to_claude_code`。
- intent 决策写入 audit。

### Phase B：代码修改任务自动路由（P0）

目标：

- `workdir + 写脚本` 自动进入 `code_write`。
- 自动只暴露 `delegate_to_claude_code`。

验收：

- 原始失败命令不加任何“必须使用 coder”提示，也能规划为 coder。

测试命令：

```powershell
uv run jarvis-cli run "在nltk项目中写一个快排的脚本，用python就可以" --workdir E:\pythonProject\jarvis\data\workspaces\nltk --resource-key nltk-live-test
```

期望：

- 返回 `waiting_approval`。
- `tasks[0].tool_name == "delegate_to_claude_code"`。

### Phase C：Tool Eligibility Filter（P0）

目标：

- planner 只能选择 allowed tools。
- LLM 返回非法工具时有审计日志。

验收：

- 对 `code_write`，LLM 即使返回 `run_shell_command`，系统也会改写或拒绝。

### Phase D：低置信度 LLM Intent Classifier + 复合任务结构（P1）

目标：

- 关键词规则低置信度时调用轻量 LLM classifier。
- `IntentDecision.plan_steps` 能表达复合任务。

验收：

- “查一下为什么 test 失败”不会被简单判为搜索。
- “搜索资料，然后在项目中实现 demo”能至少产出两个 plan steps，即使 MVP 暂只执行第一个或明确 blocked。

### Phase E：Shell Guardrail + Wrong-tool Replan（P1）

目标：

- Shell 不再处理代码修改任务。
- LLM 自行生成的 shell 代码修改命令在 strategize 阶段被拒绝。
- Windows Unix 命令失败后可自动 replan 或给出明确诊断。

验收：

- `pwd && ls -la` 不直接 blocked。
- 系统生成诊断并转入 coder approval。

### Phase F：Claude 权限最低安全基线（P1）

目标：

- 测试仓库继续允许 live push。
- S1 阶段也默认写入最低限度 `.claude/settings.local.json` deny rules 或等价 hooks。
- 默认仓库启用更严格权限，测试仓库可配置放宽。

验收：

- 工作目录外写入被拒绝。
- 未请求 push 时 push 被拒绝。
- 请求 push 时仍走 Jarvis approval。

## 17. 测试策略

### 17.1 单元测试

- `classify_intent("写一个快排脚本", workdir=...) == code_write`
- `classify_intent("搜索 LangGraph interrupt", workdir=None) == search_summary`
- `allowed_tools(code_write) == ["delegate_to_claude_code"]`
- shell guardrail 拒绝 Windows Unix 命令。

### 17.2 CLI 集成测试（mock Claude）

- `jarvis-cli run "写一个快排脚本" --workdir ...` 返回 `waiting_approval`。
- 输出 task 使用 `delegate_to_claude_code` / `coder`。
- approve 后 mock coder 创建文件。
- `status` 返回 work_results 和 artifacts。

### 17.3 Live 集成测试（真实 Claude）

使用真实测试仓库：

```text
E:\pythonProject\jarvis\data\workspaces\nltk
git@github.com:RyanWang945/nltk.git
```

建议加环境开关：

```powershell
$env:JARVIS_RUN_LIVE_INTEGRATION="1"
```

测试命令：

```powershell
uv run jarvis-cli run "在nltk项目中写一个快排的脚本，用python就可以；提交，commit message 使用 feat: add quicksort script；然后 push 到 origin main。" --workdir E:\pythonProject\jarvis\data\workspaces\nltk --resource-key nltk-live-quicksort
```

验收：

- 返回 `waiting_approval`。
- approve 后完成。
- `git log -1` 有指定 commit message。
- `git status --short --branch` 显示 `main...origin/main` 同步。
- GitHub 远端可看到更新。

## 18. 风险与取舍

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 关键词路由误判 | 简单任务被送到 coder | 高置信度规则 + 低置信度 LLM intent classifier |
| Coder worker 高风险审批变多 | 用户多一步 approve | 对代码修改这是预期安全边界 |
| 复合任务表达不足 | 搜索+编码任务被压成单 worker | `plan_steps` 先建模，后续执行多阶段 |
| Tool eligibility 过窄 | LLM 无法组合复杂任务 | 通过 plan_steps 分阶段暴露工具 |
| Shell guardrail 误拦截 | 某些合法 shell 被拒绝 | 用户显式 `--command` 仍允许；LLM 自生成 shell 受限 |
| Live test 污染远端 | 测试仓库提交变多 | 只对专用 `nltk` 测试仓库启用 |
| Claude permissions 过宽 | 可能越界执行 | S1 即加入最低 deny rules / hooks，后续 acceptEdits |

## 19. 推荐落地顺序

1. 新增 `classify_intent`，先覆盖代码修改和搜索总结。
2. 扩展 `ToolSpec` metadata 和 `ToolRegistry.list(intent_kinds=...)`。
3. 在 `strategize` 中按 `allowed_tools` 过滤工具列表。
4. 加测试复现用户原始命令，确保自动走 coder。
5. 加低置信度 LLM intent classifier。
6. 加 shell guardrail 和 wrong-tool replan。
7. 建立 Claude Code 最低权限基线。
8. 将 live integration 测试纳入手动测试脚本或 pytest marker。

## 20. 完成定义

本轮迭代完成时，应满足：

1. 用户运行原始命令：

```powershell
uv run jarvis-cli run "在nltk项目中写一个快排的脚本，用python就可以" --workdir E:\pythonProject\jarvis\data\workspaces\nltk --resource-key nltk-live-test
```

不会再规划为 `run_shell_command: pwd && ls -la`。

2. 系统返回：

```json
{
  "status": "waiting_approval",
  "tasks": [
    {
      "tool_name": "delegate_to_claude_code",
      "worker_type": "coder"
    }
  ]
}
```

3. 用户 approve 后，Claude Code 在真实 `nltk` 测试仓库中完成文件创建。

4. 若任务要求 commit/push，则远端 `git@github.com:RyanWang945/nltk.git` 可看到提交。

5. 搜索总结类任务仍稳定走 `tavily_search`，不会误走 coder 或 shell。
