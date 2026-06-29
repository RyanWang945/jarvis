# Jarvis 代码任务 Workspace 与发布设计

| 项目 | 内容 |
|------|------|
| 日期 | 2026-06-29 |
| 状态 | Draft |
| 相关模块 | `app/repositories`, `app/task_runtime`, `app/tools/codex.py`, `app/tools/coder_common.py`, `skills/code-planning` |
| 目标 | 收敛 Jarvis 代码任务的仓库、worktree、临时文件、多轮续接、隐藏 workspace context 和发布推送模型 |

---

## 1. 目标

Jarvis 在执行代码任务时，需要同时满足：

1. 普通代码修改相互隔离。
2. 普通代码任务不污染 canonical repo。
3. 多轮对话可以继续上一次 feature branch。
4. 合并到 `master/main` 与推送远程必须由特殊 publish node 执行。
5. 不新增复杂状态机，尽量通过 coder skill、workspace prompt、Git 事实和 approval 边界解决。

核心原则：

```text
普通代码任务在独立 branch + worktree 中完成；
Planner 在 coder node prompt 中写清 branch/source_ref 等工作现场意图；
发布任务由特殊 publish node 在 canonical repo 中受限执行；
多轮上下文通过 ResultAggregator 生成隐藏 workspace context 消息恢复；
Git 是真实状态来源，hidden workspace context 只是下一轮 Planner 的记忆线索。
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

一期 `default_branch` 可以从本地仓库推断：

```text
优先 master
其次 main
```

canonical repo 是项目主路径，普通 code node 不直接在 canonical repo 中开发。canonical repo 主要用于：

```text
1. 作为注册仓库入口。
2. 作为普通 node 创建 branch/worktree 的 Git 源。
3. 作为 publish node 合并和 push 的受限工作区。
```

如果 canonical repo 被 publish node 使用，执行前必须检查 clean。普通开发不能假设 canonical repo 始终可被 reset。

---

## 3. 普通代码任务：每个 Node 自己的 Branch + Worktree

当用户请求实现功能、修改代码、优化代码、review 代码或运行测试时，Planner 根据 coder skill 创建普通 coder node。

普通 coder node 的基本工作方式：

```text
source_ref: master/main、用户显式指定的 branch、或最近 hidden workspace_context 中的 branch
node branch: jarvis/<task_slug>/<node_id>
worktree path: sessions/<session_id>/nodes/<node_id>/repo
tmp path: sessions/<session_id>/nodes/<node_id>/tmp
```

`task_slug` 由 Planner 生成，必须有语义信息，不能只使用 session id。示例：

```text
jarvis/workspace-publish-design/review_design
jarvis/workspace-publish-design/add_tmp_isolation
jarvis/github-mcp-integration/fix_auth_flow
```

如果 branch 名冲突，runtime 可以追加短 hash：

```text
jarvis/workspace-publish-design/add_tmp_isolation-a13f2c
```

### 3.1 Planner 如何表达 Branch 意图

一期不修改 `PlanNode` schema，不新增 `workspace_strategy` 字段。Planner 通过 coder node 的 `objective` 或 `output_hint` 写清工作现场意图，例如：

```text
Repo: jarvis
Source ref: master
Working branch: jarvis/workspace-publish-design/add_tmp_isolation
Use the existing worktree for this branch if one is recorded in workspace context;
otherwise create a new worktree for the branch.
```

runtime / coder node 根据 prompt 和 Git 事实决定：

```text
branch 不存在：从 source_ref 创建 branch + worktree
branch 已存在且 worktree 存在：复用该 worktree
branch 已存在但 worktree 不存在：为该 branch 创建/恢复 worktree
```

### 3.2 继续修改的默认规则

如果用户说：

```text
继续刚才那个
在刚才基础上加测试
刚才那个不满意，改一下
```

Planner 应优先使用最近 hidden workspace_context 中的 `branch` 和 `worktree`，继续在旧 branch 上修改。旧 branch 可以有多轮 commit。

如果用户明确表达新方向，例如：

```text
基于刚才另做一个方案
新开一个分支试试另一种实现
```

Planner 可以把上一轮 branch 作为 `source_ref`，再生成新的语义化 branch。

### 3.3 多 Node 约束

同一个 branch 不允许被多个 coder node 同时使用。原因是 Git 不允许同一个 branch 被多个 worktree 同时 checkout，而且并发写同一 branch 会让 merge、commit 和上下文恢复复杂化。

如果多个 coder node 存在代码依赖，Planner 必须显式串联：

```text
node A:
  source_ref = master
  branch = jarvis/task/implement_api

