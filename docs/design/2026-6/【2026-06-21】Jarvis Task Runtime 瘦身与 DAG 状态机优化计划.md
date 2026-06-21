# Jarvis Task Runtime 瘦身与 DAG 状态机优化计划

| 项目 | 内容 |
| --- | --- |
| 日期 | 2026-06-21 |
| 状态 | Draft |
| 目标 | 降低 `app/task_runtime` 的抽象层数、冗余字段和隐式状态传递，形成可恢复、可审计、可长期演进的 DAG runtime |
| 范围 | `app/task_runtime`, `app/channels/feishu.py`, `app/agent_react/artifacts.py`, `app/tools/common.py`, 相关 prompt 与测试 |
| 非目标 | 本计划不要求一次性重写 runtime，不改变用户侧交互形态，不把所有代码任务迁移到新 provider |

---

## 0. 已确认的第一轮取舍

本轮优化按以下决策执行，后续设计也以这些边界为准：

1. DAG 层只识别 `llm`、`react`、`coder` 三类 runtime。
2. `codex` 和 `claude_code` 是 `coder` runtime 下的 provider，不再作为 DAG runtime 暴露。
3. 删除 DAG 层 `access_mode`、`allow_commit`、`allow_push`，不再设计“写模式节点”。
4. coder 节点的 Jarvis 管理 worktree 允许 provider 修改并由 runtime 记录 node commit。
5. task runtime 的 coder provider 不再透传 `allow_commit`、`allow_push`、`_read_only`；commit 策略由 Jarvis 管理的 node worktree 运行时承担，push 不在本轮 DAG 路径开放。
6. 一个 coder 节点执行完成后，节点 worktree 不允许留下未提交文件；如果 commit 后仍 dirty，节点失败。
7. 第一轮允许破坏旧 `raw_payload` 结构。
8. prompt 修改必须新增版本；本轮已从 `heavy_plan:v2` 逐步派生到 `heavy_plan:v5`。
9. DAG 恢复和审批后继续执行不在第一轮实现，只在后续状态机阶段处理。

---

## 1. 核心判断

当前 `app/task_runtime` 的复杂度主要来自三类问题：

1. 字段过多，且很多字段没有明确所有权。
2. 状态被分散在 `runtime_hints`、`data`、`metadata`、`payload`、manifest 和 artifact payload 中。
3. DAG 概念没有落成明确状态机，审批、恢复、重规划、产物交付都以旁路方式补丁式接入。

因此优化目标不是简单拆文件，而是先收敛协议：

```text
Plan 是静态执行图。
RuntimeContext 是运行上下文。
NodeState 是节点状态。
NodeResult 是节点结果。
Effect 是需要外部确认或外部执行的副作用。
Artifact 是统一产物协议。
```

只有这些核心协议稳定后，文件拆分才有意义。

---

## 2. 当前问题分层

### 2.1 隐式总线问题

`runtime_hints` 当前承担了过多职责：

```text
会话信息: turn_id, conversation_id, external_chat_id
时间信息: current_date, current_time, timezone
仓库信息: active_repo, source_branch, target_branch, node_branch
历史权限信息: access_mode, allow_commit, allow_push
目录信息: session_workspace_dir, node_workspace_dir, node_manifest_path, node_repos_dir
provider 信息: provider_run_dir, coder_runtime_provider
```

这会带来两个后果：

1. 行为不是由类型定义决定，而是由字符串 key 是否存在决定。
2. 新字段很容易继续塞进 `runtime_hints`，导致调用链不可推断。

目标是把它拆成强类型上下文：

```python
RuntimeContext
  turn: TurnContext
  session: SessionContext
  temporal: TemporalContext
  repo: RepoContext | None
  repo_policy: RepoExecutionPolicy
  provider: ProviderContext
```

短期不一定全部用 Pydantic 或 dataclass 实现，但逻辑上必须按这些边界传递。

注意：`RepoExecutionPolicy` 不是恢复 `access_mode/allow_commit/allow_push`。它只表达 runtime 内部固定策略，例如“Jarvis 管理 worktree 可由 coder 修改并自动记录 node commit，禁止 push”。这些策略不应再由 planner 或 DAG 节点字段控制。

### 2.2 结果垃圾桶问题

`NodeResult.data` 当前放了大量关键业务字段：

```text
tool_calls
tool_artifacts
usage_records
approval_requests
approval_id
action_kind
command
path
reason
repo_workspace
source_branch
target_branch
node_branch
node_commit
node_merge
finalizer
stdout
stderr
exit_code
provider
```

这些字段不是“附加数据”，而是核心协议。继续放在 dict 里会让调用方靠字符串 key 编程。

目标结构：

