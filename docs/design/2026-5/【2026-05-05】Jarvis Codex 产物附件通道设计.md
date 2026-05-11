# Jarvis Codex 产物附件通道设计

日期：2026-05-05

## 背景

当前主流程中，用户通过飞书让 Jarvis 调用 Codex 生成图片或其他本地文件时，Codex 能在仓库内实际落盘，但最终飞书只收到文本回复。典型表现是：

```text
Codex 已生成 E:/pythonProject/jarvis/jarvis_architecture.svg
```

用户在飞书中只能看到路径，不能直接查看图片或下载文件。

已有 `ToolExecutionResult.artifacts` 字段可以承载工具产物，但当前主流程缺少从工具结果到渠道发送的完整附件链路。这个问题不是 Codex 生成能力不足，而是 Jarvis 没有把“本地产物”作为一等输出对象处理。

## 设计目标

让 Codex 生成的图片能自动返回飞书，同时保持主流程分层清晰。首版打通 PNG、JPG、JPEG、WebP、GIF 图片闭环，并在 Runtime ArtifactResolver 层支持 SVG 转 PNG 预览；文档附件后置。

目标行为：

1. Codex 或其他工具生成本地文件后，工具结果保留结构化产物信息。
2. Runtime 在 turn state 中主动累积产物，并在最终回复前转换成统一附件协议。
3. 飞书通道根据附件类型上传并发送图片。
4. 文本回复仍正常发送，附件发送失败不影响主任务完成。
5. 产物上传必须经过路径、类型、大小和敏感文件校验。
6. 飞书附件发送必须有 `artifact_id` 和发送状态，避免重试导致重复消息。

非目标：

1. 不让 Codex 直接调用飞书 API。
2. 不通过解析最终自然语言回复作为主路径识别附件。
3. 不建设公开文件下载服务。
4. 不允许任意本地路径被上传到飞书。
5. 首版不支持文档附件和卡片内嵌图片。

## 总体方案

将产物通道拆成三层：

```text
Tool 层
  Codex 生成文件并返回 ToolArtifact

Runtime 层
  TurnState 主动累积 ToolArtifact
  ArtifactResolver 校验和规范化为 ChannelAttachment
  写入 ChannelMessage.attachments

Channel 层
  FeishuChannel 消费 ChannelMessage.attachments
  幂等上传图片并发送附件消息
  记录 artifact delivery state
```

核心原则是：**产物是 Runtime 的一等输出，不是文本回复的附属品。**

## 数据协议

### ToolArtifact

`artifacts` 不应长期使用 `tuple[str]`。字符串协议无法稳定表达类型、路径、MIME、大小、来源、幂等 ID 等信息，后续会变成难维护的隐式约定。

建议新增一等数据结构：

```python
@dataclass(frozen=True)
class ToolArtifact:
    artifact_id: str
    kind: Literal["image", "file", "directory", "log", "git_ref"]
    path: str | None = None
    mime_type: str | None = None
    filename: str | None = None
    size_bytes: int | None = None
    source_tool: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
```

`artifact_id` 必须稳定，建议首版使用：

```text
{turn_id}:{tool_call_id}:{sha256(resolved_path + mtime + size)[:16]}
```

如果同一 turn 重试并指向同一文件，应该得到相同 `artifact_id`，便于渠道层做幂等发送。

兼容策略：

1. `ToolExecutionResult.artifacts: list[str]` 可短期保留。
2. 新增 `ToolExecutionResult.tool_artifacts: list[ToolArtifact]` 或将 `artifacts` 迁移为 `list[ToolArtifact]`。
3. 对旧的 `git_file:path` 字符串，只作为兼容输入，在工具边界尽快转换为 `ToolArtifact`。

### ToolExecutionOutcome

当前 `_execute_single_tool()` 返回 `(ok, output)`，会丢失 `ToolExecutionResult.artifacts`。建议引入结构化结果：

```python
@dataclass(frozen=True)
class ToolExecutionOutcome:
    ok: bool
    output: str
    artifacts: tuple[ToolArtifact, ...] = ()
```

工具执行完成后，将工具产物带入 outcome。字符串 artifact 只允许作为兼容层存在，进入 Runtime 前应转换为 `ToolArtifact`。

