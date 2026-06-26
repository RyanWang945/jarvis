# Jarvis Feishu CardKit JSON 2.0 输出升级设计

| 项目 | 内容 |
|------|------|
| 作者 | Codex |
| 日期 | 2026-06-13 |
| 状态 | Draft |
| 相关设计 | `【2026-5-1】Feishu Markdown 卡片渲染设计.md`, `【2026-05-23】Jarvis ProgressReporter 与模块化飞书进度展示设计.md`, `【2026-05-05】Jarvis Codex 产物附件通道设计.md` |
| 目标 | 将 Jarvis 飞书输出从旧版 interactive div/lark_md 卡片升级为 CardKit JSON 2.0，并建立可回退、可测试、可分阶段落地的渲染方案 |

---

## 1. 背景

当前 Jarvis 飞书输出已经不是纯文本，但体验仍然偏简陋。主要原因是最终答案还停留在旧版卡片结构：

```text
ChannelMessage(markdown)
-> FeishuRenderer.render_markdown_card()
-> interactive card
-> elements: div + lark_md
```

现有实现的几个限制：

- `FeishuRenderer.render_markdown_card()` 只把 Markdown 切成若干 `div`。
- 每块 Markdown 限制在约 3500 字符。
- 超过 12 个元素会降级为纯文本。
- 表格、标题、引用被 `adapt_markdown_for_feishu()` 手工改写，损失飞书原生 Markdown 能力。
- CardKit JSON 2.0 只在 `render_cardkit_progress_card()` 的进度展示中局部使用，最终答案、审批卡、附件汇总卡仍未统一升级。

飞书官方 CardKit JSON 2.0 已经提供更强的卡片能力：

- `schema: "2.0"` 的卡片结构。
- `body.elements` 统一承载 Markdown、表格、图片、分割线、按钮等组件。
- `summary` 自定义聊天列表预览。
- `width_mode`、`style.text_size`、`style.color` 等展示控制。
- `element_id` 支持组件级操作和后续流式更新。
- Markdown 组件支持标题、表格、图片、代码块、标签、人员、时间等更完整语法。

因此升级方向不是简单换颜色，而是建立一层 CardKit 2.0 渲染器，让不同类型的 Jarvis 输出选择合适的飞书组件。

---

## 2. 官方约束

本设计按 2026-06-13 核对的飞书开放平台文档约束制定。

### 2.1 CardKit JSON 2.0

关键约束：

- 卡片 JSON 2.0 需要显式声明 `schema: "2.0"`。
- 飞书客户端 7.20 及之后版本支持 JSON 2.0；低版本客户端标题可展示，正文会展示升级提示。
- JSON 2.0 暂时仅支持共享卡片，`config.update_multi` 只能为 `true`。
- 单张 JSON 2.0 卡片最多 200 个元素或组件。
- `body.elements[*].element_id` 在同一卡片内需全局唯一，且仅允许字母、数字、下划线，必须以字母开头，最长 20 字符。

### 2.2 消息发送与更新

关键约束：

- 发送卡片仍使用 `msg_type="interactive"`，`content` 为卡片 JSON 序列化后的字符串。
- 卡片和富文本消息体最大 30 KB；文本消息最大 150 KB。
- 更新已发送卡片使用 `PATCH /open-apis/im/v1/messages/:message_id`。
- 更新卡片前后都必须显式声明 `config.update_multi=true`。
- 仅支持更新 14 天内发送且未撤回的 interactive 卡片。
- 单条消息更新频控为 5 QPS。

### 2.3 Markdown 与表格

关键约束：

- JSON 2.0 `markdown` 组件支持标题、列表、引用、代码块、图片、分割线、标签、人员、国际化时间等。
- Markdown 表格仅 JSON 2.0 支持；除标题行外最多展示五行，超出分页展示。
- 单个富文本组件中最多放置四个 Markdown 表格。
- 对可控列宽、数字格式、日期、标签等结构化数据，应优先使用 `table` 组件。
- 单张卡片最多放置五个 `table` 组件。

### 2.4 流式更新

飞书 CardKit 流式更新适合 AI 打字机效果，但它不是当前 `PATCH message` 的简单替换：