```python
NodeResult
  node_id: str
  runtime: NodeRuntime
  status: NodeStatus
  summary: str
  artifacts: list[Artifact]
  tool_calls: list[ToolCallRecord]
  approval_request: ApprovalRequest | None
  git: GitNodeResult | None
  usage: list[UsageRecord]
  error: NodeError | None
  debug: dict[str, Any]
```

其中 `debug` 只能放非业务关键字段，例如 raw stdout/stderr 摘要、provider 原始路径等。

### 2.3 Artifact 双模型问题

当前存在两套 artifact：

```text
NodeArtifact
ToolArtifact
```

它们之间又通过 dict payload 互转：

```text
NodeArtifact -> ToolArtifact -> artifact_to_payload -> artifact_from_payload -> ToolArtifact
```

同时还兼容：

```text
legacy string artifacts
result.data["tool_artifacts"]
result.data["tool_calls"][].tool_artifacts
manifest artifacts
session relative artifacts
absolute path artifacts
```

目标是收敛成单一协议：

```python
RuntimeArtifact
  id: str
  ref: str
  kind: ArtifactKind
  path: PathRef | None
  mime_type: str | None
  filename: str | None
  size_bytes: int | None
  publish: bool
  source: ArtifactSource
  metadata: dict[str, Any]
```

迁移期允许从旧格式读，但所有新 runtime 只能写结构化 artifact。

### 2.4 DAG 状态机缺失

当前 executor 是同步串行 loop：

```text
pending -> ready -> runtime.run() -> result
```

审批是特殊 blocked result，审批通过后由 Feishu channel 调 `continue_approval()`，不会恢复原 DAG。

目标状态机：

```text
created
  -> ready
  -> running
  -> completed
  -> failed
  -> blocked_waiting_approval
  -> blocked_waiting_user_input
  -> skipped
```

session 也要有状态：

```text
created
running
waiting_approval
waiting_user_input
completed
failed
cancelled
```

审批应该是 DAG 的暂停点，而不是旁路 continuation。

---

## 3. 字段瘦身清单

### 3.1 `PlanNode`

当前字段：

```python
id
runtime
objective
input_refs
expected_output
tool_name
runtime_hints
```

目标字段：

```python
id
runtime
objective
depends_on
artifact_inputs
policy
output_hint
```

处理建议：

| 当前字段 | 处理 | 原因 |
| --- | --- | --- |
| `id` | 保留 | DAG 节点身份 |
| `runtime` | 保留，但去掉 `codex` 别名 | 运行类型应稳定为 `llm/react/coder` |
| `objective` | 保留 | 节点任务描述 |
| `input_refs` | 拆分 | `node:` 和 `artifact:` 混在一起，不利于校验 |
| `expected_output` | 改名为 `output_hint` | 当前不是可执行合同，只是 prompt hint |
| `tool_name` | 删除 | 已被 validator 清空，是遗留兼容字段 |
| `runtime_hints` | 删除或仅迁移期保留 | 当前是隐式总线，应该拆到 `policy/context` |

建议新结构：

```python
class PlanNode:
    id: str
    runtime: Literal["llm", "react", "coder"]
    objective: str
    depends_on: list[str] = []
    artifact_inputs: list[str] = []
    output_hint: str = ""
```

`depends_on` 只允许节点 id，不再允许 `node:x` 字符串。

`artifact_inputs` 只允许 artifact ref，不再允许混合 ref。

### 3.2 `ExecutionPlan`

当前字段：

```python
user_objective
nodes
finalization_hint
usage_records
```

目标字段：

```python
objective
nodes
final_response
```

处理建议：

| 当前字段 | 处理 | 原因 |
| --- | --- | --- |
| `user_objective` | 改名为 `objective` | 简化命名 |
| `nodes` | 保留 | 核心 DAG |
| `finalization_hint` | 收缩成 `final_response` | 当前模式过多，实际只决定 pass-through 或 aggregate |
| `usage_records` | 移除 | usage 是 runtime 观测数据，不属于静态 plan |

建议新结构：

```python
FinalResponseMode = Literal["pass_through", "aggregate"]

class ExecutionPlan:
    objective: str
    nodes: list[PlanNode]
    final_response: FinalResponseMode = "aggregate"
```

如果需要保留原因，放到 planner debug 信息，不进入核心 plan。

已执行迁移：`ExecutionPlan.usage_records` 已移除。Planner LLM 调用产生的 usage 由 `TurnPlannerResult.usage_records` 返回，并通过 `PlanningRouterResult.planner_usage_records` 传给 `TaskAgentRuntime` 汇总；静态 plan 的 JSON 不再携带运行期 usage 观测数据。

已执行迁移：`FinalizationHint.reason` 已移除。当前 `finalization_hint` 只保留影响行为的 `mode` 和 `user_facing`；旧 planner payload 中的 `reason` 会被忽略，调试解释不再进入核心 plan 协议。

