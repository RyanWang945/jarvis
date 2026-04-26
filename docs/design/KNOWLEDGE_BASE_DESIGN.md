# Knowledge Base Design

本文档描述 Jarvis 项目中独立 Wikipedia 知识库模块的 V1 设计。该模块用于 Jarvis 内部检索，也提供独立 API 以支持测试、评估和指标统计。

---

## 1. 目标与范围

### 1.1 目标

- 构建一个服务于 Wikipedia 数据的独立知识库模块，V1 先支持中文，后续可扩展到英文。
- 使用本地 `SQLite` 存储原始文档与分块结果。
- 使用本地 Docker 中的 `OpenSearch` 存储检索索引。
- 支持 `BM25 + 向量` 混合检索。
- 支持前 `N` 条渐进式导入，便于开发、调试和压测。
- 提供可重复、可量化的离线评测与在线指标统计能力。

### 1.2 非目标

- V1 不支持 Wikipedia 之外的多数据源。
- V1 不做复杂权限体系。
- V1 不将 OpenSearch 作为事实源。
- V1 不在导入阶段做大规模知识抽取、关系图谱构建或摘要重写。

### 1.3 核心原则

- `SQLite` 是事实源，`OpenSearch` 是派生索引。
- 文档导入、分块、索引解耦，任何一步失败都应可重试。
- 所有核心对象都必须具备稳定 ID。
- 分块算法、Embedding 模型、索引版本都需要显式版本化。
- 所有评测都应可复现，避免只看主观效果。

---

## 2. 数据源现状

当前已接触到的数据位于：

- `data/wikipedia/sample.json`
- `data/wikipedia/wikipedia_20231101_zh_simp.jsonl`

已确认单条记录结构如下：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | Wikipedia 数据集中的文章 ID |
| `url` | string | 文章 URL |
| `title` | string | 文章标题 |
| `text` | string | 文章正文文本 |

设计结论：

- 输入天然是“文章级文档”，适合作为 `document` 粒度入库。
- `text` 是主要检索内容来源。
- `title` 与 `url` 应同步进入 SQLite 与 OpenSearch，便于召回与展示。
- 语言不应写死为中文，所有表和索引设计都应显式带 `language`。

---

## 3. 总体架构

### 3.1 模块分层

建议新增独立知识库模块，职责拆分如下：

1. `kb_ingest`
   - 流式读取 Wikipedia 文件
   - 解析 JSON / JSONL
   - 清洗文本
   - 写入 SQLite 原始文档表

2. `kb_chunker`
   - 对文档执行结构化分块
   - 生成 chunk 元数据
   - 写入 SQLite chunk 表

3. `kb_embedder`
   - 调用阿里云向量化 API
   - 生成 chunk embedding
   - 处理限流、失败重试和批量提交

4. `kb_indexer`
   - 将 chunk 写入 OpenSearch
   - 建立 BM25 与向量索引
   - 支持重建索引

5. `kb_retriever`
   - 提供统一检索接口
   - 支持 BM25、向量检索、混合召回
   - 支持评测时的 TopK 输出

6. `kb_eval`
   - 生成测试集
   - 执行离线评测
   - 记录指标与评测结果

### 3.2 数据流

```text
Wikipedia JSONL
    -> ingest
    -> kb_documents
    -> chunker
    -> kb_chunks
    -> embedder
    -> kb_chunk_embeddings
    -> indexer
    -> OpenSearch index
    -> retriever / eval API
```

### 3.3 存储职责

| 存储 | 角色 | 存什么 | 是否事实源 |
|------|------|------|------|
| SQLite | 主存储 | 原文、chunk、导入任务、评测结果 | 是 |
| OpenSearch | 检索索引 | BM25 字段、向量字段、检索元数据 | 否 |

---

## 4. 文档模型与 ID 设计

### 4.1 粒度

- `document`：一篇 Wikipedia 文章
- `chunk`：文档切分后的最小检索单元

