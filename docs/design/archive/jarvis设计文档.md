# Long-run 全能型极客助理架构设计文档

版本：v1.1  
状态：架构完善稿  
目标阶段：MVP / v1.0

## 1. 系统概述

Jarvis 是一个常驻本地运行的自主 AI Agent。系统以“任务驱动、状态持久化、闭环验证、风险拦截”为核心，通过飞书接收指令和授权，通过 LangGraph 编排任务，通过本地 Skill 调用命令行、代码 Agent、GitHub 和 Obsidian。

系统设计重点：

- 支持长任务和断点恢复。
- 支持多任务并发，但对同一物理资源串行化。
- 支持高危操作人工授权。
- 支持执行结果验证和失败自修正。
- 支持任务复盘沉淀到 Obsidian。

## 2. 架构原则

1. **事件标准化**：所有外部输入先转为统一 `AgentEvent`。
2. **资源先行调度**：创建任务前先判断资源锁，避免同仓库并发写入。
3. **状态可恢复**：每个关键节点都必须 checkpoint。
4. **工具最小权限**：命令执行必须有工作目录、超时、风险等级和审计日志。
5. **人类在环**：无法确认影响范围或高风险动作必须暂停并请求授权。
6. **结果可验证**：任务不是“执行过”即完成，而是通过 DoD 验证后完成。

## 3. 整体架构

```text
Feishu / Cron / File Watcher
          |
          v
Event Gateway -> AgentEvent
          |
          v
Session Dispatcher -> Resource Lock -> Thread Mapping
          |
          v
LangGraph Core
  - Contextualizer
  - Planner
  - Action Router
  - Human Approval
  - Executor
  - Verifier
  - Memory Compressor
          |
          v
Skill Execution Layer
  - Shell Skill
  - Coder Skill
  - GitHub Skill
  - Obsidian Skill
  - Feishu Skill
          |
          v
Persistence
  - SQLite Checkpoint
  - Task DB
  - Audit Log
  - Obsidian Vault
  - Vector Store
```

## 4. 架构分层

### 4.1 事件网关层

负责接收外部刺激并转为标准内部事件。

| 组件 | 职责 | MVP |
| --- | --- | --- |
| FastAPI Webhook | 接收飞书消息和卡片回调 | 是 |
| APScheduler | 管理定时任务 | 是 |
| File Watcher | 监听本地目录变化 | 否 |
| Event Bus | 将输入封装为 `AgentEvent` | 是 |

`AgentEvent` 建议字段：

```python
from typing import Any, Literal, TypedDict

class AgentEvent(TypedDict):
    event_id: str
    event_type: Literal["message", "approval", "schedule", "file_change", "system"]
    source: str
    user_id: str | None
    thread_id: str | None
    timestamp: str
    payload: dict[str, Any]
```

### 4.2 会话与资源调度层

负责判断新事件应该进入已有会话，还是创建新会话。

核心职责：

- 解析任务涉及的物理资源，例如仓库路径、Obsidian 路径、部署环境。
- 维护 `resource_key -> thread_id` 映射。
- 同一资源写操作串行排队。
- 无资源冲突的任务并发执行。
- 对运行中任务支持插队事件注入。

资源锁建议字段：

| 字段 | 说明 |
| --- | --- |
| `resource_key` | 规范化后的资源标识，例如仓库绝对路径 |
| `thread_id` | 当前占用该资源的 LangGraph 会话 |
| `mode` | `read` 或 `write` |
| `owner_event_id` | 加锁来源事件 |
| `created_at` | 加锁时间 |
| `expires_at` | 锁超时兜底 |

锁策略：

- 同一仓库写任务：排队或注入当前 `thread_id`。
- 同一仓库只读调研：允许并发，但不得执行写命令。
- 不同仓库写任务：允许并发。
- Critical 风险动作：即使资源空闲，也必须先授权。

### 4.3 认知编排层

基于 LangGraph 实现任务生命周期。

| 节点 | 输入 | 输出 | 说明 |
| --- | --- | --- | --- |
| Contextualizer | `AgentEvent` | 上下文包 | 从 SQLite、Obsidian、当前仓库提取背景 |
| Planner | 上下文包 | `task_list` | 生成带 DoD 的子任务 |
| Action Router | 当前任务 | 风险决策 | 判断是否可执行或需要授权 |
| Human Approval | 风险动作 | 授权结果 | 使用 interrupt 挂起并等待飞书回调 |
| Executor | 可执行任务 | 执行结果 | 调用具体 Skill |
| Verifier | 执行结果 | 验证结果 | 执行 DoD 并判断是否完成 |
| Self-Heal | 失败结果 | 修复计划 | 失败后重试或重新规划 |
| Memory Compressor | 任务历史 | 阶段摘要 | 控制上下文长度并写复盘 |

推荐状态机：

```text
created -> planning -> running -> verifying -> completed
                         |             |
                         v             v
                      waiting       repairing
                         |             |
                         v             v
                      blocked <---- failed
```

### 4.4 技能执行层

所有外部能力都封装为 Skill，避免 Planner 直接拼接复杂执行细节。

| Skill | 能力 | 风险控制 |
| --- | --- | --- |
| Shell Skill | 执行本地命令 | 工作目录、超时、输出截断、风险分级 |
| Coder Skill | 调用 Claude Code / Codex 修改代码 | 限定仓库目录，变更后必须验证 |
| GitHub Skill | Issue、PR、分支操作 | 写操作需记录审计，必要时授权 |
| Obsidian Skill | 读写 Markdown 笔记 | 限定 Vault 目录 |
| Feishu Skill | 消息、卡片、文件上传 | 失败重试和幂等发送 |

Skill 统一接口建议：