已执行迁移：`FinalizationMode` 已收缩为 `pass_through | llm`，删除 `auto` 和 `deterministic` 协议值。新增 `result_aggregator:v4` 并切为默认版本，prompt 文案同步删除 deterministic 分支。

### 3.3 `FastIntentDecision`

当前字段：

```python
route
confidence
runtime
tool_name
input_refs
finalization_hint
usage_records
reply
reason
```

目标字段：

```python
route
confidence
reply
reason
usage
```

处理建议：

| 当前字段 | 处理 | 原因 |
| --- | --- | --- |
| `runtime` | 删除 | validator 最终强制为 None |
| `tool_name` | 删除 | validator 最终强制为 None |
| `input_refs` | 删除 | fast intent 不再生成执行节点 |
| `finalization_hint` | 删除 | fast reply 天然 pass-through |
| `usage_records` | 改名 `usage` | 和其他 runtime 统一 |

已执行迁移：`FastIntentDecision.finalization_hint` 已删除。Fast intent 只输出 `route/confidence/reply/reason/usage_records`；当 router 选择 fast reply 时，由 `fast_reply_plan()` 生成 `pass_through` finalization，而不是从 fast intent 透传。

### 3.4 `AggregationResult`

当前字段：

```python
status
reply
artifact_refs
replan_instructions
missing_info_question
data
usage_records
```

目标字段：

```python
status
reply
artifact_refs
approval_requests
usage
debug
```

处理建议：

| 当前字段 | 处理 | 原因 |
| --- | --- | --- |
| `status` | 保留，但减少枚举 | 当前 `needs_replan` 没闭环 |
| `reply` | 保留 | 用户可见结果 |
| `artifact_refs` | 保留 | 附件解析需要 |
| `replan_instructions` | 删除或延后 | runtime 不消费它 |
| `missing_info_question` | 删除 | 与 `reply` 重复 |
| `data` | 拆出 `approval_requests`，剩余改 `debug` | 核心协议不应藏在 dict |
| `usage_records` | 改名 `usage` | 统一命名 |

已执行迁移：`AggregationResult` 已删除 `needs_replan`、`replan_instructions`、`missing_info_question`，并新增顶层 `approval_requests`。`TaskAgentRuntime` 只读取 `aggregation.approval_requests`；`data["approval_requests"]` 兼容 fallback 已删除。

### 3.5 `NodeResult`

当前字段：

```python
node_id
runtime
status
summary
artifacts
data
error
```

目标字段：

```python
node_id
runtime
status
summary
artifacts
tool_calls
approval_request
git
usage
error
debug
```

关键变化：

1. `approval_requests` 不再藏在 `data`。
2. `node_commit` / `node_merge` 不再藏在 `data`。
3. `tool_calls` 不再藏在 `data`。
4. `usage_records` 不再藏在 `data`。
5. 原始 stdout/stderr 不直接进入用户结果，只进 debug 或 provider log 文件。

已执行迁移：`NodeResult` 已新增顶层 `approval_requests`，coder node 新写入路径不再把 approval request 列表写入 `data["approval_requests"]`。聚合层已删除读取旧 `data["approval_requests"]` 的兼容 fallback。

已执行迁移：`NodeResult` 已新增顶层 `tool_calls` 和 `usage_records`。LLM/React runtime 新写入路径使用顶层字段；artifact 发布和 usage 汇总保留旧 `data["tool_calls"]` / `data["usage_records"]` 读取兼容。coder finalizer 的 usage 已写入顶层 `usage_records`。

已执行迁移：`NodeResult` 已新增顶层 `git` 字段。coder node 新写入路径将 `repo_workspace`、`node_commit`、`node_merge` 写入 `result.git`，不再写入 `result.data`。

已执行迁移：`NodeResult` 已新增顶层 `debug` 字段。coder finalizer 新写入路径将 `provider`、`stdout`、`stderr`、`exit_code`、`finalizer` 诊断信息写入 `result.debug`，不再写入 `result.data`。

已执行迁移：`NodeResult` 已新增顶层 `tool_artifacts` 字段。React runtime 新写入路径会从 tool call 记录中提取结构化工具产物到顶层；coder finalizer 会从 provider metadata 中弹出 `tool_artifacts` 写入顶层，不再把它并入 `result.data`。artifact 发布器只读取 `result.tool_artifacts` 和顶层 `result.tool_calls[].tool_artifacts`，旧 `data["tool_artifacts"]` / `data["tool_calls"]` fallback 已删除。

已执行迁移：coder blocked approval 结果不再把 `approval_id`、`action_kind`、`command`、`path`、`reason` 这些单个审批字段重复写入 `result.data`。审批详情只通过 `result.approval_requests` 表达，聚合层和 channel 层继续使用顶层 approval request 协议。

### 3.6 `runtime_hints`

需要拆成以下对象：