### 4.2 ID 规则

| 对象 | ID 建议 | 说明 |
|------|------|------|
| source | `wikipedia_zh_simp_20231101` | 固定数据源版本 |
| document | `wiki:{external_id}` | 例如 `wiki:13` |
| chunk | `{document_id}:chunk:{chunk_index}` | 例如 `wiki:13:chunk:0003` |
| ingest job | `kb_ingest_{uuid}` | 导入任务 |
| eval run | `kb_eval_{uuid}` | 评测任务 |

设计要求：

- 同一篇文章重复导入时，需基于 `external_id` 去重。
- 若文档正文变化，更新 `text_hash`，并重新生成 chunk 和索引。
- chunk ID 必须稳定，便于测试集引用。

---

## 5. SQLite 设计

建议使用独立数据库：

- `data/knowledge.db`

### 5.1 `kb_sources`

记录知识库数据源定义。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `source_id` | TEXT | PRIMARY KEY | 数据源唯一 ID |
| `name` | TEXT | NOT NULL | 数据源名称，如 `wikipedia` |
| `language` | TEXT | NOT NULL | 语言，如 `zh`、`en` |
| `dataset_version` | TEXT | NOT NULL | 数据版本，如 `20231101_zh_simp` |
| `file_path` | TEXT | NOT NULL | 原始文件路径 |
| `description` | TEXT | - | 数据源说明 |
| `created_at` | TEXT | NOT NULL | 创建时间 |

### 5.2 `kb_documents`

记录原始文章文档，是事实源核心表。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `doc_id` | TEXT | PRIMARY KEY | 内部文档 ID |
| `source_id` | TEXT | NOT NULL | 关联 `kb_sources.source_id` |
| `external_id` | TEXT | NOT NULL | 原始 Wikipedia `id` |
| `title` | TEXT | NOT NULL | 文章标题 |
| `url` | TEXT | NOT NULL | 文章 URL |
| `text` | TEXT | NOT NULL | 原始正文文本 |
| `text_hash` | TEXT | NOT NULL | 正文哈希，用于变更检测 |
| `char_count` | INTEGER | NOT NULL | 正文字符数 |
| `language` | TEXT | NOT NULL | 文档语言，如 `zh`、`en` |
| `metadata_json` | TEXT | - | 扩展元数据 JSON |
| `ingest_job_id` | TEXT | NOT NULL | 首次导入任务 ID |
| `created_at` | TEXT | NOT NULL | 创建时间 |
| `updated_at` | TEXT | NOT NULL | 更新时间 |

约束建议：

- `UNIQUE(source_id, external_id)`
- `INDEX(title)`

### 5.3 `kb_chunk_profiles`

记录分块策略配置。这样可以在不复制整套表结构的前提下，对不同分块策略进行并行测试。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `chunk_profile_id` | TEXT | PRIMARY KEY | 分块策略 ID |
| `name` | TEXT | NOT NULL | 策略名称 |
| `language` | TEXT | - | 适用语言，可为空表示通用 |
| `chunker_version` | TEXT | NOT NULL | 分块算法版本 |
| `target_size` | INTEGER | NOT NULL | 目标 chunk 字符数 |
| `soft_min_size` | INTEGER | NOT NULL | 软下限 |
| `hard_max_size` | INTEGER | NOT NULL | 硬上限 |
| `overlap_size` | INTEGER | NOT NULL | 重叠字符数 |
| `boundary_rules_json` | TEXT | - | 边界规则配置 |
| `normalization_rules_json` | TEXT | - | 文本清洗规则配置 |
| `is_active` | INTEGER | NOT NULL | 是否启用，0/1 |
| `created_at` | TEXT | NOT NULL | 创建时间 |

说明：

- `kb_chunk_profiles` 是“策略定义表”。
- 多种 chunk 策略共存时，不需要多建一套 `kb_chunks_xxx` 表。
- 一个文档可以在多个 `chunk_profile_id` 下生成多套 chunk 结果，用于离线对比。