- 需要基于 CardKit 卡片实体和组件级 OpenAPI。
- 需要 `cardkit:card:write` 权限。
- 单卡片实体相关 OpenAPI 频率上限为 10 次/秒。
- 流式模式开启时，卡片交互回调不能直接立即更新，需要先关闭流式模式。

因此本设计将流式更新放到二期，不作为 CardKit 2.0 首版依赖。

参考文档：

- [卡片 JSON 2.0 结构](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-json-v2-structure)
- [富文本组件](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-json-v2-components/content-components/rich-text)
- [表格组件](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-json-v2-components/content-components/table)
- [发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create)
- [更新已发送的消息卡片](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/patch)
- [流式更新卡片](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/streaming-updates-openapi-overview)

---

## 3. 设计目标

### 3.1 目标

1. 最终答案默认使用 CardKit JSON 2.0。
2. 保留 Jarvis 内部 canonical Markdown，不让飞书 JSON 污染 runtime / persistence。
3. 让短答案、长答案、审批、进度、附件汇总、结构化表格走不同的卡片模板。
4. 在 CardKit 2.0 发送失败时自动回退到旧版 interactive 或 text。
5. 将 `progress cardkit` 从实验配置扩展为正式模式。
6. 把飞书官方 30 KB、200 组件、5 QPS 等限制内化到 renderer。
7. 建立 renderer 单元测试和 payload schema 级别断言。

### 3.2 非目标

第一阶段不做：

- 不引入 CardKit 卡片实体和组件级流式 OpenAPI。
- 不要求模型直接输出飞书卡片 JSON。
- 不把所有 Markdown 解析成 AST 后逐组件渲染。
- 不重构核心 `ChannelMessage` 的基础语义。
- 不移除旧版卡片渲染器，保留为回退路径。
- 不强依赖卡片搭建工具模板，首版使用代码生成 JSON。

---

## 4. 总体架构

目标链路：

```text
Agent Runtime / Task Runtime
  produce ChannelMessage(content_type="markdown", metadata={...})

FeishuChannel
  choose FeishuRenderer strategy

FeishuRenderer
  render CardKit JSON 2.0
  validate limits
  fallback to legacy interactive / text when needed

Feishu OpenAPI
  msg_type="interactive"
  content=json.dumps(card)
```

建议在 `app/channels/feishu_renderer.py` 内拆出 CardKit 构造层：

```text
FeishuRenderer
  render(message)
  render_markdown_card()
  render_text_fallback()
  render_approval_card()
  render_progress_card()

FeishuCardKitRenderer
  render_final_answer()
  render_progress()
  render_approval()
  render_attachment_summary()
  render_error()

FeishuCardKitBuilder
  card()
  markdown()
  table()
  note()
  action()
  hr()
  validate()
```

其中：

- `FeishuRenderer` 继续作为对外入口，避免大范围改调用点。
- `FeishuCardKitRenderer` 只关心业务模板。
- `FeishuCardKitBuilder` 封装 JSON 2.0 结构、`element_id`、大小检查、公共样式。
- 旧版 `_render_card_from_elements()` 继续保留为 fallback。

---

## 5. 渲染策略

### 5.1 输出类型路由

首版按 `ChannelMessage.content_type` 和 `metadata` 选择模板：

| 场景 | 识别方式 | 首选渲染 | 回退 |
|------|----------|----------|------|
| 最终 Markdown 答案 | `content_type="markdown"` | CardKit final answer | legacy interactive -> text |
| 长 Markdown 答案 | payload 可能超过 30 KB | 多条 CardKit / post.md | text |
| 进度卡 | progress sink | CardKit progress | legacy thinking |
| 错误回复 | turn failed | CardKit error | legacy error -> text |
| Codex 审批 | approval metadata | CardKit approval | legacy approval |
| 图片附件 | `message.attachments[kind=image]` | CardKit image summary + image message | image message |
| 表格结果 | structured table metadata | CardKit table | markdown table / text |

### 5.2 Final Answer 卡片

推荐结构：