```python
from typing import Any, Literal, TypedDict

class SkillRequest(TypedDict):
    skill: str
    action: str
    workdir: str | None
    args: dict[str, Any]
    risk_level: Literal["low", "medium", "high", "critical"]
    timeout_seconds: int

class SkillResult(TypedDict):
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    artifacts: list[str]
    summary: str
```

### 4.5 记忆与持久化层

| 存储 | 用途 | MVP |
| --- | --- | --- |
| SQLite Checkpoint | LangGraph 状态恢复 | 是 |
| SQLite Task DB | 任务、锁、授权、审计记录 | 是 |
| Obsidian Vault | 长期复盘和知识沉淀 | 是 |
| ChromaDB | 向量检索和 RAG | 否 |

建议将 checkpoint 和业务任务表分开。checkpoint 解决 LangGraph 恢复，业务表解决查询、审计和飞书状态展示。

## 5. 核心数据结构

### 5.1 Task

```python
from typing import Literal, TypedDict

class Task(TypedDict):
    id: str
    parent_id: str | None
    title: str
    description: str
    resource_key: str | None
    status: Literal["pending", "running", "waiting", "success", "failed", "blocked", "cancelled"]
    dod: str
    verification_cmd: str | None
    retry_count: int
    max_retries: int
```

### 5.2 AgentState

```python
from typing import Annotated, Any, Literal, TypedDict
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    thread_id: str
    messages: Annotated[list, add_messages]
    event: dict[str, Any]
    resource_key: str | None
    task_list: list[Task]
    current_task_id: str | None
    status: Literal["created", "planning", "running", "waiting", "blocked", "completed", "failed"]
    auth_pending: bool
    pending_approval_id: str | None
    error_count: int
    context_summary: str | None
```

### 5.3 ApprovalRequest

```python
from typing import Literal, TypedDict

class ApprovalRequest(TypedDict):
    approval_id: str
    thread_id: str
    task_id: str
    command: str
    workdir: str
    risk_level: Literal["high", "critical"]
    reason: str
    status: Literal["pending", "approved", "rejected", "expired"]
    created_at: str
    expires_at: str | None
```

## 6. 关键工作流

### 6.1 同仓库插队

1. `thread_A` 正在修改 `IPOAgent` 仓库。
2. 用户发送“顺便把这个 API 的入参校验也加上”。
3. Dispatcher 识别目标资源仍是 `IPOAgent`。
4. 不创建新写任务线程，将事件注入 `thread_A`。
5. Planner 将新需求追加或插入 `task_list`。
6. 当前安全点之后继续执行。

### 6.2 高危命令授权

1. Executor 准备执行 `git push origin main`。
2. Action Router 将其判定为 High 或 Critical。
3. LangGraph 保存 checkpoint。
4. Feishu Skill 发送授权卡片。
5. 用户点击允许后，Webhook 携带 `approval_id` 和 `thread_id` 唤醒任务。
6. Executor 执行命令并写入审计日志。

### 6.3 断点恢复

1. 进程启动时读取未完成线程。
2. 对 `running` 状态任务执行恢复检查。
3. 对 `waiting` 且存在授权请求的任务重新发送或刷新卡片。
4. 从最后成功 checkpoint 继续。
5. 恢复后向飞书发送状态摘要。

## 7. 安全策略

### 7.1 默认高危命令

以下操作默认需要人工授权：

- `rm -rf` 或递归删除。
- `git push`，尤其是 `--force`。
- 生产部署命令。
- 修改全局配置，例如 `git config --global`。
- 安装全局依赖。
- 写入资源锁范围外的目录。

### 7.2 命令执行要求

每次命令执行都必须记录：

- 命令文本。
- 工作目录。
- 风险等级。
- 触发任务。
- 开始和结束时间。
- 退出码。
- 标准输出和错误输出摘要。

## 8. 技术栈

| 类别 | 方案 |
| --- | --- |
| 编排框架 | LangGraph |
| Web 网关 | FastAPI + Uvicorn |
| 定时任务 | APScheduler |
| 状态持久化 | SQLite + langgraph-checkpoint-sqlite |
| 长期知识库 | Obsidian Markdown |
| 向量检索 | ChromaDB，MVP 可暂缓 |
| 代码 Agent | Claude Code CLI / Codex CLI |
| 代码仓库 | Git CLI / GitHub API |
| 通知入口 | 飞书 OpenAPI |

## 9. MVP 实施顺序

1. 建立 FastAPI Webhook 和飞书消息解析。
2. 定义 `AgentEvent`、`Task`、`AgentState`。
3. 接入 LangGraph 基础流程和 SQLite checkpoint。
4. 实现 Shell Skill、Feishu Skill、Obsidian Skill。
5. 实现资源锁和线程映射。
6. 实现高危命令识别和授权卡片。
7. 实现任务状态查询、取消和恢复。
8. 接入 Coder Skill。
9. 补充审计日志和复盘写入。

## 10. 主要风险与改进建议

| 风险 | 影响 | 建议 |
| --- | --- | --- |
| 本地命令无沙箱 | 误删或污染环境 | MVP 必须强制工作目录限制和高危授权 |
| 长任务恢复重复执行 | 可能重复 push、部署或删除 | 外部副作用动作必须记录幂等键 |
| Planner 误判资源 | 可能并发修改同仓库 | Router 必须优先解析仓库路径，不确定时串行 |
| 飞书回调丢失 | 授权任务长期挂起 | 支持状态查询、重新发送授权卡片 |
| Token 膨胀 | 成本和稳定性问题 | 每轮任务后压缩摘要，原始日志落盘 |
| 第三方 CLI 不稳定 | 任务失败或卡死 | 统一超时、重试、输出截断和失败归因 |

