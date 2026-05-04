# Claude Skill 的加载与使用

本文总结该项目中 Claude skill 的加载、表示、暴露给模型以及执行时展开上下文的完整链路。

## 1. Skill 在项目里的本质

在这个项目里，skill 并不是一种独立的运行时对象，而是会被加载并包装成统一的 `Command`。

典型结构可以理解为：

```ts
{
  type: 'prompt',
  name: 'commit',
  description: '...',
  allowedTools: [...],
  getPromptForCommand(args, context) { ... }
}
```

也就是说，skill 加载后会变成一个 `Command(type: 'prompt')`。项目里的斜杠命令、插件命令、bundled skill、MCP prompt/skill 等，也都复用了 `Command` 这个抽象。

区别在于：

- `local` / `local-jsx` command 通常由 CLI 自己直接执行或渲染 UI。
- `prompt` command 会展开成一段 prompt，注入给模型继续执行。
- skill 本质上就是一种特殊来源的 `prompt` command。

## 2. Skill 的加载来源

本地 skill 主要来自这些目录：

```text
.claude/skills/<skill-name>/SKILL.md
~/.claude/skills/<skill-name>/SKILL.md
<managed>/.claude/skills/<skill-name>/SKILL.md
```

加载逻辑在：

```text
src/skills/loadSkillsDir.ts
```

本地 `/skills/` 目录只支持目录格式：

```text
skills/
  my-skill/
    SKILL.md
```

单独放一个 `.md` 文件在 `/skills/` 下不会被识别为 skill。

项目还兼容旧的 `/commands/` 目录。旧目录既支持单个 `.md` 文件，也支持 `<name>/SKILL.md` 目录格式。

除了本地目录，skill 还可能来自：

- bundled skills：代码内置注册，入口在 `src/skills/bundled/index.ts`。
- plugin skills：插件目录中的 `skills/`，入口在 `src/utils/plugins/loadPluginCommands.ts`。
- MCP skills：通过 MCP server 提供，和普通 MCP prompt 区分。
- remote canonical skills：实验性远程加载路径。

## 3. SKILL.md 如何变成 Command

加载 `SKILL.md` 时，项目会解析 frontmatter 和 markdown 正文。

主要解析函数：

```text
parseSkillFrontmatterFields()
createSkillCommand()
```

它们位于：

```text
src/skills/loadSkillsDir.ts
```

常见 frontmatter 字段包括：

```yaml
---
name: My Skill
description: Do something useful
when_to_use: Use this when ...
allowed-tools: Bash, Read
argument-hint: <file>
arguments:
  - file
model: inherit
effort: medium
user-invocable: true
disable-model-invocation: false
context: fork
agent: general-purpose
paths:
  - src/**
---
```

解析后会生成一个 `Command`。最重要的是它的 `getPromptForCommand()` 方法。这个方法不是加载列表时立即执行，而是在 skill 真正被调用时才执行。

## 4. 启动时是否把 SKILL.md 全文注入模型

不会。

启动或刷新时，项目会把 skill 加载成 `Command`，并把可用 skill 的名称和描述做成 `skill_listing` 附件。

模型看到的大概是：

```text
The following skills are available for use with the Skill tool:

- commit: Create a git commit
- pdf: Work with PDF files
- review-pr: Review a pull request
```

这只是轻量索引，用于让模型知道有哪些 skill 可以调用。完整 `SKILL.md` 正文不会在这个阶段全部放进上下文。

相关逻辑：

```text
src/utils/attachments.ts
src/tools/SkillTool/prompt.ts
src/utils/messages.ts
```

## 5. SkillTool 是什么

`SkillTool` 是一个真正的 Claude Code tool，和 `Read`、`Edit`、`Bash` 这些常规工具一样，都会出现在模型可调用工具列表中。

它的位置是：

```text
src/tools/SkillTool/SkillTool.ts
```

它和常规工具的相同点：

- 有 tool name。
- 有 input schema。
- 有权限检查。
- 有执行函数 `call()`。
- 会返回 tool result。

它和 `Read` / `Bash` / `Edit` 的不同点：

- `SkillTool` 自己不直接读用户文件、不改文件、不运行任务。
- 它的核心作用是按名称找到某个 skill，并把该 skill 的完整 prompt 展开成新的上下文消息。

## 6. 模型如何调用 SkillTool

`SkillTool` 的入参很简单：

```json
{
  "skill": "commit",
  "args": "-m 'Fix bug'"
}
```

或者插件命名空间形式：

```json
{
  "skill": "ms-office-suite:pdf",
  "args": "report.docx"
}
```

字段含义：

- `skill`：skill 名称，可以带命名空间。
- `args`：传给 skill 的可选参数字符串。

模型会根据前面注入的 skill listing 和用户任务，自己选择是否调用某个 skill。

## 7. SkillTool 调用后发生什么

模型调用 `SkillTool` 后，主要流程是：

1. `validateInput()` 检查 skill 名称是否合法。
2. 在所有 `Command` 中查找对应 skill。
3. 检查该 skill 是否允许模型调用。
4. `checkPermissions()` 检查是否需要用户授权。
5. `call()` 调用 `processPromptSlashCommand()`。
6. `processPromptSlashCommand()` 调用 `command.getPromptForCommand(args, context)`。
7. `getPromptForCommand()` 展开完整 `SKILL.md` 内容。
8. 展开后的内容作为 meta user message 注入对话。
9. `SkillTool` 返回类似 `Launching skill: xxx` 的 tool result。
10. 下一轮模型看到完整 skill 指令，再继续执行真正任务。

关键代码链路：

```text
SkillTool.call()
  -> processPromptSlashCommand()
    -> getMessagesForPromptSlashCommand()
      -> command.getPromptForCommand()
```

