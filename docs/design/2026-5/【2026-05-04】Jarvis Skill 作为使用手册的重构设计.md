# Jarvis Skill 作为使用手册的重构设计

| 项目 | 内容 |
|------|------|
| 作者 | Codex |
| 日期 | 2026-05-04 |
| 版本 | v1.0 |
| 状态 | 已决策 |

---

## 1. 背景

Jarvis 当前已经有两类容易混淆的扩展形态：

- `tool`：真正执行外部动作，例如搜索、读写知识库、委托 Codex、创建定时任务。
- `skill`：从 `SKILL.md` 加载的提示词片段，用来影响模型如何完成某类任务。

此前的技能设计文档借鉴了 Claude Code / OpenClaw 的 Agent Skills 体系，讨论了 `scripts/`、`references/`、`allowed-tools`、fork context、inline shell 等扩展能力。但经过当前项目定位重新评估后，Jarvis 不应该把 skill 发展成“隐藏的执行插件”。

Jarvis 的 runtime、tool registry、权限检查、审计、测试和 API key 管理都在自己掌控内。因此，如果某个能力需要真正调用外部 API、执行脚本、访问文件或产生副作用，正确做法是把它注册为正式 tool，而不是把代码藏在 skill 目录里让模型通过 shell 执行。

本设计文档明确新的边界：**skill 是使用手册，tool 是执行工具。**

---

## 2. 核心决策

### 2.1 Skill 的定位

Skill 只承载轻量的过程性指导，包括：

- 什么时候使用某个 tool。
- 如何选择 tool 参数。
- 如何组合多个 tool。
- 输出格式、检查清单、质量标准。
- 领域术语、注意事项、失败处理建议。

Skill 不应该承载：

- 业务执行代码。
- API 调用脚本。
- 文件修改脚本。
- 自动安装依赖脚本。
- 绕过 tool registry 的 shell 命令。
- 隐式权限扩大逻辑。

一句话：**skill 影响模型的行为偏好和流程，不新增系统能力。**

### 2.2 Tool 的定位

Tool 是 Jarvis 唯一的执行能力入口。凡是会真正执行动作的能力，都应该注册为 tool：

- 联网查询。
- 外部 API 调用。
- 本地命令执行。
- 文件读写。
- 代码修改。
- Git 操作。
- 飞书发送。
- 定时任务。
- 知识库写入。

Tool 必须具备：

- 结构化参数 schema。
- 明确 handler。
- 风险等级。
- runtime policy 暴露规则。
- 安全检查和授权边界。
- 审计记录。
- 单元测试或集成测试。
- 可控的错误处理和超时。

### 2.3 Resource 的定位

Skill 目录可以保留资源文件，但资源只能作为说明材料：

- `references/`：详细 API 文档、领域规则、示例。
- `templates/`：输出模板。
- `examples/`：样例输入输出。

不建议在 skill 中保留 `scripts/` 作为执行入口。若确实存在历史脚本，应迁移为正式 tool handler，或作为开发参考资料保留但不暴露给模型执行。

---

## 3. 为什么不照搬开源 Skill 执行模式

Claude Code / OpenClaw 的 skill 体系允许 `SKILL.md` 携带脚本、动态上下文注入、工具预授权等能力，主要原因是：

1. 用户需要低门槛扩展能力，不能每次都改核心代码注册 tool。
2. skill 需要跨项目、跨用户分发，目录协议比代码集成更轻。
3. 许多外部能力没有稳定的 typed tool 接口，只能通过 shell、脚本或 MCP 间接使用。
4. 开源生态希望 marketplace 化，允许第三方以 skill 包形式发布能力。

这些取舍对通用客户端合理，但 Jarvis 的目标不同：

- Jarvis 是个人长期运行 agent，不追求任意第三方 skill marketplace。
- Jarvis 更看重安全、审计、稳定性和可回归测试。
- Jarvis 可以直接修改 `app/tools`，把外部能力注册成 typed tool。
- Jarvis 的 runtime policy 已经负责工具裁剪，不需要 skill 暗中扩大能力面。

因此，对 Jarvis 来说，“skill 执行代码”带来的灵活性不值得它引入的安全和调试成本。

---

## 4. 目标架构

```text
User request
  -> Turn classifier
  -> RuntimePolicy
       - allowed_tools
       - context_sections
       - selected_skills
  -> ContextManager
       - 注入轻量 skill 使用手册
       - 注入 runtime/tool 路由规则
  -> ReAct loop
       - LLM 根据 skill 指导选择 tool
       - tool handler 执行真实动作
       - LLM 总结 tool result
  -> User reply
```