```json
{
  "schema": "2.0",
  "config": {
    "update_multi": true,
    "enable_forward": true,
    "width_mode": "fill",
    "summary": {
      "content": "Jarvis 已完成回复"
    },
    "style": {
      "text_size": {
        "body": {"default": "normal", "pc": "normal", "mobile": "normal"},
        "note": {"default": "notation", "pc": "notation", "mobile": "notation"}
      }
    }
  },
  "header": {
    "title": {"tag": "plain_text", "content": "Jarvis"},
    "subtitle": {"tag": "plain_text", "content": "Completed"},
    "template": "green",
    "text_tag_list": [
      {
        "tag": "text_tag",
        "text": {"tag": "plain_text", "content": "done"},
        "color": "green"
      }
    ]
  },
  "body": {
    "direction": "vertical",
    "padding": "12px 12px 12px 12px",
    "vertical_spacing": "8px",
    "elements": [
      {
        "tag": "markdown",
        "element_id": "answer_body",
        "content": "..."
      },
      {"tag": "hr", "element_id": "answer_hr"},
      {
        "tag": "markdown",
        "element_id": "answer_meta",
        "content": "<text_tag color='neutral'>model</text_tag> deepseek ...",
        "text_size": "note"
      }
    ]
  }
}
```

模板原则：

- Header 只表达状态和主题，不重复正文标题。
- 正文尽量保留模型原始 Markdown，不再手工把表格转成普通列表。
- 模型、token、耗时、runtime 等放到底部 notation 区域。
- 内容超过限制时不要硬塞单卡，应走拆分或回退。

### 5.3 Progress 卡片

现有 `render_cardkit_progress_card()` 可以作为基础，但建议改成正式模板：

- Header：`Jarvis 正在处理`，颜色按状态变更：
  - running: blue
  - completed: green
  - failed: red
  - blocked / approval: orange
- Body：
  - 当前阶段
  - 节点进度
  - 最近进展
  - 已完成节点
  - 输出占位区 `progress_output`

关键要求：

- 继续复用 `FeishuProgressSink` 的节流，默认不超过 2 秒更新一次。
- 终态更新必须强制 flush。
- 更新 payload 始终保持 `update_multi=true`。
- `element_id` 固定，方便二期组件级更新。

### 5.4 Approval 卡片

审批卡升级为更清晰的风险卡：

```text
Header: Codex 权限审批 / Approval Required
Body:
  风险等级 tag
  命令 plain_text / markdown code block
  原因
  作用范围
  审批状态
Actions:
  同意
  拒绝
  可选：同意此前缀
```

设计注意：

- 命令仍应使用 `plain_text` 或转义后的代码块，避免 Markdown 注入导致按钮上下文难读。
- 审批完成后更新同一张卡，按钮消失，仅保留状态。
- 继续复用现有 card callback 处理逻辑和 `behaviors: callback`。

### 5.5 Attachment Summary 卡片

当前图片附件是主消息后单独发送图片消息。升级后建议：

- 主回答卡底部展示附件摘要。
- 如果附件已上传并有 `image_key`，可在 CardKit 中嵌入图片或 Markdown 图片。
- 如果未上传或图片较多，仍发送独立 image 消息，并在卡片里展示数量和文件名。

第一阶段建议不改变附件发送语义，仅增加摘要，避免重复发送和幂等复杂度。

---

## 6. 长内容策略

飞书 interactive / post 消息体 30 KB 是硬限制，CardKit JSON 2.0 还额外有 200 组件限制。长内容必须有明确策略。

### 6.1 单卡优先

满足以下条件时发送单张 CardKit 卡：

- `len(json.dumps(card, ensure_ascii=False).encode("utf-8")) <= 28000`
- 元素数量小于 180，预留 footer / attachments / future actions 空间。
- 单个 Markdown 组件中表格数量小于等于 4。

### 6.2 拆分多卡

如果最终 Markdown 很长但结构清晰：

```text
card 1: 摘要 + 第一部分
card 2: 续 1/2
card 3: 续 2/2
```

拆分规则：