node B:
  source_ref = jarvis/task/implement_api
  branch = jarvis/task/add_tests
```

不要让依赖前序代码结果的后续 node 默认从 `master/main` 创建。

### 3.4 Commit 归属

Coder/Codex 可以在自己的 node branch/worktree 中提交 commit。由于每个普通 node 有自己的 branch，普通本地 commit 不影响 `master/main`，不需要单独透出主分支审批。

无论 commit 是 provider 自己完成，还是 runtime 在 provider 结束后统一完成，`NodeResult` 必须记录真实 Git 事实：

```text
repo_id
branch
worktree
head_sha
commit sha
diff / changed files
test log
manifest
```

普通 code node 不应该 push `master/main`，也不应该直接 push 远程。

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

git commit / artifact 收集层仍需要过滤常见执行噪音：

```text
__pycache__/
.pytest_cache/
.pytest_tmp*/
pytest-cache-files-*
```

这些文件不应作为有效代码变更、node artifact 或 commit 内容。

如果某些缓存仍然落入仓库 worktree，可以通过 `.gitignore` 兜底。但 `.gitignore` 不能替代 runtime 的 commit 前过滤，因为不同仓库已有 ignore 规则可能不一致。

---

## 5. 合并与推送：受限 Publish Node

只有当用户明确要求：

```text
合并到 master/main
推送远程
发布当前改动
把刚才的分支合进去
```

Planner 才能生成特殊 publish node。

publish node 是受限 node。它使用 canonical repo：

```text
workspace: canonical_repo_path
target branch: master/main
source_ref: hidden workspace_context 中的 branch，或用户明确指定的 branch
```

publish node 的任务范围：

```text
1. 检查 canonical repo clean。
2. fetch remote。
3. 切到 master/main。
4. 合并指定 source_ref。
5. 处理 merge conflict。
6. 运行验证命令。
7. push 到指定远程分支。
8. postflight 检查并汇报 HEAD、target branch、push 结果。
```

一期可以先不实现自动解冲突能力；如果 merge conflict 发生，可以阻塞并返回清晰恢复指令。但设计上 publish node 未来必须具备解冲突能力，否则发布流程无法闭环。

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

示例流程：

```bash
cd /data/repos/<repo_id>/canonical

git fetch origin
git checkout master

git merge --no-ff jarvis/workspace-publish-design/add_tmp_isolation

pytest

git push origin master
```

`reset --hard origin/master` 不是默认必选步骤。只有在 canonical repo 被定义为 Jarvis 托管且 clean、且 publish policy 明确允许时，才能执行强制对齐。

---

## 6. 多轮对话续接：ResultAggregator 生成隐藏 Workspace 消息

每次 coder node 执行完成后，`ResultAggregator` 根据 `NodeResult` 中的真实执行事实生成一条对用户不可见、但对下一轮 Planner 可见的隐藏消息。

隐藏消息不是 DAG node，而是 conversation context。它和历史消息一起保存，但需要有字段区分：

```text
sender_type / role / raw_payload 中标记 internal 或 hidden
用户默认看不到
Planner 和大模型可以读取
用户主动询问时，大模型可以总结并展示
```

一期不新增 workspace_context 表，直接复用历史消息存储模型。

### 6.1 职责边界

```text
CoderNodeExecuteRuntime / coder provider:
  创建或复用 branch/worktree。
  执行代码修改、测试和 commit。
  在 NodeResult 中返回 repo_id、branch、worktree、head_sha、commit、changed files、test log 等真实事实。

ResultAggregator:
  汇总 node results。
  从真实 Git metadata 构造 hidden workspace_context。
  不从自然语言 summary 里猜 branch/worktree/head_sha。
  LLM 可以辅助生成 summary，但不能决定 Git 事实。

ConversationStore:
  保存 ResultAggregator 生成的隐藏消息。

Planner:
  下一轮读取历史消息中的 workspace_context。
  根据用户输入决定复用旧 branch、从旧 branch 新建新 branch、只读旧 worktree，或规划 publish node。
