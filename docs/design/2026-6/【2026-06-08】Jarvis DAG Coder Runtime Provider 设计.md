# Jarvis DAG Coder Runtime Provider 设计

| 项目 | 内容 |
|------|------|
| 作者 | Codex |
| 日期 | 2026-06-08 |
| 状态 | Draft |
| 相关模块 | `app/task_runtime`, `app/tools/codex.py`, `app/tools/coder.py`, `app/tools/coder_common.py` |
| 目标 | 在新 DAG runtime 中只保留 `coder` 代码执行 runtime，并通过 provider 配置选择 Codex 或 Claude Code |

---

## 1. 背景

Jarvis vNext 已经具备 DAG 型 `TaskAgentRuntime`：

```text
PlanningRouter
  -> ExecutionPlan / PlanNode DAG
  -> NodeExecutor
  -> NodeExecuteRuntime
  -> ResultAggregator
```

当前代码任务主要通过 `codex` 节点执行。实现上，`CodexNodeExecuteRuntime` 会把 DAG 节点适配成 `delegate_to_codex` 工具调用，再由 `app/tools/codex.py` 调用 Codex backend。

仓库里也保留了旧的 Claude Code 工具：

```text
delegate_to_claude_code
```

但它目前是隐藏工具，面向旧 ReAct 工具体系，不适合直接作为新 DAG runtime 的长期入口。

当前目标是：

1. 新增 Claude Code runtime 能力。
2. 通过配置决定代码节点由 Codex 还是 Claude Code 执行。
3. DAG runtime 层只保留一个代码执行 runtime：`coder`。
4. 不让 planner 直接在 `codex` 和 `claude_code` 之间选择。
5. 保持 DAG runtime 的语义稳定，降低后续 provider 切换成本。

---

## 2. 结论

DAG 中“代码/仓库工作”的 runtime 语义统一为：

```text
coder
```

然后通过配置选择底层 provider：

```text
JARVIS_CODER_RUNTIME_PROVIDER=codex
```

可选值：

```text
codex
claude_code
```

目标结构：

```text
PlanNode(runtime="coder")
  -> CoderNodeExecuteRuntime
      -> CodexCoderProvider
      -> ClaudeCodeCoderProvider
```

Planner 只允许输出：

```json
{"runtime": "coder"}
```

不允许输出：

```text
codex
claude_code
```

如需兼容历史 `codex` 节点，只能作为短期输入兼容或数据迁移策略，不作为目标 runtime 设计。

---

## 3. 设计原则

### 3.1 Planner 不感知具体 coder provider

Planner 只负责判断任务需要哪类能力，不负责选择底层工具厂商。

不推荐让 planner 输出：

```text
codex
claude_code
```

原因：

1. provider 选择是部署配置，不是用户意图。
2. LLM 可能违反配置，产生与环境不一致的节点。
3. 后续接入更多 coder provider 时会污染 planner prompt。

推荐 planner 只输出：

```text
coder
```

底层 provider 由 runtime 根据配置决定。

### 3.2 Jarvis runtime 拥有安全边界

无论使用 Codex 还是 Claude Code，仓库边界和审计机制都属于 Jarvis runtime 层，而不是 provider 层：

1. 使用 repository registry 解析 `repo_id`。
2. 将 `PlanNode` 和上下文转换为 `CoderRunRequest`。
3. 执行前做 preflight。
4. 调用 provider 执行底层代码 agent。
5. 执行后做 postflight。
6. 用 `access_mode`、`allow_commit`、`allow_push` 做硬校验。
7. 将 provider 结果归一化为 `NodeResult`。
8. 产出统一 artifact 和 audit 文件。

Provider 不能直接决定仓库授权、commit/push 是否合规、read/write 是否违规，也不能只依赖 prompt 自觉遵守权限。

### 3.3 Provider 只适配底层代码 agent

`CoderProvider` 是底层代码 agent 的适配接口。一期实现两个 provider：

1. `CodexCoderProvider`：复用现有 Codex app server。
2. `ClaudeCodeCoderProvider`：基于 Claude Agent SDK。

Provider 的职责是：

1. 接收 `CoderRunRequest`。
2. 调用对应 agent backend。
3. 返回 provider 原始执行摘要、stdout/stderr、事件或产物引用。

Provider 不负责：

1. 解析 `PlanNode`。
2. 读取 conversation/session state。
3. 解析 repository registry。
4. 执行 preflight/postflight 权限判定。
5. 将结果直接写成 `NodeResult`。

