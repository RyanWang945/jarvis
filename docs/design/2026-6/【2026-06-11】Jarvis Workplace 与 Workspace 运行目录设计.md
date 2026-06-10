# Jarvis Workplace 与 Workspace 运行目录设计

| 项目 | 内容 |
| --- | --- |
| 日期 | 2026-06-11 |
| 状态 | Draft |
| 目标 | 将 plan/node 运行目录、资源 scope、artifact 产物和 coder workdir 语义拆清楚 |
| 相关模块 | `app/task_runtime`, `app/tools`, `app/agent_react/artifacts.py`, `app/repositories/registry.py` |

## 1. 背景

当前 Jarvis vNext 已经形成了比较清晰的 DAG runtime：

```text
PlanningRouter
  -> ExecutionPlan / PlanNode
NodeExecutor
  -> LLMNodeExecuteRuntime | ReactNodeExecuteRuntime | CoderNodeExecuteRuntime
ResultAggregator
```

但文件和目录语义仍然分散：

1. `PlanNode` 只有 `runtime_hints`，没有一等 workspace 描述。
2. `NodeExecutor` 只合并 global hints 和 node hints 后传给 runtime。
3. `CoderNodeExecuteRuntime` 通过 repository registry 把 `active_repo` 解析成 repo root，并作为 `CoderRunRequest.workdir`。
4. Codex provider 自己创建 `data/coder_runs/<uuid>` 作为运行日志目录。
5. `read_file`、`search_files`、`write_file` 目前默认使用 `settings.workspace_root`，scope 过大。
6. artifact 有独立表，但还没有稳定记录 artifact 来自哪个 workplace/node 目录。

因此，如果直接在 planner 后“创建一个工作目录”，再让每个 node 也有自己的工作目录，容易把以下概念混在一起：

```text
Jarvis 运行容器
工具可访问 scope
代码仓库 cwd
provider 运行日志目录
可交付 artifact 目录
临时 scratch 目录
```

本设计目标是把这些概念拆开，并给出可渐进落地的实现路径。

## 2. 结论

建议引入两个一等概念：

```text
Workplace:
  一次 turn/plan 的运行容器，负责承载本轮 scratch、node 目录、运行日志和生成产物。

Workspace:
  一个可访问资源的受控视图，负责表达 resource type、scope、purpose 和权限边界。
```

核心原则：

1. 每个 planned turn 默认创建一个 task-level workplace。
2. 每个 node 默认拥有自己的 node directory。
3. node directory 是 scratch/log/artifact staging 区，不等同于所有 runtime 的 process cwd。
4. coder 的 `workdir` 继续是 repository root，由 repository registry 解析。
5. coder 的 `run_dir` 应由 Jarvis runtime 创建，放在 node directory 或 workplace runs 目录下。
6. file tools 必须基于 workspace scope 解析路径，不能继续默认扩大到整个 `settings.workspace_root`。
7. planner 不输出绝对路径，不直接决定目录，只表达任务意图和粗粒度 runtime。
8. artifact 从 node 私有目录 promotion 到可交付 artifact 记录，跨 node 或跨 turn 依赖 artifact id，不依赖兄弟 node 私有路径。

推荐目录布局：

```text
data/workplaces/
  conversations/
    <conversation_id>/
      turns/
        <turn_id>-<plan_id>/
          manifest.json
          shared/
          nodes/
            <node_id>/
              scratch/
              logs/
              artifacts/
              run/
          artifacts/
          tmp/
```

其中：

```text
shared:
  本 plan 内多个 node 可共享的中间资料。

nodes/<node_id>/scratch:
  node 私有临时文件。

nodes/<node_id>/logs:
  Jarvis runtime 或 tool loop 日志。

nodes/<node_id>/run:
  provider 原生事件、stderr、audit 等 run_dir 内容。

nodes/<node_id>/artifacts:
  node 生成的候选产物。

artifacts:
  已登记或准备交付的产物副本/预览/索引。

tmp:
  可安全清理的短期临时文件。
```

## 3. 概念边界

### 3.1 Workplace

Workplace 是 Jarvis runtime 为一次计划执行创建的运行容器。

建议字段：

```python
@dataclass(frozen=True)
class WorkplaceRef:
    workplace_id: str
    conversation_id: int
    turn_id: int
    plan_id: str
    root_path: Path
    status: Literal["created", "running", "completed", "failed", "blocked", "cleaned"]
```

职责：