```python
TurnContext:
    conversation_id
    turn_id
    platform
    external_chat_id

TemporalContext:
    current_date
    current_time
    timezone

SessionContext:
    session_id
    root_path
    artifacts_dir
    nodes_dir

NodeWorkspaceContext:
    node_id
    node_dir
    provider_run_dir
    manifest_path

RepoContext:
    repo_id
    project_path
    source_branch
    target_branch
    node_branch
    worktree_mode

RepoExecutionPolicy:
    node_worktree_commit: bool
    push_allowed: bool
    dirty_after_node_allowed: bool
```

迁移期间可以保留一个兼容函数：

```python
RuntimeContext.from_hints(runtime_hints)
RuntimeContext.to_legacy_hints()
```

但新代码不应继续直接读写 `runtime_hints["..."]`。

---

## 4. 模块拆分目标

### 4.1 当前模块问题

`node_execute_runtime.py` 当前承担：

```text
LLM 节点执行
React 节点执行
Coder 节点执行
Skill tool 调用
Tool call 记录
Git context LLM 判断
Git worktree 准备
Git commit/merge
approval request 构造
artifact payload 解析
optional/truncate helper
```

`agent_runtime.py` 当前承担：

```text
turn 加载和状态更新
规划
session workspace 创建
节点执行
结果聚合
artifact 提取、归一化、复制、持久化
attachment 解析
usage 汇总
session state writeback
异常兜底
```

这两个文件应该是第一批拆分对象。

### 4.2 目标目录结构

建议目标结构：

```text
app/task_runtime/
  models/
    plan.py
    result.py
    context.py
    artifact.py
    approval.py
    git.py

  planning/
    router.py
    llm_planner.py
    fallback_planner.py
    fast_intent.py

  execution/
    scheduler.py
    executor.py
    state_store.py
    progress.py

  runtimes/
    llm.py
    react.py
    coder.py

  coder/
    provider.py
    instruction.py
    finalizer.py
    git_context.py

  workspace/
    session.py
    node_workspace.py
    git_workspace.py

  artifacts/
    publisher.py
    resolver.py
    legacy.py

  approvals/
    service.py
    runtime_git.py
```

迁移不是一次性完成。先拆“纯逻辑”模块，再拆状态模型。

---

## 5. DAG 状态机设计

### 5.1 为什么要补状态机

当前 DAG 执行只有最终 report，节点执行过程中没有可恢复的状态协议。

现状问题：

1. 审批 blocked 后，原 DAG 没有恢复入口。
2. executor 不能从 session 中读取已完成节点继续执行。
3. 失败节点没有统一 retry 策略。
4. progress event 和持久状态不是一个来源。
5. `previous_node_results` 是半成品，既不像恢复，也不像跨轮输入。

### 5.2 新状态模型

新增：

```python
NodeExecutionState:
    node_id: str
    status: NodeStatus
    attempts: int
    started_at: str | None
    completed_at: str | None
    result_path: str | None
    blocked_by: BlockReason | None
```

```python
SessionExecutionState:
    session_id: str
    status: SessionStatus
    current_wait: WaitState | None
    completed_order: list[str]
    nodes: dict[str, NodeExecutionState]
```

`session.json` 不再只是创建记录，要成为恢复入口。

### 5.3 审批恢复流程

目标流程：

```text
1. node 执行需要审批
2. node result.status = blocked_waiting_approval
3. session.status = waiting_approval
4. approval request 记录到服务端 store 和 session state
5. 用户确认
6. ApprovalService 校验 approval_id 与服务端记录
7. 执行被批准的 effect
8. 写入 approval effect result
9. 将 blocked node 转为 completed 或继续 running
10. Scheduler 从该 session 继续执行后续 ready nodes
```

需要注意：审批通过后不应该只发送“合并完成”，而应该继续跑 DAG。如果后续没有节点，再聚合最终回复。

### 5.4 Effect 设计

审批本质是 effect：

```python
Effect:
    id
    kind
    node_id
    status
    payload
    approval_required
```

Git merge、push、外部发送、创建日程提醒等都可以是 effect。

先只实现：

```text
runtime_git_merge
codex_provider_approval
```

后续再把 reminder、deliver_file 等纳入 effect 系统。

---

## 6. 迁移计划

### Phase 0: 基线保护

目标：在动结构前保护行为。

任务：

1. 给 `app/task_runtime` 建一组 contract tests。
2. 固定当前核心行为：
   - fast reply 不走 executor；
   - 单 LLM pass-through；
   - react tool call 记录；
   - coder write node 产出 artifact；
   - protected branch 请求审批；
   - artifact delivery；
   - session workspace 创建；
   - planner fallback；
   - result aggregation fallback。
3. 增加两个当前缺失但重要的测试：
   - coder 节点在 Jarvis 管理 worktree 中执行成功后会记录 node commit；
   - coder 节点执行后如果 worktree 仍 dirty，节点失败；
   - 审批后有下游节点时，当前行为不会继续 DAG，用作待修复基线。

