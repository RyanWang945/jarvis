# Jarvis deliver_file 与 Artifact 交付边界设计

日期：2026-05-12

## 背景

当前 Jarvis 已经引入 artifact 概念，用于承载 Codex 或其他工具生成的本地文件。现有图片生成链路的目标是：

```text
Codex 生成文件
  -> ToolArtifact
  -> Runtime 解析为 ChannelAttachment
  -> FeishuChannel 上传并发送
```

这个方向是合理的，因为“产物是什么”和“发到哪个渠道”属于两个不同生命周期：

1. `ToolArtifact` 表达工具生成了什么本地产物。
2. `ChannelAttachment` 表达某个渠道可以发送什么附件。
3. `ArtifactRecord` 持久化表达历史上有哪些产物可被查询、重发和清理。
4. `DeliveryRecord` 表达某个产物是否已经投递到某个渠道会话。

现在的问题是：是否应该新增一个 `deliver_file` 工具，让模型在生成图片后显式调用该工具，把文件发送到飞书通道。

## 结论

建议新增 `deliver_file` 能力，但不要把它作为图片生成主链路。

主链路仍应保持自动 artifact 交付：

```text
工具产物 -> Runtime artifact resolver -> 当前 channel 自动发送
```

`deliver_file` 的定位应该是补充工具，只用于用户显式要求的文件交付、历史产物重发、或非当前 turn 自动交付的文件。它不应该替代 runtime finalize 阶段的自动附件发送。

为了让历史重发可靠，需要补充 artifact 持久化表，并引入统一的 `DeliveryManager`。自动交付和 `deliver_file` 都必须经过 `DeliveryManager`，不能各自直接调用飞书附件发送逻辑。

## 设计原则

### 1. 生成和交付解耦

Codex、shell、写文件工具只负责生成或发现文件，不直接调用飞书 API。

飞书通道只消费 runtime 输出的 `ChannelMessage.attachments`，不反向理解 Codex 的语义。

### 2. Artifact 是主数据流

模型最终回复中的自然语言路径不能作为主交付依据。

正确路径是：

```text
ToolExecutionResult.tool_artifacts / artifacts
  -> TurnState.artifacts
  -> resolve_channel_attachments()
  -> ChannelMessage.attachments
```

自然语言中的“文件已保存到 xxx”只作为用户可读补充，不作为上传文件的可信来源。

进入 runtime 的 `ToolArtifact` 必须持久化为 `ArtifactRecord`。历史重发不能长期依赖扫描 message `raw_payload.artifacts`，因为消息 payload 更适合审计，不适合作为 artifact 索引。

### 3. deliver_file 是显式交付工具

`deliver_file` 只在用户明确要求时使用，例如：

```text
把刚才生成的文件重新发我一下
把这个文件发到飞书
把 E:/pythonProject/jarvis/docs/report.pdf 发给我
```

不建议在普通图片生成请求中要求模型再调用 `deliver_file`。普通图片生成请求应该由 artifact 自动交付闭环处理。

### 4. 安全校验集中在 ArtifactResolver

无论自动交付还是 `deliver_file`，本地文件都必须走同一套解析与安全校验：

1. 路径必须 resolve 成真实文件。
2. 文件必须位于允许根目录内。
3. 拒绝敏感路径、密钥、数据库、日志、缓存目录。
4. 校验扩展名、MIME、大小。
5. SVG 预览转换仍放在 runtime artifact processor 层。

不能让 `deliver_file` 绕过 `resolve_channel_attachments()` 直接上传任意 path。

### 5. 所有发送集中到 DeliveryManager

自动附件发送、`deliver_file`、后续的历史 artifact 重发，都必须调用同一个 `DeliveryManager`。

禁止出现两套真实发送路径：

```text
TurnRuntime.finalize -> FeishuChannel._send_message_attachments()
deliver_file -> FeishuChannel._send_message_attachments()
```

正确约束是：

