# Jarvis Langfuse 本地可观测性接入设计

日期：2026-06-26

## 1. 背景

Jarvis 当前已经接入 OpenTelemetry，并通过本地 OpenTelemetry Collector 导出到 Jaeger。现有链路能看到 HTTP、SQL、业务 span，但 Jaeger 的核心视角仍是分布式调用链，不适合作为 AI agent 日常排障 UI。

本设计的目标是增加一个成熟的本地 LLM observability UI：在浏览器中点开一次 Jarvis 请求，能看到：

- 所属多轮会话
- 一次 turn 的完整执行链路
- 每个 AI 节点经历了哪些步骤
- 每个节点、LLM 调用、工具调用、coder 运行的输入输出摘要
- route、status、token、latency、error、approval、workspace/artifact 路径

## 2. 结论

采用 Langfuse 本地 self-host 作为 Jarvis 的主 AI 可观测性 UI，同时保留 Jaeger 作为底层调用链排障工具。

推荐拓扑：

```text
Jarvis app
  -> OTLP HTTP exporter
  -> OpenTelemetry Collector
       -> Jaeger
       -> Langfuse /api/public/otel

Browser
  -> Langfuse UI: http://localhost:3000
  -> Jaeger UI:   http://localhost:16686
```

选择 Langfuse 的原因：

1. 它的数据模型天然贴近 Jarvis：`session -> trace -> observation`。
2. 它能展示 LLM generation、tool、span、event 的嵌套关系，比 Jaeger 更适合 agent DAG。
3. 它支持 OTLP ingest，可以复用 Jarvis 已经有的 OTel 埋点和 Collector。
4. 它支持本地 Docker Compose，适合当前单机开发和低规模内网运行。

取舍：

- Langfuse 是 open-core，本地基础观测可用；企业增强功能可能需要 license key。
- 本方案不把 Jarvis 强绑定到 Langfuse SDK，第一阶段仍以 OpenTelemetry Collector 输出为主。
- Langfuse UI 负责 AI 可读性；Jaeger 负责底层 span 结构、HTTP/SQL 排障。

## 3. Langfuse 本地服务组成

官方 v3 Docker Compose 低规模部署包含以下容器：

| 服务 | 镜像 | 作用 |
| --- | --- | --- |
| `langfuse-web` | `docker.io/langfuse/langfuse:3` | Web UI、API、OTLP ingest 入口 |
| `langfuse-worker` | `docker.io/langfuse/langfuse-worker:3` | 异步消费 ingestion event，写入 ClickHouse |
| `postgres` | `docker.io/postgres:17` | 事务数据库 |
| `clickhouse` | `docker.io/clickhouse/clickhouse-server` | trace、observation、score 等分析数据 |
| `redis` | `docker.io/redis:7` | 队列和缓存 |
| `minio` | `cgr.dev/chainguard/minio` | 本地 S3/blob storage，保存 ingestion event 和大对象 |

当前仓库已新增本地 compose：

```text
docker-compose.langfuse.yml
docker/otel/collector.langfuse.yaml
```

使用方式：

```powershell
docker compose -f docker-compose.observability.yml -f docker-compose.langfuse.yml up -d
```

默认会初始化：

```text
Langfuse UI: http://localhost:3000
User:        admin@jarvis.local
Password:    jarvis-local-admin
Project:     Jarvis Local
Public key:  pk-lf-jarvis-local
Secret key:  sk-lf-jarvis-local
```

需要对外访问的端口：

| 端口 | 服务 | 说明 |
| --- | --- | --- |
| `3000` | Langfuse Web | 主 UI 和 API |
| `9090` | MinIO S3 | 本地多媒体/大对象上传需要时使用 |

建议仅本机绑定的端口：

| 端口 | 服务 |
| --- | --- |
| `3030` | Langfuse worker |
| `5432` | Postgres |
| `6379` | Redis |
| `8123` | ClickHouse HTTP |
| `9000` | ClickHouse native |
| `9091` | MinIO console |

资源建议：

- 本地试用：至少 4 CPU、8 GiB RAM，磁盘 30 GiB 起。
- 稳定长期运行：至少 4 CPU、16 GiB RAM，磁盘 100 GiB 起。
- Postgres 和 ClickHouse 必须使用 UTC 时区，否则 Langfuse 查询可能出现空结果或时间错乱。