验收：

```text
pytest tests/test_task_runtime_e2e.py tests/test_node_executor.py tests/test_session_workspace.py tests/test_approval_runtime.py
```

### Phase 1: 删除无效字段和空扩展点

目标：先删不会改变行为的冗余。

任务：

1. 删除 `PlanNode.tool_name`。
2. 删除 `FastIntentDecision.runtime/tool_name/input_refs`。
3. 删除或停止导出 `NodeRuntime="codex"`，旧 planner payload 中的 `runtime="codex"` 直接判为非法计划并走 fallback。
4. 删除 `runtime="tool"` 到 `react` 的旧兼容映射；工具执行统一由 `react` runtime 内部决策。
5. 删除 `AggregationResult.missing_info_question`。
6. 如果不实现 replan，删除 `AggregationStatus.needs_replan` 和 `replan_instructions`。
7. 将 `expected_output` 改名为 `output_hint`，并删除当前执行路径中的旧 alias。

兼容策略：

1. planner payload 中出现 `expected_output` 时由 `extra=ignore` 丢弃，不再映射到 `output_hint`。
2. 旧 raw payload 读取不做迁移。
3. 测试中直接构造 `PlanNode(expected_output=...)` 的用例同步调整。
4. 已执行迁移：`PlanNode` 核心字段为 `output_hint`，`expected_output` 输入 alias 已删除；新增 `coder_node_execute:v3` 并切为默认版本，prompt 变量也统一为 `output_hint`。旧 prompt 版本 v1/v2 保留历史内容但不在默认路径使用。
5. 已执行迁移：`PlanNode` 不再把旧 `runtime="tool"` 自动归一成 `react`；旧 planner payload 若输出 `tool` runtime 会触发 plan validation fallback。

验收：

1. 所有 task runtime 单测通过。
2. `rg "tool_name" app/task_runtime` 不再出现 plan/fast intent 协议字段，只允许 tool call record 使用。

### Phase 2: 收敛 coder worktree 执行策略

目标：删除 DAG 层权限字段后，把 coder worktree 的真实执行策略收敛到 runtime/provider 边界。

任务：

1. 明确 `coder` runtime 的固定策略：

```python
class RepoExecutionPolicy:
    node_worktree_commit: bool = True
    push_allowed: bool = False
    dirty_after_node_allowed: bool = False
```

2. `PlanNode.runtime_hints` 已从核心模型移除；旧 payload 中的节点级 `runtime_hints` 由 Pydantic extra ignore 丢弃；planner prompt 不再要求节点输出 `runtime_hints`。
3. `CoderRunRequest` 不再携带 policy。
4. `CodexCoderProvider` 和 `ClaudeCodeCoderProvider` 不再传 `allow_commit`、`allow_push`、`_read_only` 这类 DAG 权限字段；provider 只接收 instruction、repo、workdir/run_dir 和 branch context。
5. Jarvis 只在 `SessionWorkspace` 管理的 node worktree 上自动 commit；直接使用注册 repo 时不自动 commit。
6. `commit_node_repo()` 在 commit 后再次检查 `git status --porcelain --untracked-files=all`，仍 dirty 则失败。
7. push 不通过 DAG runtime 开放；未来如需要 push，应作为显式 effect/approval，而不是恢复 `allow_push` 字段。

已执行迁移：新增 `heavy_plan:v5` 并切为默认版本。v5 不再要求节点输出 `runtime_hints`；`PlanNode` 已删除节点级 `runtime_hints` 字段；`NodeExecutor` 和 `CoderNodeExecuteRuntime` 不再合并或读取 `node.runtime_hints`。全局/session/workspace 级 runtime hints 暂时保留，等待 Phase 3 拆成强类型上下文。

已执行迁移：`CodexCoderProvider` 和 `ClaudeCodeCoderProvider` 已停止向底层工具传 `allow_commit`、`allow_push`、`_read_only`。task runtime 的 commit 约束只由 node worktree finalizer / commit 检查表达。

验收：

1. planner 输出旧权限字段或节点级 runtime hints 时会被忽略。
2. coder provider 请求不包含 `allow_commit`、`allow_push`、`_read_only`。
3. node worktree 有文件变化时产生 node commit。
4. node worktree commit 后仍 dirty 会导致节点失败。
5. protected branch merge 仍要求审批。

### Phase 3: 拆 `runtime_hints`

目标：停止新增隐式 key。

任务：

1. 新增 typed runtime context 适配层：

```python
TurnContext
TemporalContext
SessionContext
NodeWorkspaceContext
RepoContext
RepoExecutionPolicy
RuntimeContext
```

