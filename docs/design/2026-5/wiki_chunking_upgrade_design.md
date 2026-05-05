# Wiki 分块升级设计

## 背景

当前 wiki 知识库使用 `medium_overlap_v1` 分块策略：

- `target_size = 800`
- `soft_min_size = 500`
- `hard_max_size = 1200`
- `overlap_size = 120`

实现位于 `app/knowledge_base/chunking.py`。它的逻辑是先按段落、句子切分，再按字符数聚合，并在 chunk 间加入固定字符 overlap。这个策略简单稳定，但上下文表达不足：

- chunk 内没有显式注入文档标题。
- chunk 内没有显式注入章节标题或段落标题。
- `section_path` 对 wiki 始终为 `None`。
- reranker 只能看到局部正文，不能稳定知道 chunk 属于哪个词条、哪个章节。
- query 如果是实体别名、章节主题、上位概念，纯正文 chunk 可能缺少可匹配上下文。

## 目标

新增 wiki 分块策略 `wiki_heading_context_v2`，让每个 chunk 都携带更完整的检索上下文，同时保留旧分块策略用于回滚和对照评测。

目标能力：

1. 每个 chunk 注入文档标题。
2. 如果源数据包含章节/段落标题，每个 chunk 注入所在 `section_path`。
3. OpenSearch 索引保留原始正文和用于检索/rerank 的上下文文本。
4. 新旧分块 profile 可以并存、独立索引、独立评测。
5. 评测可以区分旧 chunk gold、新 chunk gold、doc-level hit 三种口径。

## 当前分块问题

当前 wiki chunk 内容大致是：

```text
数学是研究数量、结构与变化的学科。它在科学与工程中有广泛应用。
```

升级后建议用于 embedding / OpenSearch content / reranker 的文本是：

```text
标题：数学
章节：应用 > 科学

数学是研究数量、结构与变化的学科。它在科学与工程中有广泛应用。
```

如果源数据没有章节结构，则至少注入文档标题：

```text
标题：数学

数学是研究数量、结构与变化的学科。它在科学与工程中有广泛应用。
```

## 数据源要求

当前 `WikipediaIngestService` 读取的 record 主要字段是：

- `id`
- `url`
- `title`
- `text`

如果原始 wiki dump 已经丢失章节标题，那么只能做文档标题注入，不能恢复真实章节路径。

如果要做完整章节感知分块，需要 ingestion 支持以下任一输入形态：

```json
{
  "id": "13",
  "title": "数学",
  "url": "...",
  "sections": [
    {
      "section_path": ["应用", "科学"],
      "text": "..."
    }
  ]
}
```

或在 plain text 中保留可解析标题，例如：

```text
== 应用 ==

=== 科学 ===

正文...
```

## 新分块策略

### Profile

新增 `kb_chunk_profiles` 记录：

```text
chunk_profile_id = wiki_heading_context_v2
name = Wiki Heading Context V2
language = zh
chunker_version = wiki_heading_context_v2
target_size = 900
soft_min_size = 500
hard_max_size = 1300
overlap_size = 120
boundary_rules_json = {
  "mode": "heading_aware",
  "inject_document_title": true,
  "inject_section_path": true,
  "preserve_section_boundary": true
}
normalization_rules_json = {
  "context_prefix_format": "标题：{title}\\n章节：{section_path}\\n\\n{content}"
}
```

参数可以先保守接近 `medium_overlap_v1`，避免一次改变太多变量。真正影响检索的是标题/章节上下文注入和 section boundary。

### 切分规则

1. 优先按 wiki section 切分。
2. section 内按段落、句子聚合。
3. 不跨一级或二级 section 合并 chunk。
4. 同一 section 内允许 overlap。
5. section 变化时不带上一个 section 的 overlap。
6. 每个 chunk 记录：
   - `section_path`
   - `metadata_json.document_title`
   - `metadata_json.section_title`
   - `metadata_json.context_prefix`
   - `metadata_json.content_without_context_hash`

### 文本字段建议

当前 `kb_chunks` 只有：

- `raw_content`
- `normalized_content`
- `section_path`
- `metadata_json`

