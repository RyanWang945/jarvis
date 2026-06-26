# Jarvis Workspace 运行目录设计

| 项目 | 内容 |
| --- | --- |
| 日期 | 2026-06-12 |
| 状态 | Draft |
| 目标 | 固定 Jarvis 工作目录、session 目录和 DAG 节点目录的职责边界 |
| 相关模块 | `app/task_runtime`, `app/tools`, `app/repositories`, `app/agent_react/artifacts.py` |

## 1. 核心结论

`jarvis` 所在目录就是 Jarvis 工作目录，记为 `JARVIS_WORKDIR`。

一次新的 session 对应一个新的执行目录。Planner 先生成完整 DAG，DAG 校验通过后，runtime 再一次性创建 session 目录和所有 node 目录。目录结构是 runtime 的固定协议，不由 Planner、LLM、tool 或 coder provider 临时决定。

```text
用户请求
  -> Planner 生成完整 DAG
  -> 校验 DAG 节点唯一、依赖合法、无环
  -> 创建 session/node 目录
  -> NodeExecutor 按依赖执行节点
  -> 汇总结果与产物
```

## 2. 目录结构

```text
{JARVIS_WORKDIR}/
  config/
    repositories.json

  projects/
    {repo_id}/

  sessions/
    {session_id}/
      session.json
      dag.json
      summary.md
      artifacts/
      approvals/
      nodes/
        {node_id}/
          input_snapshot.md
          output.md
          result.json
          repo/
            {repo_id}/
          provider_run/
```

说明：

1. `config/repositories.json` 是允许 Jarvis 操作哪些仓库的受信来源。
2. `projects/{repo_id}` 是长期复用的本地仓库事实源和 git 数据缓存。
3. `nodes/{node_id}/repo/{repo_id}` 是该节点自己的代码工作区。
4. `sessions/{session_id}` 是一次完整 DAG 的执行记录。
5. `nodes/{node_id}` 是单个 DAG 节点的输入、输出、结果和 provider 日志目录。
6. `provider_run/` 存放 Codex、Claude Code 等 provider 的原生日志、stderr、approval request 和 Jarvis audit。
7. `artifacts/` 只放本次 session 可登记或可交付的产物，不默认暴露整个 repo。

## 3. 概念边界

### 3.1 JARVIS_WORKDIR

`JARVIS_WORKDIR` 是 Jarvis 运行根目录。开发态默认是当前 `jarvis` 项目目录；打包后可以显式配置。

如果目录不可写，启动或创建 session 时直接失败，并提示用户配置可写的 `JARVIS_WORKDIR`。

### 3.2 session

session 是文件系统中的一次 DAG 执行目录。

规则：

1. 每次完整 DAG 执行创建一个新 session 目录。
2. `session.json` 记录 `session_id`、`turn_id`、`conversation_id`、状态、时间和可选 `repo_id`。
3. `dag.json` 保存 planner 输出的最终 DAG。
4. session 可以和数据库里的 `turn_id` 关联，但不替代 `turns/messages/tool_calls`。

### 3.3 node

node 目录是 DAG 节点的私有运行目录。

规则：

1. node 目录只在完整 DAG 生成后统一创建。
2. `node_id` 必须来自校验后的 DAG，并做 path-safe 处理。
3. 节点运行前写入 `input_snapshot.md`。
4. 节点完成后写入 `output.md` 和 `result.json`。
5. 需要代码的节点从 `projects/{repo_id}` 创建自己的 `repo/{repo_id}`。
6. provider 只能写入当前 node 的 `provider_run/`，不自己选择日志目录。

### 3.4 project

project 是已注册仓库的长期本地事实源。它用于复用 git 数据、减少重复 clone 成本，不作为代码修改任务的直接工作目录。

```text
projects/{repo_id}
```

规则：

1. `repo_id` 只能来自 `config/repositories.json`。
2. 不允许用户或 Planner 直接指定任意本地 project path。
3. `projects/{repo_id}` 可以是普通 git clone，也可以后续演进为 bare mirror。
4. 修改代码前，节点从 project 创建自己的 node repo。
5. node repo 优先通过 `git worktree` 创建；不支持时可 fallback 到 `git clone --reference`。
6. 代码修改发生在 `sessions/{session_id}/nodes/{node_id}/repo/{repo_id}`。
7. session 目录和 project 目录必须分离，避免任务改动污染长期事实源。