短期文件名为 `runtime_context.py`，先提供小型 dataclass 访问器，逐步替换直接读取 `runtime_hints["..."]` / `runtime_hints.get("...")` 的代码；等调用面收敛后再合并成完整 `RuntimeContext`。

2. `NodeExecutionContext` 改成：

```python
NodeExecutionContext:
    user_objective
    node
    resolved_inputs
    context: RuntimeContext
```

3. 保留 `RuntimeContext.to_legacy_hints()`，只给旧 prompt 和 provider 使用。
4. `node_workspace_hints()` 改为返回 `NodeWorkspaceContext`。
5. `SessionWorkspaceRef.runtime_hints()` 标记为 legacy。

已执行迁移：新增 `app/task_runtime/runtime_context.py`，提供 `TemporalRuntimeContext`、`RepoRuntimeContext`、`BranchRuntimeContext`、`NodeWorkspaceRuntimeContext`、`WorkspaceRuntimeContext`。`fast_intent.py` 和 `node_execute_runtime.py` 中的 temporal、repo、provider run dir、branch、manifest path、session workspace/node workspace 读取已改为通过这些 typed accessor；`session_workspace.py` 中 repo worktree 准备路径也已改为通过 typed accessor 读取 node repo dir、session id、branch 和 session root；`node_execute_runtime.py` 只剩 `git_context_usage` 作为 usage 桥接仍直接读取 hints。

验收：

1. 新代码不直接读 `runtime_hints["session_workspace_dir"]`。
2. 新代码不直接读 `runtime_hints["node_manifest_path"]`。
3. Git workspace 准备只接收 `RepoContext/SessionContext/NodeWorkspaceContext`。

### Phase 4: 统一 artifact 协议

目标：消除 `NodeArtifact` 和 `ToolArtifact` 双模型。

任务：

1. 新增 `RuntimeArtifact`。
2. 为旧格式写 adapter：

```python
RuntimeArtifact.from_node_artifact()
RuntimeArtifact.from_tool_artifact()
RuntimeArtifact.from_legacy_string()
```

3. runtime 新输出只能返回 `RuntimeArtifact`。
4. `agent_runtime._artifact_records_from_report()` 下沉到 `artifacts/publisher.py`。
5. 删除 nested `result.data["tool_calls"][].tool_artifacts` 的新写入路径。
6. 工具执行层统一通过 `ToolExecutionResult.tool_artifacts` 传结构化 artifact。

迁移期规则：

```text
读旧格式，写新格式。
```

验收：

1. 新增 artifact publisher 单测。
2. 旧 coder image artifact 用例仍通过。
3. manifest artifact 用例仍通过。
4. absolute path artifact 仍被拒绝。
5. `NodeResult.data["tool_artifacts"]` 不再是新路径。

### Phase 5: 拆 coder Git workspace

目标：让 `CoderNodeExecuteRuntime` 只负责调用 provider 和 finalizer。

任务：

1. 新增 `workspace/git_workspace.py`：

```python
GitWorkspaceService.prepare_node_workspace()
GitWorkspaceService.commit_node_workspace()
GitWorkspaceService.merge_to_target()
```

2. 新增 `coder/git_context.py`：

```python
GitContextResolver.resolve()
```

3. `CoderNodeExecuteRuntime.run()` 变成：

```text
resolve repo context
prepare workspace
build instruction
provider.run
finalize provider result
return NodeResult
```

4. commit/merge 不在 runtime 里直接做，改成返回 effect 或调用 `GitWorkspaceService`。

验收：

1. `node_execute_runtime.py` 行数明显下降。
2. Git workspace 单测不需要实例化 coder runtime。
3. coder provider 单测不需要真实 git repo。

### Phase 6: DAG scheduler 与恢复

目标：让 DAG 真正可暂停、可恢复。

任务：

1. 新增 `execution/scheduler.py`。
2. 新增 `execution/state_store.py`，先用 session files 实现。
3. executor 不再只返回最终 report，要能：

```python
run_until_blocked_or_complete(session_id)
resume(session_id, effect_result)
```

4. `previous_node_results` 从 executor 参数中删除。
5. 跨轮继续执行改成显式 `resume_session_id`。
6. Feishu approval complete 调用 scheduler resume，而不是只调用 approval continuation。

验收：

1. DAG: `node_a -> approval -> node_b`，审批后会执行 `node_b`。
2. Jarvis 进程重启后，pending approval 仍能继续。
3. 重复点击审批卡不会重复 merge。
4. session 状态最终从 `waiting_approval` 变成 `completed`。

### Phase 7: 拆 orchestrator

目标：让 `TaskAgentRuntime` 只负责任务编排。

任务：

1. 新增：

```text
ArtifactPublisher
UsageCollector
SessionStateWriter
TurnFinalizer
```

2. `TaskAgentRuntime.run_turn()` 收缩成：