### 3.4 保持 NodeExecutor 简单

`NodeExecutor` 当前只按 `node.runtime` 查找 `NodeExecuteRuntime`，这是正确边界。

不建议把 provider 选择逻辑放进 `NodeExecutor`。`NodeExecutor` 应继续只负责 DAG 依赖解析、执行顺序和结果收集。

---

## 4. 配置设计

在 `app/config.py` 增加：

```python
coder_runtime_provider: str = "codex"
```

环境变量：

```text
JARVIS_CODER_RUNTIME_PROVIDER=codex
```

可选值：

```text
codex
claude_code
```

可选增强：

```python
claude_code_timeout_seconds: int | None = None
```

第一版可以继续共用：

```python
coder_timeout_seconds
```

避免引入过多配置项。

---

## 5. Runtime 设计

### 5.1 NodeRuntime 枚举

目标形态只保留一个代码执行 runtime：

```python
NodeRuntime = Literal["llm", "react", "coder", "tool", "deepresearch"]
```

`codex` 不再是目标 DAG runtime。历史数据或测试如仍包含 `runtime="codex"`，可以通过一次性迁移、planner prompt 修正或短期 normalize 兼容处理，但 `NodeExecutor` 的目标注册表不应长期保留两个代码 runtime。

### 5.2 Access Mode

一期 provider 只支持两种访问模式：

```text
read
write
```

语义：

```text
read:
  只读仓库。
  不允许产生 workspace diff。
  不允许 commit。
  不允许 push。

write:
  允许修改 workspace。
  是否允许 commit 由 allow_commit 控制。
  是否允许 push 由 allow_push 控制。
```

`access_mode` 应由 planner 产出到 node-level `runtime_hints`。建议 `PlanNode` 增加：

```python
runtime_hints: dict[str, Any] = Field(default_factory=dict)
```

Planner 输出示例：

```json
{
  "runtime": "coder",
  "runtime_hints": {
    "access_mode": "read"
  }
}
```

关键原则：

1. Planner 可以表达 `read/write`。
2. Planner 不能表达 `codex` / `claude_code`。
3. 如果 planner 缺省 `access_mode`，runtime 可以根据会话分类和节点 objective 派生保守默认值。
4. 不能确定是否需要写权限时，默认 `read`，并在需要修改时由 runtime 阻止或要求重新规划。

### 5.3 CoderPolicy

一期 `CoderPolicy` 只支持 `read/write` 两种模式：

```python
@dataclass(frozen=True)
class CoderPolicy:
    access_mode: Literal["read", "write"]
    allow_commit: bool = False
    allow_push: bool = False
```

规则：

```text
read:
  只读仓库。
  不允许修改 workspace。
  不允许 commit。
  不允许 push。

write:
  允许修改 workspace。
  commit 由 allow_commit 控制。
  push 由 allow_push 控制。
```

`CoderPolicy` 由 `CoderNodeExecuteRuntime` 从 node-level `runtime_hints.access_mode`、用户授权和 runtime policy 派生。Planner 只能表达 `access_mode`，不能自行放开 commit/push。

### 5.4 CoderAction 与 ApprovalPolicy

底层代码 agent 可能发起工具调用、命令执行或 approval request。Provider 不直接决定审批语义，而是将底层动作映射为统一 `CoderAction`：

```python
@dataclass(frozen=True)
class CoderAction:
    kind: Literal[
        "read_file",
        "search",
        "git_status",
        "git_diff",
        "git_log",
        "edit_file",
        "commit",
        "push",
        "secret_read",
        "dangerous_command",
        "outside_workspace_write",
        "unknown_external_action",
    ]
    command: str | None = None
    path: str | None = None
    description: str = ""
    raw_provider_payload: dict[str, Any] = field(default_factory=dict)
```

Jarvis 使用统一 `ApprovalPolicy` 决策：

| CoderAction | read | write | 决策 |
|-------------|------|-------|------|
| 常规读文件 | 自动允许 | 自动允许 | allow |
| 搜索 | 自动允许 | 自动允许 | allow |
| `git status` / `git diff` / `git log` | 自动允许 | 自动允许 | allow |
| 普通文件编辑 | 默认拒绝 | 自动允许 | deny / allow |
| commit | 默认拒绝 | 需要审批 | deny / ask |
| push | 默认拒绝 | 需要强审批 | deny / strong_ask |
| secret read | 默认拒绝 | 默认拒绝 | deny |
| 危险命令 | 默认拒绝 | 默认拒绝 | deny |
| workspace 外写入 | 默认拒绝 | 默认拒绝 | deny |
| 未知外围动作 | 需要审批 | 需要审批 | ask |

