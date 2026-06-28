# Jarvis 代码任务 Workspace 与发布设计

| 项目 | 内容 |
|------|------|
| 日期 | 2026-06-29 |
| 状态 | Draft |
| 相关模块 | `app/repositories`, `app/task_runtime`, `app/tools/codex.py`, `app/tools/coder_common.py`, `skills/code-planning` |
| 目标 | 收敛 Jarvis 代码任务的仓库、worktree、临时文件、多轮续接和发布推送模型 |

---

## 1. 目标

Jarvis 在执行代码任务时，需要同时满足：

1. 普通代码修改相互隔离。
2. 多轮对话可以继续上一次 feature。
3. 合并到 `master/main` 与推送远程要受限。
4. 不新增复杂 runtime，尽量通过 skill、workspace policy 与 canonical repo 审批解决。

核心原则：

```text
普通代码任务在独立 worktree 中完成；
发布任务在项目 canonical repo 中受限执行；
多轮上下文通过 ResultAggregator 生成的隐式 workspace 消息恢复。
```

---

## 2. 仓库模型

每个项目注册后，Jarvis 保存一个 canonical repo：

```text
RepoRegistry:
  repo_id
  canonical_repo_path
  default_branch
  remote
```

示例：

```text
/data/repos/jarvis/canonical
```

canonical repo 是项目主路径，始终保持在 `master/main` 分支，并且要求工作区 clean。普通 code node 不直接在 canonical repo 中开发。

---

## 3. 普通代码任务：Branch + Worktree

当用户请求实现功能、修改代码、优化代码时，Planner 根据 coder skill 创建普通 code node。

普通 code node 的工作方式：

```text
base branch: master/main
feature branch: jarvis/<session_id>/<node_id>
worktree path: sessions/<session_id>/nodes/<node_id>/repo
tmp path: sessions/<session_id>/nodes/<node_id>/tmp
```

初始化流程：

```bash
cd /data/repos/<repo_id>/canonical

git fetch origin
git checkout master
git reset --hard origin/master

git worktree add \
  /sessions/<sid>/nodes/<node_id>/repo \
  -b jarvis/<sid>/<node_id> \
  master
```

Coder/Codex 在该 worktree 中完成代码修改，并提交 commit：

```bash
cd /sessions/<sid>/nodes/<node_id>/repo

git add .
git commit -m "implement xxx"
```

普通 code node 的产物是：

```text
feature branch
commit sha
diff
test log
manifest
```

普通 code node 不应该直接 push `master/main`。

---

## 4. 测试与临时文件隔离

code node 应当把执行临时文件与源码 worktree 分开。Jarvis 在启动 coder provider 时为子进程注入环境变量：

```text
TMP=sessions/<sid>/nodes/<node_id>/tmp
TEMP=sessions/<sid>/nodes/<node_id>/tmp
TMPDIR=sessions/<sid>/nodes/<node_id>/tmp
TEST_TMPDIR=sessions/<sid>/nodes/<node_id>/tmp/test
PYTHONPYCACHEPREFIX=sessions/<sid>/nodes/<node_id>/tmp/pycache
PYTEST_ADDOPTS=--basetemp=sessions/<sid>/nodes/<node_id>/tmp/pytest -p no:cacheprovider
```

目标是让 Python/pytest 产生的临时文件、`.pyc`、pytest tmp 尽量落在 node `tmp` 目录，而不是污染 repo worktree。

git/artifact 收集层仍需要过滤常见执行噪音：

```text
__pycache__/
.pytest_cache/
.pytest_tmp*/
pytest-cache-files-*
```

这些文件不应作为有效代码变更或 node artifact。

---

## 5. 合并与推送：受限 Coder Node

当用户明确要求：

```text
合并到 master/main
推送远程
发布当前改动
把刚才的分支合进去
```

Planner 才能生成特殊 publish node。

publish node 仍然使用 coder runtime / Codex runtime，但它是受限 node。它的工作区不是 feature worktree，而是 canonical repo：

```text
workspace: /data/repos/<repo_id>/canonical
branch: master/main
```

publish node 的任务范围非常窄：

```text
1. fetch remote
2. 切到 master/main
3. reset 到 origin/master 或 origin/main
4. merge 指定 source_ref
5. 运行验证命令
6. push 到指定远程分支
```

示例流程：

```bash
cd /data/repos/<repo_id>/canonical

git fetch origin
git checkout master
git reset --hard origin/master

git merge --no-ff jarvis/<sid>/<node_id>

pytest

git push origin master
```

publish node 禁止：

```text
新功能开发
主动重构
force push
push tags
修改 remote url
push 非目标分支
删除分支
改写历史
```