## 8. Skill 展开时会做哪些处理

`getPromptForCommand()` 展开 skill 时，会做这些事情：

- 在正文前加上 skill 根目录：

```text
Base directory for this skill: /abs/path/to/.claude/skills/my-skill

<SKILL.md 正文>
```

- 替换参数，例如 `$ARGUMENTS` 或命名参数。
- 替换 `${CLAUDE_SKILL_DIR}` 为 skill 目录。
- 替换 `${CLAUDE_SESSION_ID}` 为当前 session id。
- 根据 `allowed-tools` 临时扩展工具权限。
- 处理 `model`、`effort` 等覆盖设置。
- 如果定义了 hooks，注册 skill hooks。
- 如果 markdown 中有 prompt shell execution 语法，执行对应 shell 命令，并把结果放进展开后的 prompt。

MCP skill 是例外：远程 MCP skill 不执行本地 inline shell command。

## 9. Skill 目录里的脚本文件如何使用

假设 skill 目录是：

```text
.claude/skills/report/
  SKILL.md
  scripts/analyze.py
```

调用 `SkillTool` 后，系统不会自动把 `scripts/analyze.py` 的全文注入上下文。

它只会把 skill 根目录告诉模型：

```text
Base directory for this skill: G:/project/.claude/skills/report
```

如果 `SKILL.md` 写了：

```md
Use the helper script at `${CLAUDE_SKILL_DIR}/scripts/analyze.py`.

Run it with:
python `${CLAUDE_SKILL_DIR}/scripts/analyze.py` <input-file>
```

展开后模型会看到类似：

```text
Use the helper script at `G:/project/.claude/skills/report/scripts/analyze.py`.

Run it with:
python `G:/project/.claude/skills/report/scripts/analyze.py` <input-file>
```

然后模型可以在下一步继续调用常规工具：

```json
{
  "command": "python G:/project/.claude/skills/report/scripts/analyze.py data.csv"
}
```

所以脚本的使用方式是：

1. `SkillTool` 注入 skill 指令和 skill 根目录。
2. 模型根据 `SKILL.md` 的说明决定是否使用脚本。
3. 真正执行脚本的是后续的 `BashTool`。
4. 如果模型需要理解脚本内容，也可以后续用 `ReadTool` 读取脚本。

脚本文件不会因为存在于 skill 目录中就自动执行，也不会自动全文进入上下文。

## 10. Inline shell execution 和脚本文件的区别

需要区分两种情况：

### 10.1 普通脚本文件

例如：

```text
scripts/analyze.py
```

这种文件只是 skill 的附属资源。`SkillTool` 只告诉模型 skill 根目录，后续是否读取或执行这个文件，由模型根据 `SKILL.md` 指令决定。

### 10.2 SKILL.md 内的 shell 注入

如果 `SKILL.md` 正文里使用了项目支持的 prompt shell execution 语法，那么在 `SkillTool` 展开 skill 的阶段就会执行该命令。

执行结果会被插入到展开后的 prompt 中，再注入上下文。

也就是说：

- 普通脚本文件：不会自动执行。
- inline shell execution：skill 展开阶段会执行，并把结果注入 prompt。

## 11. Fork 模式

如果 skill frontmatter 写了：

```yaml
context: fork
```

那么该 skill 不会直接在主对话中 inline 展开，而是通过 `executeForkedSkill()` 启动一个子 agent 执行。

流程大致是：

1. 展开 skill prompt。
2. 创建子 agent。
3. 把 skill prompt 作为子 agent 的输入。
4. 子 agent 自己运行工具和推理。
5. 将最终结果返回给主对话。

相关逻辑在：

```text
src/tools/SkillTool/SkillTool.ts
src/utils/forkedAgent.ts
src/tools/AgentTool/runAgent.ts
```

## 12. 动态发现和条件 skill

项目还支持动态发现 skill。

当模型使用文件工具读取、写入、编辑某个文件时，系统会从该文件所在目录向上查找嵌套的：

```text
.claude/skills
```

如果发现新的 skill 目录，会加载进当前 session。

相关位置：

```text
src/tools/FileReadTool/FileReadTool.ts
src/tools/FileWriteTool/FileWriteTool.ts
src/tools/FileEditTool/FileEditTool.ts
src/skills/loadSkillsDir.ts
```

`paths` frontmatter 支持条件 skill。带 `paths` 的 skill 启动时会先存起来，不立即暴露；当模型操作的文件路径匹配这些 patterns 时，才激活该 skill。

## 13. Compaction 后如何保留已调用 skill

当 skill 被调用后，项目会记录该 skill 的展开内容：

```text
addInvokedSkill()
```

这样在上下文压缩后，可以通过 `invoked_skills` attachment 把已经用过的 skill 指令重新补回去，避免模型在压缩后忘掉当前任务依赖的 skill 规则。

相关位置：

```text
src/bootstrap/state.ts
src/services/compact/compact.ts
src/utils/messages.ts
```

## 14. 总结

Claude skill 的加载与使用可以概括为：

1. 扫描 `SKILL.md`。
2. 解析 frontmatter 和正文。
3. 包装成 `Command(type: 'prompt')`。
4. 在上下文中只注入 skill 名称和描述列表。
5. 模型根据任务选择某个 skill。
6. 模型调用 `SkillTool({ skill, args })`。
7. `SkillTool` 找到对应 `Command`。
8. 调用 `getPromptForCommand()` 展开完整 skill 内容。
9. 把展开后的 skill prompt 注入下一轮上下文。
10. 模型根据完整 skill 指令继续调用常规工具完成任务。

因此，`SkillTool` 的核心作用不是执行业务逻辑，而是把按需选择的 skill 指令加载进对话上下文。真正的读文件、运行脚本、编辑代码等动作，仍然由后续的常规工具完成。
