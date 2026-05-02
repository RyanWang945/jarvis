# Claude Agent SDK 工具升级设计

| 项目 | 内容 |
|------|------|
| 作者 | Codex |
| 日期 | 2026-05-02 |
| 版本 | v1.0 |
| 状态 | 设计中 |
| 依赖 | 【2026-5-1】Jarvis 长运行多轮对话 ReAct Agent 设计 |

---

## 1. 背景

Jarvis 当前已经把高权限代码任务收敛为一个独立工具：

- `delegate_to_claude_code`

这个工具当前底层实现是通过 Claude Code 的 headless CLI 模式调用 `claude --print`，再结合 Jarvis 自己的一层 proposal gate、审计和验证命令，完成真实仓库代码修改任务。

这条链路已经能工作，但它仍然有两个明显局限：

- 底层实现本质上仍然是 subprocess + CLI 封装，结构化程度一般。
- 如果未来要接入更稳定的会话管理、权限模型、流式结构化输出，CLI 方式会逐渐成为上限。

Anthropic 官方目前已经提供基于 Claude Code agent harness 的 SDK 体系。官方材料里既提到 `Claude Code SDK`，也开始对外使用 `Claude Agent SDK` 的表述。其能力形态包括：

- headless mode
- TypeScript SDK
- Python SDK

因此，Jarvis 需要一份明确的升级设计：在不破坏现有工具契约的前提下，把 `delegate_to_claude_code` 从当前 CLI backend 平滑演进到 SDK backend。

---

## 2. 设计目标

### 2.1 核心目标

- 保持 Jarvis 上层工具名不变：`delegate_to_claude_code`
- 保持 proposal gate、审计、验证命令等业务契约不变
- 允许底层 coder backend 在 `cli_headless` 与 `agent_sdk` 之间切换
- 允许先并行验证，再逐步切默认值

### 2.2 非目标

- 不重写 Jarvis 的工具注册模型
- 不引入第二个高权限 coder 工具名
- 不在第一阶段重构全部 Claude 返回结果格式
- 不在第一阶段替换现有 proposal gate 逻辑

---

## 3. 现状问题

当前 `delegate_to_claude_code` 的实现虽然可用，但存在这些问题：

- **实现耦合在 CLI 子进程**：权限、环境变量、工作目录、输出拼接都在同一个实现里。
- **切换成本高**：如果直接替换成 SDK，会影响现有测试、真实仓库 benchmark 和审计逻辑。
- **不利于 A/B 比较**：当前没有显式 backend 抽象，无法系统比较 CLI 和 SDK 的差异。
- **测试粒度不够细**：当前更多是在测工具整体行为，而不是测 backend 行为差异。

---

## 4. 核心设计结论

我建议：

- **工具名保持不变**
- **底层实现拆成两个 backend**
- **在工具注册处选择 backend**

也就是：

```text
delegate_to_claude_code
  -> handler = select_coder_backend()
       -> run_coder_tool_cli
       -> run_coder_tool_sdk
```

这个设计的关键点是：**对 LLM 来说仍然只有一个高权限 coder 工具，但 Jarvis 内部可以逐步替换实现。**

---

## 5. 为什么不直接新增第二个工具名

表面上可以同时注册两个工具：

- `delegate_to_claude_code`
- `delegate_to_claude_agent_sdk`

但我不建议这么做，原因有三个：

- LLM 会在两个语义几乎相同的工具之间犹豫，增加误选概率。
- proposal gate、审计、测试、报表都要同时支持两个名字，维护成本翻倍。
- 这会把“backend 演进问题”暴露成“工具语义问题”，增加系统复杂度。

因此更合理的做法是：

- 工具名只有一个
- backend 可以切换

---

## 6. 架构方案

### 6.1 对外工具层

对外仍然保持一个工具定义：

```text
name: delegate_to_claude_code
description: 高权限代码任务委托工具
```

它的参数 schema、proposal gate 和审计字段保持不变。

### 6.2 backend 抽象层

引入一个轻量选择层：

```python
def select_coder_backend() -> ToolHandler:
    ...
```

返回值可能是：

- `run_coder_tool_cli`
- `run_coder_tool_sdk`

建议由配置驱动，例如：

```text
coder_backend = cli_headless | agent_sdk
```

### 6.3 具体 backend

#### `run_coder_tool_cli`

职责：

- 保留现有 `claude --print` headless 调用逻辑
- 继续作为稳定 fallback
- 用于现有 benchmark 和线上保底

#### `run_coder_tool_sdk`

职责：

- 使用 Claude Agent SDK 的 Python 或 TypeScript 形态执行非交互 agent
- 把 SDK 的权限配置、工作目录、工具范围映射到 Jarvis 的 coder contract
- 返回与现有 `ToolExecutionResult` 兼容的统一结果

---

## 7. 配置设计

建议增加配置项：

```text
coder_backend=cli_headless
```

允许值：

- `cli_headless`
- `agent_sdk`

建议默认值：

- 第一阶段默认 `cli_headless`
- 等 SDK backend 稳定后，再切默认值

这样可以实现：

- 本地灰度
- CI A/B
- 真实仓库 benchmark 对比

