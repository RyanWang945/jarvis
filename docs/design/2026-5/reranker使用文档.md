# Reranker 服务接口使用文档

本文档面向调用方，说明其他服务如何接入 `reranker-service`。

## 基础地址

调用地址以实际部署暴露的 HTTP 地址为准。

当前已验证的本机 Docker 地址：

```text
http://127.0.0.1:8000
```

跨服务调用时，将 `127.0.0.1` 替换为调用方可访问的宿主机地址或服务名：

```text
http://<reranker-host>:8000
```

OpenAPI 文档地址：

```text
http://<reranker-host>:8000/docs
http://<reranker-host>:8000/openapi.json
```

## 鉴权

当前服务不做鉴权。

如果要暴露到非可信网络，需要放在 API Gateway、反向代理或服务网格策略后面。

## 健康检查

### `GET /health`

用于检查服务进程是否存活，以及模型是否已加载完成。

请求示例：

```bash
curl "http://<reranker-host>:8000/health"
```

响应示例：

```json
{
  "status": "ok",
  "provider": "flag_embedding",
  "model": "/models/bge-reranker-v2-m3",
  "cuda_available": true,
  "gpu_name": "NVIDIA GeForce RTX 4070 Ti"
}
```

字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | string | 服务状态。`ok` 表示服务可用。 |
| `provider` | string | reranker provider，当前为 `flag_embedding`。 |
| `model` | string | 服务实际加载的模型名称或模型路径。 |
| `cuda_available` | boolean | 服务进程是否可用 CUDA。 |
| `gpu_name` | string or null | CUDA 可用时返回 GPU 名称。 |

## 重排序

### `POST /rerank`

根据 `query` 对候选文档列表进行相关性打分和重排序。

返回结果按 `score` 从高到低排序，只返回前 `top_n` 条。

请求头：

```http
POST /rerank
Content-Type: application/json
```

请求体示例：

```json
{
  "query": "3M 2023 年营业利润下降的原因是什么？",
  "top_n": 2,
  "max_length": 1024,
  "documents": [
    {
      "id": "doc-1",
      "text": "3M 2023 年营业利润受到诉讼费用、重组成本和业务需求下降影响。",
      "metadata": {
        "source": "annual-report",
        "page": 12
      }
    },
    {
      "id": "doc-2",
      "text": "3M 是一家美国跨国制造公司。",
      "metadata": {}
    }
  ]
}
```

请求字段：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `query` | string | 是 | 无 | 用户查询。服务会 trim，trim 后不能为空。 |
| `documents` | array | 否 | `[]` | 待重排序的候选文档列表。 |
| `documents[].id` | string | 是 | 无 | 调用方传入的文档 ID，不能为空。 |
| `documents[].text` | string | 否 | `""` | 候选文档文本。 |
| `documents[].metadata` | object | 否 | `{}` | 调用方自定义元数据，响应中会原样返回。 |
| `top_n` | integer or null | 否 | `8` | 返回结果数量，必须 `>= 1`。实际返回数量不会超过输入文档数量。 |
| `max_length` | integer or null | 否 | `1024` | 传给模型的最大 token 长度，范围为 `32` 到 `8192`。 |

服务限制：

| 限制项 | 当前值 |
| --- | --- |
| 单次请求最大文档数 | `100` |
| 每篇文档参与计算的最大字符数 | `8000` |
| 默认返回结果数 | `8` |
| 默认模型 `max_length` | `1024` |

如果 `documents` 为空或未传，服务返回空 `results`。

响应示例：

```json
{
  "provider": "flag_embedding",
  "model": "/models/bge-reranker-v2-m3",
  "latency_ms": 221,
  "results": [
    {
      "id": "doc-1",
      "text": "3M 2023 年营业利润受到诉讼费用、重组成本和业务需求下降影响。",
      "metadata": {
        "source": "annual-report",
        "page": 12
      },
      "score": 6.7265625,
      "rank": 1
    },
    {
      "id": "doc-2",
      "text": "3M 是一家美国跨国制造公司。",
      "metadata": {},
      "score": -4.33984375,
      "rank": 2
    }
  ]
}
```

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `provider` | string | reranker provider。 |
| `model` | string | 服务实际加载的模型名称或模型路径。 |
| `latency_ms` | integer | 服务内部统计的推理耗时，单位毫秒。 |
| `results` | array | 重排序后的结果列表。 |
| `results[].id` | string | 原始文档 ID。 |
| `results[].text` | string | 服务参与计算并返回的文档文本。超长文本可能被截断到服务限制。 |
| `results[].metadata` | object | 请求中对应文档的 metadata。 |
| `results[].score` | number | reranker 相关性分数，越高表示越相关。 |
| `results[].rank` | integer | 排名，从 `1` 开始。 |

## 错误响应

### `400 Bad Request`

请求超过服务级限制时返回。

示例：

```json
{
  "detail": "documents exceeds limit 100"
}
```

### `422 Unprocessable Entity`

请求结构或字段校验失败时返回，例如：

- `query` 为空
- `documents[].id` 为空
- `top_n < 1`
- `max_length < 32`
- `max_length > 8192`

示例：

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body", "query"],
      "msg": "Value error, query must not be empty"
    }
  ]
}
```

### `5xx Server Error`

调用方应将任意 `5xx` 视为 reranker 当前不可用。

调用方需要自行 fallback，例如使用原始检索顺序、RRF 结果或其他本地排序策略。

## 调用建议

- 只把召回阶段的前若干条候选传给 `/rerank`，例如 `input_top_k`。
- 客户端设置较短超时，例如 `3000ms`。
- fallback 逻辑放在调用方，本服务不做降级排序。
- 如果后续流程还需要原始召回分数，可以放在 `metadata` 中传入。
- 使用稳定的文档 ID，便于日志排查和链路追踪。
- 不要在没有鉴权层的情况下将服务直接暴露到公网。

## Curl 示例

```bash
curl -X POST "http://<reranker-host>:8000/rerank" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "3M 2023 年营业利润下降的原因是什么？",
    "top_n": 2,
    "documents": [
      {
        "id": "doc-1",
        "text": "3M 2023 年营业利润受到诉讼费用、重组成本和业务需求下降影响。",
        "metadata": {
          "source": "annual-report"
        }
      },
      {
        "id": "doc-2",
        "text": "3M 是一家美国跨国制造公司。"
      },
      {
        "id": "doc-3",
        "text": "苹果公司 2023 年发布了多款新产品。"
      }
    ]
  }'
```

## Python 示例

```python
import requests

response = requests.post(
    "http://<reranker-host>:8000/rerank",
    json={
        "query": "3M 2023 年营业利润下降的原因是什么？",
        "top_n": 2,
        "documents": [
            {
                "id": "doc-1",
                "text": "3M 2023 年营业利润受到诉讼费用、重组成本和业务需求下降影响。",
                "metadata": {"source": "annual-report"},
            },
            {
                "id": "doc-2",
                "text": "3M 是一家美国跨国制造公司。",
            },
        ],
    },
    timeout=3,
)
response.raise_for_status()
ranked_documents = response.json()["results"]
```
