# 【2026-5-1】Feishu Markdown 卡片渲染设计

## 1. 背景

当前项目里的飞书输出还是纯文本发送：

- `app/channels/feishu.py` 中 `_format_result()` 直接返回 `TurnResult.reply`
- `_send_text_message()` 固定使用 `msg_type="text"`
- `app/agent_react/agent_graph.py` 持久化 assistant 消息时未保留内容类型
- `app/persistence/conversation_store.py` / `app/api/agent.py` 当前 assistant message 默认落成 `content_type="text"`

这会带来几个直接问题：

- Jarvis 内部虽然天然适合产出 Markdown，但飞书侧完全丢失结构
- 标题、列表、代码块、引用等在飞书里退化成一坨纯文本
- 后续如果要支持卡片更新、artifact、按钮交互，没有稳定的“渠道适配层”

## 2. 目标

本次设计以“内部保留 Markdown，飞书适配层负责转换和降级”为原则，满足以下目标：

- 默认使用飞书 `interactive` 消息卡片
- 卡片正文默认使用 markdown 组件承载内容
- Jarvis 内部继续把 Markdown 作为 canonical reply format
- 飞书适配层负责语法映射、拆分、长度控制和降级
- 不破坏 API / CLI / 其他渠道的现有能力

## 3. 非目标

本设计暂不覆盖：

- 富交互按钮、表单、审批流
- 流式增量更新卡片
- 图片上传、文件上传、表格/图表高级渲染
- 全量 Markdown 语法 1:1 映射

第一阶段先把“最终回复渲染正确”这件事做稳。

## 4. 设计原则

### 4.1 单一真相源

Jarvis 内部只维护一份 canonical content：

- `content`: Markdown 字符串
- `content_type`: `"markdown"`

任何渠道特有格式都不回写到核心对话内容里，避免：

- 飞书卡片 JSON 污染通用消息模型
- 不同渠道互相迁就
- 后续 Web / API / CLI 再次适配时重复做反向转换

### 4.2 渠道适配后置

Markdown 到飞书卡片的转换只发生在 Feishu adapter 内部，核心 runtime 不感知飞书细节。

### 4.3 先保真，再降级

优先级如下：

1. `interactive` + markdown 组件
2. `interactive` + 分段 markdown / plain text 混排
3. 纯文本消息

也就是说，降级发生在飞书适配层，而不是让 Agent 提前放弃 Markdown。

## 5. 总体方案

### 5.1 分层

建议把输出链路拆成 3 层：

1. 核心回复层
   `AgentRuntime` / `agent_graph` 产出 canonical Markdown reply
2. 渠道无关消息层
   定义统一的 `ChannelMessage`
3. 飞书适配层
   把 `ChannelMessage(markdown)` 渲染成飞书 `interactive` 卡片或降级文本

建议新增如下抽象：

```python
@dataclass(frozen=True)
class ChannelMessage:
    content: str
    content_type: Literal["text", "markdown"]
    summary: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

`TurnResult` 不应只返回 `reply: str`，建议改为：

```python
@dataclass(frozen=True)
class TurnResult:
    turn_id: int
    conversation_id: int
    status: str
    message: ChannelMessage
```

兼容期也可以保留 `reply` 字段，但应把它视为 `message.content` 的兼容别名。

### 5.2 数据流

```text
LLM/Agent
  -> Markdown reply
  -> append_assistant_message(content=md, content_type="markdown")
  -> TurnResult.message(content_type="markdown")
  -> FeishuRenderer.render(message)
  -> interactive card payload
  -> send message