审批分级：

```text
allow:
  自动允许。

ask:
  需要普通用户审批。

strong_ask:
  需要强审批，至少明确展示 push 目标、分支、远端和风险。

deny:
  默认拒绝，不交给底层 agent 继续执行。
```

这层策略必须由 Jarvis 定义和测试。Codex provider 和 Claude provider 只能负责把各自的底层 tool call / approval request 映射成 `CoderAction`。

### 5.5 CoderRunRequest

`CoderNodeExecuteRuntime` 和 provider 之间使用 provider-neutral request：

```python
@dataclass(frozen=True)
class CoderRunRequest:
    repo_id: str
    workdir: Path
    instruction: str
    policy: CoderPolicy
    timeout_seconds: int = 1800
    run_dir: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

说明：

1. `repo_id` 和 `workdir` 已经由 Jarvis runtime 通过 repository registry 解析完成。
2. `instruction` 已经由 Jarvis runtime 根据 `PlanNode`、resolved inputs 和用户约束构造完成。
3. `policy` 是 provider 必须遵守的执行策略。
4. `allow_commit` / `allow_push` 只在 `policy.access_mode="write"` 时可能为 true。
5. `run_dir` 由 Jarvis runtime 创建，provider 只写入自己的日志或事件。
6. 具体 provider 由 `CoderNodeExecuteRuntime` 在构造时选择，不写入 planner 输出。

### 5.6 CoderNodeExecuteRuntime

新增 `CoderNodeExecuteRuntime`：

```python
class CoderNodeExecuteRuntime:
    def __init__(self, provider: CoderProvider | None = None) -> None:
        self._provider = provider or build_coder_provider(get_settings())

    def run(self, context: NodeExecutionContext) -> NodeResult:
        ...
```

职责：

1. 从 `runtime_hints.active_repo` 读取当前仓库。
2. 通过 repository registry 解析 `repo_id` 和 `workdir`。
3. 解析并构造 `CoderPolicy`。
4. 创建 `ApprovalPolicy` 或审批回调。
5. 执行 preflight。
6. 构造统一 coder instruction。
7. 构造 `CoderRunRequest`。
8. 调用底层 provider。
9. 接收 provider 映射出的 `CoderAction` 并由 Jarvis 统一审批。
10. 执行 postflight。
11. 根据 preflight/postflight 做权限硬校验。
12. 把 provider result 归一化为 `NodeResult`。

`CoderNodeExecuteRuntime` 的结果 runtime 固定为：

```text
coder
```

同时在 `data` 中记录实际 provider：

```json
{
  "provider": "codex"
}
```

如果历史输入节点是 `codex`，也应尽量在 normalize 阶段转成 `coder`，避免 `NodeResult` 继续扩散旧 runtime 名称。

### 5.7 Runtime 注册

`TaskAgentRuntime` 中目标注册：

```python
self._node_executor = node_executor or NodeExecutor(
    runtimes={
        "llm": LLMNodeExecuteRuntime(),
        "react": ReactNodeExecuteRuntime(),
        "coder": CoderNodeExecuteRuntime(),
        "tool": ToolNodeExecuteRuntime(),
    }
)
```

如确实需要短期兼容历史 `codex`，优先在 plan normalize 阶段改写为 `coder`，而不是在执行注册表中长期保留 `"codex"`。

---

## 6. Provider 接口

建议新增 provider protocol：

```python
class CoderProvider(Protocol):
    name: str

    def run(
        self,
        request: CoderRunRequest,
        *,
        decide_action: Callable[[CoderAction], ApprovalDecision],
    ) -> CoderRunResult:
        ...
```

Provider 返回 provider-neutral result：

```python
@dataclass(frozen=True)
class CoderRunResult:
    ok: bool
    exit_code: int | None = None
    summary: str = ""
    stdout: str = ""
    stderr: str = ""
    artifacts: list[str] = field(default_factory=list)
    raw_events_path: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

说明：

