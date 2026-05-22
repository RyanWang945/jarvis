# Jarvis Task Plan 与 Artifact Context 上下文设计

日期：2026-05-13

## 背景

当前 Jarvis 的 ReAct loop 会把同一会话的历史消息带给 LLM，但跨 turn 的工具协议会被清理：

1. 历史用户消息和助手最终回复会进入上下文。
2. 历史 `ToolMessage` 不会进入新 turn。
3. 历史 assistant 的 `tool_calls` 会被去掉。
4. 当前 turn 内的工具调用和工具结果仍会保留，用于 ReAct 循环继续推理。

这个原则整体是正确的。历史工具结果通常很长，包含大量 JSON、路径、日志和中间状态，直接回放会污染新 turn 的上下文，也容易把上一轮工具协议误当成当前轮可继续执行的协议。

但它带来一个明显问题：跨 turn 有价值的结构化信息没有被稳定保留下来。例如用户说：

```text
这个图画的不太对，你可以稍微再看下工具路由和 agent 引擎的关系
```

自然语言里“这个图”引用的是上一轮生成的架构图 artifact。当前系统只能从历史 assistant 文本中的文件路径间接推断，缺少结构化 artifact context。因此意图识别容易只抓到“看下工具路由和 agent 引擎关系”，输出 `workspace.inspect` / `workspace.read_file` / `workspace.search_files`，ReAct loop 随后进入开放式读文件，而没有稳定地回到“修改上一张图”这个最终目标。

## 问题分析

### 1. Capability 只描述能力，不描述目标

当前 classifier 输出的核心信息是 `turn_type` 和 `requested_capabilities`。例如：

```json
{
  "turn_type": "coding",
  "requested_capabilities": [
    "workspace.inspect",
    "workspace.read_file",
    "workspace.search_files"
  ]
}
```

这些字段说明本轮允许使用哪些能力，但没有说明：

1. 本轮最终要产出什么。
2. 读文件只是中间步骤还是最终任务。
3. 是否引用了上一轮 artifact。
4. 何时应该从 inspect 阶段切换到 act 阶段。

因此模型可能把“理解代码关系”当作整个任务，而不是“为修正架构图收集证据”。

### 2. 历史消息缺少 artifact 结构

历史消息包含上一轮 assistant 回复，例如：

```text
已生成，保存在：E:\pythonProject\jarvis\jarvis-architecture-v3.png
```

但这只是自然语言文本。它不能稳定表达：

1. 这是一个 image artifact。
2. 它是最近生成的 artifact。
3. 它是否已经交付给当前渠道。
4. 它是否适合作为“这个图”的引用对象。
5. 它来自哪个 tool call、哪个 turn。

这些信息已经更适合由 artifact 表和 delivery 表提供，而不是依赖模型从聊天文本中猜。

### 3. ReAct 缺少任务级停止条件

ReAct loop 当前有最大步数保护，但没有任务级目标约束。最大步数只能防止无限循环，不能告诉模型：

1. 轻量 inspect 最多做几步。
2. 信息足够后应该进入产出阶段。
3. 最终 deliverable 是更新图片、回答问题，还是执行代码修改。

所以出现连续 `search_files/read_file`，最后 `react max steps reached`，本质是缺少任务规划对 loop 的牵引。

## 设计目标

一期目标不是引入复杂 planner，而是在现有 classifier 和 ReAct 架构上补一层轻量任务规划：

1. classifier 根据当前消息、历史消息、session state、recent artifacts 输出 `task_plan`。
2. runtime 将 `task_plan` 渲染进 ReAct 的系统上下文。
3. artifact context 同时提供给 classifier 和 ReAct。
4. 历史 `ToolMessage` 仍不直接回放。
5. ReAct 根据 `task_plan` 明确本轮目标、证据范围和最终产物。

核心原则：

```text
历史消息保留自然语言对话；
artifact/tool 结果转成结构化上下文摘要；
task_plan 描述本轮目标和执行约束。
```