### 5.4 `kb_chunks`

记录分块结果，是检索和评测的核心对象表。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `chunk_id` | TEXT | PRIMARY KEY | chunk 唯一 ID |
| `doc_id` | TEXT | NOT NULL | 关联 `kb_documents.doc_id` |
| `chunk_profile_id` | TEXT | NOT NULL | 关联 `kb_chunk_profiles.chunk_profile_id` |
| `chunk_index` | INTEGER | NOT NULL | chunk 顺序号，从 0 开始 |
| `chunker_version` | TEXT | NOT NULL | 分块算法版本 |
| `section_path` | TEXT | - | 章节路径，V1 可为空 |
| `raw_content` | TEXT | NOT NULL | 原始切分得到的 chunk 文本，作为事实存储 |
| `normalized_content` | TEXT | NOT NULL | 清洗后用于索引和向量化的 chunk 文本 |
| `content_hash` | TEXT | NOT NULL | chunk 文本哈希 |
| `char_start` | INTEGER | NOT NULL | 在原文中的起始字符位置 |
| `char_end` | INTEGER | NOT NULL | 在原文中的结束字符位置 |
| `char_count` | INTEGER | NOT NULL | chunk 字符数 |
| `token_estimate` | INTEGER | NOT NULL | 估算 token 数 |
| `overlap_prev_chars` | INTEGER | NOT NULL | 与前一块重叠字符数 |
| `is_boundary_forced` | INTEGER | NOT NULL | 是否因超长而硬切，0/1 |
| `metadata_json` | TEXT | - | 扩展元数据 JSON |
| `created_at` | TEXT | NOT NULL | 创建时间 |

约束建议：

- `UNIQUE(doc_id, chunk_profile_id, chunk_index)`
- `INDEX(doc_id, chunk_profile_id, chunk_index)`

说明：

- `kb_chunks` 必须存储 chunk 的真实文本，不能只把 chunk 发到 OpenSearch。
- `raw_content` 用于回溯“原始切分结果”。
- `normalized_content` 用于检索、向量化和评测复现。
- 如果后续你不想区分 raw/normalized，也至少要保留一份 chunk 文本事实存储；否则无法做索引重建、失败分析和 chunk 策略对比。

### 5.5 `kb_chunk_embeddings`

记录向量化结果，与检索索引解耦。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `chunk_id` | TEXT | PRIMARY KEY | 关联 `kb_chunks.chunk_id` |
| `embedding_model` | TEXT | NOT NULL | 向量模型名 |
| `embedding_dim` | INTEGER | NOT NULL | 向量维度 |
| `embedding_json` | TEXT | NOT NULL | 向量数组 JSON |
| `text_hash` | TEXT | NOT NULL | 生成 embedding 时对应文本哈希 |
| `created_at` | TEXT | NOT NULL | 创建时间 |
| `updated_at` | TEXT | NOT NULL | 更新时间 |

说明：

- 若后续 SQLite 存储 embedding 过大，可只保留元数据，把向量只存 OpenSearch。
- V1 为保证可重建、可诊断，建议先保留 SQLite 副本。

### 5.6 `kb_ingest_jobs`

记录导入任务状态，支持前 `N` 条渐进导入。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `job_id` | TEXT | PRIMARY KEY | 导入任务 ID |
| `source_id` | TEXT | NOT NULL | 数据源 ID |
| `file_path` | TEXT | NOT NULL | 输入文件 |
| `limit_n` | INTEGER | - | 本次导入前 N 条 |
| `status` | TEXT | NOT NULL | `pending/running/succeeded/failed` |
| `started_at` | TEXT | - | 开始时间 |
| `finished_at` | TEXT | - | 结束时间 |
| `documents_seen` | INTEGER | NOT NULL | 扫描文档数 |
| `documents_inserted` | INTEGER | NOT NULL | 新增文档数 |
| `documents_updated` | INTEGER | NOT NULL | 更新文档数 |
| `documents_skipped` | INTEGER | NOT NULL | 跳过文档数 |
| `chunks_created` | INTEGER | NOT NULL | 创建 chunk 数 |
| `error_message` | TEXT | - | 失败信息 |
| `created_at` | TEXT | NOT NULL | 创建时间 |

