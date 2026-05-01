# Jarvis Token 窗口管理与上下文压缩设计

| 项目 | 内容 |
|------|------|
| 作者 | Claude Code |
| 日期 | 2026-05-01 |
| 版本 | v2.0 |
| 状态 | 设计中 |
| 依赖 | 【2026-5-1】Jarvis 长运行多轮对话 ReAct Agent 设计 |

---

## 1. 核心原则

> **数据库完整保存历史；LLM 只看"长期检索结果 + 对话摘要 + 最近完整 turn blocks + 当前输入"。**

### 1.1 数据库历史 ≠ LLM 上下文

- `messages / turns / tool_calls / tool_results` **全量保存**，作为审计、回放、搜索、重新摘要的数据源。
- **不要为了省 token 删除历史消息。**
- 发给 LLM 的上下文由 `ContextAssembler` 单独装配，与数据库查询解耦。

### 1.2 压缩单位必须是 turn block

不要按单条 message 截断。一个完整 turn block 的结构是：

```text
user
assistant (tool_call)
tool result
assistant final
```

按单条 message 截断会破坏 tool_call 协议（LLM 看不到自己发的 tool_call，或看到 tool result 但无对应调用）。

---

## 2. 上下文装配模型（ContextAssembler）

```text
ContextAssembler 输出（按顺序）：
  1. system prompt
  2. + retrieved long-term memory  （可选，Phase 3 接入）
  3. + conversation.active_summary  （对话级持久化摘要）
  4. + recent turn blocks           （最近 N 个完整 turn）
  5. + current user message         （当前输入）
```

各层职责：

| 层级 | 来源 | 生命周期 | 说明 |
|------|------|----------|------|
| system prompt | 静态配置 | 每次请求 | 角色定义、工具说明 |
| long-term memory | LLMWiki / 知识库检索 | 按需检索 | Phase 3 再做，不混入窗口压缩 |
| active_summary | `conversations.active_summary` | 跨 turn 持久化 | 旧对话的压缩摘要 |
| recent turns | `messages` 表（按 turn block） | 当前窗口内 | 保留完整 block，不拆分 |
| current input | 当前 trigger message | 当前 turn | 用户最新输入 |

---

## 3. 短期记忆 + 对话摘要（第一版重点）

### 3.1 短期记忆

`prepare` 节点加载逻辑：

```python
# 伪代码
blocks = load_turn_blocks(conversation_id, after=conversation.summarized_until_message_id)
recent_blocks = blocks[-N:]   # 保留最近 N 个完整 turn blocks（默认 N=5）
current_block = build_current_block(trigger_message, react_loop_messages)
```

- **当前 turn 的所有消息必须完整保留**，包括 ReAct loop 中的 tool_calls 和 tool results。
- **recent blocks** 是已经完结的 turn，按完整 block 保留或丢弃。
- 窗口超限时，**丢弃最老的完整 block**，不是单条 message。

### 3.2 对话摘要（active_summary）

在 `conversations` 表直接增加字段，不另建表：

```sql
ALTER TABLE conversations ADD COLUMN active_summary TEXT NULL;
ALTER TABLE conversations ADD COLUMN active_summary_token_count INT NOT NULL DEFAULT 0;
ALTER TABLE conversations ADD COLUMN summarized_until_message_id BIGINT UNSIGNED NULL;
```

**更新时机**：
- 当窗口超限且丢弃老 block 时，将被丢弃的 blocks + 现有 `active_summary` 合并，生成新的 `active_summary`。
- 用轻量模型（如 `deepseek-chat`）调用一次摘要生成。
- 更新 `summarized_until_message_id` 为已摘要覆盖的最新 message id。

**摘要 prompt 示例**：
```text
请把以下对话历史总结为 1-2 句话，保留关键事实、用户意图和已确认的结论：
{existing_summary}
{older_blocks}
```

**prepare 时加载**：
```text
system prompt
+ [active_summary 作为 SystemMessage 或 memory 注入]
+ recent blocks（summarized_until_message_id 之后）
+ current input
```

### 3.3 为什么摘要放在 conversations 表

- 一个 conversation 只需要一个活跃摘要，不需要多代历史。
- 如果需要多代摘要历史，可以额外做 `conversation_summary_history` 表（V2 扩展），但 V1 不需要。
- 减少 JOIN，prepare 时直接读取。

---

## 4. Token 计算模块（`app/llm/tokenizer.py`）

### 4.1 设计原则

- **快**：本地 tiktoken 编码，不依赖网络。
- **模型无关**：DeepSeek / OpenAI 均基于 `cl100k_base`。
- **可换**：预留接口，未来可用模型官方 tokenizer。

### 4.2 接口

