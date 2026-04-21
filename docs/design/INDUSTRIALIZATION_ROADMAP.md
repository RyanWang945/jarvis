# Jarvis 工业化路线图（2026-04-21）

版本：v1.0
状态：行动建议
日期：2026-04-21
目标：在保持"个人 AI 助手"定位的前提下，将工程标准提升到可信赖、可维护、可长期演进的工业级基线。

---

## 1. 核心原则

本项目是**个人 AI 助手**，不是企业 SaaS。但因为有 AI 参与开发，工程标准反而应当更高：

1. **AI 友好**：代码必须能被 AI 稳定理解、重构和扩展。这意味着类型注解、清晰的分层、可复用的 fixture。
2. **单人长期维护**：假设未来只有你和 AI 维护这个项目，自动化程度必须足够高，避免手动回归和部署。
3. **本地优先，但云就绪**：默认本地 SQLite + 单进程，但架构上不阻塞未来容器化和多实例。
4. **文档即契约**：设计文档已经很强，工业化文档补充的是"如何构建、如何部署、如何排障"。

---

## 2. 现状速览

| 维度 | 当前状态 | 工业级差距 |
|------|---------|-----------|
| 架构设计 | 强（CA Agent + Worker + Capability） | 小 |
| 代码组织 | 中等（分包清晰，但 `nodes.py` 51KB） | 中 |
| 测试 | 64 测试 / 1 文件，1 个失败，无覆盖率 | **大** |
| CI/CD | 无 | **大** |
| 代码质量门禁 | 无（无 black/ruff/mypy） | **大** |
| 可观测性 | 纯文本日志 | **大** |
| 部署 | 无 Docker，无容器化 | **大** |
| 安全 | 风险审批有，API 开放 | 中 |
| 数据库 | SQLite + 手写 schema | 中 |

---

## 3. 路线图（从易到难）

### Phase 1：工程基线（立即开始，1-2 周）

**目标**：让代码质量、测试和自动化达到"可放心让 AI 重构"的水平。

| 编号 | 任务 | 产出 | 优先级 | 预估难度 |
|------|------|------|--------|---------|
| P1-1 | 配置 pre-commit | `.pre-commit-config.yaml`（black, ruff, mypy） | P0 | 易 |
| P1-2 | 配置 GitHub Actions CI | `.github/workflows/ci.yml`（lint + test） | P0 | 易 |
| P1-3 | 配置 pytest + coverage | `pytest.ini` 或 `pyproject.toml` 中 pytest 段，`pytest-cov` | P0 | 易 |
| P1-4 | 修复当前失败测试 | `test_cli_complex_coder_feature_task_against_real_nltk_workspace` 通过 | P0 | 易 |
| P1-5 | 统一代码风格 | 运行 `ruff check --fix` 和 `black .`，一次性格式化全库 | P0 | 易 |
| P1-6 | 补充 type hints | 对 `app/agent/nodes.py`、`app/llm/jarvis.py` 等核心文件补全类型 | P1 | 中 |

**验收标准**：

- `git commit` 时自动跑 lint，不通过则阻止提交。
- PR / push 时 CI 自动跑测试，全绿才允许合并。
- `pytest --cov=app` 能输出覆盖率报告，基线目标 **>= 70%**。
- 当前 1 个失败测试修复并回归通过。

**AI 辅助建议**：

- 用 AI 生成 `.pre-commit-config.yaml` 和 `.github/workflows/ci.yml` 初稿。
- 用 AI 批量给核心模块补 type hints，人工 review 边界 case。

---

### Phase 2：测试重构（Phase 1 完成后，1-2 周）

**目标**：测试从"能跑"变成"可维护、可信赖"。

| 编号 | 任务 | 产出 | 优先级 | 预估难度 |
|------|------|------|--------|---------|
| P2-1 | 拆分测试文件 | `tests/unit/`、`tests/integration/`、`tests/e2e/` | P0 | 中 |
| P2-2 | 建立 `conftest.py` | 全局 fixtures（mock LLM client、mock Coder skill、temp DB） | P0 | 中 |
| P2-3 | 抽象外部依赖 mock | `tests/fixtures/` 提供 `FakeDeepSeekClient`、`FakeTavilySkill` | P0 | 中 |
| P2-4 | 增加负面测试 | 测试失败路径：approval reject、worker timeout、LLM 返回非法 JSON | P1 | 中 |
| P2-5 | 增加边界测试 | 空 instruction、超长 stdout、并发 resource lock 冲突 | P1 | 中 |

**验收标准**：

- `tests/test_agent.py` 拆分为多个文件，单文件不超过 500 行。
- 新增测试不再写 inline monkeypatch，优先用 fixture。
- 负面测试覆盖率达到核心失败路径的 **80%**。

**AI 辅助建议**：

- 让 AI 分析 `test_agent.py` 中重复的 mock 模式，抽象成 fixtures。
- 让 AI 基于 `TODO_REVIEW_FINDINGS.md` 中的风险点，生成对应的负面测试用例。

