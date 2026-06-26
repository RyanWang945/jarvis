# SKILL.md 字段完整说明

## 概述

每个 Skill 由一个目录 + `SKILL.md` 文件组成，路径如 `.claude/skills/<name>/SKILL.md`。

`SKILL.md` 是一个 Markdown 文件，文件开头可以有一段 YAML frontmatter（用 `---` 包裹），然后是 Markdown 正文（即 skill 的 prompt 内容）。

```markdown
---
name: 显示名称
description: 简短描述
when_to_use: 何时使用的详细说明
version: "1.0"
allowed-tools: Bash, Read, Write
---
# Skill 标题

这里是 skill 的具体 prompt 内容...
```

---

## 一、SKILL.md Frontmatter 字段完整列表

### 1. `name`

| 属性 | 说明 |
|------|------|
| **类型** | string |
| **必填** | 否 |
| **默认值** | 目录名 |

**用途**：Skill 的 UI 展示名称，仅影响 IDE 列表/typeahead 中的显示，不影响 Skill 的实际标识名。

**与目录名的关系**：Skill 的实际唯一标识（`skillName`）取自 **目录名** 而非此字段。例如目录 `my-skill/` 中的 SKILL.md，即使 `name: "A Different Name"`，调用时仍用 `my-skill`。

**示例**：
```yaml
name: "Git Commit Helper"
```

---

### 2. `description`

| 属性 | 说明 |
|------|------|
| **类型** | string |
| **必填** | 推荐 |
| **默认值** | 自动从 Markdown 正文首行提取 |

**用途**：Skill 的简短描述。如果未提供，系统会自动从 SKILL.md 正文的第一行提取（去掉 `#` 标题标记）。

**如何进入 system prompt**：出现在 skill 列表中，格式为 `{name}: {description}`。例如模型会看到：

```
- commit: 自动生成 git commit 信息
```

**截断规则**：单条描述最长 250 字符，超出部分用 `…` 截断。

**示例**：
```yaml
description: "分析并修复代码中的 bug，自动定位问题并提供修复方案"
```

---

### 3. `when_to_use`

| 属性 | 说明 |
|------|------|
| **类型** | string |
| **必填** | 否 |
| **默认值** | 无 |

**用途**：告诉模型**在什么场景下应该调用此 Skill** 的详细说明。

**如何进入 system prompt**：与 `description` 拼接，格式为 `{name}: {description} - {when_to_use}`。模型会看到：

```
- debug: 分析并修复代码bug - 当用户报告程序错误、崩溃或异常行为时使用
```

**重要**：此字段还是 skill 能否出现在模型列表中的**过滤条件**之一。如果 skill 没有 `when_to_use` 且 `description` 不是用户显式指定的，则可能被过滤掉（除非是 bundled skill 或来自 `/skills/` 目录）。

**示例**：
```yaml
when_to_use: "当用户报告程序错误、崩溃或异常行为时使用本技能进行排查和修复"
```

---

### 4. `allowed-tools`

| 属性 | 说明 |
|------|------|
| **类型** | string 或 string[] |
| **必填** | 否 |
| **默认值** | 无（不限制） |

**用途**：限制 Skill 执行时可以使用的工具白名单。可写为逗号分隔字符串或 YAML 数组。

**影响范围**：
- 限制模型的工具调用（如只允许 `Bash, Read, Write`）
- 限制 SKILL.md 中内联 shell 命令（`!command` 和 ` ```! ` 块）的权限上下文

**示例**：
```yaml
allowed-tools: "Bash, Read, Write, Grep, Glob"
# 或
allowed-tools:
  - Bash
  - Read
  - Write
```

---

### 5. `arguments` / `argument-hint`

#### `arguments`

| 属性 | 说明 |
|------|------|
| **类型** | string 或 string[] |
| **必填** | 否 |

**用途**：定义命名参数列表，在 prompt 中可通过 `$参数名` 引用。

**如何进入 system prompt**：**不在 skill 列表中显示**。仅在调用时用于参数替换。

**示例**：
```yaml
arguments: "branch_name base_branch"
# 或
arguments:
  - branch_name
  - base_branch
