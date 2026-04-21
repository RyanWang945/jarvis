# Jarvis Worker Capability 设计文档（2026-04-21）

版本：v0.2
状态：方案设计
日期：2026-04-21
范围：Jarvis 顶层能力模型、Worker 边界、Skill 加载收缩、Claude Code skill 注入、Shell/cmd 职责降级

## 1. 背景

当前 Jarvis 已形成 CA Agent + Worker 架构，并已落地外部 Skill 加载、ToolSpec 注册、Intent Router 和 Claude Code coder worker。最新问题是：`Skill`、`Tool`、`Worker` 三个概念的边界开始重叠。

典型现象：

- Jarvis 自己加载 Skill 包、动态 import Python、注册 ToolSpec。
- Claude Code 本身也是一个能读上下文、调用工具、编辑代码、运行命令的 coding agent。
- 代码任务既可以被 Jarvis 误判成 shell，也可以被 Claude Code 更完整地处理。
- 外部 Skill 加载既承担“Jarvis 顶层能力扩展”，又承担“代码任务上下文注入”，职责过宽。

因此需要重新定义：Jarvis 不应成为第二个 Claude Code，也不应默认维护一个过宽的 Python 插件运行时。Jarvis 的核心定位应是 supervisor + policy runtime；Claude Code 承担 coding agent runtime。

## 2. 设计结论

核心结论：

1. Jarvis 顶层加载的不是任意 `Skill`，而是少量稳定的 `WorkerCapability`。
2. 代码相关 skill 默认注入给 Claude Code，而不是注册成 Jarvis 顶层可执行工具。
3. `cmd` / shell 不是删除，而是降级为显式命令和内部验证 primitive。
4. `delegate_to_claude_code` 应从普通 Skill 升级为一级 Coder Worker 能力。
5. 外部 Python Skill 动态 import 应降级为兼容层或 experimental adapter，不作为主扩展路径。

目标架构：

```text
User / CLI / API
    |
    v
Jarvis CA Agent
    |
    +-- Intent Router
    +-- Policy / Eligibility
    +-- Approval / Audit / Resource Lock
    +-- Recovery / Report
    |
    v
WorkerCapability
    |
    +-- CoderWorker(provider=claude_code)
    +-- SearchWorker(provider=tavily)
    +-- ShellWorker(explicit_command_only)
    +-- Future: ObsidianWorker / ImageWorker / BrowserWorker
```

## 3. 当前重复与冗余

### 3.1 `SkillRegistry` 与 `ToolRegistry` 双注册

当前外部包需要同时声明：

- Skill 实现。
- ToolSpec 元数据。
- ToolSpec 到 worker_type / skill 的绑定。

这让注册链变长，也让 policy metadata 容易遗漏。后续应把 Jarvis 顶层调度对象收敛为 `WorkerCapability`，由 capability 声明 intent、risk、输入 schema 和执行边界。

`ToolSpec` 可以继续存在，但它应是兼容层或 LLM function-call 表达，不应成为系统的核心能力模型。

### 3.2 外部 Python Skill 动态 import 过重

动态 import 适合本地可信扩展，但不适合作为默认能力生态：

- 它把第三方代码加载进 Jarvis 主进程。
- 它需要处理重复注册、权限声明、沙箱、版本、热重载。
- 它和 Claude Code 的 skill/context 机制重叠。

后续默认扩展路径应优先考虑：

- 内置 worker provider，例如 Tavily provider。
- 子进程 CLI adapter。
- HTTP adapter。
- MCP adapter。

动态 Python Skill 保留为 experimental / trusted-local 机制。

### 3.3 `tavily_search` 不应长期是外部 Skill 包

搜索是 Jarvis 的基础能力，不需要通过通用 Python Skill 包绕一层。更清晰的设计是：

```text
SearchWorker
    provider: tavily
    input: query / max_results / include_answer
    output: structured SearchResult
```

这样 intent router 只需要选择 `search` capability，provider 配置由 settings 决定。

### 3.4 Claude Code 不应只是普通 Skill

代码修改、代码审查、repo 调研、commit、push 是 Jarvis 的核心工作流。Claude Code worker 应是一等能力：

```text
CoderWorker
    provider: claude_code
    modes:
      - plan
      - execute
    policy:
      - approval_required_for_write
      - approval_required_for_commit_push
      - deny_workdir_escape
      - deny_unrequested_push
    context:
      - coder skills
      - repo conventions
      - hooks
      - verification policy
```

