# Jarvis Skill Listing 与 Skill Tool 标准化设计

| 项目 | 内容 |
|------|------|
| 作者 | Codex |
| 日期 | 2026-06-08 |
| 状态 | Draft |
| 相关模块 | `app/skills`, `app/agent_react/context_manager.py`, `app/tools/skill_guidance.py`, `app/tools/definitions.py` |
| 目标 | 将 Jarvis Skill 改造成 Claude Code 风格的 skill listing + Skill tool 按需加载主链路 |

---

## 1. 背景

Jarvis 当前已有 skill 目录加载能力：

```text
data/skills/
~/.jarvis/skills/
JARVIS_SKILL_PATH
```

每个 skill 可以通过 `SKILL.md` frontmatter 或 `manifest.yaml` 声明元数据，并由 `SkillPackageLoader` 加载到 `SkillRegistry`。

但当前 runtime 的使用方式仍然偏本地匹配：

```text
用户输入
  -> SkillRegistry.select_matches() 本地词法打分
  -> ContextManager.inject_selected_skills()
  -> 把完整 SKILL.md body 注入 <system-reminder>
  -> 模型直接看到完整 skill 内容
```

目标主链路应改成 Claude Code 风格：

```text
可用 skill listing 进入 prompt
  -> 模型根据 listing 自己选择是否调用 Skill tool
  -> Skill tool 加载某个完整 SKILL.md
  -> 在同一个 ReAct loop 的下一次 LLM call 中注入完整 skill guidance
  -> 模型继续按 skill 指南调用普通工具
```

当前 Jarvis 虽然有 `load_skill_guidance` 工具，但它不是这个意义上的 skill 加载工具。它仍然是本地查询/推荐 selected skills，并没有提供一个通用的 `Skill({ skill, args })` 加载器，也没有让模型基于 listing 主动选择 skill。

---

## 2. 核心结论

Jarvis skill 标准化不应设计成大 manifest。第一期采用“小标准、宽兼容、渐进披露”：

```text
核心标识：
  skill_id = skill 目录名

一期字段：
  display_name = frontmatter name
  description
  effective_description
  when_to_use
  disable_model_invocation
  user_invocable
  allowed_tools  # 只解析，不授权

内部派生字段：
  source_path
  content_path
  loaded_from
```

运行时核心链路：

```text
scan skills
  -> normalize skill metadata
  -> render model-facing skill listing
  -> inject listing into system-reminder
  -> expose Skill tool
  -> model calls Skill({ skill, args? })
  -> runtime loads full SKILL.md
  -> same ReAct loop next LLM call sees full skill guidance
```

关键原则：

1. `skill_id` 必须由 skill 目录名派生。
2. frontmatter `name` 只作为 `display_name` / UI 展示名。
3. model-facing listing 和 Skill tool 调用都使用 `skill_id`，不使用 `name`。
4. Skill listing 是菜单。
5. `Skill` tool 是加载器。
6. 模型是选择者。
7. `SKILL.md` 是选择后才披露的操作手册。

最终原则：

```text
Skill 不执行脚本。
Skill 不授权工具。
Skill 不做路由。
Skill 不替代 planner。
Skill 只是按需加载给模型看的程序性操作手册。
```

---

## 3. 设计目标

### 3.1 目标

1. 外部 skill 可以直接接入，最低只需要一个 `SKILL.md`。
2. `SKILL.md` 使用 Claude/Codex 风格 frontmatter，核心字段保持极少。
3. 每轮 prompt 中只放轻量 skill listing，不默认放完整 skill body。
4. 模型通过通用 `Skill` tool 选择并加载某个 skill。
5. `Skill` tool call 后，在同一个 ReAct loop 的下一次 LLM call 中，以 hidden/meta message 或 system-reminder 注入完整 skill guidance。
6. 完整 skill guidance 不永久写入 base system prompt。
7. 保留现有 skill 目录与旧字段兼容，降低迁移成本。

### 3.2 非目标

1. 不在第一期实现远程 marketplace。
2. 不让 skill 自动执行 `scripts/`。
3. 不让 `allowed_tools` 自动授权或改变实际 tool exposure。
4. 不引入 embedding/LLM 预选路由作为主路径。
5. 不要求外部 skill 编写 Jarvis 专属 manifest。
6. 不实现 `paths`、`model`、`effort`、`context/fork`、`agent`、`shell`、`hooks`、named arguments、inline shell、MCP/plugin skill。