短期不改表时：

- `raw_content`：保留不带标题注入的原始 chunk 正文。
- `normalized_content`：保存注入标题/章节后的检索文本。
- `metadata_json.content_text` 或 `metadata_json.body_text`：保存不带上下文前缀的正文。

长期更清晰的表结构可以加字段：

- `body_content`
- `retrieval_content`
- `rerank_content`

但短期可以先利用现有字段承载。

## 表结构兼容性评估

### 可以容纳的部分

当前 schema 已经有这些能力：

- `kb_chunk_profiles` 可以保存多个分块 profile。
- `kb_chunks.chunk_profile_id` 可以区分不同分块策略。
- `kb_chunks.section_path` 可以保存章节路径。
- `kb_chunks.metadata_json` 可以保存标题、章节、注入配置等扩展元数据。
- OpenSearch index name 包含 `chunk_profile_id`，例如：

```text
kb_wikipedia_zh_medium_overlap_v1
kb_wikipedia_zh_wiki_heading_context_v2
```

理论上，新旧分块可以并存，新旧索引可以并存，新旧 eval run 可以分别记录。

### 当前实现的阻塞点

虽然表结构看起来支持多 profile，但当前 `WikipediaIngestService` 生成 chunk id 时没有包含 `chunk_profile_id`：

```python
chunk_id = f"{document['doc_id']}:chunk:{chunk.chunk_index:04d}"
```

而 `kb_chunks.chunk_id` 是主键。结果是：

- 同一个 document 用新 profile 重新分块时，`chunk:0000` 会和旧 profile 的 `chunk:0000` 冲突。
- `ChunkRepository.save()` 在 `ON CONFLICT(chunk_id)` 时会更新原记录。
- 这会把旧 profile chunk 覆盖成新 profile chunk。
- `kb_chunk_embeddings` 也以 `chunk_id` 为主键，会被新 profile 复用或覆盖。

所以结论是：

```text
表结构基本能容纳多 profile，但当前 chunk_id 生成规则不能安全容纳多 profile 并存。
```

必须先改 chunk id 规则。

建议新规则：

```text
{doc_id}:profile:{chunk_profile_id}:chunk:{chunk_index:04d}
```

示例：

```text
wikipedia_zh_real_300:13:profile:wiki_heading_context_v2:chunk:0000
```

兼容策略：

- 旧 chunk id 不迁移，继续保留。
- 新 profile 使用新 chunk id。
- 所有新代码按 `doc_id + chunk_profile_id + chunk_index` 查询，不依赖旧 id 格式。

## 重新分块与评测数据集

你的理解基本正确：如果重新分块，旧 eval dataset 不能直接作为严格 chunk-level gold 复用。

原因是 `kb_eval_queries.target_chunk_id` 指向旧 chunk：

```text
target_chunk_id = wikipedia_zh_real_300:106:chunk:0000
```

重新分块后：

- chunk 边界变了。
- chunk index 变了。
- chunk id 应该变了。
- 旧 target chunk 可能被拆成多个新 chunk，也可能和相邻内容合并。

因此旧评测集直接跑新 profile 会出现两类问题：

1. 严格 chunk hit 不成立：检索结果返回新 chunk id，不可能等于旧 `target_chunk_id`。
2. 即使内容相关，也会被算 miss。

### 可选评测迁移方案

#### 方案 A：重新生成 eval dataset

最干净。

流程：

1. 用 `wiki_heading_context_v2` 重新分块。
2. 基于新 chunk 重新生成 query。
3. 新建 `kb_eval_dataset_xxx`。
4. 对新索引跑 `bm25/vector/rrf/rerank`。

优点：chunk-level 指标严格有效。  
缺点：新旧 dataset 不完全同题，横向对比有噪声。

#### 方案 B：旧 query 迁移到新 chunk

保留旧 query_text，但把 gold 从旧 chunk 映射到新 chunk。

可按以下方式映射：