### 4.1 Tool 与 Skill 的关系

```text
Tool = 能力
Skill = 能力使用说明
RuntimePolicy = 本轮允许哪些能力
ContextManager = 本轮注入哪些说明
```

Skill 可以引用 tool 名称，但不能定义 tool：

```md
---
name: social-search-guide
description: Guide for searching social media posts and summarizing public sentiment.
tools:
  - x_search
---

When the user asks what people are saying on X/Twitter, use `x_search`.

Prefer:
- `handles` when the user names specific accounts.
- `date_from` / `date_to` when the user asks for a time window.
- `include_images` only when the user asks about visual posts.

In the final answer, include:
- short synthesis
- notable posts
- authors
- source links
```

这里的 `tools` 字段只是声明依赖和路由提示，不负责注册或授权工具。

---

## 5. x-search 迁移示例

当前 `data/skills/x-search-1.0.0/SKILL.md` 同时包含：

- X Search 的使用说明。
- API key 说明。
- `python3 {baseDir}/scripts/search.py ...` 调用方式。
- `scripts/search.py` 执行代码。

按照新原则，应拆成两部分。

### 5.1 固化为 tool

新增正式工具：

```text
app/tools/x_search.py
```

注册为：

```text
x_search
```

建议参数：

```yaml
query: string
handles: list[string] | null
exclude_handles: list[string] | null
date_from: string | null
date_to: string | null
include_images: boolean
include_video: boolean
max_results: integer
```

工具职责：

- 读取 `XAI_API_KEY`。
- 调用 xAI / Grok X Search API。
- 校验互斥参数，例如 `handles` 与 `exclude_handles` 不可同时使用。
- 返回结构化结果，包括帖子内容、作者、时间、链接、引用。
- 处理 rate limit、缺少 API key、API 错误。
- 写入 tool audit。

### 5.2 固化到 tool description

将原 `SKILL.md` 中稳定的“能力描述”和“参数选择原则”提炼到 `ToolDefinition.description` 与参数 `description`：

- 什么时候使用 X Search。
- 适合查询 tweets / X posts / social sentiment。
- handles / exclude_handles 的限制。
- date range 格式。
- 图片和视频理解的开关。
- 最终结果需要附引用链接。

### 5.3 保留为轻量 skill

如果还需要 skill，只保留用户体验层面的说明：

```text
data/skills/social-search-guide/SKILL.md
```

内容只包括：

- 社交舆情问题如何判断。
- 何时用 `x_search`，何时用普通 web search。
- 如何总结多个帖子。
- 如何区分事实、观点和热度。
- 如何提醒来源偏差。

不再包含脚本路径、安装命令、API key 设置和 shell 命令。

---

## 6. Skill 文件协议

### 6.1 推荐结构

```text
skills/
  social-search-guide/
    SKILL.md
    references/
      summarization-rubric.md
    templates/
      social-search-answer.md
```

### 6.2 推荐 frontmatter

```yaml
---
name: social-search-guide
description: Use when answering questions about public discussion on social platforms.
when_to_use: User asks what people are saying, asks for tweets/X posts, public reactions, social sentiment, or notable posts.
tools:
  - x_search
  - tavily_search
tags:
  - search
  - social
  - summarization
---
```

字段含义：

| 字段 | 含义 |
|------|------|
| `name` | skill 名称 |
| `description` | 简短能力说明，用于路由 |
| `when_to_use` | 更具体的触发条件 |
| `tools` | 该手册通常指导使用的正式工具，仅用于提示和校验 |
| `tags` | 路由和检索标签 |

### 6.3 禁止内容

Skill 正文中不应包含：

- `python scripts/foo.py ...`
- `curl ...`
- `pip install ...`
- `npm install ...`
- 要求模型执行未注册脚本的指令。
- 要求模型绕过 runtime policy 的指令。
- API key 明文或具体 secret 获取逻辑。

如果必须说明环境依赖，只写成面向开发者的文档，不注入模型上下文。

---

## 7. Runtime 设计调整

### 7.1 加载层

保留当前 `SkillPackageLoader`，但调整语义：

- 只加载 `SKILL.md` 作为 prompt 手册。
- `load_body()` 必须剥离 YAML frontmatter。
- 忽略或警告 `scripts/` 作为执行资源的用法。
- 支持 `references/`、`templates/` 作为可选说明资源，但默认不注入全文。

### 7.2 manifest 层