---

### Phase 3：可观测性基础（Phase 2 完成后，1-2 周）

**目标**：系统运行时状态可见，问题可定位。

| 编号 | 任务 | 产出 | 优先级 | 预估难度 |
|------|------|------|--------|---------|
| P3-1 | 结构化日志 | `app/logging_config.py` 支持 JSON 格式 + correlation ID | P0 | 中 |
| P3-2 | FastAPI 请求追踪 | 中间件注入 `request_id`，全链路携带 | P0 | 中 |
| P3-3 | Prometheus metrics | `prometheus-fastapi-instrumentator`，暴露 `/metrics` | P0 | 中 |
| P3-4 | 业务指标 | 自定义 metrics：task_count、approval_latency、worker_duration | P1 | 中 |
| P3-5 | 健康探针分离 | `/health`、`/ready`（依赖检查）、`/live` | P0 | 易 |
| P3-6 | 错误响应标准化 | 统一异常处理中间件，返回 `{error_code, message, request_id}` | P1 | 中 |

**验收标准**：

- 每条日志带 `request_id` 和 `timestamp`，可用 `jq` 解析。
- `/metrics` 能输出 HTTP 请求 QPS、延迟百分位。
- `/ready` 检查 SQLite 和 LLM provider 可达性，不可达返回 503。
- API 错误响应格式统一，客户端能通过 `request_id` 追溯日志。

**AI 辅助建议**：

- 用 AI 生成 `app/middleware/` 下的请求追踪和错误处理中间件。
- 让 AI 基于现有 `AgentState` 字段，建议需要暴露的业务指标。

---

### Phase 4：容器化与部署（Phase 3 完成后，1 周）

**目标**：项目不再只能本地运行，可以一键启动。

| 编号 | 任务 | 产出 | 优先级 | 预估难度 |
|------|------|------|--------|---------|
| P4-1 | 根级 Dockerfile | 多阶段构建（builder + runtime），Python 3.14 slim | P0 | 中 |
| P4-2 | docker-compose.yml | app + 可选 Prometheus + Grafana | P0 | 中 |
| P4-3 | .dockerignore | 排除 `.venv`、`.env`、`logs/`、`data/` | P0 | 易 |
| P4-4 | 容器健康检查 | Dockerfile `HEALTHCHECK` 调用 `/live` | P0 | 易 |
| P4-5 | 版本管理 | `bump-my-version` 或 `cz bump`，语义化版本 + changelog | P1 | 易 |

**验收标准**：

- `docker compose up` 能直接拉起服务，CLI 和 API 均可用。
- 镜像体积控制到 **< 300MB**（多阶段构建）。
- `git tag v0.2.0` 触发 CI 自动构建并推送镜像到 GitHub Packages / Docker Hub。

**AI 辅助建议**：

- 让 AI 写 Dockerfile 初稿，人工调整 UV 和 Hatchling 的构建步骤。
- 用 AI 生成 `docker-compose.yml` 和简单的 Grafana dashboard JSON。

---

### Phase 5：数据持久化升级（Phase 4 完成后，1-2 周）

**目标**：数据库从"够用"变成"可演进、可信赖"。

| 编号 | 任务 | 产出 | 优先级 | 预估难度 |
|------|------|------|--------|---------|
| P5-1 | 引入 Alembic | `alembic init`，将现有 schema 转为初始 migration | P0 | 中 |
| P5-2 | 连接池管理 | `sqlite:///...` 升级为 `create_engine(pool_size=...)` | P1 | 中 |
| P5-3 | 数据备份策略 | `app/persistence/backup.py` 提供 SQLite 备份到 `data/backups/` | P1 | 中 |
| P5-4 | DB 健康指标 | `/ready` 检查 DB 连接延迟和 WAL 模式 | P1 | 易 |

**验收标准**：

- `alembic upgrade head` 能在新环境创建完整 schema。
- `alembic revision --autogenerate` 能捕获模型变更。
- 每日自动备份 SQLite 到 `data/backups/`（保留 7 天）。

**AI 辅助建议**：

- 让 AI 对比现有 `db.py` 的 schema，生成 Alembic 初始 migration 脚本。
- 用 AI 写 SQLite 备份和轮转逻辑。

---

### Phase 6：安全与可靠性（Phase 5 完成后，2-3 周）

**目标**：系统可以暴露到内网或公网，不担心被滥用或误用。

