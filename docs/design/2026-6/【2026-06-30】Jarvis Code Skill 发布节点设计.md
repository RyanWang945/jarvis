# Jarvis Code Skill 发布节点设计

| 项目 | 内容 |
|------|------|
| 日期 | 2026-06-30 |
| 更新 | 2026-07-01 |
| 状态 | Draft |
| 相关模块 | `app/task_runtime`, `app/task_runtime/node_execute_runtime.py`, `prompt/scenarios/coder_node_execute`, code skill |
| 目标 | 让 code skill 在需要合并或推送时指示 planner 创建专用 publish coder node |

---

## 1. 背景

普通代码节点适合完成实现：

```text
创建 node worktree
写代码
测试
由 runtime 生成 node commit
合入 session branch
```

但它不适合承担发布动作：

```text
合并到 master/main
创建发布 merge commit
push 到 origin
```

最新日志里的问题是：用户要求“把刚才的分支合并到 master，不需要审批”后，planner 创建了普通 coder node。这个 node 仍受到普通代码节点约束：不要创建 commit、不要 push。由于合并到 `master` 需要 merge commit，最终没有执行发布。

需要把“普通代码实现”和“发布到真实目标分支”拆开。

---

## 2. 核心结论

不引入 integration node。

只保留两类代码节点：

```text
code node
  普通实现节点
  在任务 workspace 内工作
  可以生成 node commit，并合入 session branch
  不负责 master/main 和远程发布

publish node
  专用发布节点
  在已注册项目的源目录工作
  有完整 git 操作能力
  高风险动作需要审批，尤其 push 远程
```

`integration` 这层暂时不需要。普通 node 到 session branch 的合入是 runtime 内部收尾。session branch 到 `master/main` 的发布统一用 publish node。

---

## 3. 最小 Node JSON

publish node 不需要复杂 contract。节点 JSON 只需要能让 runtime 选对 repo、模式和目标。

示例：

```json
{
  "id": "publish_to_master",
  "runtime": "coder",
  "mode": "publish",
  "repo_id": "smoke-test",
  "input_refs": ["branch:jarvis/smoke-test/sess_3316_ca7951592815"],
  "objective": "将 jarvis/smoke-test/sess_3316_ca7951592815 合并到 master。push 远程前必须请求审批。",
  "output_hint": "输出合并结果、merge commit、测试结果；如果执行了 push，输出远程分支状态。"
}
```

必要字段：

| 字段 | 必要性 | 说明 |
|------|--------|------|
| `mode=publish` | 必须 | 让 runtime 进入发布权限模型 |
| `repo_id` | 必须 | 定位已注册项目 |
| `objective` | 必须 | 承载 source branch、target branch、是否需要 push 等细节 |
| `input_refs` | 推荐 | 明确引用上一轮 session branch、commit 或 node result |

不需要放进 node JSON 的字段：

```text
schema_version
deliverable_type
ready_to_publish
publish_plan
requires_permissions
```

这些会增加 runtime 协议复杂度。第一版把细节放在 prompt / objective / output_hint 中，由 code skill 指导 planner 写清楚。

---

## 4. Code Skill 职责

code skill 不直接生成复杂 metadata，也不直接执行发布。它只给 planner 明确规则：

```text
如果用户只要求写代码：
  生成 mode=write 的普通 coder node。

如果用户要求合并到 master/main、发布、push：
  生成 mode=publish 的专用 coder node。

普通 coder node 不能直接发布到 master/main。
publish coder node 必须包含 repo_id。
publish coder node 的 objective 必须写明 source branch 和 target branch。
push 远程必须在 objective 中写明需要审批。
```

planner 需要从历史结果中取出最近的 session branch，写进 publish node 的 prompt：

```text
source branch: jarvis/smoke-test/sess_3316_ca7951592815
target branch: master
```

如果找不到明确 source branch，planner 应先 ask user 或生成 read/inspect 节点确认，不应让 coder 猜。

---

## 5. Workspace 策略

普通 code node：

```text
workspace = sessions/<session_id>/nodes/<node_id>/
repo      = workspace/repo
```

publish node：

```text
workspace = 已注册项目源目录
repo      = data/projects/<repo_id>
```

原因：

1. 发布动作面向真实项目分支，不是面向历史 node workspace。
2. 复用历史 node workspace 容易停留在 node branch。
3. `master/main` 合并和 push 应该在注册项目源目录里执行，便于权限、远程、worktree 状态检查。
4. publish node 不应污染普通实现节点 workspace。

第一版可以直接以 registered project source dir 作为 cwd。后续如需更强隔离，再引入专用 publish worktree，但不要引入 integration node。

---

## 6. 权限与审批

普通 code node：