### 5.7 `kb_eval_datasets`

记录评测集定义。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `dataset_id` | TEXT | PRIMARY KEY | 评测集 ID |
| `name` | TEXT | NOT NULL | 评测集名称 |
| `source_id` | TEXT | NOT NULL | 数据源 ID |
| `generation_method` | TEXT | NOT NULL | 生成方法 |
| `query_model` | TEXT | - | 生成 query 的模型 |
| `sample_doc_count` | INTEGER | NOT NULL | 抽样文档数 |
| `created_at` | TEXT | NOT NULL | 创建时间 |

### 5.8 `kb_eval_queries`

记录评测 query 与标注答案。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `query_id` | TEXT | PRIMARY KEY | query ID |
| `dataset_id` | TEXT | NOT NULL | 关联评测集 |
| `doc_id` | TEXT | NOT NULL | 目标文档 ID |
| `target_chunk_id` | TEXT | - | 目标 chunk ID |
| `query_text` | TEXT | NOT NULL | 测试 query |
| `query_type` | TEXT | NOT NULL | 例如 `fact`, `definition`, `entity`, `multi_sentence` |
| `difficulty` | TEXT | NOT NULL | `easy/medium/hard` |
| `gold_answer` | TEXT | - | 参考答案 |
| `gold_evidence_json` | TEXT | - | 证据 chunk 列表 |
| `generated_by` | TEXT | - | 生成方式或模型 |
| `review_status` | TEXT | NOT NULL | `generated/reviewed/approved/rejected` |
| `created_at` | TEXT | NOT NULL | 创建时间 |

### 5.9 `kb_eval_runs`

记录每次评测运行。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `eval_run_id` | TEXT | PRIMARY KEY | 评测任务 ID |
| `dataset_id` | TEXT | NOT NULL | 评测集 ID |
| `retrieval_mode` | TEXT | NOT NULL | `bm25/vector/hybrid` |
| `top_k` | INTEGER | NOT NULL | 检索 topK |
| `chunk_profile_id` | TEXT | NOT NULL | 本次评测使用的分块策略 |
| `chunker_version` | TEXT | NOT NULL | 分块版本 |
| `embedding_model` | TEXT | - | 向量模型 |
| `index_name` | TEXT | NOT NULL | OpenSearch 索引名 |
| `status` | TEXT | NOT NULL | 运行状态 |
| `started_at` | TEXT | - | 开始时间 |
| `finished_at` | TEXT | - | 结束时间 |
| `created_at` | TEXT | NOT NULL | 创建时间 |

### 5.10 `kb_eval_results`

记录单条 query 的检索结果和指标细节。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `result_id` | TEXT | PRIMARY KEY | 结果 ID |
| `eval_run_id` | TEXT | NOT NULL | 评测任务 ID |
| `query_id` | TEXT | NOT NULL | query ID |
| `hit` | INTEGER | NOT NULL | 是否命中 gold，0/1 |
| `hit_rank` | INTEGER | - | 首次命中的排名 |
| `mrr_score` | REAL | NOT NULL | 单条 query 的 MRR 分数 |
| `ndcg_score` | REAL | NOT NULL | 单条 query 的 NDCG 分数 |
| `retrieved_chunk_ids_json` | TEXT | NOT NULL | TopK chunk ID 列表 |
| `retrieved_scores_json` | TEXT | NOT NULL | TopK 分数列表 |
| `latency_ms` | INTEGER | NOT NULL | 单次检索耗时 |
| `created_at` | TEXT | NOT NULL | 创建时间 |