## 一期方案

### 1. TurnClassification 增加轻量 task_plan

建议 classifier 输出新增字段：

```json
{
  "turn_type": "image_generation",
  "requested_capabilities": [
    "workspace.inspect",
    "workspace.read_file",
    "image.generate"
  ],
  "task_plan": {
    "objective": "revise_existing_artifact",
    "target_artifacts": ["latest_image"],
    "evidence_policy": {
      "workspace_inspection": "light",
      "suggested_limit": "2-3 focused file reads/searches"
    },
    "expected_steps": [
      "briefly inspect relevant routing and agent runtime files",
      "revise the previous architecture image using the inspection result"
    ],
    "final_deliverable": "updated_image_file",
    "execution_notes": [
      "do not stop after inspection",
      "inspection is only evidence for artifact revision",
      "avoid broad repository review unless user explicitly asks"
    ]
  }
}
```

一期不需要把 `task_plan` 做成复杂 DAG，也不需要严格枚举所有 intent 类型。它可以是结构化字段加自然语言约束的组合。

建议最小字段：

1. `objective`
2. `target_artifacts`
3. `evidence_policy`
4. `expected_steps`
5. `final_deliverable`
6. `execution_notes`

### 2. objective 的推荐枚举

一期可以保持少量高层目标，不做细碎硬编码：

```text
answer_question
inspect_workspace
edit_workspace
review_code
generate_artifact
revise_existing_artifact
deliver_existing_artifact
manage_reminder
```

这些不是关键词规则，而是 classifier 给 ReAct 的任务语义标签。它们的作用是表达目标，不直接决定具体工具。

### 3. Recent Artifacts Context

runtime 或 context manager 应提供最近 artifact 摘要，例如：

```text
Recent artifacts:
- id: 3093:call_codex:delegate_to_codex:abc123
  kind: image
  filename: jarvis-architecture-v3.png
  path: E:\pythonProject\jarvis\jarvis-architecture-v3.png
  source_tool: delegate_to_codex
  turn_id: 3093
  delivery_status: delivered
  created_at: 2026-05-13T00:24:10+08:00
```

该 context 至少应进入两个位置：

1. classifier 输入：帮助识别“这个图”“刚才那个文件”“上一版”等引用。
2. ReAct 输入：帮助执行时知道目标 artifact 的具体文件、类型和来源。

### 4. Recent Tool Outcomes Summary

不建议跨 turn 回放原始 `ToolMessage`，但可以提供短摘要：

```text
Recent tool outcomes:
- turn 3093 delegate_to_codex generated image jarvis-architecture-v3.png.
- turn 3094 read files: app/agent_react/runtime.py, app/agent_react/react_graph.py.
```

一期可以先只做 artifact 相关摘要。`read_file/search_files` 的跨 turn 摘要可以后置，因为它们容易噪声过大。

### 5. ReAct 上下文布局

建议模型上下文顺序：

```text
System prompt
Runtime temporal context
Session state
Runtime policy
Task plan
Recent artifacts
Recent tool outcome summaries
Conversation messages
```

`Task plan` 和 `Recent artifacts` 应作为高优先级上下文，避免被 token budget 裁掉。实现上可以放在 context header 中，或作为紧随 system prompt 的 system message。

### 6. ReAct 执行约束

当 `task_plan.objective = revise_existing_artifact` 且 `evidence_policy.workspace_inspection = light` 时，ReAct 应理解：

1. `read_file/search_files` 是证据收集工具，不是最终目标。
2. 读到足够信息后应进入 artifact revision。
3. 如果不能修改 artifact，应明确说明缺少能力，而不是继续读文件。
4. 不应把“看下关系”扩展成全仓库 review，除非用户明确要求。

这不要求 runtime 强行限制工具调用次数。一期可以先通过 task_plan 文本约束引导模型；后续再考虑 phase budget。

