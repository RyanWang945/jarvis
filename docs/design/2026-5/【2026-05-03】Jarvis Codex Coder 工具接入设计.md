# Jarvis Codex Coder 工具接入设计

| 项目 | 内容 |
|------|------|
| 作者 | Codex |
| 日期 | 2026-05-03 |
| 版本 | v1.0 |
| 状态 | 设计中 |
| 依赖 | Jarvis ReAct Runtime、现有 Claude Code 工具实现 |

---

## 1. 结论

建议新增一个对 LLM 暴露的高权限代码工具：

- `delegate_to_codex`

同时把当前的：

- `delegate_to_claude_code`

先保留在代码里，但不再注入给 LLM。Codex 接入完成并通过真实仓库验证后，Claude Code 工具应废弃并删除，不作为长期 fallback 或并行 provider 保留。

第一阶段不建议直接使用 Codex Python SDK 作为默认实现。更稳妥的方案是先用 Codex 官方支持的非交互 CLI：

```text
codex exec --json --sandbox workspace-write --cd <workdir>
```

原因是：

1. 当前 Jarvis 的高权限 coder 工具已经是 subprocess + headless agent 的形态，迁移到 `codex exec` 改动最小。
2. 本机已经安装 `codex-cli 0.128.0`，具备立即验证条件。
3. Codex Python SDK 当前仍偏实验性，不适合作为 Jarvis 第一版默认生产路径。
4. `codex exec --json` 可以提供 JSONL 事件输出，后续可以逐步接入结构化审计和长任务事件流。
5. 先替换工具注入，再替换底层能力，比一次性重构 runtime 风险更低。

最终目标不是把 Jarvis 和某个具体 CLI 绑死，而是先建立清晰的 Codex coder 边界：

```text
LLM-visible tool: delegate_to_codex
    -> Jarvis proposal gate
    -> Codex backend adapter
        -> phase 1: codex exec
        -> phase 2: Codex SDK / MCP server / Agents SDK
    -> ToolExecutionResult
    -> messages + tool_calls audit
```

第一阶段还必须把两个安全约束做成硬校验：

- `codex exec --json` 的 raw JSONL 不进入 `ToolExecutionResult.stdout`，避免污染后续 LLM 上下文。
- `allow_commit` / `allow_push` 不只靠 prompt，必须通过 preflight/postflight 对比做权限硬校验；违反时 `ToolExecutionResult.ok=false`。

---

## 2. 当前现状

### 2.1 当前工具注入

Jarvis 的工具注入集中在：

- `app/tools/definitions.py`
- `app/tools/runtime.py`
- `app/agent_react/react_graph.py`

当前 `builtin_tool_definitions()` 注册了一个高权限代码工具：

```text
delegate_to_claude_code
```

它的语义是：

- 用于多文件代码修改、修 bug、重构、测试、git workflow。
- 需要 `workdir`。
- 属于 `proposal` execution mode。
- 可以修改文件。
- 会经过 `check_tool_policy()` 的高权限 delegation gate。

`build_llm_tools()` 会把所有 `exposed_to_llm=True` 的工具转换成 OpenAI-compatible function tool schema 并传给主模型。

这意味着只要 `delegate_to_claude_code` 仍然是 `exposed_to_llm=True`，主模型就会继续看到并可能选择 Claude Code 工具。

### 2.2 当前 Claude Code 实现

当前实现位于：

```text
app/tools/coder.py
```

核心执行方式是：

```text
claude --print --permission-mode bypassPermissions --allowedTools ...
```

Jarvis 在外层负责：

- 构造 coder worker instruction。
- 注入 `allow_commit` / `allow_push` 约束。
- 注入 verification command。
- 检查 `workdir`。
- 处理 git `safe.directory`。
- 清理一些本地 workspace 遗留问题。
- 执行后收集 postflight：
  - branch
  - commit
  - status
  - modified files
  - diff stat
  - upstream sync state