```

### 6.2 Hidden Workspace Context 格式

workspace context 一期保留 Planner 续接与发布决策需要的字段：

```json
{
  "type": "workspace_context",
  "repo_id": "jarvis",
  "branch": "jarvis/workspace-publish-design/add_tmp_isolation",
  "worktree": "/sessions/sid123/nodes/add_tmp_isolation/repo",
  "head_sha": "abc123",
  "base_ref": "master",
  "last_node_id": "add_tmp_isolation",
  "status": "active",
  "summary": "Added tmp/cache isolation for coder node execution and updated tests."
}
```

字段含义：

1. `repo_id`：用于从 RepoRegistry 查询 canonical repo、default branch、remote。
2. `branch`：继续修改或 publish 时使用的 source ref。
3. `worktree`：继续修改或读取上一轮代码时使用的路径。
4. `head_sha`：记录生成 context 时 branch 的 HEAD，用于后续检测是否过期。
5. `base_ref`：该 branch 当时基于的 source ref，通常是 `master/main` 或上一轮 branch。
6. `last_node_id`：帮助 Planner 判断用户说的“刚才那个”指向哪个工作现场。
7. `status`：只表示会话层是否建议复用该 workspace，不表示真实 Git 状态。
8. `summary`：帮助 Planner 判断该 workspace 做了什么。

一轮中如果有多个 coder node，Aggregator 可以追加多条 `workspace_context`，不需要强行合并。下一轮由 Planner 和大模型根据用户表达、时间顺序、summary 和 branch 语义自行决定使用哪一条。

---

## 7. Planner Skill 需要固化的规则

以下规则应固化到 `skills/code-planning/SKILL.md`，因为它们属于 Planner 对代码任务的长期规划约束。

### 7.1 默认仓库和分支假设

```text
如果用户没有指定 branch，普通新代码任务默认从 repo default branch 创建工作分支。
一期 default branch 按 master/main 推断。
不要假设存在未出现在 hidden workspace_context 或用户输入中的历史分支。
```

### 7.2 Branch 命名规则

```text
普通代码任务使用语义化 Jarvis branch：
jarvis/<task_slug>/<node_id>

task_slug 来自用户目标，短而可读。
node_id 使用语义化 snake_case。
如果可能冲突，runtime 可以追加短 hash。
```

### 7.3 多轮续接规则

```text
如果用户说“刚才那个”“继续上次”“在刚才基础上改”，优先使用最近 active workspace_context。
继续修改默认复用旧 branch。
旧 branch 可以有多轮 commit。
如果用户明确要求另开方向，source_ref 写上一轮 branch，新 branch 使用新语义名。
```

### 7.4 多 Node 依赖规则

```text
同一 branch 不允许多 node 同时使用。
如果后一个 coder node 依赖前一个 coder node 的代码结果，后一个 node 的 source_ref 必须指向前一个 node 的 branch。
不要让依赖代码结果的后续 node 默认从 master/main 创建。
```

### 7.5 Publish 规则

```text
只有用户明确要求合并、推送、发布、进 master/main，才规划 publish node。
publish node 的 prompt 必须写明 source_ref 和 target_ref。
source_ref 优先来自 hidden workspace_context，或用户明确指定。
target_ref 默认 repo default branch。
publish node 不做新功能开发。
```

### 7.6 Hidden Workspace Context 使用规则

```text
Planner 可以读取 hidden workspace_context。
workspace_context 不是 Git source of truth，只是帮助解析“刚才那个”。
如果用户询问“你记录的 workspace 是什么/刚才在哪个分支”，可以用用户可见方式总结该 hidden context。
```

---

## 8. Git 状态为准

一期不维护独立 feature lifecycle 状态机。feature branch、worktree、merge、push 的真实状态以 Git 实时查询为准，避免 Jarvis 自己维护的状态和 Git 事实不一致。

`workspace_context.status` 只保留轻量会话语义：

```text
active    Planner 可以把这个 workspace 当作“刚才那个”继续复用
closed    不建议继续复用；如果用户继续修改，应从最新 master/main 新建 worktree
```

真实状态在需要时通过 Git 查询：

```text
worktree 是否存在：检查 worktree 路径 / git worktree list
当前分支：git branch --show-current
是否 clean：git status --porcelain
branch 当前 HEAD：git rev-parse HEAD
hidden head_sha 是否过期：比较 workspace_context.head_sha 和当前 branch HEAD
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

普通 code node：

```text
运行在独立 feature branch + worktree 中。
可以修改代码、运行测试、提交本地 commit。
可以在自己的 feature branch 上连续提交。
不需要为普通本地 commit 透出主分支审批。
不能直接 push master/main。
不能直接 push 远程。
```

publish node：

```text
运行在 canonical repo 中。
执行 master/main merge、验证和 push 等影响主分支或远端的动作。
必须通过 Codex/Jarvis approval 透出审批。
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
-> Planner 生成普通 coder node
-> coder node prompt 写明 source_ref 和语义化 working branch
-> Coder/Codex 在该 branch 的 worktree 中修改和 commit
-> 生成 manifest / test log / Git metadata
-> ResultAggregator 追加 hidden workspace_context
```