---

## 8. 接口契约

无论底层走哪个 backend，`delegate_to_claude_code` 都应保持相同输入：

```text
instruction
workdir
verification_cmd
allow_commit
allow_push
```

并保持相同输出语义：

```text
ok
exit_code
stdout
stderr
artifacts
summary
```

这意味着 SDK backend 不能把 Anthropic 原生返回直接泄露给上层，而必须经过一层适配。

---

## 9. 权限与安全

SDK backend 并不会自动替代 Jarvis 的权限模型，因此这几层仍然要保留：

### 9.1 proposal gate 保留在 Jarvis

继续保留：

- 是否是显式代码请求
- `workdir` 是否存在
- `allow_push` 是否依赖 `allow_commit`

这些规则属于 Jarvis 的业务边界，不应下沉给 SDK。

### 9.2 SDK 权限只做补充

SDK backend 内部可以进一步设置：

- allowed tools
- disallowed tools
- permission mode

但这只是底层执行权限，不是业务授权替代品。

### 9.3 safe.directory 继续显式处理

真实仓库 benchmark 已经暴露出一个实际问题：

- 外部测试仓库可能触发 git `dubious ownership`

因此无论 CLI backend 还是 SDK backend，都需要继续显式注入 git safe.directory 或等效配置，不能假设运行环境总是干净的。

---

## 10. 测试策略

升级完成后，测试应分三层。

### 10.1 单元层

验证：

- backend 选择逻辑是否按配置切换
- CLI 和 SDK backend 是否都返回兼容 `ToolExecutionResult`

### 10.2 工具层

继续保留现有 `delegate_to_claude_code` 的工具测试：

- 非代码请求被 proposal gate 拒绝
- 显式代码请求可执行

但这里不应该关心具体 backend 实现细节。

### 10.3 真实仓库集成层

继续保留 opt-in 的真实仓库测试，并让它能够在两种 backend 下分别运行：

```text
JARVIS_RUN_REAL_CODER_TESTS=1
JARVIS_CODER_BACKEND=cli_headless

JARVIS_RUN_REAL_CODER_TESTS=1
JARVIS_CODER_BACKEND=agent_sdk
```

验证项应包括：

- 能否新建并切换 git 分支
- 能否从 0 开始创建最小项目
- 能否在已有代码上继续修改
- 是否能运行验证命令
- 是否会引入 `.idea/`、`.pytest_cache/`、`__pycache__/` 等仓库噪音

---

## 11. 分阶段落地

### Phase 1：抽象 backend

- 将当前实现重命名为 `run_coder_tool_cli`
- 新增 backend 选择函数
- 保持工具名不变

### Phase 2：接入 SDK backend

- 新增 `run_coder_tool_sdk`
- 先只接最小能力：instruction + cwd + permissions + final output
- 保持 `ToolExecutionResult` 契约不变

### Phase 3：并行验证

- 在真实仓库测试中对比 `cli_headless` 和 `agent_sdk`
- 观察：
  - 稳定性
  - 输出质量
  - git 行为
  - 运行耗时
  - 仓库污染程度

### Phase 4：切默认值

- 当 SDK backend 稳定后，把默认 backend 从 `cli_headless` 切到 `agent_sdk`
- CLI backend 保留一段时间作为 fallback

---

## 12. 风险

### 12.1 SDK 契约和 CLI 契约不完全一致

风险：

- SDK 的返回结构、会话模型和 CLI 可能不同

应对：

- 坚持在 Jarvis 内部做统一适配，不让上层直接依赖 SDK 原始结果

### 12.2 权限模型双层叠加后行为不一致

风险：

- Jarvis proposal gate 允许，但 SDK 内部权限拒绝

应对：

- 第一阶段保持最小 SDK 权限集
- 把底层拒绝理由原样透出到 `stderr/summary`

### 12.3 真实仓库 benchmark 波动

风险：

- 不同 backend 对 git、缓存、忽略规则处理不同

应对：

- 用统一 benchmark 对比
- 把 `.gitignore`、仓库清洁、分支状态纳入验收

---

## 13. 最终结论

我建议 Jarvis 不要直接把 `delegate_to_claude_code` 整体替换成 Claude Agent SDK 实现，而应采用：

- **工具名不变**
- **backend 可切换**
- **注册处选择 handler**

这是当前最稳、最容易测试、也最方便灰度迁移的方案。

对于 Jarvis 来说，重点不是“是否立刻全面换 SDK”，而是先建立一个清晰的 coder backend 抽象，使系统能在：

- `cli_headless`
- `agent_sdk`

之间平滑演进，同时不破坏现有 proposal gate、审计、真实仓库 benchmark 和工具契约。

---

## 14. 参考

- Anthropic Claude Code SDK / Claude Agent SDK 概览  
  https://docs.anthropic.com/en/docs/claude-code/sdk

- Anthropic Headless Mode 文档  
  https://docs.anthropic.com/zh-CN/docs/claude-code/sdk/sdk-headless

- Anthropic 工程文章：Building agents with the Claude Agent SDK  
  https://www.anthropic.com/engineering/building-agents-with-the-claude-agent-sdk/