| 编号 | 任务 | 产出 | 优先级 | 预估难度 |
|------|------|------|--------|---------|
| P6-1 | API Token 认证 | `Authorization: Bearer <token>`，配置 `JARVIS_API_TOKEN` | P0 | 中 |
| P6-2 | CLI 本地认证豁免 | CLI 通过本地文件 socket 或预共享密钥绕过 token | P0 | 中 |
| P6-3 | Rate Limiting | `slowapi` 或 `fastapi-limiter`，按 IP / token 限流 | P1 | 中 |
| P6-4 | Secret 管理增强 | `.env` 保留，但增加 `JARVIS_SECRETS_DIR` 支持分文件加载 | P1 | 易 |
| P6-5 | 依赖安全扫描 | CI 中增加 `pip-audit` 或 `safety check` | P1 | 易 |
| P6-6 | Worker 超时取消 | `ThreadWorkerClient` 支持 `future.cancel()` 和 `timeout` 强制终止 | P0 | 难 |
| P6-7 | 输入校验加固 | Pydantic 模型增加字段长度限制、命令字符白名单 | P1 | 中 |

**验收标准**：

- 无 `Authorization` header 的请求返回 `401 Unauthorized`。
- 高频请求（如 100 次/分钟）被限流返回 `429`。
- `pip-audit` 在 CI 中执行，高危漏洞阻塞合并。
- Worker 超时后强制终止，不泄漏子进程。

**AI 辅助建议**：

- 让 AI 生成 FastAPI 的 Bearer Token 依赖和豁免逻辑。
- 让 AI 分析 `shell.py` 和 `coder.py` 的输入边界，生成校验规则。

---

### Phase 7：高级可观测性（可选，2-3 周）

**目标**：多步骤任务和 LangGraph 执行链路完全可视。

| 编号 | 任务 | 产出 | 优先级 | 预估难度 |
|------|------|------|--------|---------|
| P7-1 | OpenTelemetry Tracing | `opentelemetry-instrumentation-fastapi`，追踪请求到 Worker | P1 | 难 |
| P7-2 | LangGraph 节点指标 | 每个节点（strategize / dispatch / monitor）耗时和状态 | P1 | 难 |
| P7-3 | 结构化报告导出 | `/agent/runs/{id}/report` 支持 JSON + Markdown + PDF | P2 | 中 |
| P7-4 | 性能基准测试 | `tests/benchmark/` 提供任务端到端延迟基准 | P2 | 中 |

**验收标准**：

- Jaeger / Zipkin 能看到一条请求从 API -> CA Agent -> Worker 的完整 trace。
- Grafana dashboard 展示各 LangGraph 节点的平均耗时和错误率。

---

## 4. 推荐执行顺序

```text
Phase 1（工程基线）
    -> Phase 2（测试重构）
        -> Phase 3（可观测性）
            -> Phase 4（容器化）
                -> Phase 5（数据升级）
                    -> Phase 6（安全可靠性）
                        -> Phase 7（高级可观测性，可选）
```

**关键路径**：

1. Phase 1 必须先做。没有代码质量门禁和 CI，后续所有改动都缺乏安全网。
2. Phase 2 紧接 Phase 1。测试是重构的前提。
3. Phase 3 和 Phase 4 可并行。容器化和日志/metrics 互不阻塞。
4. Phase 6 的 Worker 超时取消依赖 Phase 4（Thread Worker 稳定）和 Phase 2（超时路径的测试）。

---

## 5. 与现有 MVP_PLAN.md 的衔接

| 现有计划 | 本路线补充 |
|---------|-----------|
| P1-M4 `risk_gate` 独立节点 | Phase 6 的输入校验和超时取消提供支撑 |
| P2-M6 Worker 超时取消 | Phase 6 明确列入并给出验收标准 |
| P2-M7 同资源写任务串行 | Phase 5 的 DB 升级和 Phase 3 的 metrics 提供观测能力 |
| P3-M10 飞书集成 | Phase 6 的 API Token 和 Rate Limit 是飞书暴露到公网的前置条件 |
| P3-M11 长期记忆 | Phase 5 的 Alembic 让 schema 演进不再痛苦 |

---

## 6. 完成定义

达到"个人 AI 助手工业级基线"时，应满足：

1. **任何人克隆项目后，5 分钟内能跑通测试和启动服务**（`docker compose up` 或 `uv run jarvis`）。
2. **AI 辅助重构时，不会破坏现有功能**（CI 全绿 + 覆盖率不下降）。
3. **运行时状态可见**（日志可检索、metrics 可查询、错误可追踪）。
4. **数据不丢**（定时备份、schema 可迁移）。
5. **暴露到内网不担心**（有认证、有限流、有审批）。
6. **Worker 不会失控**（超时会被杀、子进程不泄漏）。

---

## 7. 下一步行动

建议立即执行的 3 件事：

1. **配置 pre-commit**：`pip install pre-commit && pre-commit install`，添加 black + ruff + mypy。
2. **创建 CI workflow**：`.github/workflows/ci.yml`，跑 `ruff check`、`mypy app`、`pytest --cov`。
3. **修复当前失败测试**：调查 `test_cli_complex_coder_feature_task_against_real_nltk_workspace` 的 `blocked` vs `waiting_approval` 原因。

这三件事不需要架构改动，1 天内可完成，但能立刻建立工程安全网。