```

在 Markdown 正文中可通过 `$branch_name`、`$base_branch` 引用参数值。

#### `argument-hint`

| 属性 | 说明 |
|------|------|
| **类型** | string |
| **必填** | 否 |

**用途**：在 UI typeahead/IDE 命令面板中显示参数提示文本（灰色提示）。**不进入 system prompt**，模型不可见。

**示例**：
```yaml
argument-hint: "<branch> <base>"
```

---

### 6. `model`

| 属性 | 说明 |
|------|------|
| **类型** | string |
| **必填** | 否 |
| **可选值** | `"opus"`, `"sonnet"`, `"haiku"`, `"inherit"` 或具体模型名 |
| **默认值** | 使用当前对话模型 |

**用途**：覆盖 Skill 执行时使用的模型。`"inherit"` 表示使用父级模型（与不设置效果相同）。

**示例**：
```yaml
model: "haiku"  # 使用轻量模型执行
```

---

### 7. `disable-model-invocation`

| 属性 | 说明 |
|------|------|
| **类型** | boolean |
| **必填** | 否 |
| **默认值** | false |

**用途**：禁止模型自主调用此 Skill。设置为 `true` 时：
- Skill **不会出现在**模型可见的 skill 列表中
- 模型无法通过 SkillTool 调用它
- 用户仍可通过 `/name` 直接触发（如果 `user-invocable` 也为 true）

**典型场景**：像 `/debug`、`/batch` 这类需要用户主动触发的命令行工具。

**示例**：
```yaml
disable-model-invocation: true
```

---

### 8. `user-invocable`

| 属性 | 说明 |
|------|------|
| **类型** | boolean |
| **必填** | 否 |
| **默认值** | `true` |

**用途**：控制用户能否通过输入 `/skill-name` 来调用此 Skill。设为 `false` 时：
- 用户在 typeahead/IDE 命令面板中看不到此 skill
- 输入 `/name` 会被拒绝，提示"请让 Claude 帮你调用"
- **不影响模型** — 模型仍可通过 SkillTool 调用它（除非 `disable-model-invocation` 也为 true）

**典型场景**：内部辅助 skill，只应由模型自动决策使用。

**示例**：
```yaml
user-invocable: false
```

---

### 9. `effort`

| 属性 | 说明 |
|------|------|
| **类型** | string 或 number |
| **必填** | 否 |
| **可选值** | `"low"`, `"medium"`, `"high"`, `"max"` 或正整数 |
| **默认值** | 使用当前对话的 effort 设置 |

**用途**：控制 Skill 执行时的思考努力程度。值越大模型思考越深入。

**示例**：
```yaml
effort: "high"
```

---

### 10. `context`

| 属性 | 说明 |
|------|------|
| **类型** | `"fork"` |
| **必填** | 否 |
| **可选值** | 不设置（inline）或 `"fork"` |
| **默认值** | `"inline"` |

**用途**：控制 Skill 的执行模式。

- **`inline`（默认）**：Skill 的 prompt 内容直接展开到当前对话中，共享上下文和 token 预算。
- **`"fork"`**：Skill 在独立子 agent 中运行，拥有独立的上下文和 token 预算，执行完毕后将结果返回。

**示例**：
```yaml
context: fork
agent: general-purpose   # 配合 context: fork 使用
```

---

### 11. `agent`

| 属性 | 说明 |
|------|------|
| **类型** | string |
| **必填** | 否 |
| **必须配合** | `context: fork` |

**用途**：指定 fork 模式下使用的子 agent 类型。仅当 `context: fork` 时生效。

**示例**：
```yaml
context: fork
agent: "Bash"  # 使用 Bash 子 agent 执行
```

---

### 12. `hooks`

| 属性 | 说明 |
|------|------|
| **类型** | HooksSettings（嵌套对象） |
| **必填** | 否 |

**用途**：在 Skill 被调用时注册 hooks（如 PreToolUse、PostToolUse、Stop 等）。经过 `HooksSchema()` 校验。

**示例**：
```yaml
hooks:
  PostToolUse:
    - matcher: "Bash"
      hooks:
        - command: "echo 'Bash tool was used'"