---

## 4. Skill 包格式

### 4.1 最小格式

外部 skill 最小只需要：

```text
my-skill/
  SKILL.md
```

`SKILL.md`：

```markdown
---
description: Review GitHub pull requests. Use when the user asks to review a PR, inspect requested changes, or summarize risks.
---

# GitHub Review

Follow this workflow when reviewing a pull request...
```

### 4.2 skill_id 与 display_name

Jarvis 应区分两个概念：

```text
skill_id: 目录名，作为唯一标识、model-facing listing 标识和 Skill tool 调用参数
display_name: frontmatter name，可选，只用于 UI 或展示
```

例如：

```text
github-review/SKILL.md
```

即使 frontmatter 写了：

```yaml
name: GitHub PR Review Helper
```

模型和用户调用时仍应使用：

```text
github-review
```

如果没有 `name`，UI 展示可以使用 `skill_id` 作为 fallback。

### 4.3 推荐格式

```markdown
---
name: GitHub PR Review Helper
description: Review GitHub pull requests and summarize risks.
when_to_use: Use when the user asks to review a PR, inspect requested changes, or summarize requested changes.
---

# GitHub Review

...
```

### 4.4 可选目录

```text
github-review/
  SKILL.md
  references/
  scripts/
  assets/
```

这些目录只作为 skill 附带资源。`Skill` tool 不自动读取全部资源，也不自动执行脚本。模型加载 `SKILL.md` 后，如果指南要求查看某个 reference 或运行某个脚本，应继续通过普通文件/命令工具走 Jarvis 的权限系统。

---

## 5. 字段标准

### 5.1 一期字段

| 字段 | 必填 | 说明 |
|------|------|------|
| 目录名 / `skill_id` | 是 | skill 的真实唯一标识，也是 `Skill({ skill })` 的调用 key |
| `name` | 否 | 解析为 `display_name`，只用于 UI 展示，不进入 model-facing listing |
| `description` | 否 | `effective_description` 的第一优先级来源 |
| `effective_description` | 进入 listing 时必需 | loader 派生字段，用于 model-facing listing |
| `when_to_use` / `when-to-use` | 否 | 补充触发场景，拼进 listing |
| `disable_model_invocation` / `disable-model-invocation` | 否 | 为 true 时禁止模型主动调用，也不进入 listing |
| `user_invocable` / `user-invocable` | 否 | 控制用户能否用 `/skill-id` 直接调用 |
| `allowed_tools` / `allowed-tools` | 否 | 只解析为 expected tools metadata，不授权 |
| `source_path` | 派生 | skill 目录路径 |
| `content_path` | 派生 | `SKILL.md` 路径 |
| `loaded_from` | 派生 | skill 来源 |

### 5.2 effective_description 规则

进入默认 model-facing listing 的 skill 必须有 `effective_description`。

来源优先级：

1. frontmatter `description`
2. Markdown 正文首段
3. Markdown 第一个标题

如果无法提取 `effective_description`，该 skill 不进入默认 model-facing listing，但仍可被精确 slash 调用。

### 5.3 Listing 字段映射

model-facing listing 只包含：

```text
skill_id
effective_description
when_to_use
```

不包含：

```text
SKILL.md body
display_name / name
allowed_tools
arguments
paths
model
effort
hooks
```

### 5.4 后置字段

以下字段第一期暂缓：

```text
paths
model
effort
context / fork
agent
shell
hooks
named arguments
inline shell
MCP/plugin skill
```

---

## 6. Prompt 组装

### 6.1 Base system prompt

base system prompt 只放稳定机制说明，不放具体 skill body。

建议增加一段类似：

```text
Jarvis skills are procedural guidance packages. When the user request matches an available skill from the skill listing, call the Skill tool with the skill_id. Do not treat /skill-id as plain text; use the Skill tool or direct skill invocation path. Loading a skill only reveals its instructions. It does not execute scripts, grant permissions, perform routing, replace the planner, or perform the task by itself.
```

### 6.2 Skill listing system-reminder

每轮模型调用前，ContextManager 渲染可用 skill listing：