- 把结果适配成统一的 `ToolExecutionResult`。

这套外层能力仍然有价值，Codex 工具应尽量复用，而不是重写一套完全不同的审计路径。

### 2.3 当前策略 gate

`check_tool_policy()` 当前已经具备高权限 delegation 的基础规则：

- instruction 不能为空。
- 高权限工具必须有显式 workdir。
- 用户最近消息必须像是代码或仓库任务。
- `allow_push=true` 必须依赖 `allow_commit=true`。
- workdir 必须存在。

这些规则属于 Jarvis 的业务边界，不应该下沉给 Claude 或 Codex 自己决定。

Codex 工具应该继续走同一套 policy，而不是绕过 Jarvis 的 gate。

---

## 3. 设计目标

### 3.1 第一阶段目标

1. 新增 `delegate_to_codex` 工具。
2. 暂停向 LLM 注入 `delegate_to_claude_code`。
3. 过渡期短暂保留 Claude Code handler 便于紧急回滚；Codex 验证通过后废弃删除。
4. 保持现有 proposal gate、tool_calls 审计、ToolExecutionResult 契约不变。
5. Codex backend 首版通过 `codex exec` 非交互模式实现。
6. 复用现有 coder instruction、preflight、postflight 和 artifacts 语义。
7. Codex JSONL 只用于内部解析、日志和 artifact，不直接回灌到 stdout。
8. `allow_commit` / `allow_push` 第一阶段就通过 git preflight/postflight 做硬校验。

### 3.2 非目标

第一阶段不做：

- 不重构整个 ReAct runtime。
- 不引入新的 turns/messages/tool_calls 数据模型。
- 不把 Codex 原生事件完整落库成 events 表。
- 不直接迁移到 Codex Python SDK。
- 不同时向 LLM 暴露 Claude 和 Codex 两个语义近似的 coder 工具。
- 不允许 Codex 默认 commit 或 push。
- 不建设长期 `claude|codex` provider 并存架构。

---

## 4. 对外工具设计

### 4.1 工具名

新增：

```text
delegate_to_codex
```

旧工具：

```text
delegate_to_claude_code
```

处理策略：

- 过渡期保留定义和 handler，便于紧急回滚。
- 设置 `exposed_to_llm=False`。
- Codex 接入完成并通过真实仓库验证后，删除 Claude Code 工具定义、handler 和相关测试入口。

### 4.2 工具描述

`delegate_to_codex` 应明确表达：

- 这是高权限 repository development workflow 工具。
- 只用于明确的代码任务。
- 适用于多文件修改、bug fix、重构、测试、代码审查跟进。
- 不用于普通问答、事实查询、轻量 shell 检查。
- 调用前主模型必须收集足够上下文并给出完整 task contract。

建议描述：

```text
High-privilege delegation tool backed by Codex for repository development workflows.
Use this only for substantial code tasks such as multi-file edits, refactors,
bug fixes, code review follow-up, test execution, and git workflows inside a repository.
Do not use this for simple shell commands, factual questions, or lightweight search.
Before calling it, gather enough context to issue one complete task contract.
```

### 4.3 参数 schema

首版保持和 `delegate_to_claude_code` 一致：

```text
instruction: string
workdir: string
verification_cmd?: string
allow_commit?: boolean = false
allow_push?: boolean = false
```

这样可以最大化复用：

- policy gate
- tests
- audit table
- real repo benchmark
- tool result rendering

### 4.4 返回契约

继续返回 `ToolExecutionResult`：

```text
ok: bool
exit_code: int | None
stdout: str
stderr: str
artifacts: list[str]
summary: str
```

上层不直接依赖 Codex 原生事件格式。

---

## 5. Codex Backend 设计

### 5.1 首版执行方式

首版通过 subprocess 调用：

```text
codex exec --json --sandbox workspace-write --cd <workdir> -
```

instruction 通过 stdin 传入。

建议参数：

```text
codex exec
  --json
  --sandbox workspace-write
  --cd <workdir>
  -
```

