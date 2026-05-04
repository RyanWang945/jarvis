# Jarvis 工具暴露与 Intent 动态裁剪调研

| 字段 | 内容 |
| --- | --- |
| 日期 | 2026-05-04 |
| 作者 | Codex |
| 背景 | Feishu 连续指令与 `21 / 14 580 22` 异常回复暴露了工具暴露边界问题 |
| 结论 | 只改 tool description 不够，V1 应引入基于 intent/profile 的工具动态裁剪 |

## 1. 问题背景

最新日志里出现了一个典型问题：

```text
用户：看一下 jarvis 当前分支有多少未提交的
Codex 工具输出：21
14 580 22
```

随后用户追问：

```text
21
14 580 22 啥意思
```

这一轮被分类为 `chat`，但 runtime policy 仍暴露了：

```text
obsidian_wiki_query
business_knowledge_search
scheduled_task
```

模型先调用 `business_knowledge_search` 去查 “git diff stat numbers meaning”，失败后又调用 `scheduled_task` 创建了一个 `dummy / in 10 minutes` 的提醒。

这说明问题不只是工具描述不清楚，而是模型在不该看到某些工具时仍然看到了它们。

## 2. 当前 Jarvis 实现观察

当前工具暴露链路大致是：

```text
turn_classifier
  -> resolve_runtime_policy
  -> RuntimePolicy.allowed_tools
  -> build_llm_tools(allowed_tools)
  -> LLM function calling
```

当前 `basic/chat` 模式默认包含 `_ACTION_TOOLS`：

```python
_ACTION_TOOLS = ("scheduled_task",)
allowed_tools = [*_BASE_READ_TOOLS, *_ACTION_TOOLS]
```

这意味着普通解释型聊天也能看到 `scheduled_task`。

`delegate_to_codex` 的描述也有边界冲突：

- tool description 说不要用于 `simple shell commands` 或 `lightweight search`。
- runtime policy 又说本地仓库 `inspection / reports / tests / edits` 都应使用 `delegate_to_codex`。
- 结果是“查看当前分支未提交数量”这种轻量仓库检查仍会进入 Codex，但 Codex 的原始输出没有被强制整理成用户可读答案。

此外，`commit` intent 检测把“未提交”误判成用户要求 `commit`，导致只读查询被附加 `allow_commit=true`。

## 3. Tool Description 与动态工具暴露的区别

### 3.1 Tool Description

Tool description 是写给模型看的软约束：

```text
这个工具什么时候该用
这个工具不该什么时候用
参数应该怎么填
工具结果应该如何解释
```

它的优点是便宜、兼容 function calling；缺点是不能保证模型遵守。模型仍可能在失败重试、上下文混乱、提示冲突时误调用工具。

### 3.2 动态工具暴露

动态工具暴露是后端在调用 LLM 前裁剪工具列表：

```text
当前 turn 的 intent / mode / profile / permissions
  -> 计算 exposed_tools
  -> 只把这些工具 schema 发给模型
```

如果 `scheduled_task` 没有发给模型，模型就不可能创建 dummy reminder。

动态工具暴露是硬边界，tool description 是软引导。两者应该同时存在。

## 4. 开源实现调研

### 4.1 Codex CLI

Codex CLI 的工具面相对窄，核心围绕本地代码读写、命令执行、patch 和审批。它更依赖：

- sandbox mode
- approval policy
- workspace trust
- prompt 中的行为边界

Codex 的思路不是给模型大量业务工具，然后靠模型自己选，而是把能力面限制在代码 agent 所需范围内，再用审批和沙箱控制风险。

可借鉴点：

- 高风险工具不应在所有场景都暴露。
- commit/push 这类动作必须由明确用户意图或权限模式触发。
- 工具结果应该进入最终回复整理，而不是把 raw stdout 原样交给用户。

参考：

- https://developers.openai.com/codex/cli
- https://github.com/openai/codex/blob/main/codex-rs/core/prompt_with_apply_patch_instructions.md

### 4.2 Claude Code

Claude Code 的工具体系更接近“工具多，但权限细”。它区分：

- read-only tools
- file edit tools
- bash tools
- web/search 相关工具
- MCP tools
- permission modes
- allow / ask / deny 规则
- hooks / `canUseTool` 回调

Claude Code 的关键不是只靠工具描述，而是把工具使用放在权限系统里。

