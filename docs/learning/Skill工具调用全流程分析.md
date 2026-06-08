# Skill 工具调用全流程分析

## 概述

当模型决定使用某个 Skill 时，它通过一个名为 **`Skill`** 的 Tool Definition 进行工具调用。调用参数为 `{ skill: "skill名称", args: "可选参数" }`。

整个流程分为四大阶段：**校验 → 权限检查 → 处理 → 返回**。根据 Skill 的类型，处理阶段分为三条路径。

---

## 整体架构

```
模型发出 Skill 工具调用
    │
    ▼
 validateInput()        ─── 校验 skill 名称、是否存在、是否禁用
    │
    ▼
 checkPermissions()     ─── deny 规则 > allow 规则 > safe-property 自动放行 > 询问用户
    │
    ▼
 call()                 ─── 三条执行路径
    │
    ├── executeRemoteSkill()    （远程 canonical skill，实验性）
    ├── executeForkedSkill()    （context: fork 的 skill）
    └── processPromptSlashCommand() → getMessagesForPromptSlashCommand()
                                 （inline skill，默认路径）
```

---

## 阶段一：validateInput() —— 输入校验

位置：`src/tools/SkillTool/SkillTool.ts:354`

执行步骤：

1. **去除前导 `/`**：模型可能输入 `/commit` 或 `commit`，统一处理
2. **远程 canonical skill 检查**（ant 实验性）：`_canonical_<slug>` 格式特殊处理
3. **查命令注册表**：调用 `getAllCommands(context)` 获取全部命令（本地 + MCP skills）
4. **存在性检查**：`findCommand(name, commands)` 查找，不存在则返回 `errorCode: 2`
5. **`disableModelInvocation` 检查**：如为 true，返回 `errorCode: 4`，阻止模型调用
6. **类型检查**：必须是 `type === 'prompt'`，否则返回 `errorCode: 5`

**注意**：`validateInput` 阶段会**再次**检查 `disableModelInvocation`，与 `getSkillToolCommands` 的过滤形成双重防线。

---

## 阶段二：checkPermissions() —— 权限检查

位置：`src/tools/SkillTool/SkillTool.ts:432`

权限判定顺序（**先 deny 后 allow**）：

```
deny 规则匹配?  ──yes──▶ 拒绝
    │ no
    ▼
远程 canonical skill?  ──yes──▶ 自动放行
    │ no
    ▼
allow 规则匹配?  ──yes──▶ 放行
    │ no
    ▼
safe-property 检查?  ──yes──▶ 自动放行
    │ no
    ▼
询问用户
```

### 自动放行条件（safe-property 检查）

一个 Skill 只有它的**所有非空属性**都在 `SAFE_SKILL_PROPERTIES` 白名单中时才能自动放行。白名单包括：

```
type, progressMessage, contentLength, argNames, model, effort,
source, pluginInfo, disableNonInteractive, skillRoot, context, agent,
getPromptForCommand, frontmatterKeys, name, description,
hasUserSpecifiedDescription, isEnabled, isHidden, aliases, isMcp,
argumentHint, whenToUse, paths, version, disableModelInvocation,
userInvocable, loadedFrom, immediate, userFacingName
```

**不在白名单中的属性**：`hooks`、`allowedTools`、`disableNonInteractive`（启用时）。如果 skill 设置了任一这些"非安全"属性，则需要用户确认。

即——**设置了 `allowedTools` 限制的 skill 不会自动放行，需要用户点击确认**（见 `SkillTool.ts:910-933`）。

### 权限建议

如果走到"询问用户"阶段，系统会提供两条建议规则：
- 精确匹配：`Skill(skillName)` → 允许指定 skill
- 前缀匹配：`Skill(skillName:*)` → 允许该前缀的所有 skill（适用参数变化场景）

---

## 阶段三：call() —— 执行

位置：`src/tools/SkillTool/SkillTool.ts:580`

### 路径 1：远程 Canonical Skill（实验性）

条件：`EXPERIMENTAL_SKILL_SEARCH` feature 开启 + ant 用户 + skill 名以 `_canonical_` 开头