```text
<system-reminder>
The following skills are available for use with the Skill tool:

- github-review: Review GitHub pull requests and summarize risks. Use when the user asks to review a PR...
- weather: Get current weather and forecasts. Use when the user asks for current weather, forecast, temperature...
- social-search-guide: Use when answering questions about public discussion on X/Twitter...
</system-reminder>
```

listing 不包含完整 `SKILL.md` body，也不包含 `display_name/name` 和高级字段。

### 6.3 Listing 压缩

为避免 skill 数量增长导致 prompt 膨胀，listing 需要本地字符串压缩：

1. 单条 listing 先拼接 `effective_description + when_to_use`。
2. 单条描述设置字符上限，例如 250 chars。
3. 总 listing 设置预算，例如 context window 的 1% 或固定 token 上限。
4. 超预算时优先保留 `bundled` 或高优先级 skill。
5. 非优先 skill 平均压缩描述。
6. 极端情况下只保留 `skill_id`。

压缩不调用 LLM，不做 embedding 检索，不参与语义选择。选择仍然由主模型基于 listing 完成。

### 6.4 Listing 过滤

进入模型可见 listing 前，应先做本地确定性过滤：

1. `disable_model_invocation: true` 的 skill 不进入 listing。
2. 没有 `effective_description` 的 skill 不进入 listing。
3. 用户显式 `/skill_id` 调用不依赖 listing，可走 slash 精确路径。

---

## 7. Skill Tool 设计

### 7.1 Tool 定义

模型可见工具名保持：

```text
Skill
```

内部实现可以命名为：

```text
LoadSkillTool / load_skill
```

不要同时把 `load_skill_guidance` 暴露给模型，避免和 `Skill` tool 混淆。

参数：

```json
{
  "skill": "github-review",
  "args": "https://github.com/org/repo/pull/123"
}
```

schema：

```json
{
  "type": "object",
  "properties": {
    "skill": {
      "type": "string",
      "description": "skill_id of the skill to load."
    },
    "args": {
      "type": "string",
      "description": "Optional user-provided arguments or context for this skill invocation."
    }
  },
  "required": ["skill"]
}
```

### 7.2 执行行为

`Skill({ skill, args })` 执行：

1. 去掉 `skill` 前导 `/`。
2. 按 `skill_id` 查 `SkillRegistry`。
3. 校验 skill 是否存在。
4. 校验 `disable_model_invocation`，为 true 时拒绝模型调用。
5. 读取完整 `SKILL.md`。
6. 剥离 YAML frontmatter。
7. 注入 base directory 头部。
8. 替换 `$ARGUMENTS`。
9. 替换 `${JARVIS_SKILL_DIR}`。
10. 返回 loaded skill payload。

`Skill` tool 应同时接受：

```text
Skill({ "skill": "github-review" })
Skill({ "skill": "/github-review" })
```

内部统一归一为：

```text
github-review
```

返回示例：

```json
{
  "status": "loaded",
  "skill": {
    "skill_id": "github-review",
    "display_name": "GitHub PR Review Helper",
    "effective_description": "Review GitHub pull requests and summarize risks."
  },
  "expected_tools": ["read_file", "search_files"],
  "args": "https://github.com/org/repo/pull/123",
  "content": "# GitHub Review\n\nFollow this workflow..."
}
```

`expected_tools` 来自 `allowed_tools`，只作为 metadata。它不改变实际 tool exposure。

### 7.3 Prompt 内容处理

加载完整 `SKILL.md` 时，第一期只做以下处理：

1. 剥离 YAML frontmatter。
2. 在正文前加入 base directory 头部：

   ```text
   Base directory for this skill: <skill_dir>
   ```

3. 替换：

   ```text
   $ARGUMENTS -> args 完整字符串
   ${JARVIS_SKILL_DIR} -> skill 目录
   ```

4. 如果正文没有 `$ARGUMENTS` 但调用传了 `args`，在末尾追加：

   ```text
   ARGUMENTS: <args>
   ```

第一期不实现：

```text
$0
$ARGUMENTS[0]
$foo
${JARVIS_SESSION_ID}
inline shell
```

### 7.4 Runtime 注入语义

`Skill` tool 的结果应分成两层：

```text
tool_result:
  Launching skill: <skill_id>

hidden/meta user message or system-reminder:
  完整处理后的 skill guidance
```

`Skill` tool call 后，在同一个 ReAct loop 的下一次 LLM call 中，模型能看到完整 skill guidance。用户界面不必展示整段 `SKILL.md`。