```

---

### 13. `paths`

| 属性 | 说明 |
|------|------|
| **类型** | string 或 string[] |
| **必填** | 否 |

**用途**：条件激活。设置 gitignore 风格的 glob 模式后，Skill **不会在启动时加载**，只有当模型触碰（读取/写入/编辑）匹配这些 glob 模式的文件时，Skill 才会被激活并对模型可见。

支持大括号展开（如 `src/*.{ts,tsx}` → `src/*.ts` 和 `src/*.tsx`）。

**示例**：
```yaml
paths: "*.sql, migrations/*.sql"
# 或
paths:
  - "*.sql"
  - "migrations/*.sql"
```

---

### 14. `shell`

| 属性 | 说明 |
|------|------|
| **类型** | `"bash"` 或 `"powershell"` |
| **必填** | 否 |
| **默认值** | `"bash"` |

**用途**：指定 SKILL.md 正文中 `!command` 和 ` ```! ` 内联命令块使用的 shell。不影响全局设置。

**示例**：
```yaml
shell: "powershell"
```

---

### 15. 非标准字段

`FrontmatterData` 类型有 `[key: string]: unknown` 索引签名，允许任意自定义字段通过 YAML 解析但不被正式处理。例如：

```yaml
license: "MIT"
author: "your-name"
version: "2.0"
```

这些字段不会被映射到任何运行时字段，不会进入 system prompt，但可以在文件中自由使用（比如用于文档或第三方工具解析）。

---

## 二、字段与 System Prompt 的映射关系

### 直接进入 System Prompt 的字段

| SKILL.md 字段 | 在 System Prompt 中的位置 | 展示形式 |
|---|---|---|
| `description` | skill 列表 | `{name}: {description}` |
| `when_to_use` | skill 列表（拼接在 description 后） | `{name}: {description} - {when_to_use}` |
| 目录名 | skill 列表（作为 skill 的标识名） | `- {目录名}: ...` |

### 影响 System Prompt 但不直接显示文本的字段

| SKILL.md 字段 | 作用机制 |
|---|---|
| `disable-model-invocation` | 为 `true` 时整个 skill 从列表中消失 |
| `user-invocable` | 不影响列表显示，但影响 `/name` 快捷键行为 |
| `paths` | 条件过滤：匹配文件的 skill 才出现在列表中 |
| `name` | 覆盖 UI 显示名，不覆盖 skill 标识名 |

### 调用时才生效的字段

| SKILL.md 字段 | 生效时机 |
|---|---|
| `allowed-tools` | Skill 被调用时限制可用工具 |
| `model` | Skill 被调用时切换模型 |
| `effort` | Skill 被调用时设置思考程度 |
| `context` + `agent` | Skill 被调用时决定执行模式 |
| `hooks` | Skill 被调用时注册 hooks |
| `shell` | Skill 被调用时决定内联命令的 shell |
| `arguments` | Skill 被调用时解析参数替换 |
| `argument-hint` | 仅在 UI 中显示，不进 prompt |

---

## 三、Skill 列表在 System Prompt 中的完整处理流程

```
1. 加载 (loadSkillsDir.ts)
   读取 SKILL.md → 解析 YAML frontmatter → 创建 Command 对象

2. 过滤 (commands.ts::getSkillToolCommands)
   排除: disableModelInvocation=true、非 prompt 类型、无 description 且无 whenToUse

3. 格式化 (prompt.ts::formatCommandsWithinBudget)
   每个 skill 格式化为: "- {目录名}: {description} [- {when_to_use}]"
   预算: 上下文窗口的 1%（约 8000 字符）
   优先级: bundled skills 保持完整描述，其他可被截断

4. 注入 (attachments.ts → messages.ts)
   包装为: "The following skills are available for use with the Skill tool:\n\n{列表}"
   标记为 isMeta=true，以 <system-reminder> 形式插入每轮对话
```

模型每轮对话开始时都会收到这个 skill 列表作为 system-reminder 附件。

---

## 四、完整示例

```yaml
---
name: "Python 测试运行器"
description: "运行 Python 单元测试并分析覆盖率"
when_to_use: "当用户要求运行 Python 测试、检查测试覆盖率或分析测试结果时使用"
version: "1.0"
allowed-tools: Bash, Read
arguments: "test_path pytest_args"
argument-hint: "<test_path> [pytest flags]"
model: "sonnet"
effort: "high"
context: fork
agent: "general-purpose"
---

# Python Test Runner

## 概述
对项目中的 Python 代码运行 pytest，分析结果。

## 操作步骤
1. 先在 `$test_path` 中查找测试文件
2. 运行 `pytest $test_path $pytest_args --cov --cov-report=term`
3. 分析输出，总结失败的测试和覆盖率概况
```

这个 skill 加载后，模型在 system prompt 中会看到：

```
- python-test-runner: 运行 Python 单元测试并分析覆盖率 - 当用户要求运行 Python 测试、检查测试覆盖率或分析测试结果时使用
```

调用时，如果用户输入 `python-test-runner tests/ -v`，则 `$test_path` 被替换为 `tests/`，`$pytest_args` 被替换为 `-v`。