```python
class TokenCounter:
    def __init__(self, encoding_name: str = "cl100k_base"): ...

    def count_text(self, text: str) -> int: ...

    def count_message(self, msg: BaseMessage) -> int:
        """单条消息 token = content tokens + overhead"""
        ...

    def count_messages(self, messages: list[BaseMessage]) -> int: ...

    def estimate_tool_schema(self, tools: list[dict]) -> int:
        """估算 tools schema 占用"""
        ...

    def count_turn_block(self, block: TurnBlock) -> int:
        """按完整 turn block 统计，用于窗口裁剪决策"""
        ...
```

### 4.3 Overhead 参考值

| 消息类型 | Overhead |
|----------|----------|
| `system` / `user` / `assistant` | 4 tokens |
| `tool` | 4 + len(tool_call_id) tokens |
| 每次请求固定开销 | 3 tokens |

---

## 5. 窗口管理器（`app/agent_react/context_window.py`）

### 5.1 配置

```python
@dataclass
class WindowConfig:
    model_max_tokens: int = 64_000
    max_output_tokens: int = 8_000
    safety_buffer: int = 1_000
    compression_trigger_ratio: float = 0.75
    min_recent_turn_blocks: int = 2      # 至少保留的完整 turn block 数（含当前 turn）
    max_summary_length: int = 500        # 摘要文本最大字符数
```

### 5.2 状态

```python
class ContextWindow:
    config: WindowConfig
    counter: TokenCounter
    available: int          # 计算后的可用窗口
    used: int               # 当前消息占用
    messages: list[BaseMessage]
    was_compressed: bool
```

### 5.3 核心方法

```python
def fit(
    self,
    system_prompt: SystemMessage,
    active_summary: SystemMessage | None,
    recent_blocks: list[TurnBlock],
    current_block: TurnBlock,
    tools: list[dict],
) -> list[BaseMessage]:
    """
    入口：按装配模型拼接上下文，若超窗则丢弃最老的 turn block，
    并标记需要重新生成 active_summary。
    """
    ...

def _needs_compression(self, total: int, tools: list[dict]) -> bool:
    tool_tokens = self.counter.estimate_tool_schema(tools)
    return total + tool_tokens > self.available * self.config.compression_trigger_ratio

def _drop_oldest_blocks(self, blocks: list[TurnBlock], budget: int) -> tuple[list[TurnBlock], list[TurnBlock]]:
    """
    按完整 turn block 丢弃最老的 block，直到剩余 block 的 token 总和 <= budget。
    返回：(保留的 blocks, 被丢弃的 blocks)
    """
    ...
```

### 5.4 压缩流程

```text
输入：
  system_prompt
  active_summary (可能为 None)
  recent_blocks = [block_1, block_2, ..., block_N]
  current_block
  tools

步骤：
  1. 计算总 token = system + summary + all blocks + tools
  2. 如果总 token <= trigger_compression：
       直接返回全部消息
  3. 如果超窗：
       a. 确保 current_block 不动
       b. 从 recent_blocks 最老的开始，逐个丢弃完整 block
       c. 直到剩余部分 <= trigger_compression
       d. 标记 was_compressed = True，并返回被丢弃的 blocks 列表
  4. 如果丢弃到只剩 min_recent_turn_blocks 仍超窗：
       硬截断（极少触发），记录 error 日志
```

**关键**：丢弃的单位是 `TurnBlock`，不是单条 `Message`。

---

## 6. 数据层改造

### 6.1 `messages.token_count` 填充

当前 schema 已有该字段，未使用。改造点：

- **`MySQLConversationStore._append_message`**：写入时计算 `TokenCounter.count_text(content)`，存入 `token_count`。
- **工具结果也计算**：tool result 通常很长，入库时记录 token_count 有助于后续分析哪些 tool 消耗窗口。

### 6.2 `conversations` 表扩展

```sql
ALTER TABLE conversations
    ADD COLUMN active_summary TEXT NULL AFTER metadata,
    ADD COLUMN active_summary_token_count INT NOT NULL DEFAULT 0 AFTER active_summary,
    ADD COLUMN summarized_until_message_id BIGINT UNSIGNED NULL AFTER active_summary_token_count;
```

**字段说明**：

| 字段 | 说明 |
|------|------|
| `active_summary` | 当前对话摘要文本 |
| `active_summary_token_count` | 摘要 token 数，用于快速估算 |
| `summarized_until_message_id` | 摘要已覆盖到的最新 message id，`prepare` 只加载该 id 之后的消息 |

**更新逻辑**：

```python
def update_active_summary(
    conversation_id: int,
    existing_summary: str | None,
    dropped_blocks: list[TurnBlock],
    new_summary_text: str,
    summarized_until_message_id: int,
    summary_token_count: int,
):
    """
    将被丢弃的 blocks 合并进 active_summary，更新 summarized_until_message_id。
    """
    ...
```