### 3.5 node repo

node repo 是单个代码节点的隔离代码工作区。

```text
sessions/{session_id}/nodes/{node_id}/repo/{repo_id}
```

规则：

1. node repo 由当前节点创建，不在 Planner 前创建。
2. 不同代码节点默认各自拥有独立 node repo。
3. coder 的 process `workdir` 是当前 node repo。
4. coder 的 `run_dir` 是 `sessions/{session_id}/nodes/{node_id}/provider_run`。
5. 如果要合并多个代码节点的结果，应由显式集成节点处理。
6. session 结束后可以保留用于审计或恢复，也可以按清理策略删除。

## 4. Runtime 职责

### 4.1 Planner

Planner 只生成 DAG。

禁止：

1. 输出本地绝对路径。
2. 创建目录。
3. 直接 clone、checkout、commit、push。
4. 从自然语言中生成受信本地路径或受信 Git URL。

### 4.2 DAG Runtime

DAG Runtime 负责目录协议。

职责：

1. 为完整 DAG 创建 `sessions/{session_id}`。
2. 为每个 node 创建固定目录。
3. 写入 `session.json` 和 `dag.json`。
4. 把当前 session/node 路径注入 `NodeExecutionContext` 或 `runtime_hints`。
5. 更新节点状态和结果文件。

### 4.3 Coder Runtime

Coder Runtime 负责代码执行。

规则：

1. `workdir = sessions/{session_id}/nodes/{node_id}/repo/{repo_id}`。
2. `run_dir = sessions/{session_id}/nodes/{node_id}/provider_run`。
3. provider 不再自行创建 `data/coder_runs/<uuid>`，兼容期可以作为 fallback。
4. 写代码任务必须经过仓库权限和分支策略。
5. commit / push 必须用户审批。

## 5. Artifact 规则

1. node 内部临时文件默认不是可交付 artifact。
2. 可交付产物必须登记到 session 的 `artifacts/` 或 artifact 表。
3. repo 文件不能默认作为附件交付。
4. 如果要交付 repo 文件，必须复制或登记，并记录 `source_repo_id`、`source_relative_path` 和 `source_commit`。
5. artifact resolver 默认允许 session artifact root，不默认允许 repository root。

## 6. 最小实现顺序

1. 增加 `JARVIS_WORKDIR` 解析，默认当前 Jarvis 根目录。
2. 增加 `SessionWorkspaceManager`，负责创建 session/node 目录。
3. 在 planner 返回并校验 DAG 后创建目录。
4. 代码节点从 `projects/{repo_id}` 创建 node repo。
5. 在 node 执行前写入 `input_snapshot.md`。
6. 在 node 执行后写入 `output.md` 和 `result.json`。
7. 让 coder provider 使用 node repo 作为 `workdir`。
8. 让 coder provider 使用 node 的 `provider_run/` 作为 `run_dir`。
9. artifact resolver 增加 session artifact root。

## 7. 测试要点

1. 完整 DAG 生成前不创建 session/node 目录。
2. DAG 校验失败时不创建目录。
3. session 创建后包含 `session.json`、`dag.json` 和所有 node 目录。
4. node id 做 path-safe 处理，不能逃逸 `sessions/{session_id}/nodes/`。
5. coder 节点从 `projects/{repo_id}` 创建 `nodes/{node_id}/repo/{repo_id}`。
6. 不同 coder 节点默认使用不同 node repo。
7. coder `run_dir` 指向当前 node 的 `provider_run/`。
8. provider 日志不会污染 repo `git status`。
9. artifact 交付不默认允许读取整个 repository root。

## 8. 当前取舍

第一版只保留一个简单模型：

```text
Jarvis 工作目录固定。
一次 DAG 执行一个 session 目录。
完整 DAG 出来后统一创建所有 node 目录。
projects 作为长期本地事实源。
代码修改发生在 node repo。
运行日志和产物放 sessions。
Planner 不管目录，tool 不临时造目录，provider 只写 runtime 分配的目录。
```