流程：
1. `loadRemoteSkill(slug, url)` → 从 GCS/AKI 加载 SKILL.md（支持本地缓存）
2. 剥离 YAML frontmatter
3. 注入 `Base directory for this skill: <dir>` 头部
4. 替换 `${CLAUDE_SKILL_DIR}` 和 `${CLAUDE_SESSION_ID}`
5. 注册到 `addInvokedSkill()` 以支持 compaction 保留
6. 直接将内容包装为 `isMeta: true` 的 UserMessage 返回

与本地 skill 不同：**不执行** inline shell 命令（`!command`）、不执行 `$ARGUMENTS` 参数替换——因为内容是声明式 markdown 而非可执行脚本。

---

### 路径 2：Forked Skill（`context: fork`）

条件：`command.context === 'fork'`

这是最复杂的路径，在**独立子 agent** 中运行 skill。

```typescript
async function executeForkedSkill(command, commandName, args, context, ...) {
  // 1. 准备分叉上下文
  const { modifiedGetAppState, baseAgent, promptMessages, skillContent } =
    await prepareForkedCommandContext(command, args || '', context)

  // 2. 合并 skill 的 effort 设置
  const agentDefinition = command.effort !== undefined
    ? { ...baseAgent, effort: command.effort }
    : baseAgent

  // 3. 运行子 agent
  for await (const message of runAgent({
    agentDefinition,
    promptMessages,
    toolUseContext: { ...context, getAppState: modifiedGetAppState },
    canUseTool,
    isAsync: false,
    querySource: 'agent:custom',
    model: command.model,
    availableTools: context.options.tools,
    override: { agentId },
  })) {
    agentMessages.push(message)
    // 报告进度（含 tool_use / tool_result）
  }

  // 4. 提取结果文本
  const resultText = extractResultText(agentMessages, 'Skill execution completed')

  // 5. 清理
  agentMessages.length = 0  // 释放内存
  clearInvokedSkillsForAgent(agentId)

  // 6. 返回
  return {
    data: { success: true, commandName, status: 'forked', agentId, result: resultText }
  }
}
```

**关键特征**：
- 子 agent 有自己的 token 预算，独立于主对话
- `runAgent()` 返回的中间消息（tool_use/tool_result）通过 `onProgress` 报告给 UI，但不进入主对话上下文
- 最终只将 **结果文本** (`resultText`) 返回给模型
- agent 执行完毕后清理 invokedSkills 状态

**fork 模式的 mapToolResultToToolResultBlockParam**：

```typescript
// Fork 结果 → 模型看到：
"Skill \"commit\" completed (forked execution).\n\nResult:\n<结果文本>"
```

---

### 路径 3：Inline Skill（默认路径）

这是最常见的执行路径，占大多数 skill 的调用。

```typescript
// SkillTool.ts call() 中 line 635-638:
const { processPromptSlashCommand } = await import('.../processSlashCommand.js')
const processedCommand = await processPromptSlashCommand(
  commandName, args || '', commands, context
)
```

`processPromptSlashCommand` 调用 `getMessagesForPromptSlashCommand` — 这是真正的核心。

---

## 核心：getMessagesForPromptSlashCommand

位置：`src/utils/processUserInput/processSlashCommand.tsx:827`

这是将 SKILL.md 内容转化为注入对话的消息序列的完整流程。

### Step 1: Coordinator 模式短路

```typescript
if (COORDINATOR_MODE && !context.agentId) {
  // 返回简短摘要，告诉协调器如何委派给 worker
  return {
    messages: [
      createUserMessage({ content: 元数据 }),              // 可见消息
      createUserMessage({ content: 摘要, isMeta: true }),   // 模型可见但 UI 隐藏
    ],
    shouldQuery: true,
    model: command.model,
    effort: command.effort,
  }
}
```

### Step 2: getPromptForCommand —— 生成 Skill Prompt 内容

```typescript
const result = await command.getPromptForCommand(args, context)
```

这就是 `loadSkillsDir.ts:344` 中定义的 `getPromptForCommand` 方法，执行以下处理：