---

## 5.x 是否需要为不同分块策略多建表

结论：通常不需要。

推荐做法：

1. 用 `kb_chunk_profiles` 保存不同分块策略定义。
2. 用 `kb_chunks.chunk_profile_id` 区分同一文档在不同策略下产生的 chunk。
3. 用 `kb_eval_runs.chunk_profile_id` 绑定评测运行使用的策略。
4. OpenSearch 索引名或索引别名中也带上 `chunk_profile_id` 或版本号。

这样就能支持：

- 同一批文档同时保留多套 chunk
- 对比 `small / medium / large / high-overlap` 等策略
- 在不丢失事实数据的前提下重跑评测

只有当后续 chunk 数量极大、单表压力明显时，才考虑分表或分库。

---

## 6. OpenSearch 设计

### 6.1 索引命名

建议采用版本化命名：

- `kb_wikipedia_zh_chunk_v1`
- `kb_wikipedia_en_chunk_v1`

如果未来升级映射或分块策略，则新增：

- `kb_wikipedia_zh_chunk_v2`
- `kb_wikipedia_en_chunk_v2`

### 6.2 每条索引文档字段

OpenSearch 中一条记录对应一个 chunk。

| 字段 | 类型建议 | 说明 |
|------|------|------|
| `chunk_id` | keyword | chunk 唯一 ID |
| `doc_id` | keyword | 文档 ID |
| `source_id` | keyword | 数据源 ID |
| `external_id` | keyword | 原始文章 ID |
| `language` | keyword | 语言 |
| `chunk_profile_id` | keyword | 分块策略 ID |
| `title` | text + keyword | 标题，用于 BM25 与展示 |
| `url` | keyword | 原始 URL |
| `content` | text | 建议使用 `normalized_content` 建索引 |
| `section_path` | keyword | 章节路径 |
| `chunk_index` | integer | chunk 序号 |
| `char_count` | integer | chunk 长度 |
| `token_estimate` | integer | token 估计数 |
| `chunker_version` | keyword | 分块版本 |
| `embedding_model` | keyword | 向量模型 |
| `embedding` | dense_vector | 向量字段 |
| `text_hash` | keyword | 用于一致性检测 |
| `created_at` | date | 创建时间 |

### 6.3 检索模式

V1 支持三种模式：

1. `bm25`
   - 标题 + 正文字段 BM25
   - 适合精确关键词与实体名查询

2. `vector`
   - query 向量化后做相似度检索
   - 适合语义相近但词面不一致的问题

3. `hybrid`
   - BM25 和向量分别召回 TopK
   - 通过归一化加权合并排序

### 6.4 混合检索排序建议

V1 可以先使用简单稳定的线性融分：

`final_score = alpha * bm25_norm + beta * vector_norm`

建议默认参数：

- `alpha = 0.45`
- `beta = 0.55`

原因：

- Wikipedia 问题很多是自然语言问法，不是简单标题匹配。
- 向量检索通常对中文改写更稳。
- 对英文 query 也可复用同一流程，只要向量接口支持多语言且 query / chunk 使用同模型。
- 保留 BM25 可增强实体词与专有名词精确命中能力。

后续可升级为：

- Reciprocal Rank Fusion
- Learning to Rank
- Reranker 二阶段重排

---

## 7. 分块策略

### 7.1 目标

用户偏好为“中等 chunk”，因此 V1 的目标不是极短召回块，也不是超长上下文块，而是语义完整性和检索粒度折中。
同时，因为后续要支持英文，分块器不得假定只有中文标点规则。

### 7.2 推荐参数

| 参数 | 建议值 | 说明 |
|------|------|------|
| `target_size` | 800 字符 | 理想 chunk 大小 |
| `soft_min_size` | 500 字符 | 尽量避免过短 chunk |
| `hard_max_size` | 1200 字符 | 超出后必须切分 |
| `overlap_size` | 120 字符 | 相邻 chunk 重叠 |
| `token_estimate_ratio` | 1.2 到 1.8 | 中文字符到 token 的估算系数 |