### 6.3 为什么不做 `conversation_summaries` 多代表

- V1 只需要一个活跃摘要，多代表是过度设计。
- 如果需要审计摘要历史，可以通过 `turns.metadata` 或事后日志实现。
- 如果未来确实需要多代摘要，再加 `conversation_summary_history` 表，不影响当前设计。

---

## 7. API Usage 双轨制

### 7.1 `app/llm/client.py` 改造

当前 `chat()` 只返回 `choices[0].message`。改造为返回 `(message, usage)`：

```python
def chat(...) -> tuple[dict[str, Any], dict[str, int]]:
    ...
    body = response.json()
    message = body["choices"][0]["message"]
    usage = body.get("usage", {})
    return message, usage
```

`usage` 格式：

```json
{
  "prompt_tokens": 12345,
  "completion_tokens": 567,
  "total_tokens": 12912
}
```

### 7.2 `react_graph.py` 审计

```python
response, usage = client.chat(llm_messages, tools=tools)

prompt_tokens = usage.get("prompt_tokens", 0)
completion_tokens = usage.get("completion_tokens", 0)
estimated = counter.count_messages(llm_messages) + counter.estimate_tool_schema(tools)

logger.info(
    "llm_usage prompt=%s completion=%s estimated=%s delta=%s",
    prompt_tokens, completion_tokens, estimated, prompt_tokens - estimated
)
```

### 7.3 偏差校正

如果多次出现 `|actual - estimated| / actual > 0.2`：
- 调整 `TokenCounter` 的 overhead 常量。
- 或切换为模型官方 tokenizer。

---

## 8. LangGraph 接入点

### 8.1 `agent_graph.py` -> `prepare` 节点

```python
def prepare(state: AgentState) -> AgentState:
    turn = store.get_turn(turn_id)
    conversation = store.get_conversation(turn.conversation_id)

    # 1. 加载该 conversation 的 messages（全量，但只用于装配，不直接发给 LLM）
    all_records = store.list_messages(turn.conversation_id)

    # 2. 按 turn_id 聚合成 turn blocks
    blocks = _records_to_turn_blocks(all_records)

    # 3. 分离 recent blocks 和当前 block
    cutoff_id = conversation.summarized_until_message_id
    recent_blocks = [b for b in blocks if b.last_message_id > cutoff_id]
    current_block = _extract_current_block(recent_blocks, turn.id)
    recent_blocks = [b for b in recent_blocks if b.turn_id != turn.id]

    # 4. 装配上下文
    assembler = ContextAssembler(counter=TokenCounter())
    fitted_messages, was_compressed, dropped_blocks = assembler.assemble(
        system_prompt=SystemMessage(content=DEFAULT_SYSTEM_PROMPT),
        active_summary=conversation.active_summary,
        recent_blocks=recent_blocks,
        current_block=current_block,
        tools=_build_tools_for_llm(),
    )

    logger.info(
        "window_fit conversation=%s blocks_before=%s blocks_after=%s tokens=%s/%s compressed=%s",
        turn.conversation_id, len(recent_blocks) + 1,
        len(_extract_blocks_from_messages(fitted_messages)),
        assembler.used, assembler.available, was_compressed,
    )

    return {
        "turn_id": turn_id,
        "conversation_id": turn.conversation_id,
        "messages": fitted_messages,
        "was_compressed": was_compressed,
        "dropped_blocks": dropped_blocks,
        "reply": "",
        "status": "running",
        "error": None,
    }
```

### 8.2 `agent_graph.py` -> `persist` 节点扩展

```python
def persist(state: AgentState) -> AgentState:
    # ... existing persist logic ...

    # 如果本次 prepare 丢弃了 blocks，更新 active_summary
    if state.get("was_compressed") and state.get("dropped_blocks"):
        dropped = state["dropped_blocks"]
        conversation = store.get_conversation(conversation_id)

        # 异步或同步生成新摘要（可用轻量模型）
        new_summary = _generate_summary(
            existing_summary=conversation.active_summary,
            dropped_blocks=dropped,
        )
        new_summary_tokens = TokenCounter().count_text(new_summary)

        last_dropped_msg_id = max(b.last_message_id for b in dropped)
        store.update_conversation_summary(
            conversation_id=conversation_id,
            active_summary=new_summary,
            active_summary_token_count=new_summary_tokens,
            summarized_until_message_id=last_dropped_msg_id,
        )

    return {...}
```

### 8.3 `react_graph.py` -> `call_llm` 节点

改造 `client.chat()` 返回 `usage`，并在日志中记录 `prompt_tokens` 与预估算的偏差。