## 4. Jarvis 与 Langfuse 的数据映射

Jarvis 当前已有的业务 span 结构可以直接映射到 Langfuse：

| Jarvis 概念 | 当前 span | Langfuse 显示语义 |
| --- | --- | --- |
| 多轮会话 | `jarvis.conversation_id` | `langfuse.session.id` |
| 一次请求/turn | `turn.run` | trace root |
| planner 结果 | `planner.completed` event | trace metadata / event |
| 执行节点 | `node.execute` | observation type `span` |
| 普通 LLM 调用 | `llm.call` | observation type `generation` |
| React 工具调用 | `tool.call` | observation type `span` 或 `tool` metadata |
| Coder 执行 | `coder.run` | observation type `span`，metadata 标注 provider/repo/approval |
| 节点输入 | node input snapshot | observation input 摘要 + 文件路径 |
| 节点输出 | node result | observation output 摘要 + 文件路径 |
| 最终回复 | aggregation result | trace output |

建议新增或统一的属性：

```text
langfuse.trace.name = "turn.run"
langfuse.session.id = "conversation:{conversation_id}"
langfuse.trace.metadata.conversation_id = "{conversation_id}"
langfuse.trace.metadata.turn_id = "{turn_id}"
langfuse.trace.metadata.platform = "{platform}"
langfuse.trace.metadata.route = "{route}"
langfuse.trace.metadata.status = "{status}"

langfuse.observation.type = "generation"          # 仅 llm.call
langfuse.observation.input = "{json string}"      # 可控摘要或完整内容
langfuse.observation.output = "{json string}"     # 可控摘要或完整内容
langfuse.observation.model.name = "{model}"
langfuse.observation.usage_details = "{json string}"
langfuse.observation.metadata.node_id = "{node_id}"
langfuse.observation.metadata.runtime = "{runtime}"
langfuse.observation.metadata.workspace_path = "{path}"
```

兼容保留当前 `jarvis.*` 字段。`jarvis.*` 用于 Jarvis 自己的 trace-to-eval 抽取；`langfuse.*` 用于 Langfuse UI 的字段映射和过滤。

## 5. 第一阶段实现

### 5.1 修复现有 span attribute 设置

当前 `app/observability/tracing.py` 的 `span_context()` 中 attribute 设置疑似缩进错误，会导致只设置最后一个 attribute。第一阶段必须先修复，否则 Langfuse 和 Jaeger 都会缺关键字段。

目标逻辑：

```python
for key, value in attributes.items():
    if value is None:
        continue
    span.set_attribute(key, _normalize_value(value))
```

### 5.2 Collector 双写

扩展 `docker/otel/collector.yaml`：

```yaml
exporters:
  debug:
    verbosity: basic

  otlp/jaeger:
    endpoint: jaeger:4317
    tls:
      insecure: true

  otlphttp/langfuse:
    endpoint: http://langfuse-web:3000/api/public/otel
    headers:
      Authorization: Basic ${env:LANGFUSE_AUTH_STRING}
      x-langfuse-ingestion-version: "4"

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [debug, otlp/jaeger, otlphttp/langfuse]
```

`LANGFUSE_AUTH_STRING` 的生成方式：

```powershell
[Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("pk-lf-xxx:sk-lf-yyy"))
```

或者在 Linux/WSL：

```bash
echo -n "pk-lf-xxx:sk-lf-yyy" | base64 -w 0
```

说明：

- Langfuse OTLP endpoint 是 `/api/public/otel`。
- 如果使用 trace-specific endpoint，则是 `/api/public/otel/v1/traces`。
- Langfuse 目前支持 OTLP HTTP/protobuf 和 HTTP/JSON，不支持 OTLP gRPC ingest。
- API key 需要先在 Langfuse UI 中创建 project 后获得。

### 5.3 Docker Compose 组织方式

建议新增独立文件：

```text
docker-compose.langfuse.yml
```

再让现有 `docker-compose.observability.yml` 的 `otel-collector` 加入同一个 Docker network，或把 Langfuse 服务合并进 observability compose。

本地推荐启动方式：

```powershell
docker compose -f docker-compose.langfuse.yml up -d
docker compose -f docker-compose.observability.yml up -d
```