如需临时绕过 git repo 检查，可在明确场景下增加：

```text
--skip-git-repo-check
```

但默认不建议加。coder 工具本身是 repository workflow 工具，非 git 目录应先由 policy 或 preflight 拦住。

### 5.2 为什么不用 danger-full-access

当前 Claude Code 使用了：

```text
--permission-mode bypassPermissions
```

迁移到 Codex 时不建议直接等价为：

```text
--dangerously-bypass-approvals-and-sandbox
```

原因：

- Jarvis 自己的工具已经是高权限 proposal 工具，但仍应尽量把实际执行限制在目标仓库。
- Codex 支持 `workspace-write` sandbox，和 Jarvis 的 `workdir` 边界更匹配。
- 第一阶段默认不允许 push，通常不需要 full access。

只有在后续真实仓库 benchmark 证明 `workspace-write` 无法覆盖必要能力时，再评估更高权限模式，并且必须有显式配置开关。

### 5.3 Instruction 构造

Codex 复用当前 `_build_coder_instruction()` 的核心规则，但文案从 Claude-neutral 改成 provider-neutral：

```text
You are running as a Jarvis coder worker for a local repository.
Operate only inside the working directory provided by the process cwd.
Prefer direct file edits over explaining what should be changed.
Before committing or pushing, inspect git status and the relevant diff.
Treat the provided task contract and permissions as hard constraints.
Do not modify unrelated files.
End with a concise summary of files changed, commit hash if created, and push result if pushed.
```

如果 `allow_commit=false`：

```text
Do not create any git commit.
```

如果 `allow_push=false`：

```text
Do not push to origin.
```

如果提供 `verification_cmd`：

```text
Run this verification command before finishing: <verification_cmd>
```

### 5.4 输出解析

`codex exec --json` 输出 JSONL events。

首版不需要把全部事件结构落库，但必须避免 stdout 污染。`ToolExecutionResult.stdout` 会作为 tool message 回灌给主模型，因此它只能包含对后续推理有价值的干净摘要。

处理规则：

1. raw JSONL 写入日志文件或 artifact 文件，例如 `data/coder_runs/<run_id>/codex-events.jsonl`。
2. `stdout` 只包含最终可读摘要、变更文件、验证结果、权限校验结果。
3. 尽量从 JSONL 中提取最终 assistant message 作为摘要来源。
4. 解析失败时，不把完整 raw JSONL 塞回 stdout；只返回简短解析错误，并把 raw JSONL 文件路径放进 artifacts。
5. Jarvis preflight/postflight 可以进入结构化 artifact；stdout 中只保留压缩后的关键结果。

建议 stdout 结构：

```text
<codex final summary>

[JARVIS_PREFLIGHT]
branch=<branch> head=<head> status=<clean|dirty> upstream=<upstream>

[JARVIS_POSTFLIGHT]
branch=<branch> head=<head> status=<clean|dirty> files_modified=<n>

[JARVIS_PERMISSION_CHECK]
commit_allowed=<true|false> commit_changed=<true|false> result=<ok|failed>
push_allowed=<true|false> upstream_changed=<true|false> result=<ok|failed|unknown>
```

### 5.5 Postflight 仍由 Jarvis 执行

无论 Codex 原生输出说自己做了什么，Jarvis 都必须独立执行 postflight：

- `git status --short --branch`
- `git status --porcelain`
- `git diff --stat`
- `git branch --show-current`
- `git rev-parse --short HEAD`
- `git rev-parse HEAD`
- `git log -1 --pretty=%s`
- `git remote get-url origin`
- `git rev-parse --abbrev-ref --symbolic-full-name @{u}`，如果存在 upstream
- `git rev-parse @{u}`，如果存在 upstream

这是审计事实源，不应完全信任 agent 自述。

### 5.6 Preflight/Postflight 权限硬校验

Codex 执行前必须记录 preflight：