---

## 9. 长期记忆（Phase 3，不混入窗口压缩）

长期稳定信息进入独立的长期记忆系统：

```text
Obsidian Markdown / LLMWiki / OpenSearch / memory_items
```

需要时通过检索注入 prompt，不走对话摘要逻辑。

**与对话摘要的区别**：

| | 对话摘要 | 长期记忆 |
|--|----------|----------|
| 内容 | 当前对话的近期历史 | 跨对话的持久知识 |
| 更新时机 | 窗口超限时 | 用户确认、知识库入库 |
| 存储 | `conversations.active_summary` | 独立 KB / Wiki |
| 检索 | 直接加载 | 按需向量/关键词检索 |

---

## 10. 改动范围

| 文件 | 动作 | 说明 |
|------|------|------|
| `app/llm/tokenizer.py` | **新建** | TokenCounter，tiktoken 预估算 |
| `app/agent_react/context_assembler.py` | **新建** | ContextAssembler + TurnBlock 装配与裁剪 |
| `app/agent_react/context_window.py` | **新建** | 窗口预算计算（保留，但不按单条 message 压缩） |
| `app/llm/client.py` | **修改** | `chat()` 返回 `(message, usage)` |
| `app/agent_react/react_graph.py` | **修改** | 接收 usage，记录审计日志 |
| `app/agent_react/agent_graph.py` | **修改** | `prepare` 按 turn block 装配，`persist` 更新 active_summary |
| `app/persistence/conversation_store.py` | **修改** | `_append_message` 填充 `token_count`；新增 `update_conversation_summary` |
| `app/persistence/models.py` | **修改** | `ConversationRecord` 增加 `active_summary` 等字段 |
| `scripts/mysql-init/001_v1_schema.sql` | **修改** | `conversations` 表增加摘要字段 |
| `app/config.py` | **修改** | 增加 `llm_max_context_tokens`, `llm_compression_trigger_ratio` |

---

## 11. 实施建议（分阶段）

### Phase 1：最小可用（1 天）

目标：先防住窗口溢出，按 turn block 保留最近上下文。

- 创建 `TokenCounter`。
- 改造 `client.py` 返回 usage。
- `messages.token_count` 填充。
- `prepare` 节点加载逻辑改为：
  - 按 `turn_id` 聚合成 `TurnBlock`。
  - 保留最近 N 个完整 turn block + 当前 turn。
  - 丢弃最老 block，不破坏 tool_call 配对。
- `list_messages` 先加 `limit` 兜底（如 200 条），防止极端情况。
- 记录 usage 日志。

### Phase 2：对话摘要持久化（2 天）

目标：长对话（>20 轮）保持上下文连贯。

- `conversations` 表加 `active_summary`, `active_summary_token_count`, `summarized_until_message_id`。
- `prepare` 加载逻辑：`active_summary` + `summarized_until_message_id` 之后的 blocks。
- `persist` 节点：若丢弃了 blocks，生成新摘要并更新 conversations 表。
- 摘要生成走轻量模型调用（一次 / turn）。

### Phase 3：长期记忆接入（后续迭代）

目标：跨对话记忆、个性化。

- 接入 LLMWiki / 知识库检索器。
- `ContextAssembler` 第 2 层注入检索结果。
- 与对话摘要解耦，独立维护。

---

## 12. 风险与注意事项

1. **摘要丢失细节**：active_summary 会丢弃具体代码片段、精确数字。对于 coding / data 任务，建议 `min_recent_turn_blocks` 设为 3-4（而非 2），保留更多原始上下文。
2. **工具调用历史被摘要**：如果用户问"刚才那个命令的结果是什么"，而 tool output 已被摘要，模型可能答不上来。方案：当前 turn 的所有 tool 消息必须完整保留，不可压缩。
3. **并发更新 active_summary**：两个 turn 同时更新 `conversations.active_summary` 可能产生冲突。方案：conversation 级别的乐观锁，或利用队列保证单 turn 执行。
4. **摘要费用**：每次丢弃 blocks 需要额外调用一次 LLM。可用更便宜模型，或限制摘要频率（每累计丢弃 M 个 blocks 才触发一次）。
5. **TurnBlock 边界判断**：`turn_id` 为 NULL 的消息（如系统通知、未触发 turn 的用户消息）需要特殊处理，可归入相邻 block 或单独成块。

---

## 13. 参考

- OpenAI Tokenizer: https://platform.openai.com/tokenizer
- Tiktoken: https://github.com/openai/tiktoken
- DeepSeek API 文档 - Usage 字段
- 前置设计：【2026-5-1】Jarvis 长运行多轮对话 ReAct Agent 设计.md