需要注意的是，Claude Code SDK 文档里提到 `allowed_tools` 更多是权限控制和审批控制，不完全等价于“模型是否能看到工具”。但它提供了工具权限层、hook 和 tool search 等机制，本质上也是把工具可用性从纯 prompt 里拿出来。

可借鉴点：

- 工具暴露和工具执行权限是两层：看得到不等于能执行。
- 高风险工具应有 ask/deny/allow 策略。
- 可以在工具执行前做 `canUseTool` 风格的二次检查。

参考：

- https://code.claude.com/docs/en/permissions
- https://code.claude.com/docs/en/agent-sdk/permissions
- https://code.claude.com/docs/en/tools-reference

### 4.3 Hermes

Hermes 的工具组织方式更接近个人助理场景。它把工具按 toolset 组织，例如：

- `web`
- `terminal`
- `file`
- `browser`
- `cronjob`
- `delegation`

用户或平台可以指定启用哪些 toolsets，例如只启用 `web,terminal`。这不是每次都把所有工具交给模型，而是通过 toolset 配置形成当前会话或当前运行环境的工具集合。

可借鉴点：

- Jarvis 可以把工具分成 `chat`、`research`、`repo`、`reminder`、`kb_write`、`image` 等 tool profile。
- Feishu 私聊、群聊、CLI、scheduler run 可以有不同默认 toolsets。
- reminder 这种 action tool 不应默认出现在所有 chat turn。

参考：

- https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/
- https://github.com/NousResearch/hermes-agent

### 4.4 OpenClaw

OpenClaw 的工具暴露机制最直接。它有：

- `tools.profile`
- `tools.allow`
- `tools.deny`
- 工具 group，例如 `runtime`、`fs`、`automation`
- profile，例如 `minimal`、`coding`、`messaging`、`full`

文档明确说明：deny 的工具不会发送给 model provider。

这就是 Jarvis 当前最应该参考的方向：先由后端选择 profile，再把 profile 映射成工具组和 deny/allow。

可借鉴点：

- 用 profile 表达场景，而不是在一个全局 tool list 里靠模型自觉。
- deny 要在 provider 调用前生效。
- profile 可以叠加 allow/deny，让默认策略简单，特殊场景可扩展。

参考：

- https://openclaw.cc/en/tools/
- https://docs.openclaw.ai/tools

## 5. 对 Jarvis 的设计建议

### 5.1 从 mode 到 intent profile

当前 Jarvis 已有 `turn_type` 和 `session_mode`，但粒度还不够。建议补一个更靠近工具暴露的 `tool_profile`：

```text
turn_type/session_mode + intent signals
  -> tool_profile
  -> allowed_tools
  -> tool execution policy
```

建议 V1 profiles：

| Profile | 典型 intent | 暴露工具 |
| --- | --- | --- |
| `chat_explain` | 解释、追问、澄清、普通聊天 | 无工具或只读记忆查询 |
| `reminder` | 提醒、叫醒、稍后通知、取消/查看提醒 | `scheduled_task` |
| `repo_inspect` | 查看分支、diff、未提交、测试状态、代码 review | `delegate_to_codex` 只读权限 |
| `repo_edit` | 修改代码、修 bug、更新文档、跑测试 | `delegate_to_codex` 编辑权限 |
| `repo_commit` | commit / push | `delegate_to_codex`，允许 commit/push |
| `research` | 查资料、deep research、需要最新信息 | web / kb search |
| `mixed` | “10 分钟后提醒我上课，现在先改代码” | `scheduled_task` + `delegate_to_codex` |

### 5.2 `scheduled_task` 的开放规则

`scheduled_task` 不应出现在默认 `chat` 工具集中。

只有满足以下条件之一才开放：

- 用户明确要求提醒、叫醒、稍后通知、定时、取消提醒、查看提醒。
- 复杂混合任务里包含独立 reminder 子意图。
- 当前 turn 是 scheduler 自身触发的恢复/管理流程。

不应开放的场景：

- 解释数字、解释 git 输出。
- 搜索失败后的替代动作。
- 模型想“测试一下工具”。
- 用户没有明确时间或提醒对象。

即使开放，工具描述也要写清楚：

```text
不得编造 title/prompt/time_text。
不得使用 dummy。
没有明确提醒意图时不要调用。
时间不明确时先询问用户。
```