如果使用同一个 compose project，Collector 可直接访问 `http://langfuse-web:3000`。如果分开启动且网络不互通，则 Collector 里用 `http://host.docker.internal:3000/api/public/otel`。

## 6. 第二阶段实现

### 6.1 Langfuse 友好的业务属性

在 `turn.run` span 上设置：

```text
langfuse.trace.name
langfuse.session.id
langfuse.trace.input
langfuse.trace.output
langfuse.trace.metadata.conversation_id
langfuse.trace.metadata.turn_id
langfuse.trace.metadata.platform
langfuse.trace.metadata.route
langfuse.trace.metadata.status
```

在 `node.execute` span 上设置：

```text
langfuse.observation.metadata.node_id
langfuse.observation.metadata.runtime
langfuse.observation.metadata.input_path
langfuse.observation.metadata.result_path
langfuse.observation.input
langfuse.observation.output
```

在 `llm.call` span 上设置：

```text
langfuse.observation.type = "generation"
langfuse.observation.model.name
langfuse.observation.input
langfuse.observation.output
langfuse.observation.usage_details
```

### 6.2 输入输出采集策略

默认不采集完整敏感内容。新增或复用配置：

```text
JARVIS_OTEL_CAPTURE_CONTENT=false
JARVIS_LANGFUSE_CAPTURE_MODE=preview|full|off
```

建议默认：

- `off`：只记录长度、状态、路径、token、耗时。
- `preview`：记录截断后的 input/output 摘要，适合本地开发。
- `full`：记录完整 prompt、completion、tool args/output，仅限本机安全环境。

### 6.3 Baggage 或统一属性传播

Langfuse 要按 session、user、metadata 做稳定过滤时，相关 trace-level 属性最好在每个 span 上都存在。后续可以实现一个轻量 span processor 或在 `span_context()` 中自动合并当前 turn 的上下文：

```text
langfuse.session.id
langfuse.trace.metadata.conversation_id
langfuse.trace.metadata.turn_id
langfuse.trace.metadata.platform
```

第一阶段可以先由业务代码显式传递，避免引入过早抽象。

## 7. 验收标准

1. 启动 Langfuse 后可访问 `http://localhost:3000`。
2. 创建 project 后拿到 public key 和 secret key。
3. Collector 能同时把 trace 写入 Jaeger 和 Langfuse。
4. 触发一次 Jarvis 多节点请求后：
   - Jaeger 能看到 `turn.run -> node.execute -> llm.call/tool.call/coder.run`。
   - Langfuse 能看到一个 trace。
   - trace 能按 `conversation_id` 或 `turn_id` 过滤。
   - `llm.call` 能显示 model、usage、input/output 摘要。
   - `node.execute` 能显示 node_id、runtime、status、workspace/result 路径。
5. 触发多轮同一 conversation 后，Langfuse session 下能看到多个 turn trace。

## 8. 风险与约束

1. Langfuse self-host 组件较多，磁盘和内存消耗明显高于 Jaeger all-in-one。
2. Docker Compose 部署不具备高可用和备份能力，只适合本地或低规模内网。
3. 如果 Collector 与 Langfuse 不在同一个 Docker network，endpoint 必须改成 `host.docker.internal` 或宿主机 IP。
4. 完整 prompt/output 可能包含隐私、代码、token、路径，需要默认关闭或只采集 preview。
5. Langfuse OTLP ingest 需要 v3.22.0 及以上；使用 `langfuse:3` 时仍应定期升级。

## 9. 后续扩展

1. 把 Langfuse trace 抽取为 Jarvis eval candidate。
2. 将 `planner.completed` 的 plan JSON 展示为 trace-level metadata。
3. 为 `coder.run` 增加 approval、git branch、commit、artifact 的结构化 metadata。
4. 引入 Langfuse SDK v4，只在 LLM generation 层补充更准确的 input/output、usage 和 cost。
5. 在 Jarvis Web/Feishu 回复中返回 Langfuse trace 链接，方便从用户问题跳转到观测 UI。

## 10. 参考

- Langfuse self-host overview: https://langfuse.com/self-hosting
- Langfuse Docker Compose deployment: https://langfuse.com/self-hosting/deployment/docker-compose
- Langfuse OpenTelemetry ingest: https://langfuse.com/integrations/native/opentelemetry
- Langfuse docker-compose.yml: https://github.com/langfuse/langfuse/blob/main/docker-compose.yml
