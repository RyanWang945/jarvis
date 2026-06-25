# 【2026-06-23】Jarvis OpenTelemetry 少侵入接入与本地观测方案

本文档给出 Jarvis 接入 OpenTelemetry 的一版接近生产、低侵入、可本地验证的方案，重点回答以下问题：

1. 如何以少侵入方式接入 OpenTelemetry
2. Collector 如何启动
3. 如何在本地网页上看到一个请求的 span 以及中间发生了什么
4. `coder runtime` 如何处理

本文档基于当前仓库结构：

- HTTP 入口：[app/main.py](E:/pythonProject/jarvis/app/main.py)
- Turn 主执行链路：[app/task_runtime/agent_runtime.py](E:/pythonProject/jarvis/app/task_runtime/agent_runtime.py)
- 节点执行器：[app/task_runtime/node_executor.py](E:/pythonProject/jarvis/app/task_runtime/node_executor.py)
- LLM / React / Coder runtime：[app/task_runtime/node_execute_runtime.py](E:/pythonProject/jarvis/app/task_runtime/node_execute_runtime.py)
- LLM HTTP client：[app/llm/client.py](E:/pythonProject/jarvis/app/llm/client.py)
- MySQL store：[app/persistence/conversation_store.py](E:/pythonProject/jarvis/app/persistence/conversation_store.py)
- 进度事件总线：[app/progress.py](E:/pythonProject/jarvis/app/progress.py)
- Codex coder provider：[app/task_runtime/coder_provider.py](E:/pythonProject/jarvis/app/task_runtime/coder_provider.py)
- Codex tool / app server：[app/tools/codex.py](E:/pythonProject/jarvis/app/tools/codex.py), [app/tools/codex_app_server.py](E:/pythonProject/jarvis/app/tools/codex_app_server.py)

---

## 1. 目标与原则

这次接入的目标不是“把日志搬到 OTel”，而是建立一条稳定的请求级执行链路：

- 从一次 HTTP / 飞书消息进入开始
- 到 turn 规划、节点执行、LLM 调用、tool 调用、coder provider 执行、聚合回复结束
- 每一层都能在 trace UI 里看到
- trace 能和现有 `conversation_id / turn_id / node_id / tool_call_id` 对上

原则：

- 少侵入：优先自动埋点，业务代码只在稳定边界上手工埋点
- 生产可用：默认 OTLP -> Collector，不直接把应用绑死到某个后端
- 可本地验证：本地 `docker compose up` 后可在网页查看单请求 trace
- 面向后续评测：attributes 命名和 trace 结构要支持后面做 trace-to-eval

---

## 2. 推荐总体架构

本地和生产统一采用下面的拓扑：

```text
Jarvis app
  -> OTLP exporter
  -> OpenTelemetry Collector
  -> Jaeger UI（本地查看 traces）

后续生产可替换为：
  -> Tempo / Jaeger / SigNoz / Vendor APM
```

理由：

- 应用只关心 OTLP
- Collector 负责路由、批处理、采样、脱敏、导出
- 本地可以先用 Jaeger all-in-one，成本最低

---

## 3. 本次建议采集什么

第一阶段只做 traces，metrics/logs 先不强求。目标不是把所有内部状态都塞进 trace，而是让本地能直接看懂“一次请求走了哪条链路，卡在哪一步”。

### 3.1 第一阶段只保留 5 类 span

建议第一阶段只保留下面 5 类 span：

```text
HTTP POST /messages or /turns/{id}/run
  turn.run
    node.execute
      llm.call
      tool.call
    node.execute
      coder.run
```

说明：

- `turn.run`：整次 turn 的总耗时
- `node.execute`：一个 plan node 的执行耗时
- `llm.call`：一次模型调用
- `tool.call`：一次普通工具调用
- `coder.run`：一次 coder provider 执行，单独保留是因为它最重、最慢、最容易卡审批

第一阶段先不拆：

- `turn.plan`
- `turn.aggregate`
- `turn.persist`
- `turn.delivery`
- `llm.step`
- `coder.provider.approval` 子 span

原因很简单：这些会让第一眼的 trace 过密，不利于本地排障。

### 3.2 第一阶段保留的 attributes

统一保留一套 `jarvis.*` 业务字段，但只保留当前已经确认有价值的最小集合。