Jarvis 负责是否交给 CoderWorker、交给它什么上下文、是否需要审批；Claude Code 负责具体代码执行。

## 4. 新概念模型

### 4.1 WorkerCapability

`WorkerCapability` 是 Jarvis 顶层调度单位。

建议字段：

```python
class WorkerCapability(BaseModel):
    name: str
    aliases: list[str] = []
    worker_type: str
    provider: str | None = None
    action: str
    description: str
    intents: list[IntentKind]
    input_schema: dict[str, Any]
    risk_level: RiskLevel
    requires_approval: bool = False
    requires_workdir: bool = False
    explicit_only: bool = False
    can_modify_files: bool = False
```

示例：

```yaml
name: coder.claude_code
aliases:
  - delegate_to_claude_code
worker_type: coder
provider: claude_code
action: execute
description: Repository coding, review, tests, commit, and push workflows.
intents:
  - code_write
  - code_review
risk_level: high
requires_approval: true
requires_workdir: true
can_modify_files: true
```

字段语义：

- `name` 是 Jarvis 顶层调度 ID，必须稳定持久化，例如 `coder.claude_code`。
- `worker_type` 是执行器类别，例如 `coder`、`search`、`shell`。
- `provider` 是同类 worker 的后端，例如 `claude_code`、`tavily`。
- `action` 是 provider 内部动作，例如 `plan`、`execute`、`search`、`run`。
- `input_schema` 使用 JSON Schema，直接兼容 OpenAI function-calling / DeepSeek tool schema。
- `aliases` 用于兼容旧 tool name，也可作为 LLM function-calling 的安全名称。

注意：OpenAI-compatible function name 通常不接受点号。`WorkerCapability.name` 可以使用 `coder.claude_code` 这类内部稳定 ID，但暴露给 LLM 的 function name 应使用兼容 alias，例如 `delegate_to_claude_code`。Planner 返回 alias 后，Jarvis 必须解析回 canonical capability name 再进入 WorkOrder。

外部下载的 skill 不需要转成内置代码。它只需要在 `manifest.yaml` 或 `SKILL.md` frontmatter 中声明 Jarvis 适配信息：

```yaml
tools:
  - name: tavily_search
    capability_name: search.tavily
    description: Search the web through Tavily and return titles, URLs, and snippets.
    worker_type: tavily-search
    action: search
    risk_level: low
    exposed_to_llm: true
    intent_kinds:
      - search_summary
```

其中：

- `name` 是 LLM-safe tool alias。
- `capability_name` 是 Jarvis 内部 canonical capability ID。
- `worker_type` / `action` 保留给当前 executor 兼容路径。

### 4.2 CapabilityProvider

`CapabilityProvider` 替代未来的通用 `Skill.run()` 主路径。它是 WorkerCapability 的执行后端。

建议协议：

```python
class CapabilityExecutionContext(BaseModel):
    capability_name: str
    worker_type: str
    provider: str | None
    action: str
    workdir: str | None
    risk_level: RiskLevel
    timeout_seconds: int
    ca_thread_id: str
    task_id: str


class CapabilityProvider(Protocol):
    name: str
    worker_type: str

    def execute(
        self,
        args: dict[str, Any],
        context: CapabilityExecutionContext,
    ) -> WorkResult:
        ...
```

Provider 注册示例：

```text
CapabilityRegistry
    capability: coder.claude_code
        -> provider: ClaudeCodeProvider(worker_type="coder")
    capability: search.tavily
        -> provider: TavilySearchProvider(worker_type="search")
    capability: shell.command
        -> provider: ShellProvider(worker_type="shell")
```

兼容期内，现有 `SkillRegistry` 可以作为 `LegacySkillProvider` 被 CapabilityRegistry 包装：

```text
WorkerCapability(name="legacy.tavily_search", worker_type="tavily-search")
    -> LegacySkillProvider
    -> SkillRegistry.get("tavily-search").run(...)
```

但新能力不再默认走动态 import 的 Skill。

### 4.3 Capability 到 WorkOrder 的映射

现有 `WorkOrder` 只有 `worker_type`，executor 通过 `SkillRegistry.get(order.worker_type)` 路由。这会让 `coder.claude_code` 这类 capability 名称在 dispatch 后丢失。

建议新增字段：