```text
TurnRuntime.finalize -> DeliveryManager.deliver_turn_artifacts()
deliver_file -> DeliveryManager.deliver_artifact()
DeliveryManager -> FeishuDeliveryHandler
```

这样幂等、状态持久化、失败重试和渠道差异都集中在一个边界内。

## 为什么不把 deliver_file 作为图片生成主路径

### 1. 会破坏分层

如果 Codex 生成图片后必须再调用 `deliver_file`，模型层就需要知道当前通道是飞书、知道应该发附件、知道文件路径可以发送。

这会让工具编排承担 channel 责任。后续增加其他渠道时，每个生成任务都要重新考虑“发到哪里”。

### 2. 容易重复发送

当前 `FeishuChannel` 已经会在文本消息发送或更新后处理 `ChannelMessage.attachments`。如果模型再显式调用 `deliver_file`，会出现两条路径：

```text
自动附件发送
显式 deliver_file 发送
```

这是最大风险。重复发送不能只作为“注意事项”，必须由架构保证：

1. 自动交付和显式交付必须进入同一个 `DeliveryManager`。
2. `DeliveryManager` 必须先检查 `DeliveryRecord`。
3. `auto` 和 `explicit` 默认不重复发送同一个 artifact。
4. 只有用户明确表达“重新发/再发一次”时才允许 `redeliver`。

### 3. 模型可靠性不足

模型可能忘记调用 `deliver_file`，也可能发错中间文件，或者在文件还没被规范化前就尝试交付。

产物交付是确定性系统行为，不应该依赖模型二次决策。

### 4. 安全边界更难解释

如果 `deliver_file` 接受任意路径并直接发送，风险较高。

如果它又必须调用 artifact resolver，那么它本质上只是“显式触发同一套交付流程”，不应该承担自动交付主职责。

## 推荐架构

### 自动交付主链路

```text
delegate_to_codex
  -> ToolExecutionResult.tool_artifacts / legacy artifacts
  -> react_graph._tool_result_artifacts()
  -> TurnState.artifacts
  -> ArtifactRepository.upsert()
  -> TurnRuntime._finalize()
  -> resolve_channel_attachments()
  -> ChannelMessage.attachments
  -> DeliveryManager.deliver_turn_artifacts()
  -> FeishuDeliveryHandler
```

适用场景：

1. 当前 turn 生成图片。
2. 当前 turn 生成 SVG 并可渲染 PNG preview。
3. 当前 turn 生成后续支持的 PDF、DOCX、PPTX、XLSX、CSV、TXT、MD 等文件。

### deliver_file 补充链路

```text
用户显式要求发送文件
  -> LLM 调用 deliver_file
  -> runtime 注入当前 conversation/channel 上下文
  -> deliver_file 根据 artifact_id 查询 ArtifactRecord，或根据 path 构造临时 ToolArtifact 并持久化
  -> DeliveryManager.deliver_artifact()
  -> ArtifactResolver 校验并生成 ChannelAttachment
  -> FeishuDeliveryHandler 投递
  -> DeliveryRecord 持久化
```

适用场景：

1. 重发当前或历史 artifact。
2. 发送用户明确指定的本地文件。
3. 发送不是当前 turn 自动产物的仓库文件。
4. 手动补偿之前上传失败的文件。

## Artifact 持久化设计

历史重发不能只依赖 `ToolMessage.raw_payload.artifacts`。`raw_payload` 是审计记录，不是可靠查询索引。

需要新增 artifact 表，作为 artifact 的一等存储。

### artifacts 表

建议字段：

```text
id
artifact_id
conversation_id
turn_id
tool_call_id
source_tool
kind
path
mime_type
filename
size_bytes
metadata_json
status
created_at
updated_at
```

字段说明：

```text
artifact_id      稳定业务 ID，用于幂等和跨表关联
conversation_id  归属会话，支持“刚才那个文件”查询
turn_id          归属 turn
tool_call_id     来源 tool call
source_tool      例如 delegate_to_codex
kind             image / file / directory / log / git_ref
path             本地路径，发送前仍需重新 resolve
status           available / missing / rejected / expired
metadata_json    来源信息、preview_for、source_path 等扩展字段
```