| 字段 | 含义 | 什么时候有用 | 建议挂载位置 |
| --- | --- | --- | --- |
| `jarvis.platform` | 请求来源，例如 `feishu`、`web`、`cli` | 区分流量入口，排查某一接入渠道的问题 | `turn.run` |
| `jarvis.conversation_id` | 会话 ID | 从 trace 反查会话记录 | `turn.run`，子 span 可继承 |
| `jarvis.turn_id` | 本次 turn ID | 定位一次具体执行 | `turn.run`，子 span 可继承 |
| `jarvis.route` | 本次 turn 走的是 `fast_reply` 还是 `planned` | 解释“为什么没走执行链” | `turn.run` |
| `jarvis.node_id` | 当前 node 的 ID | 看是哪一个执行节点慢/失败 | `node.execute`，`llm.call`，`tool.call`，`coder.run` |
| `jarvis.runtime` | node 的 runtime，例如 `llm`、`react`、`coder` | 看不同 runtime 的稳定性和耗时 | `node.execute` |
| `jarvis.tool_name` | 工具名 | 看具体哪个工具出问题 | `tool.call` |
| `jarvis.provider` | 提供方，例如 `deepseek`、`codex`、`claude_code` | 现在主要对 `coder.run` 有价值；LLM 侧先预留 | `llm.call`，`coder.run` |
| `jarvis.model` | 具体模型名 | 现在先预留，后面切模型时有用 | `llm.call` |
| `jarvis.status` | 业务执行结果，如 `completed`、`failed`、`blocked` | 快速看节点/turn 结果 | `turn.run`，`node.execute`，必要时 `tool.call` / `coder.run` |
| `jarvis.approval_required` | 是否需要用户审批 | 对 coder 链路非常关键，能解释为什么卡住 | `coder.run` |

补充数值字段：

- `jarvis.elapsed_ms`
- `jarvis.usage.prompt_tokens`
- `jarvis.usage.completion_tokens`
- `jarvis.usage.total_tokens`

### 3.3 字段取舍说明

当前明确保留的原因：

- `platform`：你已经确认有价值
- `route`：你已经确认有价值
- `provider`：LLM 侧现在价值有限，但 `coder.run` 侧仍然有价值
- `model`：现在基本固定，但为后面多模型对比预留
- `status`：直接解释结果
- `approval_required`：直接解释 coder 卡住原因

当前不建议第一阶段保留的字段：

- `jarvis.tool_call_id`
- `jarvis.repo_id`
- `jarvis.workspace`
- `jarvis.artifact_count`
- `jarvis.prompt_version`

这些字段不是没用，而是第一阶段会分散注意力。

### 3.4 第一阶段只保留极少数 event

第一阶段不建议把大量内部事件映射成 span event。

只保留：

- `error`
- `approval_requested`

可选保留：

- `artifact_published`

其余例如：

- `progress.event`
- `tool.observation`
- `approval.completed`
- `session.workspace.created`
- `coder.raw_event.parsed`

第一阶段先不要做，避免 trace 里噪音太大。

---

## 4. 少侵入接入方案

### 4.1 新增独立 observability 模块

建议新增：

```text
app/observability/
  __init__.py
  setup.py
  tracing.py
  attributes.py
  progress_sink.py
```

职责：

- `setup.py`: 初始化 `TracerProvider`、`Resource`、OTLP exporter、batch processor
- `tracing.py`: 提供 `start_span(...)`、`set_attrs(...)`、`record_exception(...)`
- `attributes.py`: 统一 `jarvis.*` 字段名，避免业务代码里散落 magic string
- `progress_sink.py`: 把 `ProgressReporter.emit(...)` 转成当前 span 的 event

这样业务代码改动点少，且能逐步扩展。

### 4.2 优先自动埋点

第一层先接：

- FastAPI
- `httpx`
- SQLAlchemy
- logging correlation

这样即使业务 span 还没补全，也能先把：

- HTTP 入站
- 调用外部 LLM API 的 HTTP 出站
- MySQL 查询

串到一条 trace 里。

### 4.3 业务手工埋点只落在稳定边界

建议只在以下边界加手工 span：

- [app/task_runtime/agent_runtime.py](E:/pythonProject/jarvis/app/task_runtime/agent_runtime.py)
  - `turn.run`