1. 提供本轮 task-level root。
2. 提供 node-level directory 分配。
3. 记录 manifest，便于审计和恢复。
4. 为 cleanup 提供生命周期边界。
5. 为 artifact resolver 提供 allowed root。

不负责：

1. 决定某个 tool 是否有权限访问 repo。
2. 替代 repository registry。
3. 替代 artifact 表。
4. 直接把所有文件都暴露给 LLM。

### 3.2 Workspace

Workspace 是资源访问 scope，不一定对应一个新目录。

建议结构：

```python
@dataclass(frozen=True)
class WorkspaceScope:
    workspace_type: Literal["task", "node", "artifact", "repository", "knowledge", "system"]
    workspace_id: str
    root_path: Path | None
    path_scope: Literal["temp", "generated", "docs", "source", "test", "config", "secret", "external"] | None
    purpose: Literal[
        "research",
        "document",
        "delivery",
        "repo_analysis",
        "code_change",
        "verification",
    ]
    permissions: frozenset[str]
```

典型映射：

| workspace_type | workspace_id | root_path | 用途 |
| --- | --- | --- | --- |
| `task` | `workplace_id` | workplace root | 本轮临时工作区 |
| `node` | `workplace_id:node_id` | node directory | node 私有 scratch/log/artifact |
| `artifact` | `artifact_id` | artifact path parent 或 resolver 结果 | 历史产物读取/交付 |
| `repository` | `repo_id` | registered repo root | 仓库读写、review、测试 |
| `knowledge` | `vault_id` / kb id | vault 或 kb root | wiki/知识库 |
| `system` | `host` | null 或受限 root | 系统级服务，不默认开放文件访问 |

### 3.3 workdir

`workdir` 表示 provider 或 subprocess 的 process cwd。

规则：

1. coder `workdir` 只能来自 repository registry。
2. react 的普通 tool 默认不应使用 repo root 作为 cwd。
3. shell 类工具如果保留，应由 permission guard 决定 cwd 和命令权限。
4. planner 不输出 `workdir`。
5. LLM 不应自由传绝对 `workdir`。

### 3.4 run_dir

`run_dir` 是 provider 的原生日志与审计目录。

规则：

1. `CoderRunRequest.run_dir` 应由 `CoderNodeExecuteRuntime` 创建。
2. provider 只写入 run_dir，不自己决定 run_dir 根。
3. run_dir 不是用户产物目录。
4. raw events、stderr、approval requests、jarvis audit 都写入 run_dir。

推荐：

```text
data/workplaces/.../nodes/<node_id>/run/
  codex-events.jsonl
  codex-stderr.log
  codex-approval-requests.json
  jarvis-audit.log
```

兼容期可以继续保留 `data/coder_runs/<run_id>`，但目标形态应由 runtime 传入 `run_dir`。

### 3.5 artifact directory

artifact directory 用于可交付产物。

规则：

1. tool 或 provider 生成的候选文件先进入 node artifacts。
2. runtime 将可交付文件登记为 `ToolArtifact`。
3. `TaskAgentRuntime` 持久化 artifact 表。
4. channel delivery 只依赖 artifact id 和 resolver，不直接依赖模型输出路径。
5. 跨 turn 引用通过 artifact 表，不通过聊天文本中的路径。

## 4. 执行链路

### 4.1 Plan 创建后分配 workplace

目标链路：

```text
TaskAgentRuntime.run_turn
  -> PlanningRouter.plan
  -> WorkplaceManager.create_for_plan(turn, plan)
  -> runtime_hints["workplace"] = workplace summary
  -> NodeExecutor.execute(...)
```

`workplace summary` 可以包含：

```json
{
  "workplace_id": "wp_123_456_abcd",
  "root": "data/workplaces/conversations/123/turns/456-abcd",
  "shared_dir": "shared",
  "artifact_dir": "artifacts"
}
```

注意：传给 LLM 的上下文不一定需要暴露绝对 root。对 LLM 更适合暴露 stable id 和允许的相对路径规则。

### 4.2 Node 执行前分配 node directory

`NodeExecutor` 在调用 runtime 前创建 node workspace：

```text
WorkplaceManager.prepare_node(workplace, node_id)
  -> nodes/<node_id>/
       scratch/
       logs/
       artifacts/
       run/
```

然后构造：

```python
NodeExecutionContext(
    ...,
    runtime_hints={
        ...,
        "workplace": {...},
        "node_workspace": {...},
    },
)
```

长期目标是给 `NodeExecutionContext` 增加显式字段：