### TurnState artifacts

Runtime 不应只靠扫描 `ToolMessage` 汇总产物。扫描持久化消息适合作为审计和恢复手段，但主流程应该在内存状态中主动传递。

建议扩展 `TurnRuntimeState`：

```python
class TurnRuntimeState(TypedDict):
    ...
    artifacts: list[ToolArtifact]
```

执行工具后的主链路：

```text
execute_tools()
  -> ToolExecutionOutcome.artifacts
  -> next_state["artifacts"]
  -> TurnRuntime._finalize()
  -> ArtifactResolver
  -> ChannelMessage.attachments
```

`ToolMessage.raw_payload.artifacts` 仍然要写，用于持久化、审计、问题排查和异常恢复，但不能作为唯一的主数据流。

### ToolMessage raw_payload

工具消息持久化时记录 artifacts，便于 Runtime 汇总和审计追踪：

```python
raw_payload={
    "source": "agent_react.tool_result",
    "tool_name": tool_name,
    "tool_call_id": tool_call_id,
    "artifacts": [artifact_to_payload(item) for item in outcome.artifacts],
}
```

`ToolMessage.content` 继续保留给 LLM 阅读的文本输出，`raw_payload.artifacts` 作为机器可读通道。

### ChannelAttachment

附件也不应藏在 `ChannelMessage.metadata` 中。附件是渠道输出的一等内容，应该成为 `ChannelMessage` 正式字段。

```python
@dataclass(frozen=True)
class ChannelAttachment:
    artifact_id: str
    kind: Literal["image", "file"]
    path: str
    mime_type: str
    filename: str
    size_bytes: int
    source_tool: str
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class ChannelMessage:
    content: str
    content_type: Literal["text", "markdown"] = "text"
    summary: str | None = None
    attachments: tuple[ChannelAttachment, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
```

首版最终消息示例：

```python
ChannelMessage(
    content=reply,
    content_type="markdown",
    attachments=(
        ChannelAttachment(
            artifact_id="42:call_abc:1f2e3d4c5b6a7890",
            kind="image",
            path="E:/pythonProject/jarvis/jarvis_architecture.png",
            mime_type="image/png",
            filename="jarvis_architecture.png",
            size_bytes=123456,
            source_tool="delegate_to_codex",
        ),
    ),
)
```

## Artifact Resolver

新增 Runtime 侧 helper，负责将 `ToolArtifact` 转换成可发送的 `ChannelAttachment`。

职责：

1. 接收 `ToolArtifact`，兼容解析旧的 artifact 字符串。
2. 将相对路径解析到对应仓库根目录。
3. 使用 `Path.resolve(strict=True)` 获得真实路径。
4. 校验 resolve 后路径是否在允许目录内。
5. 防止软链接、junction、reparse point 逃逸。
6. 校验敏感文件、MIME、扩展名和大小。
7. 返回 attachments 和 rejected artifacts。

允许目录：

1. 已注册仓库根目录。
2. `data/coder_runs`。
3. 后续明确配置的 artifact 输出目录。

拒绝规则：

1. 文件不存在。
2. 路径逃逸到允许目录外。
3. `Path.resolve(strict=True)` 失败。
4. resolve 后路径不在允许根目录内。
5. 文件或任一父目录是软链接、junction 或 reparse point，且真实路径逃逸。
6. 文件超过大小限制。首版图片建议 10 MB。
7. 文件名或路径命中敏感规则：`.env`、密钥、证书、数据库、日志、缓存目录。
8. MIME 或扩展名不在允许列表。
9. MIME 与扩展名不匹配。

首版支持类型：

| 扩展名 | kind | 处理方式 |
| --- | --- | --- |
| `.png` `.jpg` `.jpeg` `.webp` `.gif` | `image` | 上传图片 |
| `.svg` | `image` | Runtime 生成 PNG 预览后上传图片 |

后置类型：

| 扩展名 | 后续处理 |
| --- | --- |
| `.pdf` `.docx` `.pptx` `.xlsx` | 作为文件附件发送 |
| `.md` `.txt` `.csv` | 作为文件附件发送，大小受限 |