```python
class WorkOrder(BaseModel):
    order_id: str
    task_id: str
    ca_thread_id: str
    capability_name: str
    worker_type: str
    provider: str | None = None
    action: str
    args: dict[str, Any]
    workdir: str | None = None
    risk_level: RiskLevel
    reason: str
    verification_cmd: str | None = None
    verification_policy: VerificationPolicy | None = None
    timeout_seconds: int = 30
```

映射规则：

| Capability | WorkOrder.capability_name | WorkOrder.worker_type | WorkOrder.provider | WorkOrder.action |
| --- | --- | --- | --- | --- |
| `coder.claude_code` | `coder.claude_code` | `coder` | `claude_code` | `execute` |
| `coder.claude_code.plan` | `coder.claude_code.plan` | `coder` | `claude_code` | `plan` |
| `search.tavily` | `search.tavily` | `search` | `tavily` | `search` |
| `shell.command` | `shell.command` | `shell` | `local_shell` | `run` |

执行路由应改为：

```text
WorkOrder.capability_name
    -> CapabilityRegistry.get(capability_name)
    -> provider.execute(args, context)
```

`worker_type` 保留为状态展示、资源调度和粗粒度统计字段，不再作为唯一执行路由 key。

兼容策略：

- Phase A/B 期间可以暂时用 `tool_name == capability_name`。
- 旧 WorkOrder 没有 `capability_name` 时，用 `worker_type + action` 做 best-effort legacy 解析。
- 持久化层新增字段时允许 nullable，避免破坏旧记录读取。

### 4.4 ToolSpec 兼容视图

LLM planner 现在接收 `list[ToolSpec]`，底层需要 function-calling schema。因此 CapabilityRegistry 必须提供兼容视图。

转换规则：

```python
def capability_to_tool_spec(capability: WorkerCapability) -> ToolSpec:
    return ToolSpec(
        name=capability.llm_tool_name,
        capability_name=capability.name,
        description=capability.description,
        args_schema=capability.input_schema,
        skill=capability.provider or capability.worker_type,
        worker_type=capability.worker_type,
        action=capability.action,
        risk_level=capability.risk_level,
        exposed_to_llm=True,
        intent_kinds=capability.intents,
        requires_explicit_user_command=capability.explicit_only,
        can_modify_files=capability.can_modify_files,
        requires_workdir=capability.requires_workdir,
    )
```

注意：

- `WorkerCapability.input_schema` 必须保持 JSON Schema，不引入另一套 schema DSL。
- `ToolSpec.skill` 在兼容期只用于 legacy executor，不应继续承载架构语义。
- Planner 可以返回 LLM-safe alias，例如 `delegate_to_claude_code`；Jarvis 在 `strategize()` 中解析为 canonical capability name，例如 `coder.claude_code`。
- WorkOrder、audit、report 中应优先保存 canonical capability name。

### 4.5 Capability 相比 ToolSpec 的新增价值

Capability 不是简单把 `intent_kinds` 改名。它新增的是 policy 和 provider 语义：

- capability name 是稳定业务 ID，可持久化、审计、授权和恢复。
- capability 绑定 provider，解决 `ToolSpec.skill` / `worker_type` 双字段混乱。
- capability 可以声明 intent priority，支持同一 intent 下的默认能力选择。
- capability 可以声明 policy override，例如只读 review、需要 workdir、显式命令限定、是否可修改文件。
- capability 可以生成 ToolSpec 兼容视图，但不被 ToolSpec 反向约束。

建议补充字段：

```python
priority: int = 100
policy: CapabilityPolicy
```

低数字优先。例如 `search.tavily` 对 `search_summary` 的 priority 可以是 10，legacy search adapter 是 90。

### 4.6 CoderSkill

`CoderSkill` 是注入给 CoderWorker 的上下文，不由 Jarvis 直接执行。

示例目录：

```text
data/coder-skills/
  python-project/
    SKILL.md
  jarvis-repo/
    SKILL.md
    hooks/
      pre_tool_use.py
```

示例 manifest：

```yaml
kind: coder_skill
name: python-project-conventions
date: 2026-04-21
applies_to:
  languages:
    - python
  files:
    - pyproject.toml
inject_to:
  - claude_code
content:
  - SKILL.md
hooks:
  - hooks/pre_tool_use.py
```

Claude Code worker 在启动前根据 workdir、语言、repo 标识选择相关 coder skills，并把它们作为 prompt context、filesystem context 或 Claude hooks 注入。