1. `CoderRunResult` 不是最终 `NodeResult`。
2. 权限违规不由 provider 直接定性，provider 只报告底层执行事实。
3. `CoderNodeExecuteRuntime` 根据 provider result 和 postflight 权限检查生成最终 `NodeResult`。
4. provider 可以返回 raw event path，但 raw event 不直接进入最终用户上下文。
5. provider 遇到底层 tool call 或 approval request 时，必须先映射为 `CoderAction`，再调用 `decide_action` 获取 Jarvis 的统一审批结果。

`ApprovalDecision` 建议结构：

```python
@dataclass(frozen=True)
class ApprovalDecision:
    decision: Literal["allow", "ask", "strong_ask", "deny"]
    reason: str = ""
    approval_id: str | None = None
```

如果决策为：

```text
allow:
  provider 可以继续执行底层动作。

deny:
  provider 必须阻止底层动作，并把拒绝原因写入 result metadata。

ask / strong_ask:
  provider 必须暂停或返回等待审批状态，由 Jarvis 渠道层向用户发起审批。
```

Provider 不得把 Codex 或 Claude 的原生 approval 语义直接暴露给上层；必须先转成 `CoderAction`。

Provider factory：

```python
def build_coder_provider(settings: Settings) -> CoderProvider:
    provider = (settings.coder_runtime_provider or "codex").strip().lower()
    if provider == "claude_code":
        return ClaudeCodeCoderProvider()
    if provider == "codex":
        return CodexCoderProvider()
    raise ValueError(f"Unsupported coder runtime provider: {provider}")
```

### 6.1 CodexCoderProvider

Codex provider 复用当前 Codex app server 能力：

```python
run_codex_app_server_turn(...)
```

一期可以复用 `app/tools/codex.py` 中已验证的解析、事件和 artifact 逻辑，但目标形态应下沉到 provider：

1. `app/tools/codex.py`
2. `app/tools/codex_app_server.py`
3. `app/tools/coder_common.py`

迁移时要避免把 `delegate_to_codex` 的工具层契约直接带进 DAG provider。DAG provider 应接收 `CoderRunRequest`，返回 `CoderRunResult`。

Codex app server 原生 approval request 需要映射为统一 `CoderAction`：

```text
shell command -> git_status / git_diff / git_log / commit / push / dangerous_command / unknown_external_action
file write/edit -> edit_file / outside_workspace_write
file read -> read_file / secret_read
```

映射后调用 `decide_action`。Codex provider 不直接决定自动批准、用户审批或拒绝。

### 6.2 ClaudeCodeCoderProvider

Claude Code provider 建议新增：

```text
app/tools/claude_code.py
```

入口：

```python
def run_claude_code_agent(request: CoderRunRequest) -> CoderRunResult:
    ...
```

一期基于 Claude Agent SDK，而不是旧 `claude --print` CLI 封装。

旧 `app/tools/coder.py` 可以作为迁移参考，但不应作为新 DAG provider 的目标实现。

---

## 7. Claude Code Backend 设计

### 7.1 Agent SDK 形态

一期 Claude Code provider 基于 Claude Agent SDK。目标形态：

```python
class ClaudeCodeCoderProvider:
    name = "claude_code"

    def run(self, request: CoderRunRequest) -> CoderRunResult:
        ...
```

Provider 负责：

1. 根据 `CoderRunRequest` 构造 Claude Agent SDK 调用。
2. 设置工作目录或等价 workspace 上下文。
3. 根据 `request.policy.access_mode` 配置只读或可写工具集合。
4. 流式或批量收集 SDK 事件。
5. 将最终文本、事件路径、stdout/stderr 和 provider metadata 返回为 `CoderRunResult`。

### 7.2 工具能力映射

Claude provider 的工具能力必须由 `request.policy.access_mode` 决定：

```text
policy.access_mode=read:
  允许读取文件、搜索文件、运行安全只读检查。
  不允许 Write/Edit/MultiEdit。
  不允许产生 workspace diff。
  不允许 commit/push。

policy.access_mode=write:
  允许读写 workspace。
  是否允许 git commit 由 allow_commit 控制。
  是否允许 git push 由 allow_push 控制。
```

实现时需要按当前 Claude Agent SDK 能力确认：

1. 如何声明可用 tools。
2. 如何限制 workspace。
3. 如何处理 shell/bash 权限。
4. 如何接收 approval / permission 事件。
5. 如何获取最终 assistant message 和结构化事件。

如果 SDK 无法原生阻止某些写操作，Jarvis runtime 的 postflight 仍是最终硬校验；但 provider 应尽量在 SDK 配置层先收紧。