- [app/task_runtime/node_executor.py](E:/pythonProject/jarvis/app/task_runtime/node_executor.py)
  - `node.execute`

- [app/task_runtime/node_execute_runtime.py](E:/pythonProject/jarvis/app/task_runtime/node_execute_runtime.py)
  - `llm.call`
  - `tool.call`
  - `coder.run`

- [app/llm/client.py](E:/pythonProject/jarvis/app/llm/client.py)
  - enrich 当前 span，而不是再造一层重复 span

### 4.4 不建议第一阶段做的事情

- 不要到每个 helper 函数都加 span
- 不要默认把完整 prompt / response 原文塞进 attribute
- 不要一开始就同时接 traces + metrics + logs 全量
- 不要先改数据库 schema 才能起步

---

## 5. 本地 Collector 与网页查看方案

本地目标很简单：

- `docker compose up` 启动 collector + jaeger
- Jarvis 把 spans 发到 collector
- 浏览器打开 Jaeger UI 看单次请求 trace

### 5.1 推荐本地组件

推荐组合：

- `otel/opentelemetry-collector-contrib`
- `jaegertracing/all-in-one`

理由：

- Collector 是生产形态
- Jaeger UI 足够看调用链
- Jaeger all-in-one 对本地最轻

### 5.2 端口约定

建议本地使用：

- Collector OTLP gRPC: `4317`
- Collector OTLP HTTP: `4318`
- Jaeger UI: `16686`

本地网页入口：

- `http://127.0.0.1:16686`

### 5.3 推荐 docker-compose 片段

可新增一个独立文件，例如 `docker-compose.observability.yml`：

```yaml
services:
  otel-collector:
    image: otel/opentelemetry-collector-contrib:latest
    command: ["--config=/etc/otelcol/config.yaml"]
    volumes:
      - ./docker/otel/collector.yaml:/etc/otelcol/config.yaml:ro
    ports:
      - "4317:4317"
      - "4318:4318"
      - "13133:13133"
    depends_on:
      - jaeger

  jaeger:
    image: jaegertracing/all-in-one:1.76.0
    environment:
      COLLECTOR_OTLP_ENABLED: "true"
    ports:
      - "16686:16686"
```

### 5.4 推荐 collector 配置

对应 `docker/otel/collector.yaml`：

```yaml
receivers:
  otlp:
    protocols:
      grpc:
      http:

processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 256
  batch:
    timeout: 1s
    send_batch_size: 512

exporters:
  debug:
    verbosity: basic
  otlp/jaeger:
    endpoint: jaeger:4317
    tls:
      insecure: true

extensions:
  health_check:

service:
  extensions: [health_check]
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [debug, otlp/jaeger]
```

### 5.5 启动命令

```powershell
docker compose -f docker-compose.observability.yml up -d
```

检查：

```powershell
Invoke-RestMethod http://127.0.0.1:16686
Invoke-RestMethod http://127.0.0.1:13133
```

---

## 6. 应用侧初始化方案

### 6.1 配置项

建议在 [app/config.py](E:/pythonProject/jarvis/app/config.py) 新增：

```text
JARVIS_OTEL_ENABLED=true
JARVIS_OTEL_SERVICE_NAME=jarvis-api
JARVIS_OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
JARVIS_OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
JARVIS_OTEL_TRACES_SAMPLER=parentbased_traceidratio
JARVIS_OTEL_TRACES_SAMPLER_ARG=1.0
JARVIS_OTEL_CAPTURE_CONTENT=false
```

说明：

- 本地默认可采样 `1.0`
- 生产再降采样
- `capture_content` 独立开关，控制是否记录 prompt/response 摘要

### 6.2 初始化时机

在 [app/main.py](E:/pythonProject/jarvis/app/main.py) 的 `create_app()` 之前或内部初始化：

- `TracerProvider`
- `Resource(service.name=jarvis-api, service.version=...)`
- OTLP exporter
- BatchSpanProcessor
- FastAPI / httpx / SQLAlchemy instrumentation

建议只初始化一次，不要在 runtime 内重复创建 provider。

---

## 7. 如何在网页上看到“一个请求的 span 以及中间发生了什么”