```text
load turn/context
plan
create session
scheduler.run
aggregate
publish artifacts
finalize turn
writeback session
```

3. artifact、usage、attachment、session writeback 不再写在同一个文件里。

验收：

1. `agent_runtime.py` 只保留主流程和异常处理。
2. artifact 发布逻辑有独立单测。
3. usage footer 逻辑有独立单测。

---

## 7. 设计取舍

### 7.1 不建议一次性重写

原因：

1. 当前测试已经覆盖了不少行为。
2. runtime 涉及 Feishu、Git、Codex、artifact、prompt，重写风险高。
3. 最危险的不是文件组织，而是协议不清。协议应该先稳定。

建议每个 phase 都保持可运行。

### 7.2 不建议继续扩展 `runtime_hints`

新增功能如果继续使用 `runtime_hints`，会抵消本次优化。允许短期兼容，但新设计必须有明确上下文对象。

### 7.3 不建议保留 `data` 作为核心扩展机制

可以保留 `debug` 或 `raw`，但核心字段必须显式化。否则审批恢复、artifact 发布、usage 汇总都会继续靠字符串 key。

### 7.4 不建议让 planner 决定底层 provider

planner 只选择能力：

```text
llm
react
coder
```

不选择：

```text
codex
claude_code
具体 shell 工具
具体 MCP 工具
```

工具选择属于 worker 内部能力。

---

## 8. 需要删除或迁移的重复逻辑

### 8.1 重复 helper

当前重复出现：

```text
_optional_text
_optional_int
_optional_bool
_truncate
_first_text
_artifact_ref
_looks_like_artifact_delivery
_resolve_timezone
_jsonable
```

处理：

1. 基础解析放到 `app/task_runtime/utils/coerce.py`。
2. 文本截断放到 `app/task_runtime/utils/text.py`。
3. 时间上下文放到 `app/task_runtime/models/context.py` 或共享 `TemporalContext`。
4. artifact ref 只保留在 artifact 模块。
5. fallback intent 判断只保留在 `planning/fallback_planner.py`。

### 8.2 fallback planner 重复

当前 `planning_router.py` 和 `planner.py` 都有 artifact delivery 和 fallback single node 逻辑。

目标：

```text
planning/fallback_planner.py
  fallback_fast_reply_plan
  fallback_single_llm_plan
  fallback_artifact_delivery_plan
  fallback_repo_plan
  fallback_code_decomposition_plan
```

`PlanningRouter` 不直接构造复杂 fallback plan，只调用 `FallbackPlanner`。

### 8.3 temporal context 重复

当前 temporal hints 在 `agent_runtime.py` 生成，在 `planner.py` 补齐，在 `fast_intent.py`、`node_execute_runtime.py` 再提取。

目标：

```python
TemporalContext.now(settings.default_timezone)
```

由 `TaskAgentRuntime` 创建一次，后续传对象。

---

## 9. 测试计划

### 9.1 Contract tests

新增 `tests/task_runtime_contract/`，覆盖：

1. plan schema validation；
2. node dependency validation；
3. artifact input resolution；
4. permission policy；
5. approval wait/resume；
6. artifact publishing；
7. aggregation fallback；
8. session state persistence。

### 9.2 Golden raw payload tests

保留少量 raw payload golden case，防止 Feishu/card/attachment 集成断裂。

重点检查：

```text
raw_payload["approval_requests"]
raw_payload["artifacts"]
raw_payload["attachments"]
raw_payload["usage_records"]
```

迁移完成后 raw payload 可以变化，但必须同步 Feishu channel 和 artifact store。

### 9.3 State machine tests

新增：

```text
test_scheduler_runs_ready_nodes
test_scheduler_blocks_on_failed_dependency
test_scheduler_waits_for_approval
test_scheduler_resumes_after_approval
test_scheduler_is_idempotent_for_duplicate_approval
test_scheduler_recovers_from_session_files
```

### 9.4 Git workspace tests

把当前 `test_session_workspace.py` 中 Git 行为拆成：

```text
test_git_workspace_prepare_node_branch
test_git_workspace_commit_node_changes
test_git_workspace_merge_to_feature_branch
test_git_workspace_requests_approval_for_main
test_git_workspace_rejects_dirty_integration_tree
```

### 9.5 Artifact tests

覆盖：

```text
manifest artifact
tool artifact
legacy string artifact
absolute path rejection
session relative path promotion
duplicate artifact id
missing file artifact
image attachment resolution
```

---

## 10. 验收标准

### 10.1 结构指标

优化完成后：

1. `node_execute_runtime.py` 不超过 500 行。
2. `agent_runtime.py` 不超过 400 行。
3. `runtime_hints` 不再作为新代码的主要输入。
4. `NodeResult.data` 不包含 approval/git/tool_calls/usage 这些核心字段。
5. `PlanNode` 不再包含 `tool_name`。
6. `ExecutionPlan` 不再包含 `usage_records`。
7. artifact 新写入路径只有一个结构化模型。

