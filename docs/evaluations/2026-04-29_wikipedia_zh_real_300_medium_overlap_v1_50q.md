# 2026-04-29 Wiki 评测报告

## 评测范围

- 评测日期：`2026-04-29`
- 源数据集：`wikipedia_zh_real_300`
- 评测集 ID：`kb_eval_dataset_71aa7d6a-9871-4335-8597-4fb19631d0b1`
- 评测集名称：`wikipedia_zh_real_300:medium_overlap_v1:llm`
- Query 生成方式：`llm`
- Query 生成模型：`deepseek-v4-pro`
- 抽样文档数：`50`
- Query 数量：`50`
- 语言：`zh`
- 分块策略：`medium_overlap_v1`
- 检索 `top-k`：`5`
- 本次使用的 OpenSearch 地址：`http://127.0.0.1:9201`
- 使用索引：`kb_wikipedia_zh_medium_overlap_v1`

## 评测方法

本次评测复用了 `knowledge.db` 中已有的 50 条 wiki 评测 query，直接对 `9201` 端口上的 OpenSearch 索引执行检索。

测试方法本身没有变化。和前一份 10 条 query 的评测相比，这次唯一重要变化是评测集规模从 `10` 提升到了 `50`。

由于同一时间有其他 session 访问同一个 SQLite 文件，数据库不适合安全回写，因此这次评测仍使用只读快照方式执行，没有写入新的 `kb_eval_runs` / `kb_eval_results`。

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
| `bm25` | 0.64 | 0.128 | 0.5650 | 0.5839 | 0.64 | 0.00 | 44 ms | 47 ms |
| `vector` | 0.98 | 0.196 | 0.8307 | 0.8679 | 0.98 | 0.00 | 9 ms | 12 ms |
| `hybrid` | 0.98 | 0.196 | 0.8453 | 0.8789 | 0.98 | 0.00 | 47 ms | 51 ms |

## 结论

在更大的 50 条 query 评测集上，`hybrid` 仍然是整体最优模式，但相对 `vector` 的领先幅度比 10 条样本时更小。

`vector` 和 `hybrid` 都维持了很高的召回，而 `bm25` 在召回和排序质量上仍然明显偏弱。

相比 10 条 query 的小样本结果，这次 50 条评测集的结果更有参考价值，也说明之前的小样本指标略偏乐观。

## 备注

- `9201` 是当前有效 wiki 索引所在端口，也与项目配置一致。
- `9200` 是另一个空集群，本次评测没有使用。
- 如果后续需要稳定保留评测历史，建议把评测结果写入与共享 `knowledge.db` 分离，避免多个 session 竞争同一个 SQLite 文件。