```text
branch
head
short_head
status_short_branch
status_porcelain
upstream_name
upstream_head
remote_url
```

Codex 执行后记录 postflight，并与 preflight 对比。

硬校验规则：

- 如果 `allow_commit=false` 且 `postflight.head != preflight.head`，则 `ToolExecutionResult.ok=false`。
- 如果 `allow_push=false` 且存在 upstream，且 `postflight.upstream_head != preflight.upstream_head`，则 `ToolExecutionResult.ok=false`。
- 如果执行后无法读取 git 状态，且任务发生过非零退出或权限状态无法判断，则 `ToolExecutionResult.ok=false`。
- 如果 branch 发生变化，stdout 和 summary 必须明确提示；是否失败取决于任务 contract，第一阶段建议先标记 warning，后续可按配置收紧。

注意：

- `allow_commit=true` 只表示允许创建 commit，不表示必须 commit。
- `allow_push=true` 必须继续要求 `allow_commit=true`，且只表示允许 push，不表示必须 push。
- 如果没有 upstream，push 校验结果应为 `unknown`；默认 no-push 场景下，如果无法证明 upstream 未变化，应在 summary 中明确说明。

---

## 6. 配置设计

第一阶段不建议新增 `JARVIS_CODER_PROVIDER=claude|codex`。

当前目标不是长期 provider 并存，而是把 Jarvis 的高权限 coder 工具迁移到 Codex。因此第一阶段直接：

- `delegate_to_codex` 暴露给 LLM。
- `delegate_to_claude_code` 不暴露给 LLM。

过渡期如需回滚，可以通过代码改动或短期 feature flag 切回旧工具注入。但 Codex 验证通过后，应删除 Claude Code 工具，而不是继续维护 provider 配置。

---

## 7. 工具注入策略

### 7.1 第一阶段

`builtin_tool_definitions()` 中：

```text
delegate_to_codex: exposed_to_llm=True
delegate_to_claude_code: exposed_to_llm=False
```

这样主模型只看到 Codex 工具，不会在两个 coder 工具之间摇摆。

### 7.2 回滚策略

Codex 刚接入时可以保留短期回滚路径。如果 Codex backend 不稳定，可以快速切回：

```text
delegate_to_codex: exposed_to_llm=False
delegate_to_claude_code: exposed_to_llm=True
```

因为：

- 工具 schema 一致。
- policy gate 一致。
- audit 结构一致。
- runtime 不需要迁移。

这个回滚路径只服务迁移期，不是长期架构目标。

### 7.3 后续策略

当 Codex 稳定后：

1. 删除 Claude Code 工具定义和 handler。
2. 删除 Claude Code 专属测试或改造成历史迁移测试。
3. 保持 `delegate_to_codex` 作为唯一高权限 coder 工具。
4. 如后续接入 SDK / MCP，只替换 `delegate_to_codex` 的 backend adapter，不重新引入 Claude provider。

---

## 8. 安全边界

### 8.1 Jarvis policy 仍是第一道门

Codex 工具必须继续使用 `check_tool_policy()`。

至少保留：

- 非代码请求拒绝高权限 delegation。
- 空 instruction 拒绝。
- 空 workdir 拒绝。
- 不存在的 workdir 拒绝。
- `allow_push=true` 且 `allow_commit=false` 拒绝。

### 8.2 Codex sandbox 是第二道门

首版默认：

```text
--sandbox workspace-write
```

这可以降低 agent 修改仓库外文件的风险。

### 8.3 prompt 约束不是唯一安全机制

`allow_commit` / `allow_push` 不能只靠 prompt，第一阶段就必须硬校验。

执行前记录 preflight，执行后记录 postflight，然后对比：

- `allow_commit=false` 但 HEAD 变化：`ToolExecutionResult.ok=false`。
- `allow_push=false` 但 upstream head 变化：`ToolExecutionResult.ok=false`。
- 权限校验失败时，`summary` 必须明确写出违反了哪个权限。
- 权限校验失败不应丢弃 Codex 输出和 postflight artifact；这些信息仍然用于排查。

