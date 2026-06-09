# Jarvis Action Permission 与 Tool Runtime 收敛设计

| 项目 | 内容 |
| --- | --- |
| 日期 | 2026-06-09 |
| 状态 | Draft |
| 目标 | 将 DAG runtime 收敛为智能 worker，把权限下沉到每一次 tool action |
| 相关模块 | `app/task_runtime`, `app/tools`, `app/tools/runtime.py::check_tool_policy` |

## 1. 背景

当前 Task DAG 同时存在 `llm`、`react`、`coder`、`tool` runtime。其中 `tool` runtime 只负责执行单个确定性工具，例如 `scheduled_task` 或 `deliver_file`。

这个边界不够干净：

1. `tool` 不是智能 worker，而是一次 action。
2. Planner 被迫选择具体工具，污染了粗粒度 DAG 语义。
3. 权限容易被误解成 runtime 级别授权，而不是 action 级别授权。
4. 提醒、发文件、发邮件、日历写入等副作用本质上都应该由统一 permission guard 管控。

新的方向是：

```text
DAG runtime: llm | react | coder
Tool action permission: per tool/action/input/context
```

`react` 可以承担单个或多个普通 tool call。`coder` 承担本地仓库和代码任务。`tool` 不再作为 Planner 可返回的 runtime。

## 2. 设计原则

1. Planner 只做语义分解，不做授权。
2. Runtime 只表达智能执行者，不表达单个工具动作。
3. 每一次 tool call 执行前都必须经过 action permission。
4. 权限判断基于 `tool + action + input + context`，而不是只基于 runtime。
5. 用户授权应该可以是 one-shot、turn-scoped、conversation-scoped 或 workspace-scoped。
6. 所有允许、拒绝、询问都要进入审计记录。

## 3. Runtime 边界

### llm

用于无需工具的直接推理、改写、解释、摘要。单节点且 `user_facing=true` 时可以 pass-through。

### react

用于非仓库的工具调用和研究型任务：

- web/current info/search
- deepresearch
- knowledge lookup
- reminder/calendar/email/delivery 等个人助理动作
- 单个确定性工具调用，例如 `scheduled_task`、`deliver_file`

### coder

用于本地仓库和代码任务：

- repo inspect/review
- code edit/test/refactor
- markdown report inside repo
- commit/push
- 代码任务中的自检和修复循环

凡是涉及 active/local repository 的任务，Planner 应优先选择 `coder`。

`coder` 不是 `react` 可调用的普通 tool。`delegate_to_codex` / `delegate_to_claude_code` 这类名称只能作为 provider adapter 的内部实现细节存在，不能进入 `builtin_tool_definitions()`、`build_llm_tools()` 或 `tool_search` 的 action surface。DAG 里代码工作只能体现为：

```json
{"runtime": "coder"}
```

### workspace 边界

`react` 与 `coder` 的边界不能简单等同于“是否访问文件”。需要把 workspace 作为一等上下文：

```text
workspace_type: task | artifact | repository | knowledge | system
workspace_id: turn-scoped id / artifact store / repo_id / vault_id / host
path_scope: temp | generated | docs | source | test | config | secret | external
purpose: research | document | delivery | repo_analysis | code_change | verification
```

推荐默认边界：

1. `react` 可以做 task/artifact/knowledge workspace 的轻量文件动作，例如生成临时报告、读取用户指定文档、交付 artifact、写 wiki 草稿。
2. `react` 可以在 repository workspace 中读取或写入明确的非代码文档，但必须由 action permission 按 path/purpose 判断；不能靠 runtime 粗暴拒绝。
3. `react` 不应执行 shell、git、测试、构建、代码 agent 委托或多文件仓库分析。
4. `coder` 负责 repository workspace 中的代码、测试、架构分析、review、执行链分析、git workflow。
5. `file.read/write/search` 必须带上 workspace scope；没有 scope 时只能退化到低权限默认值，不能隐式扩大到整个 active repo。

因此，日志中 `research_agent_swarm` 的问题不是 “react 调了文件工具” 本身，而是一个 external research node 缺少 workspace/purpose 限制，导致它把 `repository` workspace 当成了可自由探索的上下文。

## 4. Permission 模型

权限策略应接近 OpenCode 的 action permission 思路：

```text
tool/action + input pattern + context -> allow | ask | deny
```

建议内部归一成：

```json
{
  "actor": "react | coder",
  "tool": "scheduled_task",
  "action": "create",
  "resource": {
    "path": null,
    "command": null,
    "recipient": null,
    "artifact_id": null
  },
  "context": {
    "workspace": "jarvis",
    "conversation_id": 123,
    "turn_id": 456,
    "node_id": "remind_user"
  }
}
```

策略输出：

```json
{
  "decision": "allow | ask | deny",
  "reason": "string",
  "scope": "once | turn | conversation | workspace | global"
}
```

## 5. 默认权限建议

```json
{
  "*": "ask",
  "web.search": "allow",
  "knowledge.query": "allow",
  "artifact.deliver": "ask",
  "scheduled_task.create": "ask",
  "scheduled_task.list": "allow",
  "scheduled_task.remove": "ask",
  "email.send": "ask",
  "calendar.write": "ask",
  "file.read": {
    "*": "allow",
    "**/.env": "deny",
    "**/.env.*": "deny",
    "**/.env.example": "allow"
  },
  "file.edit": {
    "*": "ask"
  },
  "shell.command": {
    "git status*": "allow",
    "git diff*": "allow",
    "rg *": "allow",
    "pytest *": "ask",
    "git commit*": "ask",
    "git push*": "ask",
    "rm *": "deny"
  }
}
```

## 6. 执行链路

```text
Planner
  -> PlanNode(runtime=react|coder|llm)
NodeExecutor
  -> selected runtime
Runtime tool loop
  -> PermissionGuard.check(action)
  -> allow: execute
  -> ask: create approval request and block/wait
  -> deny: return rejected tool observation
Audit
  -> record requested/allowed/denied/executed result
Aggregator
  -> summarize node results
```

Planner 不应该输出 `tool_name`。如果为了兼容旧 payload 收到 `runtime=tool`，归一为 `runtime=react`，并把具体工具意图保留在 node objective/expected_output 里。

## 7. 迁移计划

### P0

1. Planner prompt/schema 移除 `tool` runtime。
2. `runtime_hints.available_runtimes` 默认改为 `["llm", "react", "coder"]`。
3. `ToolNodeExecuteRuntime` 从默认 Task runtime 删除。
4. React runtime 默认允许 `scheduled_task` 与 `deliver_file`。
5. 旧 `runtime=tool` payload 归一为 `react`。
6. `delegate_to_codex` / `delegate_to_claude_code` 从普通 tool registry 移除，只保留 provider 内部 adapter。

### P1

1. 新增 `PermissionGuard` 抽象。
2. 将 `app.tools.runtime.check_tool_policy` 升级为统一 action policy engine。
3. 记录每次 action decision 到 tool audit。
4. 支持 one-shot/turn/conversation/workspace scope。

### P2

1. 将 coder provider 的 `CoderAction` 与普通 tool action 统一到同一 permission decision schema。
2. UI/飞书卡片支持 ask/allow always/deny always。
3. 对高风险 action 增加强确认。

## 8. 测试要点

1. Planner 不再返回 `runtime=tool`。
2. 旧 `runtime=tool + tool_name=deliver_file` 被归一为 react 节点。
3. React 节点可以执行 `scheduled_task`、`deliver_file`。
4. Repo 任务仍然必须走 coder。
5. Action permission 拦截发生在工具执行前。
6. deny/ask 不应被 Aggregator 误报为完成。
