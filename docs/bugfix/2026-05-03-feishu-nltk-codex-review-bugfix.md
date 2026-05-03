# 2026-05-03 Feishu + Codex Review Bugfix 记录

## 背景

用户在飞书输入：

```text
review 下nltk项目的代码
```

预期行为：

- Jarvis 识别为 coding turn。
- 选择已注册仓库 `nltk`。
- 调用 `delegate_to_codex(repo_id="nltk")`。
- Codex 完成仓库 review 后，Jarvis 将 review 结果更新到飞书 thinking card。
- 同一条飞书消息只触发一次执行、一次 thinking card、一次最终更新。

实际行为出现过多种异常：

- Jarvis 尝试用 `shell_inspect` / `shell_run_command` 访问 `G:\pycharm-project\nltk`，被 workspace 边界拒绝。
- Codex 已经产出结果，但 Jarvis 显示 `Codex completed without a parseable final message`。
- 下一轮模型请求失败：`assistant message with tool_calls must be followed by tool messages`。
- 同一条飞书 event 被重复处理，发送多张 thinking card，并发启动多个 Codex run。
- Codex 返回 review 结果后，Jarvis 又让主模型继续规划，继续追加 Codex 调用，没有及时返回最终 review。
- 最终回复里暴露了 `[JARVIS_PREFLIGHT]`、`[JARVIS_POSTFLIGHT]`、`[JARVIS_PERMISSION_CHECK]` 等内部审计块，对飞书用户造成干扰。

## 根因

### 1. Coding policy 暴露了不适合外部仓库的 shell 工具

`nltk` 是注册在 `G:\pycharm-project\nltk` 的外部仓库。Jarvis 自身的 shell 工具只允许在 Jarvis workspace 内部执行。

旧 coding policy 同时暴露：

- `shell_inspect`
- `shell_run_command`
- `delegate_to_codex`

主模型会先尝试 shell 工具，导致外部仓库访问被拒绝，之后进入“换工具重试”的循环。

### 2. Codex JSONL 解析不兼容当前事件格式

当前 Codex CLI 的最终消息格式是嵌套事件：

```json
{"type":"item.completed","item":{"type":"agent_message","text":"..."}}
```

旧解析器只看顶层 `type/content/text/message`，没有递归解析 `item`，因此误判为没有最终消息。

### 3. 跨 turn 复用了历史 tool protocol

数据库里保存了上一轮 assistant `tool_calls` 和 tool message。下一轮构造 LLM 上下文时，旧逻辑会把这些历史工具协议原样恢复给模型。

如果上一轮失败、截断，或工具调用序列不完整，就会让 DeepSeek/OpenAI 兼容接口报错：

```text
An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'.
```

工具调用协议应该是 turn-local 的，不应该跨 turn 作为原始协议消息重放。

### 4. 飞书重复 event 没有跨 clear generation 幂等

飞书同一条消息可能被 websocket 重放。日志中同一 `message_id` 被收到两次。

旧去重逻辑只按当前 `conversation_id + external_message_id` 查重。执行 `/clear` 后会产生新的 conversation generation，同一条飞书消息被重放时，当前会话查不到旧消息，于是重复创建/复用 turn 并再次发送 thinking card。

### 5. Coding turn 在 Codex 返回后还继续 ReAct

Codex 已经返回了仓库 review 文本，但 Jarvis 又把 tool result 交给主模型继续调用。主模型可能认为需要“继续检查更多文件”，于是发起第二次、第三次 `delegate_to_codex`。

对于当前轻量 coding 模式，Codex 是实际执行单元。一次明确的 Codex 结果应该可以作为 turn 的最终结果返回，避免主流程继续扩展。

## 修复内容

### 1. Coding policy 只暴露 Codex

文件：

- `app/agent_react/runtime_policy.py`
- `tests/test_runtime_policy.py`
- `tests/test_tools_codex.py`

修复：

- coding turn 的 allowed tools 改为只包含 `delegate_to_codex`。
- 明确不再向 coding turn 注入 `shell_inspect` / `shell_run_command`。
- runtime prompt 中强调优先使用 `repo_id`，不要猜未注册 workdir。

效果：

- 外部仓库任务不再走 Jarvis workspace shell。
- 仓库读写统一委托给 Codex，在 Codex 自己的 sandbox/workdir 中完成。

### 2. Codex JSONL 递归解析 nested item

文件：

- `app/tools/codex.py`
- `tests/test_tools_codex.py`

修复：

- `_extract_event_text()` 支持递归读取 `event["item"]`。
- 新增测试覆盖 `item.completed -> item.agent_message -> text`。

效果：