这里不是只看 HTTP span，而是要能从一次 turn 看到完整业务过程。

### 7.1 关键要求

必须保证以下信息挂在 trace 上：

- `conversation_id`
- `turn_id`
- `route`
- `node_id`
- `runtime`
- `tool_name`
- `provider`

这样在 Jaeger 里搜索时，即使 service 只有一个，也能快速定位一次请求。

### 7.2 推荐搜索方式

Jaeger UI 中按以下维度搜：

- service: `jarvis-api`
- operation: `turn.run`
- tag: `jarvis.turn_id=<id>`
- tag: `jarvis.conversation_id=<id>`

### 7.3 中间发生了什么：靠 span + event，不靠日志全文

第一阶段每个关键阶段只要求在 trace 树中可见：

- `turn.run` 看整次请求
- `node.execute` 看每个 node 的 runtime 和结果
- `llm.call` 看模型调用耗时、token、finish_reason
- `tool.call` 看工具名和结果
- `coder.run` 看 coder 总时长、审批、失败原因

这样 UI 里不会过于拥挤，但已经能解释“中间发生了什么”。

### 7.4 与日志关联

建议开启 logging correlation，把 `trace_id` / `span_id` 打进日志格式。这样：

- 在 Jaeger 点开某个 span 拿到 trace ID
- 去日志里 grep 同一个 trace ID
- 补充看长文本细节

这比把所有 stdout / raw payload 都塞 span 更稳。

---

## 8. coder runtime 处理方案

`coder runtime` 是这套里最特殊的一段，因为它不是简单的内存内函数调用，而是 provider 驱动的外部执行链路。

当前相关代码：

- [app/task_runtime/node_execute_runtime.py](E:/pythonProject/jarvis/app/task_runtime/node_execute_runtime.py)
- [app/task_runtime/coder_provider.py](E:/pythonProject/jarvis/app/task_runtime/coder_provider.py)
- [app/tools/codex.py](E:/pythonProject/jarvis/app/tools/codex.py)
- [app/tools/codex_app_server.py](E:/pythonProject/jarvis/app/tools/codex_app_server.py)
- [app/tools/coder.py](E:/pythonProject/jarvis/app/tools/coder.py)

### 8.1 第一阶段推荐做法

第一阶段不要追求跨进程完整 trace 传播，先保证在 Jarvis 主进程里把 coder runtime 包成一个高价值 span：

- span 名：`coder.run`
- attributes：
  - `jarvis.node_id`
  - `jarvis.runtime=coder`
  - `jarvis.provider=codex|claude_code`
  - `jarvis.repo_id`
  - `jarvis.run_dir`
  - `jarvis.approval_required`
  - `jarvis.exit_code`
  - `jarvis.artifact_count`

这样能先把“coder 节点为什么慢 / 为什么失败 / 是否卡审批”看清楚。

### 8.2 Codex provider 的细化建议

`codex` 现在本身已经产出很好的运行材料：

- `codex-events.jsonl`
- `jarvis-audit.log`
- `codex-stderr.log`
- `codex-approval-requests.json`

这些不需要都全文写进 span。建议做法：

1. `coder.run` 作为主 span
2. 把关键阶段作为 event 挂上去
3. 把文件路径作为 attribute 挂上去

示例 event：

- `coder.run.started`
- `coder.events.persisted`
- `coder.approval.requested`
- `coder.approval.completed`
- `coder.permission_check.failed`
- `coder.run.finished`

示例 attribute：

- `jarvis.codex.events_path`
- `jarvis.codex.audit_path`
- `jarvis.codex.stderr_path`
- `jarvis.codex.approval_path`

### 8.3 是否需要把 Codex 事件拆成子 span

第一阶段不建议。

原因：

- 事件源是 JSONL，天然更适合做 span event
- 直接拆成很多子 span 会让 trace 噪音很大
- 不同 provider 事件模型并不稳定

建议规则：

- 第一阶段审批只用 `approval_requested` event 表示
- 其余 provider 内部事件：先不拆子 span

### 8.4 以后是否做跨进程传播

可以，但放第二阶段。

第二阶段如果要做：

- 在 Jarvis 主进程生成 W3C trace context
- 通过环境变量或 app-server 协议把 trace context 传给 provider 侧
- provider 侧若也能发 OTLP，则可形成真正子 span