### 7.3 分块流程

V1 建议采用三阶段分块，而不是单纯固定长度滑窗。

#### 阶段一：文本清洗

- 统一换行符
- 去除首尾空白
- 压缩连续空行
- 保留必要标点
- 基于 `language` 使用不同的句边界规则

#### 阶段二：结构预切

优先按较自然边界切分：

- 空行
- 段落
- 句号、问号、感叹号、分号
- 列表边界

#### 阶段三：重叠合并

- 将相邻小段拼接到 `target_size`
- 若接近 `hard_max_size`，优先在句末切开
- 生成 chunk 后，将末尾 `overlap_size` 字符带入下一个 chunk
- 若单段自身超长，则退化为硬切滑窗

### 7.4 为什么不用纯固定窗口

纯固定窗口的问题：

- 可能在句中截断，损伤可读性
- 召回后证据边界不自然
- 评测时难以解释“命中但文本碎裂”的情况

本方案优点：

- 语义边界更自然
- chunk 仍保持中等长度
- 重叠区可减少跨段信息断裂

### 7.5 分块版本化

必须显式记录：

- `chunker_version = v1`
- `chunk_profile_id = medium_overlap_v1`

后续任何参数调整或规则变化，都应升级版本号并允许重建索引，避免评测集和线上行为混淆。

---

## 8. 导入与重建流程

### 8.1 渐进导入

V1 只支持按前 `N` 条导入，满足开发调试需求。

建议接口参数：

- `source_file`
- `limit_n`
- `offset`，可选
- `rechunk`
- `reindex`

### 8.2 导入流程

1. 流式读取 JSONL
2. 解析单条记录
3. 计算 `text_hash`
4. upsert `kb_documents`
5. 选择 `chunk_profile_id`
6. 对新增或变更文档执行分块
7. 将 `raw_content / normalized_content` 写入 `kb_chunks`
8. 调用向量化 API
9. 写入 `kb_chunk_embeddings`
10. 写入 OpenSearch
11. 更新 `kb_ingest_jobs`

### 8.3 重建策略

必须支持以下重建方式：

1. 从 SQLite 全量重建 OpenSearch 索引
2. 按 `doc_id` 重建单篇文章索引
3. 按 `chunker_version` 重建某一版本索引
4. 按 `embedding_model` 重建向量字段

---

## 9. API 设计

知识库主要供内部使用，但需要独立 API 便于测试与指标统计。

### 9.1 导入 API

`POST /kb/ingest`

请求字段建议：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `source_file` | string | 是 | Wikipedia 文件路径 |
| `limit_n` | integer | 否 | 导入前 N 条 |
| `offset` | integer | 否 | 从第几条开始 |
| `rechunk` | boolean | 否 | 是否强制重做 chunk |
| `reindex` | boolean | 否 | 是否强制重建索引 |

### 9.2 检索 API

`POST /kb/search`

请求字段建议：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | string | 是 | 查询文本 |
| `mode` | string | 是 | `bm25/vector/hybrid` |
| `top_k` | integer | 否 | 返回数量 |
| `source_id` | string | 否 | 数据源过滤 |
| `language` | string | 否 | 语言过滤 |
| `chunk_profile_id` | string | 否 | 分块策略过滤 |
| `include_content` | boolean | 否 | 是否返回 chunk 正文 |

### 9.3 评测 API

`POST /kb/eval/run`

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `dataset_id` | string | 是 | 评测集 ID |
| `retrieval_mode` | string | 是 | 检索模式 |
| `top_k` | integer | 是 | TopK |
| `index_name` | string | 否 | 指定索引 |

### 9.4 指标 API

`GET /kb/eval/runs/{eval_run_id}`

返回：

- 总体 Recall@K
- MRR
- NDCG
- 平均耗时
- 按 query 类型拆分指标

