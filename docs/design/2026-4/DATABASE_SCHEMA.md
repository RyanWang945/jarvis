# Database Schema

本文档描述 Jarvis 项目业务数据库（`data/business.db`，SQLite）的表结构与字段含义。

---

## 概述

项目使用 SQLite 作为业务数据库，通过 `sqlite3` 模块直接执行原生 SQL 进行 Schema 管理。数据库主要服务于 Agent 运行编排、任务工单管理、审批控制与审计追溯。

共 7 张核心表：

| 表名 | 用途 |
|------|------|
| `runs` | 记录 Agent 运行会话 |
| `tasks` | 记录运行下的具体任务 |
| `work_orders` | 记录已分派的工作工单 |
| `work_results` | 记录工单执行结果 |
| `approvals` | 记录高风险操作的审批请求 |
| `audit_logs` | 记录系统关键操作审计日志 |
| `resource_locks` | 记录资源并发锁 |

---

## 表关系

```
runs (1) ───< tasks (N) ───< work_orders (N) ───< work_results (1:1)
   │              │                │
   │              │                └─< approvals
   │              │
   └──────────────┴──────< audit_logs
```

`resource_locks` 通过 `resource_key` 与 `tasks.resource_key` 对应，用于并发控制。

---

## 1. runs（运行会话表）

记录一次 Agent 运行的会话信息。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `run_id` | TEXT | PRIMARY KEY | 运行唯一标识 |
| `thread_id` | TEXT | NOT NULL, UNIQUE | 对话/线程 ID |
| `status` | TEXT | NOT NULL | 运行状态 |
| `instruction` | TEXT | - | 用户原始指令 |
| `summary` | TEXT | - | 运行摘要/总结 |
| `created_at` | TEXT | NOT NULL, DEFAULT `datetime('now')` | 创建时间 |
| `updated_at` | TEXT | NOT NULL, DEFAULT `datetime('now')` | 更新时间 |

---

## 2. tasks（任务表）

记录一次 `run` 下的具体任务。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `task_id` | TEXT | PRIMARY KEY | 任务唯一标识 |
| `run_id` | TEXT | NOT NULL | 所属运行 ID（关联 `runs`） |
| `title` | TEXT | - | 任务标题 |
| `description` | TEXT | - | 任务描述 |
| `status` | TEXT | NOT NULL | 任务状态 |
| `resource_key` | TEXT | - | 资源标识（用于资源锁定） |
| `dod` | TEXT | - | Definition of Done，完成标准 |
| `verification_cmd` | TEXT | - | 验证命令 |
| `tool_name` | TEXT | - | 工具名称 |
| `worker_type` | TEXT | - | 执行 Worker 类型 |
| `order_id` | TEXT | - | 关联的工单 ID |
| `retry_count` | INTEGER | DEFAULT 0 | 当前重试次数 |
| `max_retries` | INTEGER | DEFAULT 0 | 最大重试次数 |
| `result_summary` | TEXT | - | 结果摘要 |
| `created_at` | TEXT | NOT NULL, DEFAULT `datetime('now')` | 创建时间 |
| `updated_at` | TEXT | NOT NULL, DEFAULT `datetime('now')` | 更新时间 |

---

## 3. work_orders（工单表）

记录已分派的具体工作指令。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `order_id` | TEXT | PRIMARY KEY | 工单唯一标识 |
| `task_id` | TEXT | NOT NULL | 关联任务 ID |
| `ca_thread_id` | TEXT | NOT NULL | Capability Agent 线程 ID |
| `capability_name` | TEXT | - | 能力名称 |
| `worker_type` | TEXT | NOT NULL | Worker 类型 |
| `provider` | TEXT | - | 服务提供商 |
| `action` | TEXT | NOT NULL | 执行动作 |
| `args` | TEXT | - | 动作参数（JSON 字符串） |
| `workdir` | TEXT | - | 工作目录 |
| `risk_level` | TEXT | NOT NULL | 风险等级 |
| `reason` | TEXT | - | 执行原因/依据 |
| `verification_cmd` | TEXT | - | 验证命令 |
| `timeout_seconds` | INTEGER | DEFAULT 30 | 超时时间（秒） |
| `status` | TEXT | NOT NULL, DEFAULT `'pending'` | 工单状态 |
| `dispatched_at` | TEXT | - | 分派时间 |
| `completed_at` | TEXT | - | 完成时间 |
| `created_at` | TEXT | NOT NULL, DEFAULT `datetime('now')` | 创建时间 |