Claude Agent SDK 的底层 tool call / permission event 也必须映射成 `CoderAction`：

```text
Read / file read -> read_file / secret_read
Glob / Grep / search -> search
Edit / Write / MultiEdit -> edit_file / outside_workspace_write
Bash(git status/diff/log) -> git_status / git_diff / git_log
Bash(git commit) -> commit
Bash(git push) -> push
unknown Bash / network / process action -> unknown_external_action 或 dangerous_command
```

映射后调用 `decide_action`。Claude provider 不直接解释审批等级。

### 7.3 日志和 artifact

Claude Code provider 应写入：

```text
data/coder_runs/<run_id>/claude-events.jsonl
data/coder_runs/<run_id>/claude-summary.md
data/coder_runs/<run_id>/jarvis-audit.log
```

如果 SDK 暴露 stdout/stderr 或等价诊断流，可以增加：

```text
claude-stdout.log
claude-stderr.log
```

返回 artifacts：

```text
claude_run:<run_dir>
claude_stdout:<stdout_path>
claude_stderr:<stderr_path>
claude_events:<events_path>
jarvis_audit:<audit_path>
git_file:<path>
permission_violation:<message>
```

---

## 8. Planner Prompt 调整

当前 heavy planner prompt 里允许：

```text
codex: active/local repository reading, code review, code editing, testing, commit, push, or workspace-based implementation work
```

目标改为：

```text
coder: active/local repository reading, code review, code editing, testing, commit, push, or workspace-based implementation work
```

并将：

```text
Use codex for any task involving the active/local repository.
```

改为：

```text
Use coder for any task involving the active/local repository.
```

`runtime_hints.available_runtimes` 应包含：

```json
["llm", "react", "coder", "tool", "deepresearch"]
```

不应包含：

```text
codex
claude_code
```

`PlanNode` 输出应增加 `runtime_hints.access_mode`：

```json
{
  "id": "inspect_runtime",
  "runtime": "coder",
  "objective": "检查 Jarvis 当前 DAG runtime 中代码执行节点的实现边界。",
  "input_refs": [],
  "expected_output": "给出只读分析，不修改文件。",
  "runtime_hints": {
    "access_mode": "read"
  }
}
```

如果用户明确要求修改、修复、生成文件或执行代码改动，planner 输出：

```json
{
  "runtime": "coder",
  "runtime_hints": {
    "access_mode": "write"
  }
}
```

`allow_commit` 和 `allow_push` 不由 planner 自由推断。它们应来自用户显式授权、会话策略或 runtime policy。

---

## 9. Runtime Hints

`TaskAgentRuntime` 构造 `runtime_hints` 时建议增加：

```python
runtime_hints = {
    "active_repo": session_state.active_repo_id,
    "platform": conversation.platform,
    "conversation_id": turn.conversation_id,
    "turn_id": turn_id,
    "external_chat_id": conversation.external_chat_id,
    "available_runtimes": ["llm", "react", "coder", "tool", "deepresearch"],
    "coder_runtime_provider": get_settings().coder_runtime_provider,
    **_runtime_temporal_hints(),
}
```

说明：

1. `available_runtimes` 用于约束 planner 输出。
2. `coder_runtime_provider` 用于日志、调试和 raw payload 回溯。
3. planner 不应根据 `coder_runtime_provider` 改变节点类型。
4. node-level `runtime_hints.access_mode` 用于表达本节点需要 read 还是 write。

---

## 10. 工具层关系

### 10.1 新 DAG runtime

新 DAG runtime 使用：

```text
coder node
  -> CoderNodeExecuteRuntime
  -> provider
```

### 10.2 旧 ReAct runtime

旧 ReAct runtime 当前仍通过工具策略暴露：

```text
delegate_to_codex
```

是否让旧 ReAct runtime 也受 `coder_runtime_provider` 控制，可以作为后续任务。

第一版建议只保证新 DAG runtime provider 可配。旧 ReAct 维持现状，避免一次性扩大变更面。

如果后续希望统一，推荐新增一个语义工具：

```text
delegate_to_coder
```

并让它内部根据 provider 配置选择 Codex 或 Claude Code。但这会影响大量 runtime policy、测试 fixture 和工具描述，应单独设计。

---

## 11. 兼容策略

### 11.1 历史 `CodexNodeExecuteRuntime`

目标架构不再需要 `CodexNodeExecuteRuntime` 作为 DAG runtime。