- Codex 输出不再误判为 `without a parseable final message`。
- 真实 `codex-events.jsonl` 已验证能解析出 review 文本。

### 3. 历史上下文剥离 tool protocol

文件：

- `app/agent_react/context_manager.py`
- `tests/test_context_manager.py`

修复：

- 从数据库恢复历史消息时，丢弃历史 `ToolMessage`。
- 对历史 `AIMessage` 去掉 `tool_calls`，只保留可读文本。
- 过滤 DeepSeek 偶发输出的 raw DSML tool markup。

效果：

- 下一轮对话不会被上一轮半截 tool_calls 污染。
- 避免 LLM API 400：`tool_calls must be followed by tool messages`。

### 4. 飞书重复消息跨会话幂等

文件：

- `app/persistence/conversation_store.py`
- `app/api/agent.py`
- `tests/test_conversation_runtime.py`

修复：

- MySQL store 的重复消息查询从 `conversation_id + external_message_id` 改为：

```text
platform + external_chat_id + external_message_id
```

- InMemory store 同步实现同样语义。
- duplicate ingest 统一返回 `should_respond=False`。
- 新增测试：`/clear` 之后飞书重放旧 external message，不再触发新 turn。

效果：

- 同一条飞书消息被重放时不会再发第二张 thinking card。
- 不会并发启动多个相同 turn / Codex run。

### 5. Coding mode 下 Codex 返回即完成 turn

文件：

- `app/agent_react/runtime.py`
- `tests/test_tools_coder.py`

修复：

- 在 `runtime_policy.mode == "coding"` 时，`delegate_to_codex` 返回非空 tool result 后，直接追加为最终 assistant message 并完成 turn。
- 不再让主模型基于 Codex 结果继续二次规划。
- 测试确认 explicit code request 只调用一次 LLM，Codex 输出直接成为最终 reply。

效果：

- Codex review 完成后直接回传飞书。
- 避免主模型继续发起“再读文件”“再检查结构”等额外 Codex 调用。

### 6. 用户回复隐藏 Jarvis 内部审计块

文件：

- `app/tools/codex.py`
- `tests/test_tools_codex.py`

修复：

- `delegate_to_codex` 的 `stdout` 只返回 Codex 最终正文。
- `[JARVIS_PREFLIGHT]`、`[JARVIS_POSTFLIGHT]`、`[JARVIS_PERMISSION_CHECK]`、`[JARVIS_PERMISSION_WARNINGS]` 等内部审计信息写入 `jarvis-audit.log` artifact。
- 保留 `codex-events.jsonl` 和 `jarvis-audit.log` 供调试，不再展示给飞书普通用户。

效果：

- 飞书最终消息更干净，只包含 review 结果。
- 调试时仍可从 artifacts 追踪仓库状态、权限检查、preflight/postflight。

## 验证

执行过的回归测试：

```text
python -m pytest tests/test_conversation_runtime.py tests/test_tools_coder.py tests/test_feishu_channel.py tests/test_context_manager.py tests/test_tools_codex.py tests/test_runtime_policy.py -q
```

结果：

```text
46 passed, 1 warning
```

真实日志验证：

- `review 下nltk项目的代码` 已正确触发 `delegate_to_codex(repo_id="nltk")`。
- Codex 已能输出 `Repository Overview` 等 review 内容。
- 最新修复后，重复飞书 event 应进入 duplicate 分支，不再发送第二张 thinking card。
- 最新修复后，coding turn 应在 Codex 返回后直接完成，不再继续追加 Codex 调用。
- 最新修复后，飞书用户不再看到 `[JARVIS_*]` 审计块；审计信息保存在 `jarvis_audit` artifact 中。

## 重启与复测步骤

需要重启 `uv` 启动的 Jarvis 进程，让代码生效。

建议复测：

```text
/clear
review 下nltk项目的代码
```

预期：

- 只发送一张 thinking card。
- 只启动一次 agent run。
- 只调用一次主要 Codex review。
- Codex 完成后 thinking card 被更新为 review 结果。

## 后续注意

- 飞书 websocket event 可能重复投递，入口必须长期保持幂等。
- tool protocol 只应在当前 turn 内对模型可见，跨 turn 应保存为审计数据，不应原样恢复为 LLM tool 消息。
- 对 coding mode，当前轻量方案是“Conversation 负责路由，Codex 负责执行”。除非后续引入更强的 coder planner，否则不要让主 ReAct 在 Codex 结果后继续无限扩展。
- `nltk` 当前注册仓库实际代码结构是 `greetings` 示例项目，不是 upstream NLTK 源码；review 结果里出现这一点是符合当前仓库现状的。