## 示例链路

用户上一轮：

```text
使用 codex 的 image gen skill 画一下 jarvis 的架构图
```

系统产生 artifact：

```text
jarvis-architecture-v3.png
```

用户下一轮：

```text
这个图画的不太对，你可以稍微再看下工具路由和 agent 引擎的关系
```

classifier 看到 recent artifacts 后输出：

```json
{
  "turn_type": "image_generation",
  "requested_capabilities": [
    "workspace.inspect",
    "workspace.read_file",
    "workspace.search_files",
    "image.generate"
  ],
  "task_plan": {
    "objective": "revise_existing_artifact",
    "target_artifacts": ["jarvis-architecture-v3.png"],
    "evidence_policy": {
      "workspace_inspection": "light"
    },
    "final_deliverable": "updated_image_file",
    "execution_notes": [
      "Use file inspection only to correct the routing/agent-engine relationship.",
      "The final output should be a revised architecture image."
    ]
  }
}
```

ReAct 预期行为：

```text
search/read 关键文件 1-3 次
  -> 总结工具路由与 agent 引擎关系
  -> 调用可生成/修改图片 artifact 的能力
  -> runtime 自动交付新 artifact
```

## 与现有模块的关系

### turn_classifier

职责从“分类 + capabilities”扩展为：

```text
分类 + capabilities + 本轮 task_plan
```

classifier 不负责执行，也不直接指定某个工具必须调用。它只描述任务目标和约束。

### runtime_policy

继续根据 `turn_type` 和 `requested_capabilities` 决定 allowed tools。`task_plan` 不应绕过 policy。

如果 `task_plan` 需要的能力没有被 policy 授权，ReAct 应按 policy 执行，最终说明能力不足或请求澄清。

### context_manager

负责把以下内容渲染为模型可见上下文：

1. runtime policy
2. task_plan
3. recent artifacts
4. 必要的 tool outcome summaries
5. 历史自然语言消息

它仍应清理历史 `ToolMessage`。

### artifact / delivery

artifact 表和 delivery 表成为 recent artifact context 的来源。

artifact delivery 仍由 runtime/channel 层处理，不应让模型直接理解飞书上传细节。

## 非目标

一期不做：

1. 复杂多阶段 planner/DAG。
2. 强制工具调用序列编排。
3. 跨 turn 原始 ToolMessage 回放。
4. 对所有任务类型建立细碎硬编码规则。
5. 让模型直接操作飞书附件发送协议。

## 后续演进

### Phase Budget

如果仅靠 task_plan 提示仍出现过度读文件，可以在 runtime 增加轻量 phase budget：

```text
phase=inspect max_tool_calls=3
phase=act required_final_deliverable=updated_image_file
```

这应作为二期能力，不建议一期直接做复杂状态机。

### Artifact Reference Resolution

后续可以将“这个图”“上一版”“刚才那个文件”解析为明确 artifact id：

```json
{
  "artifact_reference": {
    "phrase": "这个图",
    "artifact_id": "...",
    "confidence": 0.86
  }
}
```

一期可以先用 `target_artifacts: ["latest_image"]` 或具体 filename。

### Tool Outcome Memory

可以为重要工具结果生成短摘要并持久化：

```text
delegate_to_codex: generated file X, modified files Y, tests Z.
tavily_search: answered current-info question about Q.
```

这些摘要应服务于跨 turn 任务连续性，而不是复现完整工具输出。

## 结论

当前问题不应通过恢复历史 ToolMessage 或增加大量关键词规则解决。

更合适的架构方向是：

```text
classifier 输出轻量 task_plan
  + artifact 表提供 recent artifact context
  + context_manager 将两者注入 ReAct
  + ReAct 把 read_file/search_files 当作证据收集，而不是任务终点
```

这能在不引入复杂 planner 的前提下，解决“这个图不对”这类跨 turn artifact 修改任务的目标丢失问题。