- 优先按 Markdown 二级标题拆分。
- 不在 fenced code block 中间拆分。
- 每张卡独立 `summary`，如 `Jarvis 回复 2/3`。
- 第一张卡保留模型 footer；或最后一张统一保留 footer，二者只能选一个。

首版可先不做多卡拆分，先实现单卡 + legacy fallback。

### 6.3 post.md 作为长文备用

飞书 `post` 消息的 `md` 标签支持 CommonMark 0.31 + GFM，包括表格、任务列表、删除线、自动链接。对于非常长、主要是 Markdown 文档的输出，`post.md` 比 CardKit 更保真。

推荐策略：

- 需要更新 thinking card 的最终答案：优先 CardKit interactive。
- 不需要覆盖 thinking card，且是长文：可以新增 `post.md` 渲染路径。
- 如果 CardKit 超限但 post 未超限：发送 `post`。
- 如果都超限：text fallback 或交付为 artifact 文件。

---

## 7. 配置设计

新增配置建议：

```python
feishu_cardkit_enabled: bool = False
feishu_cardkit_final_enabled: bool = False
feishu_cardkit_approval_enabled: bool = False
feishu_cardkit_attachments_enabled: bool = False
feishu_cardkit_fallback_mode: str = "legacy"  # legacy | post | text
feishu_cardkit_max_payload_bytes: int = 28000
feishu_cardkit_max_elements: int = 180
```

现有配置继续保留：

```python
feishu_progress_updates_enabled: bool = False
feishu_progress_mode: str = "patch"
feishu_progress_min_interval_seconds: float = 2.0
feishu_progress_max_recent_events: int = 5
```

推荐上线阶段：

```text
阶段 1: feishu_progress_updates_enabled=true, feishu_progress_mode=cardkit
阶段 2: feishu_cardkit_enabled=true, feishu_cardkit_final_enabled=true
阶段 3: feishu_cardkit_approval_enabled=true
阶段 4: feishu_cardkit_attachments_enabled=true
```

---

## 8. 实现计划

### 8.1 Phase 1：CardKit Builder 与 Final Answer

改动点：

- 新增 `FeishuCardKitBuilder`。
- 新增 `FeishuRenderer.render_cardkit_markdown_card()`。
- 在 `FeishuRenderer.render()` 中按配置启用 CardKit final answer。
- 保留旧版 `render_markdown_card()` 为 fallback。

测试：

- Markdown final answer 生成 `schema=2.0`。
- `config.update_multi is True`。
- `body.elements[0].tag == "markdown"`。
- payload 超 28 KB 自动 fallback。
- cardkit 发送失败后 `_send_channel_message()` 仍能 text fallback。

### 8.2 Phase 2：Progress 正式化

改动点：

- 将现有 `render_cardkit_progress_card()` 改为走 builder。
- 固定 progress 相关 `element_id`。
- Header 根据状态设置 template。
- `.env.example` 增加 progress cardkit 配置说明。

测试：

- progress running / completed / failed 三种状态 header template 正确。
- 节流逻辑不变。
- final update 可把 output markdown 放到 `progress_output`。

### 8.3 Phase 3：Approval CardKit

改动点：

- 新增 `render_cardkit_approval_card()`。
- 新增 `render_cardkit_approval_decision_card()`。
- 保持 callback `value` 结构兼容。

测试：

- approve / reject button callback payload 与旧版一致。
- 审批完成更新后不再出现按钮。
- 命令中的特殊字符不会破坏 Markdown。

### 8.4 Phase 4：Structured Blocks

改动点：

- 扩展 `ChannelMessage.metadata`，支持结构化 block。
- 定义最小 block 协议：

```python
{
    "feishu_blocks": [
        {"type": "markdown", "content": "..."},
        {"type": "table", "columns": [...], "rows": [...]},
        {"type": "attachments", "items": [...]},
    ]
}
```

- Renderer 优先消费 `feishu_blocks`，没有时仍按 Markdown 渲染。

测试：

- table block 生成 CardKit `table` 组件。
- 超过五个 table 触发 fallback 或裁剪提示。
- number/date/options 列类型输出合法。

### 8.5 Phase 5：post.md 长文备用

改动点：