```
原始 SKILL.md markdown
    │
    ▼
1. 添加 Base directory 头部: "Base directory for this skill: <path>\n\n"
    │
    ▼
2. 参数替换 (substituteArguments)
   $ARGUMENTS → 完整参数字符串
   $ARGUMENTS[0], $ARGUMENTS[1] → 按索引取值
   $0, $1 → 同上（简写）
   $foo, $bar → 命名参数（需匹配 frontmatter arguments 列表）
   如果无占位符但有参数 → 追加 "\n\nARGUMENTS: <args>"
    │
    ▼
3. 路径变量替换
   ${CLAUDE_SKILL_DIR} → skill 目录路径（正斜杠规范化）
   ${CLAUDE_SESSION_ID} → 当前会话 ID
    │
    ▼
4. 内联 Shell 命令执行 (MCP skill 跳过)
   !`command` → 执行 bash 并替换为结果
   ```! command ``` → 执行并替换为结果
   shell 选择：默认 Bash，frontmatter shell: powershell 则用 PowerShell
   权限受 allowedTools 白名单约束
    │
    ▼
5. 返回 [{ type: 'text', text: finalContent }]
```

### Step 3: 注册 Hooks

```typescript
if (command.hooks && hooksAllowedForThisSkill) {
  registerSkillHooks(context.setAppState, sessionId, command.hooks, command.name, command.skillRoot)
}
```

- `skillRoot` 被设为 `CLAUDE_PLUGIN_ROOT` 环境变量，方便 hook 脚本引用 skill 内的脚本
- 仅在 hook 未被策略锁定时注册

### Step 4: 记录 Invoked Skill（Compaction 保留）

```typescript
const skillContent = result.filter(b => b.type === 'text').map(b => b.text).join('\n\n')
addInvokedSkill(command.name, skillPath, skillContent, agentId)
```

这是 compaction 恢复机制的关键：将 skill 名称、文件路径、**已处理后的内容**（含参数替换、inline 命令结果）记录到会话状态中，确保对话被压缩后 skill 的上下文不会丢失。

### Step 5: 构建消息序列

```typescript
const metadata = formatCommandLoadingMetadata(command, args)  // "Launching skill: commit"
const additionalAllowedTools = parseToolListFromCLI(command.allowedTools ?? [])

const messages = [
  // 消息 1: 元数据（UI 可见，如 "/command: commit  Command: commit"）
  createUserMessage({ content: metadata, uuid }),

  // 消息 2: 主内容（isMeta: true，模型可见但 UI 隐藏）
  // 即 SKILL.md 处理后的完整 prompt 内容
  createUserMessage({ content: mainMessageContent, isMeta: true }),

  // 消息 3: 附件消息（参数中的 @mentions、MCP resources 等）
  ...attachmentMessages,

  // 消息 4: 权限声明
  createAttachmentMessage({
    type: 'command_permissions',
    allowedTools: additionalAllowedTools,
    model: command.model
  }),
]
```

---

## 回到 call() —— 返回结果的处理

```typescript
// SkillTool.ts call() 中 line 729-840

// 获取 toolUseID，用于标记消息归属
const toolUseID = getToolUseIDFromParentMessage(parentMessage, 'Skill')

// 标记消息：这些 newMessages 属于此工具调用
const newMessages = tagMessagesWithToolUseID(
  processedCommand.messages.filter(/* 过滤掉 progress 和 command-message */),
  toolUseID,
)

// 返回结果
return {
  data: {
    success: true,
    commandName,
    allowedTools: allowedTools.length > 0 ? allowedTools : undefined,
    model,
  },
  newMessages,  // ← 这些消息会注入到对话流中
  contextModifier(ctx) {
    // 1. 更新 allowedTools 白名单
    // 2. 切换 model（如 model: opus → 保留 [1m] 后缀）
    // 3. 覆盖 effort 值
    return modifiedContext
  },
}
```

**`contextModifier` 做了什么**：

| Skill 字段 | contextModifier 行为 |
|---|---|
| `allowedTools` | 合并到 `alwaysAllowRules.command[]`，此后对话中模型可使用这些工具 |
| `model` | 通过 `resolveSkillModelOverride()` 更新 `mainLoopModel`，保留 `[1m]` 等后缀 |
| `effort` | 设置 `getAppState().effortValue` |

---

## Inline Skill 的结果返回给模型