扩展 `SkillManifest`：

```python
class SkillManifest(BaseModel):
    name: str
    description: str = ""
    when_to_use: str | None = None
    version: str | None = None
    tags: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
```

不引入 `allowed-tools` 作为授权字段。若为了兼容外部 skill 读到 `allowed-tools`，只作为普通 metadata 存储，不改变 runtime policy。

### 7.3 路由层

`SkillRegistry.select_for_query()` 后续应升级为：

1. 显式命令匹配，例如 `/social-search-guide`。
2. 基于 `name / description / when_to_use / tags / tools` 的粗排。
3. 中文友好的 token 或字符 n-gram 匹配。
4. 可选 embedding 召回。
5. 返回命中原因，便于日志调试。

### 7.4 注入层

ContextManager 注入 skill 时遵守：

- 默认只注入少量高置信 skill。
- 单个 skill body 有 token 上限。
- 多个 skill 有总 token 上限。
- 被注入的 skill 只作为“procedural guidance”。
- 若 skill 引用的 tool 未被 runtime policy 允许，skill 仍可作为背景说明，但模型不得调用未允许工具。

后续可以引入 `load_skill` 低风险工具，实现真正的两级披露：

```text
L1: system prompt 只列出 skill name / description / when_to_use
L2: 模型显式调用 load_skill(skill_name) 后，完整手册进入上下文
```

第一阶段可以继续沿用当前自动注入模式，但要加上长度限制和更准的选择规则。

---

## 8. 与 RuntimePolicy 的关系

RuntimePolicy 仍然是工具权限的唯一来源。

Skill 中声明的 `tools` 字段不能让工具自动开放：

```text
runtime_policy.allowed_tools = ["tavily_search"]
skill.tools = ["x_search"]
```

在这种情况下，模型可以看到 skill 对社交搜索的流程建议，但不能调用 `x_search`。如果任务确实需要 `x_search`，应该由：

- turn classifier 识别 capability。
- runtime policy 暴露 `x_search`。
- 或模型通过 `tool_search` 发现并请求开放。

Skill 不参与授权，只参与说明。

---

## 9. 迁移计划

### Phase 1：清理当前 skill 语义

1. `Skill.load_body()` 剥离 frontmatter。
2. `SkillManifest` 增加 `when_to_use`、`tags`、`tools`。
3. skill 注入增加 token 上限。
4. 更新文档，明确 skill 不执行代码。
5. 对 `data/skills/*/SKILL.md` 做审计，标记包含脚本执行说明的 skill。

### Phase 2：把执行型 skill 迁移为 tool

优先迁移：

1. `x-search` -> `x_search` tool。
2. 若 `tavily` 已经有 `tavily_search` tool，则删除或压缩 `data/skills/tavily-1.0.0/SKILL.md`，避免模型被脚本调用方式误导。

每个迁移项需要：

- `app/tools/<name>.py`
- `ToolDefinition`
- 参数 schema
- runtime policy capability
- 单元测试
- 错误路径测试
- 简短 guide skill（可选）

### Phase 3：路由和两级披露

1. 优化 `select_for_query()`。
2. 记录 selected skill 和命中原因。
3. 引入 `load_skill` 低风险工具。
4. system prompt 默认只放 skill listing，不直接放全文。

### Phase 4：资源说明层

1. 支持 `references/`、`templates/` 的声明和按需读取。
2. 大参考文档不直接注入。
3. 仅当模型需要补充规则或模板时，通过正式读取机制加载。

---

## 10. 验收标准

重构完成后应满足：

- 没有任何 skill 通过 shell 脚本提供新增执行能力。
- 所有外部 API 调用都有正式 tool。
- skill 只描述流程、参数选择和输出规范。
- runtime policy 是工具开放的唯一入口。
- `SKILL.md` frontmatter 不再污染模型上下文。
- x-search 类能力能通过结构化 tool 调用完成，而不是通过 `python scripts/search.py`。
- 测试能覆盖 skill 选择、skill 注入、tool 暴露和工具执行的边界。

---

## 11. 结论

Jarvis 的 skill 不应继续向“可执行插件”方向演进，而应收敛为轻量、可检索、可注入的使用手册。真正的能力必须进入 tool registry，由 runtime policy 控制暴露，由 tool handler 负责执行，由测试和审计保证可靠性。

这条边界能让 Jarvis 同时保留 skill 的灵活性和 tool 的工程可控性：

```text
Skill teaches.
Tool acts.
RuntimePolicy authorizes.
ReAct orchestrates.
```