完整 skill guidance 不永久写入 base system prompt。

同时，runtime 应记录 invoked skill：

```text
skill_id
content_path
processed_content
tool_call_id
turn_id
```

用途：

1. 对话 compaction 后仍能保留当前任务依赖的 skill 指南。
2. 审计某次回答受哪些 skill 影响。
3. 避免同一 turn 重复加载相同 skill。

### 7.5 安全边界

`Skill` tool 不做以下事情：

1. 不执行 `scripts/`。
2. 不直接调用 Codex、shell、web search、GitHub 等工具。
3. 不因为 `allowed_tools` 字段就改变实际 tool exposure。
4. 不把 skill 内容持久写入 base system prompt。
5. 不绕过 runtime policy。

`allowed_tools` 第一阶段只解析为 loaded skill metadata 中的 `expected_tools`，用于提示“skill expected tools”。真实工具权限仍由 Jarvis runtime policy 决定。

---

## 8. Slash 调用

用户可以直接调用：

```text
/github-review https://github.com/org/repo/pull/123
```

处理方式：

```text
检测 /skill-id
  -> 去掉前导 /
  -> 按 skill_id 查 SkillRegistry
  -> 校验 user_invocable
  -> 复用 Skill tool loading path
  -> 在同一个 ReAct loop 的下一次 LLM call 中注入完整 skill guidance
```

如果 `user_invocable: false`，则拒绝直接 slash 调用，但模型仍可在未禁用 `disable_model_invocation` 时主动调用。

slash 调用使用的标识必须是目录名 `skill_id`，不是 frontmatter `name`。

---

## 9. 与现有实现的差异

### 9.1 当前实现

```text
ContextManager.build_initial_messages()
  -> SkillRegistry.select_for_query()
  -> inject_selected_skills()
  -> _render_selected_skills()
  -> 完整 SKILL.md body 直接进 <system-reminder>
```

问题：

1. 本地词法匹配替模型做了主要选择。
2. 中文和复杂语义选择不稳定。
3. 模型在未主动选择 skill 前就看到完整 body。
4. skill 越多，prompt 注入越不可控。
5. `load_skill_guidance` 不是通用 skill 加载器。

### 9.2 改造后

```text
ContextManager
  -> render model-facing skill listing
  -> listing 进 <system-reminder>

ToolDefinition("Skill")
  -> LoadSkillTool / load_skill
  -> load full SKILL.md by skill_id
  -> same ReAct loop next LLM call sees full skill guidance
```

本地 registry 只负责发现、解析、查找和 listing 渲染，不负责替模型决定自然语言请求该用哪个 skill。

---

## 10. 阶段划分

### P0：Loader 规范化

1. `skill_id = directory name`。
2. `name = display_name`。
3. 增加 `effective_description`。
4. `effective_description` 来源优先级：
   - frontmatter `description`
   - Markdown 正文首段
   - Markdown 第一个标题
5. 基础 alias parsing：
   - `when-to-use` -> `when_to_use`
   - `allowed-tools` -> `allowed_tools`
   - `user-invocable` -> `user_invocable`
   - `disable-model-invocation` -> `disable_model_invocation`
6. 派生 `source_path`、`content_path`、`loaded_from`。

### P1：Listing + Skill tool

1. 每轮注入轻量 listing。
2. listing 只包含 `skill_id`、`effective_description`、`when_to_use`。
3. 新增模型可见 `Skill` tool。
4. 内部实现可命名为 `LoadSkillTool` / `load_skill`。
5. 按 `skill_id` 加载完整 `SKILL.md` body。
6. 剥离 frontmatter。
7. 注入 base directory。
8. 替换 `$ARGUMENTS` 和 `${JARVIS_SKILL_DIR}`。
9. 同一 ReAct loop 的下一次 model call 注入完整 guidance。
10. 记录 invoked skill，用于 compaction 和审计。

### P2：Slash invocation

1. `/skill-id` 复用 Skill tool loading path。
2. `user_invocable` 控制用户直接调用。

### P3：Advanced policy

后续再实现：

1. `allowed_tools` 与 runtime policy 集成。
2. `paths` activation。
3. `model` / `effort` / `context` / `fork` / `hooks`。
4. named arguments。
5. inline shell。
6. MCP/plugin skill。