---

## 10. 测试集与评测设计

这一部分是 V1 的重点。

### 10.1 核心思路

建议用大模型基于 chunk 自动生成 query，但不能直接把自动生成结果当最终标准。更稳妥的方案是：

1. 从已导入文档中抽样 chunk
2. 用大模型基于 `normalized_content` 生成多种 query
3. 自动绑定 gold chunk / gold 文档
4. 抽样人工复核一部分 query
5. 形成可复用评测集

这比纯人工写 query 成本低，也比纯自动生成更可靠。

### 10.2 Query 生成原则

每个 chunk 建议让大模型生成多类问题：

- `fact`：事实问句
- `definition`：定义类问句
- `entity`：实体定位类问句
- `paraphrase`：改写问句
- `cross_sentence`：需要跨句理解的问题

示例要求：

- 不直接复制原文整句
- 尽量改写表达
- 问题长度有短有长
- 避免答案在 query 中泄露

### 10.3 Gold 标注策略

V1 推荐两级 gold：

1. 文档级 gold
   - 正确答案所在文档

2. chunk 级 gold
   - 最小证据 chunk
   - 必要时允许多个证据 chunk

这样可以同时评估：

- 文档召回是否成功
- chunk 定位是否精准

### 10.4 指标体系

#### 召回指标

| 指标 | 说明 | 作用 |
|------|------|------|
| `Recall@1` | Top1 是否命中 gold | 衡量首位命中 |
| `Recall@3` | Top3 是否命中 gold | 常用快速召回指标 |
| `Recall@5` | Top5 是否命中 gold | 常用主指标 |
| `Recall@10` | Top10 是否命中 gold | 评估召回上限 |

#### 排序指标

| 指标 | 说明 | 作用 |
|------|------|------|
| `MRR` | 首次命中倒数排名均值 | 衡量命中位置 |
| `NDCG@K` | 考虑相关性与排名折损 | 衡量整体排序质量 |

#### 定位指标

| 指标 | 说明 | 作用 |
|------|------|------|
| `Chunk Hit Rate` | 是否命中正确 chunk | 衡量 chunk 设计是否有效 |
| `Doc Hit Rate` | 是否命中正确文档 | 衡量文档级召回 |
| `Boundary Spill Rate` | 命中邻接 chunk 但未命中 gold chunk 的比例 | 评估 chunk 边界问题 |

#### 效率指标

| 指标 | 说明 | 作用 |
|------|------|------|
| `P50 Latency` | 中位检索耗时 | 日常性能观察 |
| `P95 Latency` | 95 分位耗时 | 评估尾部延迟 |
| `Index Throughput` | 每秒索引 chunk 数 | 评估导入性能 |
| `Embedding Throughput` | 每秒向量化 chunk 数 | 评估向量 API 吞吐 |

#### 数据质量指标

| 指标 | 说明 | 作用 |
|------|------|------|
| `Duplicate Chunk Rate` | 重复 chunk 比例 | 检查分块退化 |
| `Empty Chunk Rate` | 空 chunk 比例 | 检查清洗异常 |
| `Oversize Chunk Rate` | 超过上限 chunk 比例 | 检查分块约束 |
| `Embedding Failure Rate` | 向量化失败比例 | 检查外部 API 稳定性 |

### 10.5 重点关注指标

V1 最值得盯的不是单一 Recall，而是下面几组组合：

1. `Recall@5 + MRR`
   - 判断“能不能找到”和“排得靠不靠前”

2. `Chunk Hit Rate + Boundary Spill Rate`
   - 判断 chunk 方案是否合理

3. `P95 Latency + Embedding Failure Rate`
   - 判断系统能否稳定运行

4. `Hybrid vs BM25 vs Vector`
   - 判断混合检索是否真的带来收益

### 10.6 推荐评测流程