### 10.2 多轮继续修改

```text
用户说“刚才那个改一下”
-> Planner 读取 hidden workspace_context
-> Planner 在 coder node prompt 中写明复用旧 branch/worktree
-> Coder/Codex 在原 branch/worktree 中继续修改并 commit
-> ResultAggregator 追加新的 hidden workspace_context
```

### 10.3 查看上一轮代码

```text
用户说“展示下刚才的逻辑”
-> Planner 读取 hidden workspace_context
-> coder node 只读旧 worktree / branch
-> 返回解释，不修改代码，不提交 commit
```

### 10.4 合并并推送

```text
用户明确要求合并/推送
-> Planner 读取 hidden workspace_context 或用户指定 source_ref
-> 生成 publish node
-> 受限 publish node 在 canonical repo 中执行
-> merge source_ref 到 master/main
-> run verification
-> push origin master/main
-> ResultAggregator 可以追加 status=closed 的 hidden workspace_context
```

---

## 11. 设计分析

### 11.1 成立的部分

这个方案能解决当前暴露出来的主要问题：

1. 普通代码任务不再污染 canonical repo。
2. 每个 node 有自己的 branch/worktree，隔离性强，容易解释。
3. 多轮对话可以通过 hidden workspace_context 继续旧 branch。
4. Python/pytest 临时文件有明确落点，不再默认污染 worktree。
5. 合并和推送从普通代码编辑动作中分离出来，可以通过 publish node 审批单独治理。

它也符合 Git 的基本约束：同一个 branch 不应被多个 worktree 同时 checkout。因此同一 branch 不允许多 node 并发使用。

### 11.2 主要风险

第一个风险是 Planner 需要正确选择 `source_ref`。没有 session integration branch 后，如果后续 node 依赖前序 node 的代码结果，Planner 必须把前序 branch 写成后续 node 的 `source_ref`，否则后续 node 会从 `master/main` 开始，丢失上下文。

第二个风险是 publish node 在 canonical repo 中运行。canonical repo 是长期主路径，一旦 publish node 运行失败，可能留下 merge conflict、未提交文件或半完成状态。因此 publish node 必须在开始前和结束后都检查 clean worktree，并在失败时保留清晰恢复指令。

第三个风险是 `reset --hard origin/master`。它对 canonical repo 很强势，只有在 canonical repo 被定义为受 Jarvis 管理、且禁止人工脏改时才成立。一期不应把它作为默认步骤。

第四个风险是 hidden workspace_context 过期。它只是历史记忆，不是 Git source of truth。续接或 publish 前应通过 Git 重新确认 branch、worktree、HEAD 和 dirty 状态。

第五个风险是临时文件被误提交。即使注入 tmp env，也需要 commit 前过滤或 `.gitignore` 兜底。

### 11.3 落地优先级

建议按以下顺序实现：

1. 更新 `skills/code-planning/SKILL.md`，固化 branch 命名、多轮续接、多 node 依赖和 publish 规则。
2. coder node prompt 中加入 `source_ref`、语义化 `branch`、复用旧 worktree 的约束。
3. 普通 coder node 支持根据 prompt/Git 事实创建或复用 `jarvis/<task_slug>/<node_id>` branch + worktree。
4. coder provider 启动时注入 node `tmp` 相关环境变量。
5. git commit / artifact 收集过滤 Python/pytest cache 和临时目录。
6. `NodeResult` 增加或规范化 Git metadata：`repo_id`、`branch`、`worktree`、`head_sha`、`base_ref`、`commit_sha`、`files`。
7. `ResultAggregator` 根据结构化 Git metadata 追加 hidden workspace_context 消息。
8. 增加 publish node 的 planner skill guidance 和 canonical repo 审批文案。
9. 最后开放 publish node 的 push 能力。

---

## 12. 总结

Jarvis 的代码任务设计收敛为：

```text
普通开发：node-owned semantic branch + isolated worktree
多轮续接：Planner 读取 hidden workspace_context 并复用旧 branch
发布动作：canonical repo + restricted publish node
治理方式：coder skill + workspace prompt + Git source of truth + publish approval
```

核心原则：

```text
branch 负责承载代码变更；
worktree 负责隔离开发现场；
tmp 负责承载执行污染；
ResultAggregator 生成的隐藏消息负责多轮恢复；
publish node 负责合并和推送；
Git 实时查询负责最终状态判断。
```