### 4.7 CoderWorker plan / execute 契约

现有 `app/skills/coder.py` 同时承担 instruction 构建、Claude CLI 调用、postflight 收集。一级化后，这些执行逻辑移动到 `ClaudeCodeProvider`，而 `CoderSkill` 只表示可注入上下文。

建议拆成两类 capability：

```text
coder.claude_code.plan
    worker_type: coder
    provider: claude_code
    action: plan
    risk_level: low or medium

coder.claude_code
    worker_type: coder
    provider: claude_code
    action: execute
    risk_level: high
```

`coder_plan` 输出结构：

```python
class CoderPlan(BaseModel):
    summary: str
    proposed_changes: list[str]
    files_to_read: list[str] = []
    files_to_modify: list[str] = []
    verification_commands: list[str] = []
    requires_write: bool
    requires_commit: bool = False
    requires_push: bool = False
    risk_level: RiskLevel
    approval_reason: str
```

LangGraph 集成：

```text
code_review intent
    -> coder.claude_code.plan
    -> summarize or ask follow-up

code_write intent
    -> either direct coder.claude_code execute
    -> or coder.claude_code.plan first when risk/ambiguity is high
    -> approval on CoderPlan / WorkOrder
    -> coder.claude_code execute with approved_plan
```

`coder_execute` 输入结构：

```python
class CoderExecuteArgs(BaseModel):
    instruction: str
    approved_plan: CoderPlan | None = None
    verification_cmd: str | None = None
    allow_commit: bool = False
    allow_push: bool = False
```

短期兼容：

- `delegate_to_claude_code` 继续作为 `coder.claude_code` 的 legacy alias。
- `_rule_based_tool_calls()` 仍可生成 legacy tool name，但 Phase B 后必须改为 capability 查询。
- `CoderSkill.run()` 可以先被 `ClaudeCodeProvider` 包装，后续再迁移文件位置。

### 4.8 Provider 类型

Provider 可以是内置 Python 类、HTTP client、CLI subprocess 或 MCP client。它不需要进入通用 Skill 动态 import 链。

示例：

- `SearchWorker(provider=tavily)`
- `CoderWorker(provider=claude_code)`
- `ImageWorker(provider=openai_images)`

## 5. 路由与执行边界

新的 intent 到 worker 映射：

| Intent | WorkerCapability | 说明 |
| --- | --- | --- |
| `code_write` | `coder.claude_code` | 修改文件、实现 feature、修 bug、提交、推送 |
| `code_review` | `coder.claude_code` | 审查 diff、定位问题、只读 repo 调研；后续可用 plan mode |
| `search_summary` | `search.tavily` | 外部信息搜索、带来源总结 |
| `explicit_shell` | `shell.command` | 用户显式提供命令时执行 |
| `test_only` | `shell.test` 或 `coder.claude_code` | 配置化验证命令；代码上下文复杂时交给 coder |
| `simple_chat` | `answer.echo` | 无外部副作用的简单响应 |

原则：

1. Intent Router 选择 capability，不选择底层 shell 命令。
2. Planner 只看到当前 intent 允许的 capability。
3. Strategize 必须二次校验 capability eligibility。
4. Worker 执行前统一经过 policy gate。
5. Worker 内部工具不暴露给 Jarvis 顶层 planner。

## 6. Shell / cmd 策略

Shell 保留，但默认内收。

允许场景：

- 用户显式传入 `command`。
- Jarvis 执行受控 verification command。
- Worker postflight 收集确定性诊断。

禁止场景：

- LLM planner 为代码修改自行生成探索命令。
- `cmd` 作为代码任务的低风险 fallback。
- 未经 policy 的 commit / push / destructive command。

后续目标：

```text
No explicit command -> no ShellWorker
Code task -> CoderWorker
Search task -> SearchWorker
Verification -> allowlisted command or approved command
```

### 6.1 verification_cmd policy

`verification_cmd` 不是普通 worker 内部自由动作。它必须满足以下规则：

1. 创建 WorkOrder 时，`verification_cmd` 必须参与风险计算。
2. 如果 verification 命令风险高于主 capability，WorkOrder 的 `risk_level` 必须提升。
3. 提升后的 WorkOrder 必须在 dispatch 前经过同一套 approval。
4. executor 不应接收未经过 policy 标记的高风险 verification。

当前代码已在 strategize 阶段把 `verification_cmd` 纳入 `_work_order_risk()`，这是短期可接受的边界。但长期应把 verification 显式建模：