### 5.3 `delegate_to_codex` 的开放规则

`delegate_to_codex` 应按 repo intent 开放，而不是所有 coding-adjacent turn 都默认高权限。

建议分成执行权限，不一定拆成多个工具名：

```text
repo_inspect:
  exposed_tools = ["delegate_to_codex"]
  tool_policy = read_only
  allow_commit = false
  allow_push = false

repo_edit:
  exposed_tools = ["delegate_to_codex"]
  tool_policy = edit
  allow_commit = false

repo_commit:
  exposed_tools = ["delegate_to_codex"]
  tool_policy = edit
  allow_commit = true
  allow_push = user_requested_push
```

需要修正 intent 检测：

```text
“未提交 / uncommitted changes / 有多少未提交” = repo_inspect
“提交一下 / 创建 commit / commit 并 push” = repo_commit
```

### 5.4 工具结果收口

动态暴露解决“乱调工具”，但不能解决“工具输出不好读”。

`delegate_to_codex` 返回后应分两类：

1. Codex 已返回用户可读报告：可以直接回传。
2. Codex 返回 raw stdout / 数字 / diff stat：必须再经过 final summarization。

对 `21 / 14 580 22` 这种输出，最终回复应变成：

```text
jarvis 当前有 21 个未提交条目。

diff 统计显示：
- 14 个文件发生变化
- 新增 580 行
- 删除 22 行
```

## 6. 推荐 V1 实施方案

### Phase 1：最小修复

1. 从 `basic/chat` 默认工具中移除 `scheduled_task`。
2. reminder intent 明确时才加入 `scheduled_task`。
3. 修正 `commit` intent，不把“未提交”当成 commit 请求。
4. 强化 `scheduled_task` description，禁止 dummy / 编造时间。
5. 强化 `delegate_to_codex` description，要求 raw 输出必须整理。

### Phase 2：Tool Profile

新增 `ToolProfile`：

```python
class ToolProfile:
    name: str
    exposed_tools: tuple[str, ...]
    execution_policy: dict[str, Any]
```

在 `resolve_runtime_policy` 中输出：

```python
RuntimePolicy(
    mode="coding",
    tool_profile="repo_inspect",
    allowed_tools=("delegate_to_codex",),
    tool_permissions={"delegate_to_codex": {"allow_commit": False, "allow_push": False}},
)
```

### Phase 3：工具组与 deny/allow

参考 OpenClaw，定义工具组：

```text
group:memory = obsidian_wiki_query, business_knowledge_search
group:reminder = scheduled_task
group:repo = delegate_to_codex
group:web = tavily_search
group:kb_write = obsidian_wiki_draft, obsidian_wiki_apply
```

最终由 profile + channel + user policy 合成：

```text
effective_tools = profile.default_groups
effective_tools += user_allow
effective_tools -= user_deny
effective_tools -= channel_deny
```

## 7. 与当前问题的直接映射

| 日志问题 | 根因 | 修复方向 |
| --- | --- | --- |
| `21 / 14 580 22` 原样回复 | Codex 原始输出没有 final summarization | 工具结果收口 |
| “啥意思”触发 `business_knowledge_search` | chat_explain 仍允许搜索工具 | chat_explain profile 限制工具 |
| “啥意思”触发 dummy reminder | `scheduled_task` 暴露过宽，description 不够硬 | reminder intent 才暴露 |
| “未提交”被识别成 commit | commit intent 正则过宽 | 增加 read-only uncommitted 例外 |
| `/clear` 与 running turn race | turn 没在 Feishu 提交前 claim running | 执行前原子 claim |

## 8. 结论

Jarvis 不应该把工具暴露问题完全交给模型提示词解决。

更稳的方向是：

```text
意图识别负责确定任务类型
runtime policy 负责裁剪工具
tool description 负责细化调用语义
tool execution policy 负责权限校验
final summarization 负责把工具结果变成用户答案
```

对于 V1，最应该先做的是：

1. `scheduled_task` 从默认 chat 工具集中移除。
2. reminder intent 明确时动态加入。
3. 修正 `delegate_to_codex` 的只读/commit/push intent。
4. Codex raw 输出强制整理成人话。

这比只改 description 更接近 Codex、Claude Code、Hermes、OpenClaw 的实际工程思路。