### 10.2 行为指标

必须保持：

1. fast reply 低延迟路径仍可用。
2. 普通 LLM 节点仍可 pass-through。
3. React 节点仍能调用工具。
4. Coder 节点仍能使用 Codex/Claude provider。
5. 产物仍能返回飞书附件。
6. protected branch merge 仍需审批。
7. 审批通过后可以继续原 DAG。
8. session 文件可以用于排查和恢复。

### 10.3 安全指标

必须做到：

1. DAG 层不存在 `access_mode/allow_commit/allow_push` 权限字段。
2. approval payload 不信任客户端回传。
3. runtime git approval 必须校验 approval id、repo、branch、commit。
4. artifact absolute path 默认拒绝，除非来自受信根目录。
5. info 日志不输出完整 planner input 和 LLM raw response。

---

## 11. 推荐执行顺序

优先级从高到低：

1. Phase 0: 补基线测试。
2. Phase 2: 收敛 coder worktree 执行策略。
3. Phase 1: 删除明显无效字段。
4. Phase 3: 拆 `runtime_hints`。
5. Phase 4: 统一 artifact。
6. Phase 5: 拆 coder Git workspace。
7. Phase 6: DAG scheduler 恢复。
8. Phase 7: 收缩 `TaskAgentRuntime`。

原因：

1. worktree 写入、提交和 push 边界是实际风险，优先级最高。
2. 字段删除要在测试保护后做。
3. `runtime_hints` 和 artifact 是复杂度核心，但改动面大，需要分批。
4. scheduler 恢复依赖前面的状态模型收敛，不宜一开始做。

---

## 12. 第一轮可落地任务清单

第一轮建议控制在 1 到 2 个 PR：

### PR 1: coder worktree 策略与无效字段瘦身

改动：

1. 删除 DAG 层 `access_mode/allow_commit/allow_push`。
2. 固定 coder provider 参数为可 commit、不可 push、非 readonly。
3. 修复 coder 自动 commit 条件：只对 Jarvis 管理 node worktree 自动 commit。
4. commit 后校验 node worktree 不能 dirty。
5. 删除 `FastIntentDecision.runtime/tool_name/input_refs`。
6. 删除 `PlanNode.tool_name`。
7. 新增 `heavy_plan:v3`，停止要求 planner 输出 `access_mode`。

风险：

1. 旧 prompt 版本里可能仍要求 planner 输出 `expected_output`。
2. 测试里有旧字段构造。

缓解：

1. 保留输入兼容 alias。
2. 分别更新 prompt 和测试。

### PR 2: AggregationResult 和 approval 字段显式化

改动：

1. `AggregationResult` 增加 `approval_requests` 字段。
2. `_fallback_aggregation()` 不再把 approval 放进 `data`。
3. `TaskAgentRuntime` 从 `aggregation.approval_requests` 读取。
4. 保留从 `data["approval_requests"]` 读取的兼容 fallback。
5. 删除 `missing_info_question`。

状态：已落地。新写入路径使用 `aggregation.approval_requests`，raw payload 继续写顶层 `approval_requests` 供 Feishu channel 使用。

风险：

1. Feishu channel 依赖 raw payload。

缓解：

1. raw payload 仍写 `approval_requests` 顶层字段。
2. 加 Feishu approval card 测试。

---

## 13. 最终目标示意

最终主流程应该接近：

```text
TaskAgentRuntime.run_turn
  -> build RuntimeContext
  -> PlanningRouter.plan
  -> SessionStateStore.create
  -> Scheduler.run_until_wait_or_complete
  -> ResultAggregator.aggregate
  -> ArtifactPublisher.publish
  -> TurnFinalizer.finalize
```

审批恢复流程：

```text
Feishu approval callback
  -> ApprovalService.resolve(approval_id)
  -> ApprovalService.apply(decision)
  -> Scheduler.resume(session_id)
  -> ResultAggregator.aggregate when complete
  -> TurnFinalizer.send/update message
```

代码节点流程：

```text
CoderRuntime.run
  -> GitContextResolver.resolve
  -> GitWorkspaceService.prepare
  -> CoderInstructionBuilder.build
  -> CoderProvider.run
  -> CodeNodeFinalizer.finalize
  -> GitWorkspaceService.commit_or_effect
  -> NodeResult
```

artifact 流程：

```text
Runtime returns RuntimeArtifact[]
  -> ArtifactPublisher validates paths
  -> ArtifactPublisher promotes to session artifacts dir
  -> ArtifactStore persists
  -> ChannelAttachmentResolver resolves attachments
```

这个结构的关键收益是：每个层只处理自己的协议，不再靠 `dict[str, Any]` 在全链路传隐式状态。