1. 从前 `N` 条文档中抽样
2. 为每种 `chunk_profile_id` 生成 chunk
3. 生成 query 数据集
4. 分别运行 `bm25`
5. 分别运行 `vector`
6. 分别运行 `hybrid`
7. 对比不同 `chunk_profile_id` 下的总体指标
8. 对比不同 query 类型指标
9. 抽查失败案例
10. 反推 chunk 参数、混合权重和索引映射问题

### 10.7 我对测试方案的补充建议

除了“让大模型针对 chunk 生成问题”，还应补三类测试：

1. 标题导向 query
   - 例如直接用文章标题、标题改写、别名
   - 用来测 BM25 基线能力

2. 干扰 query
   - 故意构造表述相近但答案不同的问题
   - 用来测混合检索和排序抗干扰能力

3. 分块策略 A/B 测试
   - 固定同一评测集，对不同 `chunk_profile_id` 分别跑检索
   - 用来比较不同 chunk 策略的召回、排序和延迟差异

原因：

- 只用 LLM 基于 chunk 生成 query，容易偏“教科书式问法”
- 实际用户 query 经常更短、更歧义、更口语化
- 不做 chunk 策略 A/B，很难知道性能差异究竟来自检索算法还是分块方式

---

## 11. 推荐 V1 默认配置

| 项目 | 默认值 |
|------|------|
| 数据源 | `wikipedia_20231101_zh_simp.jsonl`，后续增加英文源 |
| 导入模式 | 前 `N` 条渐进导入 |
| 主存储 | `data/knowledge.db` |
| 索引 | `kb_wikipedia_{language}_chunk_v1` |
| 检索模式 | `hybrid` |
| 默认分块策略 | `medium_overlap_v1` |
| chunk target size | 800 字符 |
| overlap size | 120 字符 |
| BM25 / vector 融合权重 | 0.45 / 0.55 |
| 评测主指标 | `Recall@5`, `MRR`, `Chunk Hit Rate`, `P95 Latency` |

---

## 12. 实施顺序

### Phase 1

- 建立 `knowledge.db`
- 完成 `kb_sources / kb_documents / kb_chunk_profiles / kb_chunks / kb_ingest_jobs`
- 支持前 `N` 条导入
- 支持 chunk 生成

### Phase 2

- 接入阿里云向量化 API
- 建立 `kb_chunk_embeddings`
- 建立 OpenSearch 索引
- 支持 `bm25 / vector / hybrid`

### Phase 3

- 建立 `kb_eval_datasets / kb_eval_queries / kb_eval_runs / kb_eval_results`
- 接入 query 自动生成
- 跑离线评测
- 固化指标看板与失败案例分析

---

## 13. 风险与注意事项

### 13.1 分块风险

- overlap 过小会丢上下文
- overlap 过大导致重复召回增多
- chunk 过短会使检索噪声上升
- chunk 过长会降低定位精度

### 13.2 向量化风险

- 外部 API 有限流与失败重试问题
- 模型切换会导致历史 embedding 不可比
- query embedding 与 chunk embedding 必须使用同一模型体系

### 13.3 OpenSearch 风险

- 映射一旦不合理，重建成本较高
- 混合检索的分数归一化方案会影响排序稳定性

### 13.4 评测风险

- LLM 自动生成 query 容易分布过于理想化
- 如果没有抽样人工复核，指标可能虚高

---

## 14. 结论

本方案将知识库设计为一个面向 Wikipedia 的单源模块，支持多语言扩展，采用 `SQLite` 作为事实源、`OpenSearch` 作为检索索引，围绕中等粒度、重叠式 chunk 构建 `BM25 + 向量` 混合检索能力。

V1 的关键不在于一次性做复杂，而在于先把以下四件事做稳：

1. 文档与 chunk 的事实存储
2. 可并行比较的分块策略体系
3. 可重建的索引管线
4. 可复现的离线评测指标体系

只要这四件事稳定，后续再优化 chunk 参数、语言规则、向量模型、重排策略，成本都会可控。