### 兼容旧路径

1. 保留 `load_skill_guidance` 一段时间，作为内部兼容入口。
2. 默认不要向模型暴露 `load_skill_guidance`，避免和 `Skill` tool 混淆。
3. 后续逐步删除 `ContextManager.inject_selected_skills()` 自动注入完整 body 的路径。

---

## 11. 测试计划

### 11.1 Loader 测试

覆盖：

1. `skill_id` 来自目录名。
2. frontmatter `name` 只生成 `display_name`，不覆盖 `skill_id`。
3. `effective_description` 按 frontmatter description、Markdown 正文首段、Markdown 第一个标题提取。
4. `when-to-use`、`allowed-tools`、`user-invocable`、`disable-model-invocation` 可归一。
5. 无法提取 `effective_description` 的 skill 不进入默认 listing。

### 11.2 Listing 测试

覆盖：

1. listing 包含 `skill_id`、`effective_description`、`when_to_use`。
2. listing 不包含完整 body。
3. listing 不包含 `display_name/name`。
4. listing 不包含 `allowed_tools`、`arguments`、`paths`、`model`、`effort`、`hooks`。
5. 超预算时截断描述。
6. `disable_model_invocation` 的 skill 不进入模型可调用 listing。

### 11.3 Skill tool 测试

覆盖：

1. `Skill({ skill })` 成功加载 body。
2. unknown skill 返回明确错误。
3. 禁用模型调用的 skill 被拒绝。
4. body 剥离 frontmatter。
5. tool 不自动执行 scripts。
6. `/skill_id` 和 `skill_id` 输入都能归一。
7. `args` 能替换 `$ARGUMENTS`，无占位符时追加 `ARGUMENTS:`。
8. 正文包含 `${JARVIS_SKILL_DIR}` 时替换为 skill 目录。
9. 一期不替换 `$0`、`$ARGUMENTS[0]`、`$foo`。
10. `allowed_tools` 只进入 loaded skill metadata，不改变实际 tool exposure。

### 11.4 Runtime 测试

覆盖：

1. 初始模型上下文只有 listing。
2. 模型调用 `Skill` 后，同一个 ReAct loop 的下一次 LLM call 包含完整 skill guidance。
3. 完整 skill guidance 不永久写入 base system prompt。
4. 后续工具调用仍受 runtime policy 限制。
5. slash 调用和模型 tool 调用复用同一加载路径。
6. invoked skill 在 context compaction 后仍被保留。

---

## 12. 风险与取舍

### 12.1 风险：模型漏选 skill

从本地强制选择改成模型选择后，可能出现模型没有调用合适 skill 的情况。

缓解：

1. 写好 listing 里的 `effective_description` 和 `when_to_use`。
2. system prompt 明确要求匹配时调用 `Skill` tool。
3. 对 `/skill-id` 保留精确调用路径。
4. 评测集中覆盖常见 skill 触发场景。

### 12.2 风险：listing 太长

skill 数量增加会挤占 prompt。

缓解：

1. listing 压缩。
2. invocation flag 过滤。
3. 后续可加 `paths` activation 或本地轻量预筛，但预筛只影响 listing 候选，不直接加载 skill。

### 12.3 风险：allowed_tools 被误解为授权

skill 作者可能以为声明 `allowed_tools` 就能获得工具权限。

缓解：

1. 文档明确它只是声明/提示。
2. runtime policy 仍是唯一授权来源。
3. `Skill` tool 返回时只携带 `expected_tools` metadata，不修改 actual allowed tools。

---

## 13. 推荐最终形态

Jarvis skill 应向 Claude Code / Codex 风格靠齐：

```text
外部作者写一个 SKILL.md
  -> Jarvis 启动扫描
  -> 每轮把轻量 listing 放入 system-reminder
  -> 模型根据用户请求选择 Skill tool
  -> Skill tool 加载完整 SKILL.md
  -> 同一 ReAct loop 下一次 LLM call 注入完整 guidance
  -> 模型按指南继续完成任务
```

这比当前“本地选择 + 完整 body 自动注入”更稳定，也更适合外部 skill 后续统一接入。

第一期最重要的不是扩字段，而是改执行心智模型：

```text
from: Jarvis selects and injects skill body
to: Jarvis lists skills, model selects, Skill tool loads body
```