---

## 4. work_results（工单结果表）

记录工单执行后的结果输出。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `order_id` | TEXT | PRIMARY KEY | 关联工单 ID |
| `task_id` | TEXT | NOT NULL | 关联任务 ID |
| `ca_thread_id` | TEXT | NOT NULL | Capability Agent 线程 ID |
| `worker_type` | TEXT | NOT NULL | Worker 类型 |
| `ok` | INTEGER | NOT NULL | 是否成功（0=失败，1=成功） |
| `exit_code` | INTEGER | - | 进程退出码 |
| `stdout` | TEXT | - | 标准输出内容 |
| `stderr` | TEXT | - | 标准错误内容 |
| `artifacts` | TEXT | - | 产物/附件（JSON 字符串） |
| `summary` | TEXT | - | 结果摘要 |
| `created_at` | TEXT | NOT NULL, DEFAULT `datetime('now')` | 创建时间 |

---

## 5. approvals（审批表）

记录高风险操作的审批请求。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `approval_id` | TEXT | PRIMARY KEY | 审批唯一标识 |
| `thread_id` | TEXT | NOT NULL | 所属线程 ID |
| `task_id` | TEXT | NOT NULL | 关联任务 ID |
| `order_id` | TEXT | - | 关联工单 ID |
| `action_kind` | TEXT | - | 动作类型 |
| `command` | TEXT | - | 待执行命令 |
| `risk_level` | TEXT | - | 风险等级 |
| `reason` | TEXT | - | 请求原因 |
| `status` | TEXT | NOT NULL, DEFAULT `'waiting'` | 审批状态 |
| `approved_by` | TEXT | - | 审批人 |
| `approved_at` | TEXT | - | 审批时间 |
| `created_at` | TEXT | NOT NULL, DEFAULT `datetime('now')` | 创建时间 |

---

## 6. audit_logs（审计日志表）

记录系统关键操作的审计跟踪。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `log_id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 日志自增 ID |
| `thread_id` | TEXT | NOT NULL | 线程 ID |
| `task_id` | TEXT | - | 任务 ID |
| `order_id` | TEXT | - | 工单 ID |
| `node` | TEXT | NOT NULL | 节点名称 |
| `action` | TEXT | NOT NULL | 动作名称 |
| `detail` | TEXT | - | 详情（JSON 字符串） |
| `created_at` | TEXT | NOT NULL, DEFAULT `datetime('now')` | 创建时间 |

---

## 7. resource_locks（资源锁表）

用于并发控制，防止多个线程同时操作同一资源。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `resource_key` | TEXT | PRIMARY KEY | 资源唯一标识 |
| `owner_thread_id` | TEXT | NOT NULL | 持有锁的线程 ID |
| `status` | TEXT | NOT NULL, DEFAULT `'held'` | 锁状态 |
| `acquired_at` | TEXT | NOT NULL, DEFAULT `datetime('now')` | 获取锁的时间 |
| `updated_at` | TEXT | NOT NULL, DEFAULT `datetime('now')` | 更新时间 |

---

## Schema 维护说明

- Schema 定义位于 `app/persistence/db.py` 中的 `SCHEMA` 常量。
- 启动时会通过 `init_business_db()` 自动建表，并通过 `_ensure_column()` 动态兼容新增字段（`capability_name`、`provider`、`artifacts`）。
- `runs` 表会运行去重逻辑 `_dedupe_runs()`，确保 `thread_id` 唯一。
- 索引：`idx_runs_thread_id`（`runs(thread_id)`）。