1. 读取旧 query 的 `doc_id` 和旧 `target_chunk_id`。
2. 找到旧 target chunk 的字符区间 `[char_start, char_end]`。
3. 在同一 `doc_id` 的新 profile chunks 中找 overlap 最大的新 chunk。
4. 生成新 eval dataset：
   - `query_text` 沿用旧 query。
   - `doc_id` 沿用旧 doc。
   - `target_chunk_id` 改为新 chunk id。
   - `gold_evidence_json` 记录新 chunk id，同时保留旧 chunk id 和 overlap 信息。

优点：新旧 query 相同，对比分块策略更公平。  
缺点：如果 chunk 边界变化大，一个旧 gold 可能对应多个新 chunks。

建议支持多 gold：

```json
{
  "old_target_chunk_id": "wikipedia_zh_real_300:106:chunk:0000",
  "mapped_target_chunk_ids": [
    "wikipedia_zh_real_300:106:profile:wiki_heading_context_v2:chunk:0000",
    "wikipedia_zh_real_300:106:profile:wiki_heading_context_v2:chunk:0001"
  ],
  "mapping_method": "char_overlap"
}
```

当前 `run_evaluation()` 只检查 `target_chunk_id` 单一命中，不支持多 gold。短期可以先取 overlap 最大的新 chunk；长期应改成支持 `gold_evidence_json` 多 chunk 命中。

#### 方案 C：增加 doc-level / boundary-aware 指标

为了比较不同 chunk profile，可以补充：

- `doc_hit_rate@k`：命中文档即可。
- `char_overlap_hit@k`：检索 chunk 与 gold 字符区间有足够 overlap 即命中。
- `multi_gold_recall@k`：命中任一 gold evidence chunk 即命中。

这比单一 `target_chunk_id` 更适合评估分块策略变化。

## 实施计划

### Phase 1：安全支持新 profile 并存

1. 新增 chunk id 生成函数。
2. 新 profile 使用包含 `chunk_profile_id` 的 chunk id。
3. 保持旧 profile chunk id 不变，避免迁移风险。
4. 增加测试：同一 doc 下 `medium_overlap_v1` 和 `wiki_heading_context_v2` chunks 可以并存。

### Phase 2：实现 wiki heading-aware chunker

1. 新增 `app/knowledge_base/wiki_chunking.py`。
2. 支持 document title 注入。
3. 支持 section_path 注入。
4. 若源数据没有 sections，退化为 title-only context chunking。
5. 写入 `section_path` 和 `metadata_json`。

### Phase 3：重新分块与索引

1. 添加 `wiki_heading_context_v2` profile。
2. 对 `wikipedia_zh_real_300` 生成新 chunks。
3. 建新索引：

```text
kb_wikipedia_zh_wiki_heading_context_v2
```

4. 不删除旧索引。

### Phase 4：评测迁移

优先做方案 B：

1. 基于旧 200q dataset 生成迁移 dataset。
2. 用字符 overlap 映射旧 target chunk 到新 target chunk。
3. 跑同题对照评测。
4. 报告同时列：
   - old chunk profile
   - new chunk profile
   - doc_hit_rate
   - chunk_hit_rate
   - MRR
   - nDCG
   - latency

### Phase 5：多 gold 指标

升级 `KnowledgeBaseEvaluationService`：

1. 解析 `gold_evidence_json`。
2. 支持多个 target chunk。
3. `_find_hit_rank()` 改为命中任一 gold id。
4. 增加 char-overlap hit 可选口径。

## 结论

当前库表设计方向是对的，已经有 `chunk_profile_id`、`section_path`、`metadata_json`、`eval_runs.chunk_profile_id`，可以支撑重新分块、重新索引和重新评测。

但有两个必须修的问题：

1. 当前 `chunk_id` 没有包含 `chunk_profile_id`，新旧分块不能安全并存。
2. 当前 eval dataset 的 gold 是单一 `target_chunk_id`，重新分块后旧 dataset 不能直接作为严格 chunk-level 评测复用。

推荐路线是先修 chunk id，然后做 `wiki_heading_context_v2`，再把旧 200q query 通过字符 overlap 迁移成新 dataset。这样可以最大程度保留同题可比性。