迁移期可以保留该类，但只作为历史兼容 wrapper，不参与 planner 目标输出：

```python
class CodexNodeExecuteRuntime(CoderNodeExecuteRuntime):
    def __init__(self) -> None:
        super().__init__(provider=CodexCoderProvider())
```

或者先不动原类，新增 `CoderNodeExecuteRuntime` 后在 `TaskAgentRuntime` 里切换。

### 11.2 兼容历史 `codex` 节点

历史 raw payload、测试和 planner 输出可能仍包含：

```json
{"runtime": "codex"}
```

兼容方式：

1. 优先在 plan normalize 阶段将 `codex` 改写为 `coder`。
2. planner prompt 必须改为只输出 `coder`。
3. 测试 fixture 逐步迁移为 `coder`。
4. 不建议在 `NodeExecutor.runtimes` 中长期注册 `codex`。

---

## 12. 安全边界

### 12.1 Repository Registry 是第一道边界

`CoderNodeExecuteRuntime` 必须通过 `repo_id` 解析授权仓库。

不允许 provider 接受任意未注册 workdir。provider 接收到的 `CoderRunRequest.workdir` 必须已经由 Jarvis runtime 验证。

### 12.2 ApprovalPolicy 是前置边界

所有 provider 的底层动作都必须经过统一 `ApprovalPolicy`：

1. Provider 将原生 tool call / approval request 映射为 `CoderAction`。
2. `CoderNodeExecuteRuntime` 调用 Jarvis `ApprovalPolicy` 得到 `ApprovalDecision`。
3. Provider 根据 `ApprovalDecision` 继续、暂停或拒绝底层动作。

默认策略：

1. 常规读、搜索、`git status` / `git diff` / `git log` 自动允许。
2. `write` 模式下普通文件编辑自动允许。
3. commit 需要审批。
4. push 需要强审批。
5. secret read、危险命令、workspace 外写入默认拒绝。
6. 未知外围动作默认需要审批。

Provider 不直接决定审批语义。

### 12.3 Postflight Permission Check 是硬边界

以下规则必须在 Jarvis 层执行：

1. `access_mode="read"` 但 workspace diff 改变：`ok=false`。
2. `access_mode="read"` 但 HEAD 改变：`ok=false`。
3. `access_mode="read"` 但 upstream 改变：`ok=false`。
4. `access_mode="write"` 且 `allow_push=true` 但 `allow_commit=false`：直接失败。
5. `access_mode="write"` 且 `allow_commit=false` 但 HEAD 改变：`ok=false`。
6. `access_mode="write"` 且 `allow_push=false` 但 upstream 改变：`ok=false`。

这些规则不能只写进 prompt，也不能只依赖 provider 的审批映射。即使 provider 或底层 agent 错误放行，postflight 仍必须能检测违规。

### 12.4 Provider 输出不能污染主模型上下文

Codex JSONL、Claude raw stdout/stderr 都可能很长。

返回给上层的 `stdout` 应是干净摘要，raw 输出写 artifact/log。

---

## 13. 实施计划

### Phase 1：PlanNode 与配置

改动：

1. `Settings` 增加 `coder_runtime_provider`。
2. `PlanNode` 增加 `runtime_hints`。
3. `NodeRuntime` 目标集合增加 `coder`，planner prompt 改为只输出 `coder`。
4. planner 输出 `runtime_hints.access_mode`，取值只允许 `read` / `write`。
5. `runtime_hints.available_runtimes` 不包含 `codex` / `claude_code`。

验证：

1. planner 对本地仓库只读任务输出 `runtime="coder"` + `access_mode="read"`。
2. planner 对明确修改任务输出 `runtime="coder"` + `access_mode="write"`。
3. planner 不输出 `codex` 或 `claude_code`。

### Phase 2：CoderRunRequest 与 CoderNodeExecuteRuntime

改动：

1. 新增 `CoderNodeExecuteRuntime`。
2. 新增 `CoderPolicy`、`CoderRunRequest` / `CoderRunResult`。
3. `CoderNodeExecuteRuntime` 负责 repo registry、preflight、postflight、权限校验、NodeResult 归一化。
4. `TaskAgentRuntime` 只注册 `coder` 代码执行 runtime。
5. 历史 `codex` 节点如需兼容，在 normalize 阶段改写为 `coder`。

验证：