```python
@dataclass(frozen=True)
class NodeExecutionContext:
    user_objective: str
    node: PlanNode
    resolved_inputs: list[ResolvedInput]
    runtime_hints: dict[str, Any]
    instructions: list[str]
    workplace: WorkplaceRef | None = None
    node_workspace: WorkspaceScope | None = None
```

短期可用 `runtime_hints` 过渡，但不要让 `runtime_hints` 成为长期目录模型。

### 4.3 React runtime

React runtime 的默认 workspace：

```text
workspace_type=node
purpose=research/document/delivery
root=nodes/<node_id>
```

行为建议：

1. `read_file/search_files` 只允许在明确 workspace scope 中执行。
2. 如果任务需要读取 repo 文档，必须带 repository workspace scope，并由 permission guard 判断 path_scope。
3. `write_file` 默认写 node scratch 或 generated docs，不直接写 repo root。
4. artifact delivery 优先通过 artifact id。

React 不应执行：

```text
shell
git
test/build
multi-file repo analysis
code agent delegation
```

这些应进入 coder runtime。

### 4.4 Coder runtime

Coder runtime 同时使用两个目录概念：

```text
workdir = repository root
run_dir = node run directory
```

执行链路：

```text
CoderNodeExecuteRuntime
  -> resolve repo_id via RepositoryRegistry
  -> create node run_dir
  -> collect preflight in repo workdir
  -> CoderRunRequest(workdir=repo.root, run_dir=node.run)
  -> provider.run(...)
  -> collect postflight in repo workdir
  -> normalize artifacts
```

`CoderRunRequest` 目标形态：

```python
CoderRunRequest(
    repo_id=repo.repo_id,
    workdir=repo.canonical_root_path,
    run_dir=node_workspace.run_dir,
    instruction=...,
    policy=...,
)
```

provider 不应自行选择 `data/coder_runs/<uuid>`；它只使用 request 中的 `run_dir`。如果 `run_dir` 为空，provider 可以临时 fallback 到旧路径，但应记录 deprecation warning。

### 4.5 Artifact persistence

artifact 表建议补充来源信息。

短期可以写入 `ArtifactRecord.metadata`：

```json
{
  "workplace_id": "wp_123_456_abcd",
  "node_id": "generate_image",
  "node_workspace": "wp_123_456_abcd:generate_image",
  "relative_path": "nodes/generate_image/artifacts/result.png"
}
```

长期可考虑字段：

```sql
ALTER TABLE artifacts
  ADD COLUMN workplace_id VARCHAR(128) NULL,
  ADD COLUMN node_id VARCHAR(128) NULL;
```

artifact resolver 的 allowed roots 应包含：

1. registered repository roots。
2. current workplace artifact roots。
3. current workplace node artifact roots。
4. `data/artifact_previews`。
5. 兼容期的 `data/coder_runs`。

不应默认把整个 `settings.workspace_root` 都当成可交付 root。

## 5. 数据模型

### 5.1 manifest.json

每个 workplace 创建 manifest：

```json
{
  "version": 1,
  "workplace_id": "wp_123_456_abcd",
  "conversation_id": 123,
  "turn_id": 456,
  "plan_id": "abcd",
  "status": "running",
  "created_at": "2026-06-11T00:00:00+08:00",
  "updated_at": "2026-06-11T00:00:00+08:00",
  "plan": {
    "user_objective": "...",
    "node_ids": ["research", "review"]
  },
  "nodes": {
    "research": {
      "runtime": "react",
      "status": "created",
      "relative_path": "nodes/research"
    },
    "review": {
      "runtime": "coder",
      "status": "created",
      "relative_path": "nodes/review"
    }
  },
  "artifacts": []
}
```

manifest 是文件层审计，不替代数据库。数据库仍是跨 turn 查询主路径。

### 5.2 数据库记录

一期可以不新增表，先写入：

1. `turns.metadata.workplace`
2. `assistant_message.raw_payload.workplace`
3. `artifacts.metadata.workplace_id`
4. `artifacts.metadata.node_id`

二期建议新增表：

```sql
CREATE TABLE workplaces (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    workplace_id VARCHAR(128) NOT NULL,
    conversation_id BIGINT UNSIGNED NOT NULL,
    turn_id BIGINT UNSIGNED NOT NULL,
    plan_id VARCHAR(128) NOT NULL,
    root_path TEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
    metadata JSON,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    UNIQUE KEY uk_workplaces_workplace_id (workplace_id),
    KEY idx_workplaces_conversation_created (conversation_id, created_at),
    KEY idx_workplaces_turn (turn_id)
);
```