## SVG 预览策略

SVG 转 PNG 预览放在 Runtime ArtifactResolver / ArtifactProcessor 层，而不是飞书通道层。

原因：

1. 飞书图片上传更适合位图。
2. SVG 原文件可能包含不适合直接渲染的内容。
3. SVG 转 PNG 是本地产物规范化，不是飞书专属发送逻辑。
4. 其他渠道后续也可以复用同一 PNG preview。

处理策略：

```text
SVG 源文件
  -> 通过 cairosvg 转为 PNG 预览
  -> 飞书发送 PNG 图片
  -> SVG 原文件作为 file attachment 或文本路径保留
```

如果运行环境没有 `cairosvg`：

1. 不在运行时自动安装依赖。
2. 记录 `svg_preview_unavailable` 日志。
3. 不生成图片附件，文本回复仍保留本地 SVG 路径。

注意：`cairosvg` 依赖 native Cairo 运行库。Windows 环境如果缺少 `cairo-2` / `libcairo-2.dll`，也按 `svg_preview_unavailable` 降级处理。

## 飞书发送流程

`FeishuChannel._send_channel_message()` 和 `_update_channel_message()` 在文本卡片成功后处理附件。首版只处理 `kind=image`。

推荐首版流程：

```text
1. 发送或更新文本卡片
2. 遍历 message.attachments
3. image 类型：
   local image -> 上传图片 -> image_key -> 发送 msg_type=image
4. 单个附件失败只记录错误并发送简短降级提示
```

### 发送状态与幂等

飞书附件发送必须记录 `artifact_id` 和发送状态，避免重试或 queued turn 恢复时重复发送图片。

建议新增渠道发送状态记录：

```python
@dataclass(frozen=True)
class ArtifactDeliveryRecord:
    turn_id: int
    artifact_id: str
    channel: str
    receive_id: str
    status: Literal["pending", "uploaded", "sent", "failed", "skipped"]
    upload_key: str | None = None
    external_message_id: str | None = None
    error_message: str | None = None
```

幂等键：

```text
turn_id + artifact_id + channel + receive_id
```

发送前检查：

1. 如果状态是 `sent`，跳过发送。
2. 如果状态是 `uploaded` 且有 `image_key`，可直接重试发送消息。
3. 如果状态是 `failed`，按重试策略决定是否重试。

注意：发送状态不要塞回 `ToolArtifact`。`ToolArtifact` 表达“本地产物是什么”，`ArtifactDeliveryRecord` 表达“某个渠道对这个产物做了什么”，两者生命周期不同。

为什么不首版塞进交互卡片：

1. 额外发送图片或文件消息更容易落地。
2. 不受当前卡片 JSON 长度和元素数量限制影响。
3. 上传失败可以局部降级，不影响主文本卡片。

后续可以优化为：图片预览嵌入最终 interactive card，文件作为额外消息发送。

## 错误处理

文本回复和附件发送应解耦。

如果主任务成功、附件上传失败，最终状态仍是 completed，但需要：

1. 记录结构化日志。
2. 可选发送一条文本提示。
3. 保留本地路径，便于用户后续定位。

示例提示：

```text
图片已生成到本地，但上传飞书失败：jarvis_architecture.png
本地路径：E:/pythonProject/jarvis/jarvis_architecture.png
```

关键日志：

```text
artifact detected turn_id=... path=... kind=...
artifact rejected turn_id=... path=... reason=...
artifact attachment resolved turn_id=... attachment_count=...
feishu attachment upload starting kind=... path=...
feishu attachment upload completed kind=... key=...
feishu attachment delivery skipped artifact_id=... reason=already_sent
feishu attachment send failed kind=... path=... error=...
```

## 实施步骤

### 第 1 阶段：打通内部产物链路

1. 新增 `ToolArtifact` 和 `ToolExecutionOutcome`。
2. 修改 `_execute_single_tool()` 返回 outcome。
3. `TurnRuntimeState` 增加 `artifacts`。
4. `execute_tools()` 将产物追加到 state artifacts。
5. `execute_tools()` 写入 `ToolMessage.raw_payload.artifacts` 作为持久化审计。
6. 增加测试覆盖工具 artifacts 不丢失。