---

## 6. 多轮对话续接：ResultAggregator 生成隐式 Workspace 消息

每次 code node 执行完成后，`ResultAggregator` 根据 `NodeResult` 中的真实执行事实生成一条对用户不可见、但对下一轮 Planner 可见的隐式消息。它不是 DAG node，而是 conversation context。

职责边界：

```text
CoderNodeExecuteRuntime:
  创建/复用 worktree，执行 coder provider，返回 repo_id、branch、worktree、node summary 等事实。

ResultAggregator:
  汇总 node results，判断是否需要创建或更新 workspace context。

ConversationStore:
  保存 ResultAggregator 生成的隐式消息。

Planner:
  下一轮读取 workspace context，选择 NEW_WORKTREE / REUSE_EXISTING_WORKTREE / READ_EXISTING_WORKTREE / CANONICAL_REPO_PUBLISH。
```

workspace context 一期只保留 Planner 续接与发布决策需要的最小字段：

```json
{
  "type": "workspace_context",
  "repo_id": "jarvis",
  "branch": "jarvis/sid123/node_add_eval",
  "worktree": "/sessions/sid123/nodes/node_add_eval/repo",
  "status": "active",
  "summary": "Implemented eval module and added initial tests."
}
```

字段含义：

1. `repo_id`：用于从 RepoRegistry 查询 canonical repo、default branch、remote。
2. `branch`：继续修改或 publish 时使用的 source ref。
3. `worktree`：继续修改或读取上一轮代码时使用的路径。
4. `status`：只表示会话层是否建议复用该 workspace，不表示真实 git 状态。
5. `summary`：帮助 Planner 判断用户说的“刚才那个”指向哪个工作现场。

`tmp_path`、`head_sha`、`base_sha`、`manifest`、`diff`、`test_log` 不放入 workspace context。一期需要这些信息时，从 session result、git 或 artifact 系统查询。

下一轮用户如果说：

```text
刚才那个不满意，改一下
继续完善这个功能
在刚才基础上加测试
展示下刚才那个逻辑
```

Planner 可以根据这条隐式消息决定复用已有 worktree，而不是重新创建。

---

## 7. Planner 的 Workspace 选择规则

coder skill 需要指导 Planner 选择以下模式之一。

### 7.1 NEW_WORKTREE

用于新的代码任务：

```text
创建新 feature branch
创建新 worktree
让 coder node 在新 worktree 中开发
```

### 7.2 REUSE_EXISTING_WORKTREE

用于继续修改上一轮未发布 feature：

```text
复用已有 worktree
复用已有 feature branch
在原分支上继续提交 commit
```

### 7.3 READ_EXISTING_WORKTREE

用于用户只想查看、解释、总结上一轮代码：

```text
读取已有 worktree
不修改代码
不提交 commit
```

### 7.4 CANONICAL_REPO_PUBLISH

用于用户明确要求合并或推送：

```text
使用 canonical repo
合并指定 source_ref
推送到目标分支
```

---

## 8. Git 状态为准

一期不维护独立的 feature lifecycle 状态机。feature branch、worktree、merge、push 的真实状态以 Git 实时查询为准，避免 Jarvis 自己维护的状态和 Git 事实不一致。

`workspace_context.status` 只保留轻量会话语义：

```text
active    Planner 可以把这个 workspace 当作“刚才那个”继续复用
closed    不建议继续复用；如果用户继续修改，应从最新 master/main 新建 worktree
```

真实状态在需要时通过 Git 查询：

```text
worktree 是否存在：检查 worktree 路径
当前分支：git branch --show-current
是否 clean：git status --porcelain
是否已合并：git merge-base --is-ancestor <source_ref> <target_branch>
是否已推送：git ls-remote 或 remote tracking ref
是否处于冲突/merge 中：git status --porcelain 和 .git/MERGE_HEAD
```

原则：

```text
Git 是 source of truth；
workspace_context 只是 Planner 的指路牌。
```

---

## 9. 权限与审批边界

Jarvis 不需要新增 PublishRuntime，也不需要为一期引入复杂的 GitPolicyGuard。权限边界先通过 workspace 类型区分：

普通 code node：

```text
运行在独立 feature worktree 中
可以高权限执行代码修改、git add、git commit
可以在自己的 feature branch 上连续提交
不需要为普通本地提交透出审批
```

publish node：

```text
运行在 canonical repo 中
执行 master/main merge、push 等影响主分支或远端的动作
使用正常权限边界
需要通过 Codex/Jarvis approval 透出审批
```

publish node 的提示词和审批文案必须把任务范围限制清楚：