以及：

```sql
CREATE TABLE workspace_nodes (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    workplace_id VARCHAR(128) NOT NULL,
    node_id VARCHAR(128) NOT NULL,
    runtime VARCHAR(32) NOT NULL,
    root_path TEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
    metadata JSON,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    UNIQUE KEY uk_workspace_nodes_workplace_node (workplace_id, node_id)
);
```

是否新增表取决于是否需要 UI 查询、恢复和 cleanup 可观测性。MVP 可以先用 metadata。

## 6. Tool path resolution

### 6.1 当前问题

当前工具默认 root：

```text
read_file/search_files -> settings.workspace_root
write_file             -> settings.workspace_root
shell path guard       -> settings.workspace_root
deliver_file path      -> request.workdir 或 raw path
```

这会导致：

1. React 节点可以无意间搜索整个 Jarvis repo。
2. `write_file` 默认写项目根。
3. file tool 没有表达 task/artifact/repository/knowledge scope。
4. 权限策略无法基于 workspace/purpose 判断。

### 6.2 目标接口

文件工具目标参数：

```json
{
  "workspace_id": "wp_123_456_abcd:node:research",
  "path": "scratch/notes.md"
}
```

或 repository 文档读取：

```json
{
  "workspace_id": "repo:jarvis",
  "path": "docs/design/..."
}
```

兼容期允许不传 `workspace_id`，但降级策略必须保守：

| 工具 | 无 workspace_id 时 |
| --- | --- |
| `read_file` | 只允许当前 node workspace 或明确 artifact path |
| `search_files` | 默认搜索当前 node workspace，不搜索 repo |
| `write_file` | 默认写当前 node scratch/generated docs |
| `deliver_file` | 优先 artifact_id；path fallback 只能在 allowed workspace roots |

### 6.3 Path guard

所有路径解析必须遵守：

1. 禁止绝对路径作为 LLM 输入。
2. 禁止 `..` escape。
3. resolve 后必须在 workspace root 内。
4. 禁止 `.git`、secret、env、key、db、log 等敏感路径，除非工具和 policy 明确允许。
5. 写入时父目录不能是 symlink/junction。
6. 已存在目标不能是 symlink。
7. repository workspace 中的 write 必须经过 repository grant / permission guard。

## 7. 生命周期与清理

Workplace status：

```text
created
running
completed
failed
blocked
cleaned
```

清理策略建议：

| 状态 | 默认保留 |
| --- | --- |
| `completed` | 7-30 天 |
| `failed` | 30 天 |
| `blocked` | 30-90 天 |
| `running` | 启动时 stale detection |
| `cleaned` | 只保留 DB/manifest 摘要 |

清理规则：

1. 已登记 artifact 不应被直接删除，除非 artifact status 也进入 expired/cleaned。
2. 已交付 artifact 的原文件删除前，应确认有可恢复副本或明确标记 missing。
3. raw provider events 可以较早压缩或只保留 audit summary。
4. cleanup 需要写审计记录，不能静默删除。

## 8. 权限关系

Workplace 不是授权本身。

正确关系：

```text
workspace scope + tool/action + input + actor + purpose
  -> PermissionGuard
  -> allow | ask | deny
```

默认建议：

| actor | workspace | read | write |
| --- | --- | --- | --- |
| `llm` | none | n/a | n/a |
| `react` | node/task | allow | allow generated/docs |
| `react` | artifact | allow metadata/read deliverable | deny mutation |
| `react` | repository docs | allow with path_scope docs | ask/deny |
| `react` | repository source/test | ask/deny | deny |
| `coder` | repository | allow | by `access_mode` |
| `coder` | node run_dir | allow | allow logs |

workspace-scoped approval 可以表达：

```json
{
  "scope": "workspace",
  "workspace_id": "repo:jarvis",
  "capability": "write_files",
  "expires_at": "..."
}
```

但最终执行前仍必须检查 path 和 action。

## 9. 迁移计划

### Phase 0: 设计对齐

1. 保持 planner prompt 不输出目录。
2. 继续使用 `runtime_hints.access_mode` 表达 coder read/write。
3. 文档明确 workplace/workspace/workdir/run_dir/artifact_dir 边界。

### Phase 1: WorkplaceManager MVP

新增：

```text
app/task_runtime/workplace.py
```

职责：

1. `create_for_plan(conversation_id, turn_id, plan) -> WorkplaceRef`
2. `prepare_node(workplace, node_id, runtime) -> NodeWorkspaceRef`
3. 写入 `manifest.json`
4. 更新 manifest node status
5. 提供 relative path helpers