这一步的前提是：

- 我们能控制 provider 子进程
- 或 app server 协议支持透传 metadata

当前仓库里，`codex_app_server` 可控程度较高，但仍建议先把“主进程内可观察”做好。

---

## 9. 推荐代码改动点

### 9.1 最小代码改动清单

1. 新增 `app/observability/*`
2. 在 [app/config.py](E:/pythonProject/jarvis/app/config.py) 增加 OTel 配置
3. 在 [app/main.py](E:/pythonProject/jarvis/app/main.py) 初始化 tracing
4. 在 [app/task_runtime/agent_runtime.py](E:/pythonProject/jarvis/app/task_runtime/agent_runtime.py) 加 `turn.run`
5. 在 [app/task_runtime/node_executor.py](E:/pythonProject/jarvis/app/task_runtime/node_executor.py) 加 `node.execute`
6. 在 [app/task_runtime/node_execute_runtime.py](E:/pythonProject/jarvis/app/task_runtime/node_execute_runtime.py) 加 `llm.call` / `tool.call` / `coder.run`
7. 给 `ProgressReporter` 增加 OTel sink
8. 在日志 formatter 中加 `trace_id` / `span_id`
9. 新增 `docker-compose.observability.yml` 与 `docker/otel/collector.yaml`

### 9.2 推荐先不改数据库

第一阶段不要求新增 trace 表。

只建议在已有 `raw_payload` 或 `turn.metadata` 里回写：

- `trace_id`
- `span_id`

这样可以从业务记录跳回 Jaeger，也可以从 Jaeger 反查业务记录。

---

## 10. 采样、隐私与内容采集建议

### 10.1 采样

本地：

- `100%` 采样

生产：

- root turn 建议 `10% ~ 30%`
- 错误、超时、审批请求强制保留

### 10.2 内容采集

默认不要记录：

- 完整用户消息
- 完整 prompt
- 完整 tool stdout/stderr
- 完整审批原文

默认记录：

- 长度
- hash
- 截断摘要
- 结构化状态

通过 `JARVIS_OTEL_CAPTURE_CONTENT=true` 才允许在本地调试环境记录更多文本。

### 10.3 路径与仓库信息

文件路径、repo 名称可能敏感。

建议：

- 本地调试保留绝对路径
- 生产仅保留 repo_id、相对路径、或哈希化路径

---

## 11. 分阶段落地建议

### Phase 1：本地可见

目标：

- Jaeger UI 上能看到单请求全链路
- 能看到 turn / node / llm / tool / coder

交付：

- OTel 初始化
- collector + jaeger compose
- `turn.run` / `node.execute` / `llm.call` / `tool.call` / `coder.run`
- 极少量 event：`error`、`approval_requested`

### Phase 2：生产可用

目标：

- 支持采样、脱敏、日志关联
- `trace_id` 回写业务记录

交付：

- 配置化采样
- trace/log correlation
- content capture 开关
- metadata/raw_payload trace linkage

### Phase 3：评测飞轮

目标：

- 从真实 trace 自动抽样生成 eval 候选
- 对失败、审批、长耗时、无效工具调用做聚类

交付：

- trace-to-eval extractor
- 失败类型 taxonomy
- prompt/tool policy 对比报表

---

## 12. 结论

对 Jarvis 来说，第一阶段最正确的接入方式不是“大面积打点”，而是：

- 用自动埋点接住 HTTP / httpx / SQLAlchemy
- 用少量业务 span 把 `turn -> node -> llm/tool/coder` 串起来
- 用 Collector 解耦后端
- 用 Jaeger UI 先把“单次请求中间发生了什么”看清楚

`coder runtime` 不要一开始就追求 provider 内部完整分布式 trace。第一阶段把它视为一个高价值业务 span `coder.run`，并把现有 `codex-events.jsonl / jarvis-audit.log / approval` 信息挂成少量 event 和 attribute，收益最高，侵入最小。

---

## 13. 外部参考

- OpenTelemetry Python instrumentation: https://opentelemetry.io/docs/languages/python/instrumentation/
- OpenTelemetry Collector install: https://opentelemetry.io/docs/collector/install/
- Jaeger deployment and UI ports: https://www.jaegertracing.io/docs/1.76/deployment/