唯一约束：

```text
unique(artifact_id)
```

常用索引：

```text
conversation_id + created_at
conversation_id + turn_id
source_tool + created_at
status + updated_at
```

### ArtifactRecord 与 ToolArtifact 的关系

`ToolArtifact` 是工具执行期间的内存协议。

`ArtifactRecord` 是持久化后的历史索引。

工具执行后应该：

```text
ToolArtifact
  -> ArtifactRepository.upsert_from_tool_artifact()
  -> ArtifactRecord
```

发送前不能直接信任 `ArtifactRecord.path`。即使 artifact 表里有路径，也必须重新走 `ArtifactResolver`，因为文件可能已经被删除、替换、移动，或路径解析结果发生变化。

### 为什么 message raw_payload 不够

只扫 message `raw_payload.artifacts` 会有这些问题：

1. 查询历史 artifact 成本高。
2. 不方便表达 artifact 当前状态。
3. 不方便和 delivery records 做稳定关联。
4. 无法可靠支持“上次那个报告”“刚才那张图”。
5. 后续清理本地文件时，没有统一状态入口。

`raw_payload.artifacts` 仍应保留，但定位是审计和问题排查，不是主查询路径。

## deliver_file 工具设计

### 命名

首选名称：

```text
deliver_file
```

如果后续更强调 artifact 优先，可以演进为：

```text
deliver_artifact
```

当前建议先使用 `deliver_file`，但描述中明确它优先发送 artifact，不鼓励任意 path。

### 参数协议

建议 schema：

```json
{
  "type": "object",
  "properties": {
    "artifact_id": {
      "type": "string",
      "description": "Preferred artifact id to deliver or redeliver."
    },
    "path": {
      "type": "string",
      "description": "Workspace-local file path fallback when artifact_id is unavailable."
    },
    "filename": {
      "type": "string",
      "description": "Optional display filename."
    },
    "delivery_mode": {
      "type": "string",
      "enum": ["auto", "image", "file"],
      "default": "auto"
    }
  },
  "anyOf": [
    {"required": ["artifact_id"]},
    {"required": ["path"]}
  ]
}
```

不要让模型传这些字段：

```text
conversation_id
platform
external_chat_id
receive_id
access_token
```

这些必须由 runtime 根据当前 turn 注入。

### ToolExecutionRequest 注入上下文

类似 `scheduled_task` 的上下文注入，`deliver_file` 应由 `react_graph._inject_tool_runtime_context()` 注入：

```python
conversation_id
turn_id
platform
external_chat_id
```

这样模型只表达“要交付哪个文件”，不负责决定“发到哪个飞书 chat”。

### 工具返回

成功：

```text
文件已发送：diagram.png
```

失败：

```text
文件未发送：unsupported_type
```

同时返回结构化 summary，便于审计：

```json
{
  "status": "sent",
  "artifact_id": "...",
  "filename": "diagram.png",
  "channel": "feishu",
  "external_message_id": "..."
}
```

## DeliveryManager 边界

`deliver_file` 不建议直接调用 `FeishuChannel` 实例方法。自动交付也不应该继续由飞书通道自己做幂等判断。

建议引入 `DeliveryManager`：

```text
DeliveryManager
  deliver_turn_artifacts(conversation_id, turn_id, purpose="auto")
  deliver_artifact(conversation_id, artifact_id, purpose="explicit")
  deliver_path(conversation_id, path, purpose="explicit")
```

内部依赖：

```text
ArtifactRepository
ArtifactResolver
DeliveryRecordRepository
ChannelDeliveryHandlerRegistry
FeishuDeliveryHandler
```

这样工具层、runtime finalize、飞书通道不会各自维护发送状态。

### Channel Handler

渠道 handler 只负责具体平台 API：

```text
FeishuDeliveryHandler
  upload_image()
  upload_file()
  send_image_message()
  send_file_message()
```