```text
workspace 内无需审批
不允许 push
不允许直接发布到 master/main
runtime 可在收尾阶段创建 node commit
runtime 可合入 session branch
```

publish node：

```text
拥有完整 git 操作能力
允许 checkout / fetch / merge / commit
允许处理冲突
允许运行测试
push 远程必须审批
危险 destructive 操作仍需审批
```

用户说“不需要审批”只能降低普通 workspace 内动作的交互成本，不能取消 push 审批。尤其是：

```text
git push
git push --force
删除分支
重写历史
```

这些动作必须审批。

---

## 7. Planner 行为

### 7.1 写代码

用户说：

```text
在 smoke-test 中写一个 FastAPI demo
```

planner 生成：

```json
{
  "id": "write_fastapi_demo",
  "runtime": "coder",
  "mode": "write",
  "repo_id": "smoke-test",
  "objective": "实现 FastAPI demo",
  "input_refs": []
}
```

### 7.2 合并到 master/main

用户说：

```text
把刚才的分支合并到 master
```

planner 生成：

```json
{
  "id": "publish_to_master",
  "runtime": "coder",
  "mode": "publish",
  "repo_id": "smoke-test",
  "input_refs": ["branch:jarvis/smoke-test/sess_3316_ca7951592815"],
  "objective": "在已注册项目源目录中，将 jarvis/smoke-test/sess_3316_ca7951592815 合并到 master。本轮不 push。",
  "output_hint": "输出 merge commit、冲突情况、测试结果。"
}
```

### 7.3 合并并推送

用户说：

```text
把刚才的分支合并到 master 并 push
```

planner 生成：

```json
{
  "id": "publish_to_master_and_push",
  "runtime": "coder",
  "mode": "publish",
  "repo_id": "smoke-test",
  "input_refs": ["branch:jarvis/smoke-test/sess_3316_ca7951592815"],
  "objective": "在已注册项目源目录中，将 jarvis/smoke-test/sess_3316_ca7951592815 合并到 master。push origin/master 前必须请求审批。",
  "output_hint": "输出 merge commit、push 审批与执行结果、远程分支状态。"
}
```

---

## 8. Publish Node 执行流程

publish node 的 prompt 应要求 coder：

```text
1. 确认 cwd 是 repo_id 对应的已注册项目源目录。
2. 确认 source branch 和 target branch。
3. 检查 worktree clean。
4. checkout target branch。
5. merge source branch。
6. 如有冲突，停止并返回 blocked。
7. 运行必要测试或说明未运行原因。
8. 如用户要求 push，先请求审批，再 push。
9. 输出真实执行结果。
```

状态规则：

| 情况 | node status |
|------|-------------|
| merge 成功 | `completed` |
| merge 成功且 push 成功 | `completed` |
| merge 冲突 | `blocked` |
| push 等待审批 | `needs_user_input` 或 `blocked` |
| 权限不足 | `failed` 或 `blocked` |
| 实际未执行 merge | 不能返回 `completed` |

---

## 9. Aggregator 行为

aggregator 不执行 merge/push，也不需要复杂发布 contract。

第一版：

```text
planner 负责生成 publish node
publish node 负责执行发布
aggregator 负责总结结果
```

后续如果做动态 finalization，可以让它决定“还需要创建 publish node”，但执行仍由 publish node 完成。

---

## 10. 用户反馈

最终回复必须明确分支层级：

```text
代码已完成，位于 session branch。
已合并到 master，merge commit 是 xxx。
已 push 到 origin/master。
未合并，因为存在冲突。
未 push，因为等待审批。
```

不要把 session branch 完成误报为 master 已更新。

---

## 11. 实施计划

### P0

1. planner 支持生成 `mode=publish`。
2. code skill 增加 publish node 生成规则。
3. runtime 识别 `mode=publish`，cwd 设置为已注册项目源目录。
4. publish node 拥有完整 git 操作能力。
5. push 远程必须审批。
6. publish node 不复用历史普通 code node workspace。
7. 未执行 merge/push 时不能返回 `completed`。

### P1

1. 稳定解析“刚才的分支”为最近 session branch。
2. Feishu 卡片展示 publish 状态、source branch、target branch。
3. merge conflict 结构化输出。

### P2

1. 支持专用 publish worktree。
2. 支持 GitHub PR 发布模式。
3. 支持 tag、release note、changelog。

---

## 12. 测试建议

1. 普通 code node 不生成 publish 权限。
2. 用户要求合并 master 时，planner 生成 `mode=publish`。
3. publish node cwd 是注册项目源目录。
4. publish node 不复用历史 node workspace。
5. publish node 能把 session branch 合并到 master。
6. 未要求 push 时不 push。
7. 要求 push 时必须出现审批。
8. merge 冲突时返回 blocked。
9. 实际未 merge 时不能返回 completed。