```python
class VerificationPolicy(BaseModel):
    command: str
    source: Literal["user", "capability_default", "approved_plan"]
    risk_level: RiskLevel
    approved: bool = False
```

两种可选实现：

- 简单实现：verification 仍由 provider/executor 在主任务成功后执行，但 WorkOrder 持有 `verification_policy`，executor 只执行 `approved=True` 或低风险 allowlist 命令。
- 更严格实现：verification 拆成单独 `shell.verify` WorkOrder，经 CapabilityRegistry 和 policy gate 调度。

推荐短期采用简单实现，长期迁到单独 WorkOrder，便于审计和失败重规划。

## 7. Skill 加载迁移策略

### Phase A：硬化现状

短期不大拆代码，先修可靠性：

- `SkillToolManifest` 补齐 `intent_kinds`、`requires_workdir`、`requires_explicit_user_command`、`can_modify_files`。
- `_planned_tool_calls()` 禁止在 intent 过滤失败时 fallback 到全工具。
- `strategize()` 对 LLM 返回的 tool/capability 做 execution-time eligibility guard。
- live integration 测试缺少本地 repo 时 `skip`，避免默认测试红。
- 明确 `delegate_to_claude_code`、`tavily_search`、`run_shell_command` 都是 legacy tool name，新增 capability alias 表。

### Phase B：引入 CapabilityRegistry

新增 `CapabilityRegistry`，让现有 `ToolRegistry` 成为兼容视图：

```text
CapabilityRegistry
    -> list capabilities by intent
    -> convert capability to ToolSpec for LLM function calling
    -> build WorkOrder
    -> resolve legacy tool names to capability names
```

此阶段不删除 SkillRegistry，只是不再让它作为架构中心。

Phase B 必须同时清理硬编码路由：

- `_classify_intent()` 不再返回硬编码 `allowed_tools=["delegate_to_claude_code"]`，改为 `allowed_capabilities=registry.defaults_for_intent("code_write")`。
- `_rule_based_tool_calls()` 不再写死 `delegate_to_claude_code`、`tavily_search`、`run_shell_command`，改为查询默认 capability。
- `_is_objective_success()` 不再硬编码 tool name 集合，改为 capability policy，例如 `completion_mode="objective"`。
- `strategize()` 通过 capability name 构建 WorkOrder，并写入 `capability_name`。

建议 alias 表：

| Legacy tool | Capability |
| --- | --- |
| `delegate_to_claude_code` | `coder.claude_code` |
| `tavily_search` | `search.tavily` |
| `run_shell_command` | `shell.command` |
| `run_tests` | `shell.test` |
| `echo` | `answer.echo` |

实现约束：

- `allowed_capabilities` 使用 canonical capability name。
- LLM tool schema 使用 alias / function-safe name。
- `strategize()` 对 planner 返回名做 alias resolution，再与 `allowed_capabilities` 比较。
- `Task.tool_name` 在兼容期可以继续存在，但其值应逐步改为 canonical capability name。
- 外部 skill 的 canonical capability 应优先来自 manifest 的 `capability_name`，而不是 Jarvis 代码硬编码猜测。

### Phase C：内置 SearchWorker

把 `tavily_search` 从外部 Skill 包迁移为：

```text
app/workers/search.py
app/search/tavily.py
```

外部 Tavily skill 包可保留一段时间，但标记 deprecated。

### Phase D：CoderWorker 一级化

把 Claude Code worker 从普通 skill 抽到 dedicated worker：

```text
app/workers/coder.py
app/coder/claude_code.py
app/coder/skills.py
app/coder/hooks.py
```

新增：

- `coder_plan`
- `coder_execute`
- coder skill selection
- Claude hooks / deny rules
- workdir boundary checks

### Phase E：收缩通用 Python Skill

将当前外部 Python Skill 加载标为：

```text
trusted-local experimental adapter
```

默认文档不再推荐第三方 Python 包直接 import 到 Jarvis 主进程。第三方扩展优先走 HTTP / CLI / MCP adapter。

目录共存策略：

```text
data/skills/
    legacy trusted-local Python Skill packages
    loaded only when JARVIS_ENABLE_LEGACY_SKILLS=true or during compatibility window

data/capabilities/
    declarative WorkerCapability manifests
    preferred Jarvis-level extension path

data/coder-skills/
    Claude Code / coder worker context and hooks
    never executed by Jarvis directly
```