- 新增 `render_post_markdown()`，输出 `msg_type="post"`。
- `_update_channel_message()` 只能更新 interactive，所以 thinking card 更新场景仍走 CardKit 或 legacy interactive。
- 直接发送最终消息时允许 CardKit 超限后 fallback 到 post。

测试：

- post content 包含 `zh_cn.content[[{"tag":"md"}]]`。
- post 超 30 KB 后继续 text fallback。

---

## 9. 回退策略

回退顺序建议：

```text
CardKit JSON 2.0
-> legacy interactive div/lark_md
-> post.md
-> text
```

但不同场景顺序略有差异：

- 需要更新 thinking card：`CardKit -> legacy interactive`，不能回退到 `post`，因为 `PATCH message` 只支持 interactive card 更新。
- 直接新发最终消息：`CardKit -> post.md -> text`。
- 审批卡：`CardKit approval -> legacy approval`，不能回退到纯文本，因为需要按钮回调。
- 错误卡：`CardKit error -> legacy error -> text`。

回退必须记录日志：

```text
feishu cardkit render fallback reason=payload_too_large bytes=...
feishu cardkit send fallback reason=openapi_error code=...
```

---

## 10. 风险与处理

| 风险 | 影响 | 处理 |
|------|------|------|
| 老客户端不支持 JSON 2.0 | 正文显示升级提示 | 配置默认关闭，灰度启用；保留 legacy fallback |
| payload 超 30 KB | 发送失败 | builder 预检字节数，超限前 fallback |
| 卡片组件数超限 | 发送失败 | builder 预检元素数量 |
| Markdown 特殊字符破坏组件 | 展示异常或创建失败 | 对 plain_text / command / tag 内容做转义，模型正文保持 Markdown |
| 更新频率过高 | 触发 5 QPS 限制 | 继续使用 progress sink 节流 |
| 表格组件结构不合法 | 230099 子错误 | table block 只从结构化数据生成，不从任意 Markdown 猜测 |
| 交互卡 callback 兼容性 | 审批不可用 | callback value 保持旧结构，先用测试覆盖 |
| CardKit 2.0 和旧卡混用 | 维护复杂 | 对外入口仍是 `FeishuRenderer`，内部策略配置化 |

---

## 11. 验收标准

### 11.1 功能验收

- 开启 `JARVIS_FEISHU_CARDKIT_ENABLED=true` 后，普通 Markdown 最终回复发送为 JSON 2.0 interactive card。
- 长任务开启 `JARVIS_FEISHU_PROGRESS_MODE=cardkit` 后，进度卡和最终覆盖卡均为 JSON 2.0。
- CardKit final answer 失败时，用户仍收到旧版卡片或文本。
- 审批卡在启用后仍能同意、拒绝，并更新原卡状态。
- 图片附件不会重复发送。

### 11.2 测试验收

新增或调整测试：

- `tests/test_feishu_cardkit_renderer.py`
- `tests/test_feishu_channel.py`
- `tests/test_feishu_progress.py`

覆盖点：

- JSON 2.0 基本结构。
- payload size fallback。
- element_id 唯一。
- final / progress / error / approval 模板。
- CardKit 发送异常回退。
- 旧配置下行为不变。

### 11.3 运行验收

本地人工验证建议：

1. 短 Markdown：标题、列表、代码块正常展示。
2. 表格 Markdown：CardKit Markdown 能保留表格。
3. 长回答：不会发送失败，能 fallback。
4. 长任务：进度卡有阶段和节点信息，最终结果覆盖同一卡。
5. 审批请求：按钮可用，审批后状态更新。
6. 图片附件：主卡有摘要，图片消息只发送一次。

---

## 12. 建议落地顺序

推荐先做最小闭环：

1. 新增 CardKit Builder。
2. final answer CardKit 2.0 单卡模板。
3. payload / element 预检与 fallback。
4. progress cardkit 模板收敛。
5. approval cardkit 模板。
6. structured table / attachment summary。
7. post.md 长文备用。
8. CardKit card entity + 流式更新。

这个顺序的好处是每一步都能独立上线和回退，且第一阶段不需要新增飞书权限。