```

## 6. 核心改动点

### 6.1 Runtime / Graph

涉及文件：

- `app/agent_react/runtime.py`
- `app/agent_react/agent_graph.py`

建议修改：

- `persist()` 中 assistant 最终回复默认按 `content_type="markdown"` 落库
- `complete_turn(summary=...)` 仍保留 summary，但 summary 只是摘要字段，不再承担“渠道输出格式”
- `TurnResult` 返回 `ChannelMessage`

这样做的关键价值是：飞书、API、CLI 都拿到同一份 Markdown 原文。

### 6.2 Conversation Store

涉及文件：

- `app/persistence/conversation_store.py`
- `app/api/agent.py`
- `app/api/schemas.py`

建议修改：

- `append_assistant_message()` 增加 `content_type: str = "markdown"`
- In-memory store 与 MySQL store 都同步支持 assistant 的 `content_type`
- `MessageResponse` 已有 `content_type` 字段，接口层无需大改，只要确保真实写入即可

如果数据库表 `messages.content_type` 已存在，则大概率不需要 schema migration；只需要停止把 assistant message 写死为 `"text"`。

### 6.3 Feishu Adapter

建议新增模块：

- `app/channels/feishu_renderer.py`

职责：

- 接收 canonical Markdown
- 归一化 Markdown
- 映射为飞书卡片元素
- 判断是否需要拆分/截断/降级
- 产出最终飞书发送 payload

建议定义：

```python
@dataclass(frozen=True)
class FeishuDelivery:
    msg_type: Literal["interactive", "text"]
    content: str
```

其中：

- `interactive` 时，`content` 为 JSON string
- `text` 时，`content` 为 `{"text": "..."}`

`FeishuChannel` 只负责发送，不负责理解 Markdown。

## 7. 飞书渲染策略

### 7.1 默认卡片结构

默认使用单卡片：

- header: 固定标题，例如 `Jarvis`
- body: 一个或多个 markdown 组件

示意结构：

```json
{
  "type": "template",
  "data": {
    "template_id": "",
    "template_version_name": ""
  }
}
```

如果不走模板卡片，也可直接发送原始卡片 JSON。第一阶段更建议直接构造原始卡片 JSON，减少模板管理成本。

建议原始卡片结构类似：

```json
{
  "config": {
    "wide_screen_mode": true
  },
  "elements": [
    {
      "tag": "markdown",
      "content": "# 标题\n\n- 列表项\n\n```python\nprint('hi')\n```"
    }
  ],
  "header": {
    "title": {
      "tag": "plain_text",
      "content": "Jarvis"
    }
  }
}
```

这里的具体字段命名在实现前需要再按飞书最新开放平台文档核一遍；但设计上可以明确：默认承载单元就是 markdown 组件，不是 text 组件。

### 7.2 Markdown 语法支持范围

第一阶段建议支持：

- 标题
- 段落
- 有序/无序列表
- 引用
- 行内代码
- 代码块
- 链接
- 粗体/斜体

第一阶段建议降级处理：

- 表格：转成等宽文本块
- 图片：转成链接或占位文本
- HTML：去除标签
- 任务列表：转成普通列表

原因很简单：这些语法在跨渠道时最容易出现不一致，应该在适配层集中收敛。

### 7.3 拆分策略

不要把整篇超长 Markdown 盲目塞进一个组件。建议采用“块级拆分”：

- 先按代码块、标题、列表、段落拆成 block
- 再按飞书单组件长度限制和整卡片大小限制分片
- 每个分片映射成一个 markdown 组件

建议预留如下方法：

```python
def split_markdown_blocks(markdown: str) -> list[str]
def build_card_elements(blocks: list[str]) -> list[dict[str, Any]]
```

这样后续做卡片更新或 artifact 插入时也能复用。

## 8. 降级策略

### 8.1 触发条件

当出现以下情况时触发降级：

- 卡片 JSON 超出飞书限制
- 单个 markdown 组件超长
- Markdown 包含大量不支持语法
- 卡片发送失败且返回可识别的格式错误

### 8.2 降级路径

建议采用确定性降级：

1. 单卡片 markdown
2. 多 markdown 组件卡片
3. 卡片内部分块改为 plain text
4. 纯文本消息

纯文本消息生成规则：

- 保留标题、列表缩进、代码块围栏
- 去掉飞书不支持的富语法
- 保证可读，不要求视觉最优

### 8.3 错误处理

如果卡片发送失败，不要直接吞掉。建议：

- 记录 card payload 摘要和失败码
- 自动尝试一次文本降级重发
- 如果文本也失败，再打 error log

## 9. 建议模块设计

### 9.1 `app/channels/feishu_renderer.py`

建议提供以下接口：

```python
class FeishuRenderer:
    def render(self, message: ChannelMessage) -> FeishuDelivery: ...
    def render_markdown_card(self, markdown: str) -> FeishuDelivery: ...
    def render_text_fallback(self, text: str) -> FeishuDelivery: ...