```typescript
// SkillTool.ts:857-861
mapToolResultToToolResultBlockParam(result, toolUseID) {
  // Inline skill 的 tool_result
  return {
    type: 'tool_result',
    tool_use_id: toolUseID,
    content: `Launching skill: ${result.commandName}`,
  }
}
```

模型实际看到的对话流：

```
[Assistant]: <tool_use name="Skill" id="xyz">{"skill": "commit", "args": "-m 'fix bug'"}</tool_use>

[User (isMeta=true)]: Base directory for this skill: /home/user/.claude/skills/commit

# Git Commit Skill
## Steps
1. Run git status...
2. Run git diff --staged...
...

[User (metadata)]: Launching skill: commit

[Assistant]: <tool_result tool_use_id="xyz">Launching skill: commit</tool_result>
```

然后模型继续处理 skill 的 prompt 内容。

---

## 三条路径对比总结

| | Inline（默认） | Fork（`context: fork`） | Remote（实验性） |
|---|---|---|---|
| **执行环境** | 当前对话 | 独立子 agent | 当前对话 |
| **Token 预算** | 共享 | 独立 | 共享 |
| **Prompt 注入** | `isMeta: true` UserMessage | `runAgent()` 内部处理 | `isMeta: true` UserMessage |
| **参数替换** | 支持 `$ARGUMENTS`/`$foo` | 支持 | 不支持 |
| **Shell 命令执行** | 支持 `!command` | 支持 | 不支持 |
| **中间工具调用** | 模型直接看到（同一对话） | 通过 `onProgress` 报告，不进主对话 | 同 inline |
| **结果格式** | `Launching skill: <name>` | `Skill completed. Result:\n<text>` | `Launching skill: <name>` |
| **contextModifier** | 更新 allowedTools/model/effort | 无（子 agent 内部处理） | 无 |
| **Compaction 保留** | `addInvokedSkill()` | `addInvokedSkill()` + `clearInvokedSkillsForAgent()` | `addInvokedSkill()` |

---

## 完整时序图

```
模型调用 Skill(skill="commit", args="-m msg")
│
├─ validateInput()
│   ├─ 去掉前导 /
│   ├─ 检查 _canonical_ 前缀（实验性）
│   ├─ 查 getAllCommands() 注册表
│   ├─ findCommand("commit") 确认存在
│   ├─ 确认 !disableModelInvocation
│   └─ 确认 type === 'prompt'
│
├─ checkPermissions()
│   ├─ deny 规则? → 否
│   ├─ allow 规则? → 否
│   ├─ safe-property? → 有 hooks/allowedTools → 否
│   └─ 询问用户 → 用户同意
│
└─ call()
    │
    ├─ context === 'fork'? → 否
    │
    └─ processPromptSlashCommand("commit", "-m msg", ...)
        │
        └─ getMessagesForPromptSlashCommand()
            │
            ├─ Coordinator 模式? → 否
            │
            ├─ command.getPromptForCommand("-m msg")
            │   ├─ 添加 "Base directory for this skill: ..."
            │   ├─ $ARGUMENTS → "-m msg"
            │   ├─ ${CLAUDE_SKILL_DIR} → "/home/.../skills/commit"
            │   ├─ ${CLAUDE_SESSION_ID} → "abc-123"
            │   ├─ !`git --version` → 执行并替换输出
            │   └─ 返回 [{ type: 'text', text: "处理后的完整prompt" }]
            │
            ├─ registerSkillHooks() (如果有 hooks 设置)
            │
            ├─ addInvokedSkill("commit", path, content, agentId)
            │   用于 compaction 保留
            │
            └─ 构建消息数组
                ├─ [0] metadata: formatCommandLoadingMetadata()
                ├─ [1] mainContent: isMeta=true, SKILL.md 处理后的内容
                ├─ [2] attachments: @mentions 等
                └─ [3] command_permissions: allowedTools, model

返回给 SkillTool.call():
│
├─ tagMessagesWithToolUseID(newMessages, toolUseID)
│   标记消息属于此次工具调用
│
└─ return { data, newMessages, contextModifier }
     │
     ├─ data: { success: true, commandName: "commit", allowedTools: [...], model }
     ├─ newMessages: 注入到对话流（模型可见）
     └─ contextModifier: 更新 allowedTools/model/effort 到后续对话
```