```text
只允许 merge 用户指定的 source_ref
只允许 push 到用户指定的 target branch
禁止 force push
禁止 push tags
禁止修改 remote url
禁止删除分支
禁止改写历史
```

---

## 10. 最终流程

### 10.1 新功能开发

```text
用户请求实现功能
-> Planner 选择 NEW_WORKTREE
-> 创建 feature branch + worktree
-> Codex 修改代码并 commit
-> 生成 manifest
-> ResultAggregator 追加 workspace_context 隐式消息
```

### 10.2 多轮继续修改

```text
用户说“刚才那个改一下”
-> Planner 读取 workspace_context
-> 选择 REUSE_EXISTING_WORKTREE
-> Codex 在原 worktree 中继续修改并 commit
-> ResultAggregator 更新 workspace_context 隐式消息
```

### 10.3 查看上一轮代码

```text
用户说“展示下刚才的逻辑”
-> Planner 选择 READ_EXISTING_WORKTREE
-> 读取已有 worktree 中的代码
-> 返回解释，不修改代码
```

### 10.4 合并并推送

```text
用户明确要求合并/推送
-> Planner 选择 CANONICAL_REPO_PUBLISH
-> 受限 coder node 在 canonical repo 中执行
-> merge source_ref 到 master/main
-> run verification
-> push origin master/main
-> ResultAggregator 可以将 workspace_context.status 标记为 closed
```

---

## 11. 设计分析

### 11.1 成立的部分

这个方案能解决当前暴露出来的三个主要问题：

1. 普通代码任务不再污染 canonical repo。
2. Python/pytest 临时文件有明确落点，不再默认污染 worktree。
3. 合并和推送从普通代码编辑动作中分离出来，可以通过 canonical repo 审批单独治理。

它也符合 Git 的基本约束：同一个分支不应被多个 worktree 同时 checkout。普通 node 使用独立 feature branch，可以避免当前 `master is already checked out` 这类失败。

### 11.2 主要风险

第一个风险是 publish node 在 canonical repo 中运行。canonical repo 是长期主路径，一旦 publish node 运行失败，可能留下 merge conflict、未提交文件或半完成状态。因此 publish node 必须在开始前和结束后都检查 clean worktree，并在失败时保留清晰的恢复指令。

第二个风险是 `reset --hard origin/master`。它对 canonical repo 很强势，只有在 canonical repo 被定义为受 Jarvis 管理、且禁止人工脏改时才成立。如果用户也会手动在 canonical repo 中开发，这条规则会误删本地未提交改动。

第三个风险是让 Codex 直接执行 push。第一期不引入复杂 GitPolicyGuard，但 publish node 必须通过审批把具体命令、目标分支、远端暴露给用户；后续如风险变高，再补充更强的 postflight 校验。

### 11.3 建议调整

publish node 可以继续复用 coder runtime，但执行前应明确工作区和审批边界：

```text
preflight:
  canonical repo clean
  target branch == default_branch
  source_ref 来自最近 workspace_context 或用户明确指定
  remote url 可展示给用户确认

approval:
  展示 merge 命令
  展示 push 命令
  展示 target branch 和 remote

postflight:
  canonical repo clean
  汇报 HEAD、target branch、push 结果
```

如果后续发现 canonical repo 经常被 publish 弄脏，再考虑把 publish workspace 改成固定 integration worktree。第一阶段可以先按本文方案落地，避免过早新增 runtime。

### 11.4 落地优先级

建议按以下顺序实现：

1. RepoRegistry 明确保存 `canonical_repo_path`、`default_branch`、`remote`。
2. 普通 code node 统一使用 `jarvis/<session_id>/<node_id>` feature branch 和独立 worktree。
3. coder provider 启动时注入 node `tmp` 相关环境变量。
4. git/artifact 收集过滤 Python/pytest cache 和临时目录。
5. ResultAggregator 增加 workspace context 生成与更新逻辑，支持多轮复用 worktree。
6. 增加 publish node 的 planner skill guidance 和 canonical repo 审批文案。
7. 最后开放 `CANONICAL_REPO_PUBLISH` 的 push 能力。

---

## 12. 总结

Jarvis 的代码任务设计可以收敛为：

```text
普通开发：feature branch + isolated worktree
多轮续接：ResultAggregator 生成 workspace context
发布动作：canonical repo + restricted coder node
治理方式：coder skill + workspace policy + canonical repo approval
```

核心原则：

```text
worktree 负责隔离开发现场；
branch 负责承载代码变更；
tmp 负责承载执行污染；
ResultAggregator 生成的隐式消息负责多轮恢复；
受限 coder node 负责合并和推送。
```