### 8.4 safe.directory 必须保留

当前环境已经暴露过 git `dubious ownership` 问题。

Codex backend 也必须像 Claude backend 一样显式注入：

```text
GIT_CONFIG_COUNT=1
GIT_CONFIG_KEY_0=safe.directory
GIT_CONFIG_VALUE_0=<workdir>
```

同时 Jarvis 自己执行 postflight git 命令时继续使用：

```text
git -c safe.directory=<workdir> ...
```

---

## 9. 实施计划

### Phase 1：新增 Codex 工具

改动范围：

- 新增 `app/tools/codex.py`
- 更新 `app/tools/definitions.py`
- 更新 `app/tools/__init__.py`
- 更新 `app/tools/runtime.py` 中风险提示文案
- 新增或更新 coder 工具测试

行为：

- LLM 只看到 `delegate_to_codex`。
- Claude Code 工具保留但不注入。
- Codex 通过 `codex exec` 执行。
- Codex JSONL 写入 artifact/log，不进入 stdout。
- `allow_commit` / `allow_push` 通过 preflight/postflight 硬校验。

### Phase 2：复用和整理 coder 公共逻辑

当前 `app/tools/coder.py` 中有大量 provider-neutral 能力：

- instruction builder
- preflight cleanup
- postflight git audit
- artifact extraction
- modified files parsing

建议抽到：

```text
app/tools/coder_common.py
```

Claude 和 Codex backend 共同复用。

第一阶段建议直接抽公共模块，尤其是 preflight/postflight 和权限硬校验，避免 Claude/Codex 两份 git 审计逻辑漂移。

### Phase 3：废弃 Claude Code

Codex 通过单元测试、runtime 测试和真实仓库 benchmark 后：

- 删除 `delegate_to_claude_code` 工具定义。
- 删除或归档 Claude CLI backend。
- 更新文档和测试，不再把 Claude Code 当作可选 provider。

### Phase 4：真实仓库 benchmark

复用现有 opt-in 测试思路：

```text
JARVIS_RUN_REAL_CODER_TESTS=1
JARVIS_REAL_CODER_REPO=<repo>
```

验证：

- 能否创建分支。
- 能否从零创建最小 Python 项目。
- 能否继续修改已有代码。
- 能否运行 verification command。
- 是否污染 `.idea/`、`.pytest_cache/`、`__pycache__/`、`.venv/`。
- 是否尊重 no commit / no push。
- no commit / no push 被违反时是否 `ok=false`。
- stdout 是否保持干净，不包含 raw JSONL。

### Phase 5：SDK / MCP 演进

当 CLI backend 稳定后，再评估：

1. Codex TypeScript SDK。
2. Codex MCP server。
3. OpenAI Agents SDK 编排 Codex。
4. 长任务事件流接入 Jarvis `events` 表。

这一步不影响 `delegate_to_codex` 的上层工具契约。

---

## 10. 测试策略

### 10.1 单元测试

新增测试：

- `delegate_to_codex` 出现在 `build_llm_tools()`。
- `delegate_to_claude_code` 不出现在 `build_llm_tools()`。
- `get_tool_definition("delegate_to_claude_code")` 仍可用。
- 非代码请求调用 `delegate_to_codex` 会被 proposal gate 拒绝。
- `allow_push=true` 且 `allow_commit=false` 会被拒绝。
- Codex CLI 缺失时返回清晰错误。
- `allow_commit=false` 且 HEAD 变化时 `ToolExecutionResult.ok=false`。
- `allow_push=false` 且 upstream head 变化时 `ToolExecutionResult.ok=false`。
- raw Codex JSONL 不出现在 `ToolExecutionResult.stdout`。

### 10.2 Runtime 测试

更新现有 `test_tools_coder.py`：

