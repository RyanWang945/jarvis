# 2026-05-05 Wiki Reranker 评测报告

## 评测范围

- 评测日期：`2026-05-05`
- 源数据集：`wikipedia_zh_real_300`
- 评测集 ID：`kb_eval_dataset_7523e419-b827-4db9-baf7-fdc9c2242866`
- 评测集名称：`wikipedia_zh_real_300:medium_overlap_v1:llm_realistic_v2`
- Query 生成方式：`llm`
- Query 生成模型：`deepseek-v4-flash`
- Query 数量：`200`
- 语言：`zh`
- 分块策略：`medium_overlap_v1`
- 检索 `top-k`：`5`
- OpenSearch 地址：`http://127.0.0.1:9201`
- 使用索引：`kb_wikipedia_zh_medium_overlap_v1`
- Reranker 地址：`http://127.0.0.1:8000`
- Reranker 模型：`/models/bge-reranker-v2-m3`

## 评测方法

本次评测使用 `knowledge.db` 中最新的 200 条 realistic wiki query，只读访问数据库，不写入新的 `kb_eval_runs` / `kb_eval_results`。

`rrf_v2+rerank` 的流程为：

1. BM25 召回前 `50` 条。
2. Vector 召回前 `50` 条。
3. RRF 融合得到前 `50` 条候选。
4. 调用 reranker `/rerank`，返回最终前 `5` 条。
5. reranker 失败时回退到 RRF 候选顺序。

## 评测结果

### 主链路对比

| 模式 | 召回/候选流程 | Recall@5 | Candidate Hit | Precision@5 | MRR | nDCG | Boundary Spill Rate | 平均延迟 | P95 延迟 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `bm25` | BM25 5 -> top5 | 0.690 | 0.690 | 0.138 | 0.5864 | 0.6123 | 0.020 | 122 ms | 139 ms |
| `vector` | Vector 5 -> top5 | 0.855 | 0.855 | 0.171 | 0.7285 | 0.7605 | 0.005 | 14 ms | 19 ms |
| `hybrid` | BM25 5 + Vector 5 -> score fusion top5 | 0.855 | 0.855 | 0.171 | 0.7302 | 0.7619 | 0.005 | 127 ms | 138 ms |
| `rrf_v2` | BM25 20 + Vector 20 -> RRF top5 | 0.865 | 0.955 | 0.173 | 0.7211 | 0.7572 | 0.015 | 158 ms | 175 ms |
| `rrf_wide_50x50` | BM25 50 + Vector 50 -> RRF top5 | 0.870 | 0.980 | 0.174 | 0.7225 | 0.7595 | 0.015 | 258 ms | 427 ms |
| `rrf_wide_50x50+rerank` | BM25 50 + Vector 50 -> RRF 50 -> rerank top5 | 0.950 | 0.980 | 0.190 | 0.8497 | 0.8750 | 0.010 | 1423 ms | 1651 ms |

### Vector Rerank 对照

| 模式 | 召回/候选流程 | Recall@5 | Candidate Hit | Precision@5 | MRR | nDCG | Boundary Spill Rate | 平均延迟 | P95 延迟 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `vector_top5` | Vector 5 -> top5 | 0.855 | 0.855 | 0.171 | 0.7285 | 0.7605 | 0.005 | 103 ms | 117 ms |
| `vector_wide_100` | Vector 100 -> top5 | 0.855 | 0.975 | 0.171 | 0.7285 | 0.7605 | 0.005 | 103 ms | 117 ms |
| `vector_wide_100+rerank` | Vector 100 -> rerank top5 | 0.945 | 0.975 | 0.189 | 0.8366 | 0.8640 | 0.005 | 2399 ms | 2611 ms |

说明：

- `Candidate Hit` 表示 target chunk 是否进入候选池，不是最终 `top5`。
- `rrf_wide_50x50+rerank` 的 reranker fallback 次数为 `0`，服务内部平均推理耗时约 `1158 ms`。
- `vector_wide_100+rerank` 的 reranker fallback 次数为 `0`，服务内部平均推理耗时约 `2291 ms`。

## 结论

`rrf_wide_50x50+rerank` 在 200 条 realistic query 上明显提升排序质量和最终命中率：

- 相比 `rrf_v2`，Recall@5：`0.865 -> 0.950`
- 相比 `rrf_v2`，MRR：`0.7211 -> 0.8497`
- 相比 `rrf_v2`，nDCG：`0.7572 -> 0.8750`
- 相比 `rrf_wide_50x50`，Recall@5：`0.870 -> 0.950`
- 相比 `rrf_wide_50x50`，MRR：`0.7225 -> 0.8497`

`BM25 50 + Vector 50` 只做 RRF、不做 rerank 时，最终 Recall@5 只比 `rrf_v2` 多 `0.005`，说明主要收益来自 reranker 重排，而不是候选池变宽本身。

代价是平均端到端延迟从 `258 ms` 增加到 `1423 ms`。建议作为显式模式用于需要高质量答案的知识库检索，低延迟场景继续使用 `rrf_v2`。

## 5 QPS 并发压测

补充执行了固定发起速率 `5 qps` 的并发压测，路径为 `BM25 50 + Vector 50 -> RRF 50 -> rerank top5`。

| Reranker Timeout | Recall@5 | Candidate Hit | MRR | nDCG | 平均端到端延迟 | P95 端到端延迟 | Fallback |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `3s` | 0.870 | 0.980 | 0.7200 | 0.7576 | 3232 ms | 3426 ms | 198/200 |
| `10s` | 0.870 | 0.980 | 0.7225 | 0.7595 | 10441 ms | 10727 ms | 200/200 |

5 QPS 下当前 reranker 服务无法稳定处理 `50` 个候选的重排请求。请求大量超时后会退回 RRF 顺序，因此最终质量接近 `rrf_wide_50x50_top5`，没有获得 reranker 的排序收益。

压测结束后 `/health` 在 `10s` 内仍未响应，说明服务端存在明显排队或阻塞。当前不建议在 `5 qps` 下直接启用 `input50` rerank，除非增加服务并发能力、减少候选数，或在调用侧加入限流/熔断。