```

辅助函数：

```python
def normalize_markdown(markdown: str) -> str: ...
def split_markdown_blocks(markdown: str) -> list[str]: ...
def downgrade_markdown_to_text(markdown: str) -> str: ...
```

### 9.2 `app/channels/feishu.py`

建议把现有发送入口改成：

```python
def _send_delivery(self, receive_id: str, delivery: FeishuDelivery) -> None: ...
```

然后：

- `run_turn()` 完成后拿到 `TurnResult.message`
- 调 `FeishuRenderer.render(...)`
- `_send_delivery(...)`

当前 `_send_text_message()` 可以保留为 fallback helper，但不再作为主路径。

## 10. 兼容性

### 10.1 对 CLI / API

CLI / API 默认不需要改协议，只需要：

- API 返回的 assistant message `content_type` 真实反映为 `markdown`
- CLI 可以继续直接打印 Markdown 原文

也就是说，飞书卡片渲染不会污染其他渠道。

### 10.2 对现有 Skill

`app/skills/feishu.py` 当前是主动推送文本消息。建议分两步：

- 第一步只改被动回复链路
- 第二步再给 skill 增加 `content_type="markdown"` 能力

避免一次把主动推送、被动回复、交互卡片都搅在一起。

## 11. 测试方案

### 11.1 单元测试

建议新增：

- `tests/test_feishu_renderer.py`

覆盖：

- Markdown -> interactive 卡片
- 长文本拆分
- 代码块保留
- 表格/图片降级
- 卡片失败后的文本 fallback

### 11.2 集成测试

建议补充：

- `tests/test_conversation_runtime.py`
- `tests/test_feishu_channel.py`

覆盖：

- assistant message 落库为 `content_type="markdown"`
- `TurnResult` 带 `ChannelMessage`
- 飞书通道默认发 `interactive`
- 失败后降级发 `text`

## 12. 实施步骤

建议按以下顺序落地：

1. 调整 `TurnResult` 和 assistant message 持久化，先把 Markdown 保存在内部
2. 新增 `FeishuRenderer`，只做 Markdown -> payload 转换单测
3. 改造 `FeishuChannel` 发送主链路，默认走 `interactive`
4. 加入发送失败自动降级
5. 视效果再补模板卡片、按钮、artifact 区域

## 13. 风险与取舍

### 13.1 风险

- 飞书卡片字段和能力边界比纯文本更严格
- Markdown 支持不是“任意 CommonMark 全兼容”
- 超长回复可能触发卡片大小限制

### 13.2 取舍

本设计刻意不让 Agent 直接生成飞书卡片 JSON，而是坚持：

- Agent 生成 Markdown
- Adapter 负责渠道落地

这是更稳的边界。否则后面一旦接 Web、企业微信、邮件，就会把渠道格式反向渗透进核心逻辑。

## 14. 推荐结论

推荐采用以下最终方案：

- Jarvis 内部 assistant reply 统一保存为 Markdown
- `TurnResult` 升级为携带 `content_type`
- 新增 `FeishuRenderer` 作为独立适配层
- 飞书默认发送 `interactive` 卡片
- 卡片正文默认使用 markdown 组件
- 发送失败时由飞书适配层自动降级到纯文本

这是当前需求下最干净的边界划分，也能为后续卡片更新、artifact、按钮交互留下可扩展空间。

## 15. 参考

- 当前代码入口：`app/channels/feishu.py`
- 当前 runtime：`app/agent_react/runtime.py`
- 当前持久化：`app/agent_react/agent_graph.py`
- 当前 store：`app/persistence/conversation_store.py`
- 飞书消息发送接口：`/open-apis/im/v1/messages`