它不负责判断是否重复发送，不负责查询历史 artifact，也不负责决定用户意图。

### finalize 与飞书通道关系

当前 `FeishuChannel._send_channel_message()` 会在主消息发送成功后调用 `_send_message_attachments()`。

引入 `DeliveryManager` 后，建议迁移为：

```text
1. FeishuChannel 发送或更新主文本/卡片
2. FeishuChannel 调用 DeliveryManager.deliver_message_attachments(...)
3. DeliveryManager 做幂等和状态持久化
4. DeliveryManager 调用 FeishuDeliveryHandler 真实发送
```

或者进一步让 runtime finalize 只返回 `artifact_ids`，由 FeishuChannel 在知道 `external_chat_id` 后调用：

```text
DeliveryManager.deliver_turn_artifacts(
  conversation_id=...,
  turn_id=...,
  platform="feishu",
  external_chat_id=...,
  purpose="auto",
)
```

首版可以保留 `ChannelMessage.attachments` 作为兼容协议，但真实发送状态必须进入 `DeliveryManager`。

## 幂等设计

delivery state 不应写回 `ToolArtifact` 或 `ArtifactRecord`。建议新增 `delivery_records` 表。

### delivery_records 表

建议字段：

```text
id
delivery_id
artifact_id
conversation_id
turn_id
channel
external_chat_id
purpose
status
upload_key
external_message_id
error_message
attempt_count
created_at
updated_at
```

幂等键：

```text
channel + external_chat_id + artifact_id + purpose
```

`purpose` 建议取值：

```text
auto
explicit
redeliver
```

语义：

```text
auto       当前 turn 自动交付，只允许发送一次
explicit   用户显式要求发送，默认不重复发送已 auto/explicit 成功的 artifact
redeliver  用户明确要求重新发送，允许产生新的发送动作
```

### 重复发送策略

```text
auto:
  如果任意 auto record 已 sent，跳过。

explicit:
  如果 auto 或 explicit 已 sent，默认返回 already_sent，不重复发。

redeliver:
  允许再次发送，但必须创建新的 delivery_id，保留历史记录。
```

`deliver_file` 首版可以不暴露 `redeliver` 参数，让 runtime 或工具根据用户请求文本识别“再发一次/重新发”语义后设置内部 purpose。

### 硬性约束

所有真实发送前必须调用：

```text
DeliveryRecordRepository.check_before_send(...)
```

所有真实发送后必须调用：

```text
DeliveryRecordRepository.mark_uploaded(...)
DeliveryRecordRepository.mark_sent(...)
DeliveryRecordRepository.mark_failed(...)
```

不能存在绕过 delivery record 的发送路径。

## Runtime Policy

`deliver_file` 不应该进入所有普通 chat turn 的默认工具列表。

建议暴露条件：

1. 用户显式要求发送、重发、交付、上传文件。
2. 当前会话存在可发送 artifact，且用户提到“刚才那个文件/图片”。
3. tool_search 可以发现该工具，但 grant 条件要严格匹配文件交付意图。

不建议在 `image_generation` mode 默认允许 `deliver_file`，否则模型可能在自动交付之外重复调用。

## 与现有自动 artifact 交付的关系

### 当前图片生成

用户说：

```text
用 Codex 画一张 Jarvis 架构图
```

期望链路：

```text
delegate_to_codex -> artifact 自动发送
```

不需要 `deliver_file`。

### 用户要求重发

用户说：

```text
刚才那张图再发我一下
```

期望链路：

```text
deliver_file(artifact_id=last_image_artifact_id)
  -> DeliveryManager purpose=redeliver 或 explicit
```

### 用户指定路径

用户说：

```text
把 docs/report.pdf 发到飞书
```

期望链路：

```text
deliver_file(path="docs/report.pdf")
  -> ArtifactResolver 校验
  -> 文件附件发送
```

如果文件类型暂不支持，明确返回不支持原因。