### 第 2 阶段：Runtime 汇总附件

1. 新增 Artifact Resolver。
2. `TurnRuntime._finalize()` 从 state artifacts 生成 attachments。
3. `ChannelMessage` 增加正式字段 `attachments`。
4. `AgentRuntime.run_turn()` 将 attachments 带入 `TurnResult.message.attachments`。
5. 增加路径白名单、软链接逃逸、敏感文件、MIME、大小限制测试。

### 第 3 阶段：飞书图片发送

1. `FeishuChannel` 增加图片上传 API。
2. 文本卡片发送或更新后，发送 image 消息。
3. 支持 PNG、JPG、JPEG、WebP、GIF。
4. 增加 `ArtifactDeliveryRecord` 或等价发送状态。
5. 附件发送失败不影响文本回复。
6. 重试时基于 `artifact_id` 避免重复发送。

### 第 4 阶段：文档文件发送

后置。

1. 增加飞书文件上传 API。
2. 支持 PDF、DOCX、PPTX、XLSX、CSV、TXT、MD。
3. 增加文件大小和 MIME 校验。

### 第 5 阶段：SVG 预览

1. 增加 SVG 转 PNG helper。
2. 优先发送 PNG 预览。
3. 缺少转换依赖时降级。

## 测试计划

单元测试：

1. 工具返回 `ToolArtifact` 后，`TurnRuntimeState.artifacts` 保留产物。
2. 工具返回 artifacts 后，`ToolMessage.raw_payload` 保留序列化产物。
3. Runtime 从 state artifacts 生成 `ChannelMessage.attachments`。
4. 相对路径能按仓库根目录解析。
5. `Path.resolve(strict=True)` 失败时拒绝。
6. 路径逃逸、软链接逃逸、敏感文件、超大文件被拒绝。
7. MIME 和扩展名不匹配时拒绝。
8. PNG、JPG、JPEG、WebP、GIF 被识别为 `image`。
9. SVG 能在 `cairosvg` 可用时生成 PNG preview attachment。
10. SVG 在缺少 `cairosvg` 时记录 `svg_preview_unavailable`。
11. `AgentRuntime.run_turn()` 返回的 `ChannelMessage.attachments` 不丢失。
12. 飞书图片上传成功后发送 `msg_type=image`。
13. 飞书附件发送失败时仍保留主文本回复。
14. 同一 `turn_id + artifact_id + receive_id` 重试不会重复发送图片。

集成测试：

1. 模拟 Codex 生成 `jarvis_architecture.png`，飞书收到文本卡片和图片消息。
2. 模拟同一 turn 重试，飞书不会收到重复图片。
3. 模拟 Codex 生成不支持类型，飞书只收到文本卡片和本地路径说明。

## 风险与取舍

1. **误传敏感文件**：必须由 Artifact Resolver 做白名单和敏感路径拒绝，不能由飞书层兜底。
2. **上传失败影响体验**：附件失败不应回滚主任务，应提示本地路径和失败原因。
3. **LLM 产物描述不可靠**：自然语言路径解析只能作为兜底，主路径必须使用工具 artifacts。
4. **只靠扫描 ToolMessage 不稳**：主链路使用 `TurnState.artifacts`，扫描持久化消息只作为恢复手段。
5. **重复发送附件**：用 `artifact_id` 和 delivery state 做幂等控制。
6. **卡片内嵌图片复杂度更高**：首版用独立附件消息，后续再优化卡片展示。
7. **文档附件增加范围复杂度**：首版后置文档附件，先验证图片和 SVG preview 闭环。

## 推荐落地顺序

优先做最小闭环：

```text
ToolArtifact 不丢
  -> TurnState.artifacts
  -> ChannelMessage.attachments
  -> Feishu 图片上传
  -> ArtifactDeliveryRecord 防重复
```

首个可验收场景：

```text
用户：用 Codex 画一张 Jarvis 架构图，发到飞书里

预期：
1. Codex 在仓库生成图片文件
2. Jarvis 文本卡片说明已完成
3. 飞书额外收到一条图片消息
4. 日志能看到 artifact detected / upload completed
```

完成这个闭环后，再扩展文档附件。
