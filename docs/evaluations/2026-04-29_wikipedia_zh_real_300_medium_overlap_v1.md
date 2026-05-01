# 2026-04-29 Wiki 评测报告

## 评测范围

- 评测日期：`2026-04-29`
- 源数据集：`wikipedia_zh_real_300`
- 评测集 ID：`kb_eval_dataset_1fbf7f03-1f2a-499e-ac7f-e1d9fd185562`
- 评测集名称：`wikipedia_zh_real_300:medium_overlap_v1:llm`
- Query 生成方式：`llm`
- Query 生成模型：`deepseek-v4-pro`
- 抽样文档数：`10`
- Query 数量：`10`
- 语言：`zh`
- 分块策略：`medium_overlap_v1`
- 检索 `top-k`：`5`
- 本次使用的 OpenSearch 地址：`http://127.0.0.1:9201`
- 使用索引：`kb_wikipedia_zh_medium_overlap_v1`

## 评测方法

本次评测复用了 `knowledge.db` 中已有的 wiki 评测 query 集，直接对 `9201` 端口上的 OpenSearch 索引执行检索。

由于同一时间有其他 session 访问同一个 SQLite 文件，数据库写入状态不稳定，因此这次评测使用只读快照方式执行，没有把新的 `kb_eval_runs` / `kb_eval_results` 回写到数据库。

指标计算口径与 [app/knowledge_base/eval.py](E:/pythonProject/jarvis/app/knowledge_base/eval.py) 中现有逻辑一致，包括：

- `recall@5`
- `precision@5`
- `MRR`
- `nDCG`
- `chunk_hit_rate`
- `boundary_spill_rate`
- `avg_latency_ms`
- `p95_latency_ms`

## 评测结果

| 模式 | Recall@5 | Precision@5 | MRR | nDCG | Chunk Hit Rate | Boundary Spill Rate | 平均延迟 | P95 延迟 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `bm25` | 0.70 | 0.14 | 0.5833 | 0.6131 | 0.70 | 0.00 | 82 ms | 241 ms |
| `vector` | 1.00 | 0.20 | 0.9000 | 0.9262 | 1.00 | 0.00 | 20 ms | 56 ms |
| `hybrid` | 1.00 | 0.20 | 0.9500 | 0.9631 | 1.00 | 0.00 | 54 ms | 59 ms |

## 结论

在这套 wiki 小样本评测集上，`hybrid` 的排序质量最好，同时保持了完整的 `recall@5`。

`vector` 也达到了完整的 `recall@5`，但 gold chunk 的排序位置略差于 `hybrid`。

`bm25` 在这套 query 上明显更弱，主要体现在召回和排序质量都偏低。

## 备注

- `9200` 是另一个空集群，本次评测没有使用。
- `9201` 才是当前有效的 wiki 索引所在端口，也与项目配置一致。
- 如果后续还需要把评测结果稳定回写到 SQLite，应该避免多个 Codex session 并发写同一个数据库文件，或者把评测结果拆到独立数据库。
