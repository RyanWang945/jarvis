# Jarvis Workspace 长任务续接极简设计

| 项目 | 内容 |
|------|------|
| 日期 | 2026-06-29 |
| 更新 | 2026-06-30 |
| 状态 | Draft |
| 目标 | 用 workspace 文件系统承载长任务状态，支持“继续刚才那个” |

---

## 1. 核心想法

长任务状态不放在隐式上下文里，也不围绕 `NodeResult` 恢复。

每个长任务有自己的 workspace：

```text
workspace/
  TASK.md
  PROGRESS.md
  RESULT.md
  state.json
  artifacts/
  repo/
```

职责：

```text
TASK.md      任务目标、约束、用户要求
PROGRESS.md  已完成事项、当前状态、阻塞点、下一步
RESULT.md    当前可交付结果
state.json   Runtime 读取的机器状态
artifacts/   报告、日志、数据、图片等非代码产物
repo/        代码任务的 Git worktree
```

`repo/` 是代码现场；`repo/` 之外是任务状态现场。

---

## 2. 总体流程

```text
User Message
  -> Planner 判断新建 / 继续 / 查看 / 发布准备
  -> Runtime 创建新 workspace，或根据历史 node:<id> 输入复用旧 workspace
  -> Runtime 读取 TASK / PROGRESS / RESULT / state
  -> Agent 在 workspace 中执行
  -> Agent / Runtime 更新 PROGRESS / RESULT / state
  -> Aggregator 展示 RESULT 和本轮变化
```

下一轮用户说“继续刚才那个”：

```text
TaskAgentRuntime 从历史 assistant raw_payload 抽取最近 workspace_path
Planner 看到 previous_node_results，生成引用旧节点的 plan
Runtime 根据 node:<id> 输入进入对应 workspace
读文件系统和 Git 现状
继续执行
```

---

## 3. Workspace 目录

当前 P0 路径：

```text
sessions/<session_id>/nodes/<node_id>/
  TASK.md
  PROGRESS.md
  RESULT.md
  state.json
  artifacts/
  repo/
```

这里的 node workspace 就是最小 workspace。后续如果需要跨节点共享同一任务现场，可以增加 `workspaces/<workspace_id>/` 索引或别名，不改变内部文件结构。

`workspace_id` / `node_id` 尽量语义化：

```text
workspace-publish-design
agent-fact-checking-research
github-mcp-auth-fix
```

一个 conversation 可以有多个 workspace。当前轮使用哪个 workspace，由 Planner 根据用户表达和历史消息选择；Runtime 最终以 workspace 文件和 Git 状态为准。

---

## 4. state.json

`state.json` 是 Runtime 的稳定入口。

代码 workspace 示例：

```json
{
  "schema_version": 1,
  "workspace_id": "workspace-publish-design",
  "domain": "code",
  "status": "active",
  "repo_id": "jarvis",
  "repo_path": "repo",
  "branch": "jarvis/workspace-publish-design",
  "base_ref": "master",
  "last_head_sha": "abc123",
  "updated_at": "2026-06-30T12:00:00+08:00"
}
```

研究 workspace 示例：

```json
{
  "schema_version": 1,
  "workspace_id": "agent-fact-checking-research",
  "domain": "research",
  "status": "active",
  "topic": "Agent 事实性核查和数据计算核查",
  "source_count": 18,
  "claim_count": 32,
  "updated_at": "2026-06-30T12:00:00+08:00"
}
```

Markdown 给人和模型读，`state.json` 给 Runtime 读。

---

## 5. Planner

Planner 不需要恢复 Git 细节。

Planner 只决定：

```text
新建 workspace
继续某个 workspace
查看某个 workspace
基于某个 workspace 另开方案
发布准备
```

Plan 继续使用现有 `ExecutionPlan / PlanNode` schema：

```text
id
runtime
mode
objective
repo_id?
input_refs
output_hint
```

workspace 选择可以先写在 `objective` 中，Runtime 再解析 / 绑定实际 workspace。

---

## 6. Code Runtime

代码 workspace 的结构：

```text
workspace/
  TASK.md
  PROGRESS.md
  RESULT.md
  state.json
  artifacts/
  repo/
```

`repo/` 是 Git worktree。

Runtime 负责：

```text
创建 workspace
从 RepoRegistry 准备 repo/ worktree
执行前校验 repo/ Git 状态
启动 code agent，cwd 为 workspace
约束 code agent 只改 repo/ 内代码
提交时只提交 repo/ 内容
更新 PROGRESS.md / RESULT.md / state.json
```

Git 校验：

```bash
git -C <workspace>/repo branch --show-current
git -C <workspace>/repo rev-parse HEAD
git -C <workspace>/repo status --porcelain
git worktree list
```

这样 agent 可以读取：

```text
TASK.md
PROGRESS.md
RESULT.md
artifacts/
repo/
```

但代码变更只发生在：

```text
repo/
```

---

## 7. 非代码节点读取

其他节点可以读取 workspace 中除 `repo/` 以外的状态文件：

```text
TASK.md
PROGRESS.md
RESULT.md
state.json
artifacts/
```

典型用途：

```text
总结当前进展
生成报告
检查研究证据
基于 RESULT.md 做下一步规划
```

默认不直接读取或修改 `repo/`，除非 runtime 是 coder。

---

## 8. Research Runtime

研究 workspace 可以使用同一结构：

```text
workspace/
  TASK.md
  PROGRESS.md
  RESULT.md
  state.json
  artifacts/
    sources.jsonl
    claims.jsonl
    evidence_map.json
    gaps.json
    draft_report.md
    final_report.md
```

Research Runtime 负责维护 `artifacts/` 中的证据账本，并更新 `PROGRESS.md` / `RESULT.md`。

---

## 9. Aggregator

Aggregator 只汇总用户可见结果。

优先展示：

```text
RESULT.md
本轮 PROGRESS.md 增量
代码 changed files / commit / tests
研究 sources / claims / gaps / report
```

Aggregator 不生成续接状态。续接状态在 workspace 文件里。

---

## 10. NodeResult

`NodeResult` 退化为执行日志，不再是长任务状态核心。

最小记录：

```json
{
  "node_id": "update_design",
  "runtime": "coder",
  "status": "completed",
  "summary": "更新了 workspace 设计文档。",
  "data": {
    "workspace_path": "sessions/<sid>/workspaces/workspace-publish-design"
  }
}
```

Runtime 仍可在 `git`、`artifacts`、`data` 中记录本轮事实，但下一轮恢复以 workspace 文件系统为准。

---

## 11. 落地顺序

P0：

```text
1. 定义 workspace 目录结构。
2. Code Runtime 创建 / 复用 workspace。
3. Code agent cwd 改为 workspace。
4. repo/ 作为 Git worktree。
5. Agent / Runtime 更新 TASK.md、PROGRESS.md、RESULT.md、state.json。
6. NodeResult 记录 workspace_path。
7. Planner 通过历史消息中的 workspace_path 选择继续对象。
```

P1：

```text
1. workspace index / list / switch。
2. branch / worktree lock。
3. publish merge 准备。
4. 其他 runtime 读取 workspace 非 repo 状态。
```

P2：

```text
1. Research workspace 标准账本。
2. refresh_sources / fill_gaps / verify_claims / synthesize_report。
3. 远端 push / 发布流程。
```

---

## 12. 总结

```text
Workspace 是长任务状态单元。
TASK / PROGRESS / RESULT 是模型可读接口。
state.json 是 Runtime 可读接口。
repo/ 是代码 worktree。
NodeResult 只是执行日志。
Aggregator 只展示结果。
```