- fake LLM tool call 从 `delegate_to_claude_code` 改成 `delegate_to_codex`。
- audit 中记录的 tool_name 应是 `delegate_to_codex`。
- reject 和 completed 两条路径都要覆盖。

### 10.3 Backend 测试

对 `run_codex_coder_tool()` 做 subprocess mock：

- 验证命令包含 `codex exec`。
- 验证包含 `--json`。
- 验证包含 `--sandbox workspace-write`。
- 验证包含 `--cd <workdir>`。
- 验证 instruction 从 stdin 传入。
- 验证 postflight 被追加。
- 验证 preflight 在 Codex 执行前采集。
- 验证权限硬校验能覆盖 HEAD 变化和 upstream 变化。
- 验证 raw JSONL 被写入 artifact/log，而不是 stdout。

### 10.4 真实仓库测试

真实仓库测试继续 opt-in，不进默认 CI。

因为它会：

- 依赖本机 Codex 登录状态。
- 消耗真实模型调用。
- 修改真实工作树。
- 运行时间较长。

---

## 11. 风险与应对

### 11.1 Codex CLI JSONL 格式变化

风险：

- `codex exec --json` 的事件字段可能随 CLI 版本变化。

应对：

- 首版只做宽松解析。
- 解析失败时保留 raw JSONL artifact/log。
- 解析失败时 stdout 只写简短错误，不回灌完整 JSONL。
- 上层只依赖 `ToolExecutionResult`，不依赖 Codex 原生字段。

### 11.2 Codex sandbox 限制过严

风险：

- `workspace-write` 可能拦截某些必要命令或缓存写入。

应对：

- 首选让任务在 workspace 内完成。
- 如需额外 writable dir，后续通过 `--add-dir` 显式增加。
- 不默认使用 full access。

### 11.3 LLM 仍尝试调用旧工具

风险：

- 历史上下文、测试 fixture 或模型记忆里可能出现 `delegate_to_claude_code`。

应对：

- 旧工具不注入给模型。
- 迁移期如果模型仍幻觉调用旧工具，`get_tool_definition()` 仍能找到旧工具，不会直接 unknown tool。
- 可在 system prompt 或工具描述中强化 Codex 是当前 coder 工具。
- Codex 稳定后删除旧工具，此时幻觉调用应按 unknown tool 或显式废弃提示处理。

### 11.4 no commit / no push 约束被突破

风险：

- Agent 可能误执行 commit 或 push。

应对：

- prompt 明确禁止。
- preflight/postflight 独立审计。
- 第一阶段就硬校验：违反权限时 `ToolExecutionResult.ok=false`，tool_call 标记 failed。

---

## 12. 最终建议

建议按以下顺序实现：

1. 抽出 `coder_common.py`，复用 instruction、preflight、postflight。
2. 新增 `codex.py`，实现 `run_codex_coder_tool()`。
3. 新增 `delegate_to_codex` 工具定义并暴露。
4. 将 `delegate_to_claude_code` 改为不暴露。
5. 更新 policy 文案和 tests。
6. 用 mock 测试验证 runtime 行为。
7. 再跑一次 opt-in 真实仓库 Codex benchmark。

这条路线能满足当前目标：

- 新增 Codex 工具。
- Claude Code 先不注入。
- Codex 稳定后废弃 Claude Code。
- 不破坏 Jarvis 现有 ReAct runtime。
- 迁移期短暂保留旧实现作为回滚路径。
- 保持 stdout 干净，避免 raw JSONL 污染主模型上下文。
- 从第一阶段开始硬校验 commit/push 权限。
- 为后续 SDK / MCP / Agents SDK 演进留下空间。

---

## 13. 参考

- Codex SDK  
  https://developers.openai.com/codex/sdk

- Codex non-interactive mode  
  https://developers.openai.com/codex/noninteractive

- Codex with Agents SDK / MCP  
  https://developers.openai.com/codex/guides/agents-sdk

- OpenAI GPT-5.3-Codex model  
  https://developers.openai.com/api/docs/models/gpt-5.3-codex