1. `PlanNode(runtime="coder")` 可执行。
2. `runtime_hints.active_repo` 缺失时返回 blocked。
3. `access_mode="read"` 产生 diff 时 `NodeResult.status="failed"`。
4. `access_mode="write"` 下 no commit / no push 违规时失败。

### Phase 3：CoderAction 与 ApprovalPolicy

改动：

1. 新增 `CoderAction`。
2. 新增 `ApprovalDecision`。
3. 新增 Jarvis `ApprovalPolicy`。
4. 将 `decide_action` 回调传给 provider。
5. 将审批等待状态归一化为 `CoderRunResult` / `NodeResult`。

验证：

1. 常规读、搜索、`git status` / `git diff` / `git log` 自动允许。
2. read 模式下普通文件编辑拒绝。
3. write 模式下普通文件编辑自动允许。
4. commit 返回普通审批。
5. push 返回强审批。
6. secret read、危险命令、workspace 外写入拒绝。
7. 未知外围动作返回审批。

### Phase 4：CodexCoderProvider

改动：

1. 新增 `CoderProvider` protocol 和 provider factory。
2. 新增 `CodexCoderProvider`。
3. 复用现有 Codex app server。
4. 将工具层 `delegate_to_codex` 契约适配为 `CoderRunRequest` / `CoderRunResult` 契约。
5. 将 Codex app server 原生 approval request 映射为 `CoderAction`。

验证：

1. 默认配置仍走 Codex。
2. Codex app server 成功结果能归一化为 coder NodeResult。
3. Codex app server approval request 会走 Jarvis `ApprovalPolicy`。
4. failed / timeout 能保留原有摘要和 artifact。

### Phase 5：ClaudeCodeCoderProvider

改动：

1. 新增 `app/tools/claude_code.py` 或 `app/task_runtime/coder_providers.py`。
2. 基于 Claude Agent SDK 实现 provider。
3. 根据 `request.policy.access_mode` 配置 read/write 工具能力。
4. 将 Claude SDK tool call / permission event 映射为 `CoderAction`。
5. 写入 Claude SDK events、summary 和诊断日志。

验证：

1. SDK 缺失或未配置时返回清晰错误。
2. fake SDK 成功时返回 `CoderRunResult(ok=True)`。
3. read 模式下 provider 不配置写工具。
4. write 模式下 provider 根据 allow_commit / allow_push 收紧 git 能力。
5. Claude SDK permission event 会走 Jarvis `ApprovalPolicy`。

### Phase 6：清理旧接口

条件：

1. DAG planner 稳定输出 `coder`。
2. 真实仓库 Codex 和 Claude Code provider 都通过 opt-in 验证。
3. 旧 ReAct runtime 的策略是否统一已有单独决策。

清理：

1. 删除 `codex` DAG runtime alias 或 normalize 兼容代码。
2. 明确 `delegate_to_claude_code` 是否继续保留为隐藏工具。
3. 更新文档和测试命名。

---

## 14. 测试策略

### 14.1 单元测试

新增或更新：

1. `Settings` 能读取 `JARVIS_CODER_RUNTIME_PROVIDER`。
2. `PlanNode` 能解析 `runtime_hints.access_mode`。
3. provider factory 默认返回 Codex。
4. provider factory 在 `claude_code` 时返回 Claude Code。
5. 非法 provider 抛出清晰错误。
6. `CoderNodeExecuteRuntime` 缺 active repo 时 blocked。
7. `CoderNodeExecuteRuntime` 能把 context 转成 `CoderRunRequest`。
8. `NodeResult.data.provider` 记录实际 provider。
9. `NodeResult.runtime` 固定为 `coder`。
10. `ApprovalPolicy` 对每类 `CoderAction` 返回预期 `ApprovalDecision`。

### 14.2 DAG runtime 测试

覆盖：

1. `PlanNode(runtime="coder")` 执行成功。
2. 多节点 DAG 中 react -> coder 的 input_refs 正常传递。
3. coder node 失败时 aggregator 能生成用户可理解的失败回复。
4. 历史 `runtime="codex"` 输入如需兼容，应在 normalize 阶段改写为 `coder`。
5. `access_mode="read"` 下 provider 造成 workspace diff 时失败。
6. `access_mode="write"` 下允许 workspace diff，但仍按 allow_commit / allow_push 校验。
7. provider 上报 commit action 时 runtime 返回等待审批状态。
8. provider 上报 push action 时 runtime 返回强审批状态。

### 14.3 Codex provider 测试