接入点：

1. `TaskAgentRuntime.run_turn` 在 plan 后创建 workplace。
2. `NodeExecutor.execute` 在 node run 前创建 node directory。
3. raw_payload 写入 workplace 摘要。

### Phase 2: Coder run_dir 下沉

1. `CoderNodeExecuteRuntime` 创建/接收 node run_dir。
2. `CoderRunRequest.run_dir` 必填或优先使用。
3. `CodexCoderProvider` 使用 request.run_dir。
4. `ClaudeCodeCoderProvider` 使用 request.run_dir。
5. 旧 `data/coder_runs/<uuid>` 作为 fallback，并记录 warning。

### Phase 3: File tools scope 化

1. 新增 `WorkspaceResolver`。
2. `read_file/search_files/write_file/deliver_file` 支持 `workspace_id`。
3. 无 `workspace_id` 时降级到当前 node workspace。
4. repository path 访问进入 permission guard。
5. 测试覆盖 path escape、absolute path、repo docs read、repo source write deny。

### Phase 4: Artifact 来源记录

1. artifact metadata 写入 `workplace_id/node_id/relative_path`。
2. recent artifact context 展示来源。
3. resolver allowed roots 加入 workplace artifact roots。
4. cleanup 避免删除可交付 artifact。

### Phase 5: DB 表与恢复

当需要 UI 查询、恢复、长运行任务观察时，再新增 `workplaces` 和 `workspace_nodes` 表。

## 10. 测试要点

### Unit

1. WorkplaceManager 创建稳定目录和 manifest。
2. Node workspace 创建幂等。
3. workplace id 不包含用户输入原文。
4. path resolver 拒绝绝对路径和 `..` escape。
5. path resolver 拒绝 symlink/junction escape。
6. file tools 默认只访问 node workspace。
7. repository workspace 必须通过 repo id 解析。

### Runtime

1. plan created 后 raw_payload 包含 workplace summary。
2. node_started progress 包含 workplace_id/node_workspace_id。
3. coder request 的 `workdir` 是 repo root。
4. coder request 的 `run_dir` 是 node run directory。
5. read-only coder 造成 repo diff 时失败。
6. react 不会默认搜索整个 `settings.workspace_root`。

### Artifact

1. node 产物能登记为 artifact。
2. artifact metadata 包含 workplace_id/node_id。
3. deliver_file 使用 artifact_id 成功交付。
4. path fallback 只允许 current workplace allowed roots。
5. cleanup 不删除 available artifact。

### Regression

1. 旧 `data/coder_runs` artifact 仍可解析。
2. 旧 recent artifact context 不因缺少 workplace metadata 失败。
3. `CodexNodeExecuteRuntime` 兼容 wrapper 仍返回 runtime=`coder`。

## 11. 风险与取舍

### 11.1 不要让 planner 管目录

如果让 planner 输出目录，会重新引入裸路径风险：

```text
LLM -> arbitrary path -> tool execution
```

目录必须由 Jarvis runtime 分配，planner 最多表达：

```text
runtime
objective
input_refs
expected_output
access_mode
```

### 11.2 不要把 coder workdir 改成 node dir

coder 需要在 repository root 下运行，原因：

1. git status/diff/commit 以 repo root 为自然边界。
2. 测试/build 通常依赖 repo cwd。
3. repository registry 已经承担授权边界。
4. preflight/postflight 逻辑基于 repo root。

node dir 应作为 `run_dir` 和 artifact staging，而不是 coder cwd。

### 11.3 不要只创建目录而不改 tool scope

如果 file tools 仍默认扫 `settings.workspace_root`，workplace 只是新增了一批目录，不能解决权限和污染问题。

真正的收益来自：

```text
workspace-aware path resolver
permission guard
artifact source tracking
cleanup lifecycle
```

## 12. 推荐下一步

优先实现最小闭环：

1. 新增 `WorkplaceManager`。
2. plan 后创建 workplace。
3. node 前创建 node directory。
4. raw_payload 和 progress 记录 workplace 信息。
5. `CoderRunRequest.run_dir` 使用 node run directory。

暂不立刻改 planner schema，不让 LLM 看到绝对目录。

随后再改 file tools 的 workspace scope。这个阶段完成后，Jarvis 才真正具备“每次 plan 有运行容器、每个 node 有隔离目录、每次文件访问有明确 scope”的基础。