## 文件类型支持顺序

### 阶段 1：复用现有图片链路

支持：

```text
.png
.jpg
.jpeg
.webp
.gif
.svg -> png preview
```

目标是让 `deliver_file` 和自动 artifact 交付共享同一个 resolver。

### 阶段 2：飞书文件附件

支持：

```text
.pdf
.docx
.pptx
.xlsx
.csv
.txt
.md
```

需要新增飞书文件上传 API 封装。

### 阶段 3：历史 artifact 索引

支持用户说：

```text
把上次生成的报告发我
```

这要求从 `artifacts` 表中查找历史 artifact。message `raw_payload` 只作为兜底审计来源。

## 测试计划

### 自动交付不回归

1. Codex 返回 PNG artifact，finalize 后 `ChannelMessage.attachments` 包含图片。
2. 飞书发送文本卡片后发送 image 消息。
3. LLM 未调用 `deliver_file` 时，图片仍能自动发送。
4. 同一 artifact 重试不会重复发送。

### deliver_file 工具

1. `artifact_id` 存在时，从 `artifacts` 表查询并发送。
2. `path` 存在且安全时，创建或 upsert artifact record 后发送。
3. path 越权时拒绝。
4. 敏感文件拒绝。
5. 不支持类型拒绝。
6. 已发送 artifact 默认不重复发送。
7. 用户明确重发时允许产生新的 `redeliver` delivery record。
8. 缺少当前 channel 上下文时返回清晰错误。

### Artifact 持久化

1. 工具产物进入 `TurnState.artifacts` 后写入 `artifacts` 表。
2. 同一 `artifact_id` 重复写入时 upsert，不重复建记录。
3. artifact 文件被删除后，发送前标记为 `missing`。
4. resolver 拒绝后，记录或更新 artifact status 为 `rejected`，并保留拒绝原因。

### DeliveryManager 幂等

1. finalize 自动发送和 `deliver_file` 都调用同一个 `DeliveryManager`。
2. auto 已 sent 时，重复 finalize 不再发送图片。
3. explicit 遇到已 auto sent 的 artifact，默认返回 already_sent。
4. redeliver 可以再次发送，但会生成新的 delivery record。
5. 飞书上传成功但发送消息失败时，状态为 uploaded，可复用 upload_key 重试。

### Policy

1. 普通图片生成 turn 不默认暴露 `deliver_file`。
2. “重发刚才图片”类请求可以通过 tool_search 获得 `deliver_file`。
3. “生成图片”请求不会因为 `deliver_file` 存在而重复发送。

## 推荐实施步骤

1. 保持现有 artifact 自动交付链路为主路径，不改图片生成编排。
2. 新增 `artifacts` 表和 `ArtifactRepository`，工具产物进入 turn state 后同步持久化。
3. 新增 `delivery_records` 表。
4. 抽出 `DeliveryManager`，统一自动交付和 `deliver_file` 的发送路径。
5. 将飞书图片上传/发送封装成 `FeishuDeliveryHandler`。
6. 迁移 finalize 附件发送：不再由飞书通道内存态单独判断幂等，改由 `DeliveryManager` 判断。
7. 新增 `deliver_file` 工具，但只在显式交付意图下暴露。
8. 首版只支持图片与 SVG preview，复用现有 `resolve_channel_attachments()`。
9. 后续再扩展飞书文件上传 API 和历史 artifact 查询体验。

## 最终取舍

`deliver_file` 是有价值的，但它解决的是“用户显式要求交付某个文件”的问题，不是“图片生成后如何自动返回飞书”的问题。

图片生成后的飞书返回，应继续由 artifact 自动交付链路处理。历史重发和显式文件发送，则由 `deliver_file` 触发 `DeliveryManager` 完成。

最终架构约束是：

```text
artifact 必须持久化
真实发送必须经过 DeliveryManager
重复发送必须由 delivery_records 幂等控制
```

这样系统边界更清晰，模型行为更稳定，也更容易扩展到其他渠道。