使用 Codex app server fake / mock 覆盖：

1. `CoderRunRequest` 被正确映射为 Codex app server 调用。
2. Codex 原生命令审批请求能映射为 `CoderAction`。
3. `git status` / `git diff` / `git log` 映射为自动允许动作。
4. `git commit` 映射为 commit action。
5. `git push` 映射为 push action。
6. 危险命令、workspace 外写入、secret read 映射为拒绝类 action。
7. provider 调用 `decide_action`，不自行决定审批结果。

### 14.4 Claude provider 测试

使用 Claude Agent SDK fake / mock 覆盖：

1. `CoderRunRequest` 被正确映射为 SDK 调用。
2. `request.policy.access_mode="read"` 时不暴露写工具。
3. `request.policy.access_mode="write"` 时暴露必要写工具。
4. allow_commit / allow_push 会影响 git 相关工具或指令约束。
5. SDK 事件写入 `claude-events.jsonl`。
6. provider 返回 `CoderRunResult`，不直接生成 `NodeResult`。
7. Claude SDK tool call / permission event 能映射为 `CoderAction`。
8. provider 调用 `decide_action`，不自行决定审批结果。

### 14.5 真实仓库 opt-in 测试

不进默认 CI。

建议环境变量：

```text
JARVIS_RUN_REAL_CODER_TESTS=1
JARVIS_CODER_RUNTIME_PROVIDER=claude_code
JARVIS_REAL_CODER_REPO=jarvis
```

验证：

1. 只读设计分析。
2. 最小文件修改但不 commit。
3. 用户允许 commit 时创建 commit。
4. 默认不 push。
5. 违反权限时 Jarvis 能检测。

---

## 15. 风险与应对

### 15.1 Claude Agent SDK 权限配置不足

风险：

```text
SDK 工具配置可能无法完全表达 Jarvis 需要的 read/write 权限边界。
```

应对：

1. 第一版只在注册仓库内运行。
2. `access_mode` 在 Jarvis runtime 层做硬校验。
3. 严格 preflight/postflight。
4. 记录 run_dir、SDK events 和 audit。
5. 实现前按当前 Claude Agent SDK 文档确认 tool 权限和 workspace 限制能力。

### 15.2 Planner 继续输出 `codex`

风险：

历史 prompt、fixture 或模型惯性可能继续输出 `codex`。

应对：

1. 迁移期在 plan normalize 阶段将 `codex` 改写为 `coder`。
2. prompt 明确 `coder` 是本地仓库任务 runtime。
3. 测试逐步改为断言 `coder`。

### 15.3 旧 ReAct 和新 DAG 行为不一致

风险：

新 DAG 可以配置 Claude Code，但旧 ReAct 仍只看到 `delegate_to_codex`。

应对：

第一版接受这个差异，因为目标是新 DAG runtime。旧 ReAct 工具统一应单独设计，避免扩大变更面。

### 15.4 Provider 输出格式差异

风险：

Codex app-server、Codex CLI、Claude Code 的 raw 输出格式不同。

应对：

上层只依赖 `CoderRunResult` 和 `NodeResult`，不依赖 provider 原生事件。raw 输出只进入 artifact/log。

---

## 16. 最终建议

推荐按以下方向落地：

```text
DAG runtime 语义：coder
Provider 配置：JARVIS_CODER_RUNTIME_PROVIDER=codex|claude_code
Provider 抽象：CoderProvider
Runtime 适配：CoderNodeExecuteRuntime
访问策略：CoderPolicy(read|write)
审批策略：CoderAction -> Jarvis ApprovalPolicy
迁移兼容：历史 codex 输入在 plan normalize 阶段改写为 coder
```

这样可以同时满足：

1. 新 DAG runtime 支持 Claude Code。
2. Codex / Claude Code 可以通过配置切换。
3. planner 不需要理解 provider。
4. `NodeExecutor` 不需要变复杂。
5. Jarvis runtime 统一拥有 repo registry、preflight、postflight、权限校验和 NodeResult 归一化边界。
6. Jarvis 统一定义审批语义，commit/push/危险动作不会被 provider 各自解释。
7. provider 只负责适配底层代码 agent，并把原生 tool call / approval request 映射为 `CoderAction`。

第一版应优先让 planner 和 DAG runtime 统一到 `coder`，默认 provider 继续走 Codex app server，再接入基于 Claude Agent SDK 的 `claude_code` provider。