迁移路径：

1. 兼容期继续扫描 `data/skills/`、`~/.jarvis/skills/`、`JARVIS_SKILL_PATH`，但启动时 warning：legacy Python Skill loader is deprecated for new capabilities。
2. 新增 `data/capabilities/` loader，只加载声明式 YAML，不动态 import Python。
3. 新增 `data/coder-skills/` loader，由 CoderWorker 在运行前读取，不注册到 ToolRegistry。
4. 一个版本窗口后，将 legacy Python Skill loader 置于配置开关后。
5. 最终只保留 trusted-local experimental adapter。

## 8. 当前 review 结论与采纳

2026-04-21 复核结论：

| Review 点 | 判断 | 文档处理 |
| --- | --- | --- |
| Capability 与 WorkOrder 映射不清晰 | 成立 | 新增 `capability_name`、`provider` 映射规则 |
| CoderWorker plan/execute 接口不完整 | 成立 | 新增 `CoderPlan`、`CoderExecuteArgs`、LangGraph 集成 |
| CapabilityRegistry 到 ToolSpec 兼容转换缺失 | 成立 | 新增 `capability_to_tool_spec()` 规则 |
| 硬编码工具名清理计划缺失 | 成立 | Phase B 增加硬编码清理和 alias 表 |
| verification_cmd policy 不明确 | 基本成立 | 明确 WorkOrder 风险提升和长期单独 WorkOrder 方案 |
| 外部 Skill 加载迁移边界模糊 | 成立 | 明确 `data/skills`、`data/capabilities`、`data/coder-skills` 共存策略 |
| Capability intents 与 ToolSpec intent_kinds 区别不明显 | 成立 | 补充 capability 的 policy/provider/priority 新价值 |

## 9. 现有文档关系

本文件是新的能力模型主文档。

与存量文档关系：

- `ARCHITECTURE.md`：保留总体架构；后续应把 Skill 执行层调整为 Worker Capability + Worker 内部工具。
- `TOOL_ROUTING_DESIGN.md`：保留 intent routing 和 shell guardrail 设计；后续把 Tool Eligibility 改名或升级为 Capability Eligibility。
- `SKILL_PLUGIN_DESIGN.md`：标记为历史方案和兼容层说明，不再作为未来主扩展模型。
- `MVP_PLAN.md`：保留项目计划；后续新增 CapabilityRegistry、SearchWorker、CoderWorker 一级化任务。
- `LANGGRAPH_AGENT_DESIGN.md`：保留 CA Agent / LangGraph 事件循环设计，不直接处理 skill loading。

重复度最高的是 `SKILL_PLUGIN_DESIGN.md`。它描述的是“Jarvis 作为通用 Python Skill 插件运行时”的方案；本文件改为“Jarvis 调度 Worker Capability，worker 内部承载 provider / skill / hooks”。

## 10. 推荐下一步工作

1. 修现有 routing 安全边界：manifest metadata 透传、禁止全工具 fallback、`strategize()` 二次校验。
2. 为 `WorkOrder` 增加 nullable `capability_name` 和 `provider`，并补持久化字段。
3. 新增 `CapabilityRegistry` 草图，先从现有 built-in tools 生成 capability，并生成 ToolSpec 兼容视图。
4. 清理 `_classify_intent()`、`_rule_based_tool_calls()`、`_is_objective_success()` 中的硬编码 tool name。
5. 将 `tavily_search` 迁移为内置 `SearchWorker(provider=tavily)`。
6. 将 Claude Code worker 升级为 dedicated CoderWorker，并引入 coder skill 注入目录。
7. 将 `SKILL_PLUGIN_DESIGN.md` 中的动态 import 方案降级为兼容层。

## 11. 完成定义

本设计完成后应满足：

- 代码任务默认只走 CoderWorker，不走 shell。
- 搜索任务默认只走 SearchWorker，不依赖通用 Python Skill 插件。
- Jarvis 顶层 planner 只看到 intent 允许的 capability。
- WorkOrder 持久化 capability name，恢复时不依赖 legacy `worker_type` 唯一路由。
- Claude Code 能接收 repo-specific coder skills 和 hooks。
- Shell 只能用于显式命令、受控验证或 worker postflight。
- verification command 必须经过 risk classification 和 approval policy。
- 通用 Python Skill 动态 import 不再是主扩展路径。
