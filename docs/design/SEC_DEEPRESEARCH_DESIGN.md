# SEC DeepResearch 设计文档

版本：v0.2  
状态：Draft  
日期：2026-04-28

## 1. 目标

在现有知识库能力之上，构建一个面向 SEC 财报的 `deepresearch` 能力，使 Jarvis 可以：

1. 将 SEC 财报 PDF 作为一类正式数据源接入；
2. 使用阿里云解析接口解析 PDF；
3. 对解析后的财报内容按适合金融研究的方式分块；
4. 将可检索的 chunk 存储到 OpenSearch；
5. 基于财报证据执行多步研究、对比、总结，并输出带引用的结论。

该能力应复用现有知识库的基础设施能力，例如 SQLite、embedding、检索服务封装与评估框架；
但在 OpenSearch 层面，应为金融研究单独建设一套独立索引体系，而不是继续复用当前 Wikipedia / 通用知识库索引。

## 2. 范围

### 2.1 纳入范围

- SEC 年报和季报 PDF 导入；
- 阿里云文档解析接入；
- 解析结果清洗、标准化与分块；
- 基于 OpenSearch 的混合检索；
- 以财报为主证据源的研究编排能力；
- 带 chunk 级引用的证据化回答。

### 2.2 V1 暂不纳入

- 严重扫描件的 OCR 兜底；
- 完整的结构化表格重建；
- 与财报之外的数据源混合研究，例如新闻、电话会、外部网站；
- 精排 reranker 或公网浏览 agent；
- 估值模型、Excel 级财务建模。

## 3. 设计原则

- `SQLite` 仍然是事实源，`OpenSearch` 仍然是派生检索索引；
- SEC 数据导入与 deepresearch 编排是两层职责，不混在一起；
- 解析结果必须具备可复现性，并对解析器、分块器、embedding 模型显式版本化；
- 每个 deepresearch 结论都必须能回溯到 chunk、section、页码；
- 数据模型必须支持多数据源，不能只适配 `wikipedia`。

## 4. 当前可复用能力与新增工作

### 4.1 当前可复用

- `app/knowledge_base/repositories.py`
- `app/knowledge_base/chunking.py`
- `app/knowledge_base/indexing.py`
- `app/knowledge_base/search.py`
- 现有 SQLite 表：`sources/documents/chunks/embeddings/eval`
- 现有 OpenSearch 混合检索链路

### 4.2 需要新增

- 将当前 `WikipediaIngestService` 抽象成多数据源 ingest；
- 增加 SEC 财报元数据与解析产物跟踪；
- 接入阿里云 PDF 解析客户端；
- 增加面向财报的分块策略和 metadata 富化；
- 在检索之上增加 deepresearch workflow；
- 增加 SEC ingest / parse / research API。

## 5. 目标用户流程

1. 用户把一个或多个 SEC 财报 PDF 放进本地目录；
2. 系统扫描目录中的 PDF；
3. 系统把 PDF 发送到阿里云解析服务；
4. 系统保存原始解析结果与标准化后的文本；
5. 系统按 section-aware 策略分块；
6. 系统做 embedding 并写入 OpenSearch；
7. 用户提研究问题，例如：
   - “比较微软和谷歌近三年年报中云业务增长驱动因素”
   - “总结英伟达披露的供应链风险，并给出证据”
8. deepresearch workflow 检索相关 chunk、聚合证据、生成带引用的回答。

## 6. 总体架构

```text
SEC PDF
  -> 本地文件扫描
  -> 阿里云文档解析
  -> 原始解析结果落盘
  -> 标准化财报文档
  -> 分块
  -> embedding
  -> OpenSearch
  -> deepresearch retriever
  -> answer synthesizer with citations
```

推荐分层：

- `kb_sources`：数据源定义与数据集归属；
- `kb_documents`：每一份财报 PDF 或财报逻辑文档；
- `kb_chunks`：检索最小单元；
- `kb_chunk_embeddings`：向量缓存；
- `deepresearch service`：问题分析、子查询、证据聚合、回答生成。

## 7. 数据源模型调整

当前 ingest 强依赖 Wikipedia JSONL，这对 SEC 财报不够。

建议增加 source typing：

- `source_type`：`wikipedia`、`sec_filing`
- `source_id`：数据集级别标识
- `document_type`：`10-K`、`10-Q`、`20-F`、`8-K`、`filing_section`

建议在 `kb_sources` 的 metadata 中增加：

- `source_type`
- `dataset_name`
- `description`
- `owner`
- `region`
- `metadata_json`

建议在 `kb_documents.metadata_json` 中保存：

- `company_name`
- `ticker`
- `cik`
- `accession_no`
- `form_type`
- `filing_date`
- `fiscal_year`
- `fiscal_period`
- `pdf_path`
- `sec_url`
- `parser_vendor`
- `parser_version`
- `parse_job_id`

## 8. SEC 导入模型

### 8.1 filing 与 artifacts 模型

从长期架构看，不应把 “SEC 财报 = 一份 PDF” 作为核心数据模型。

更合理的方式是：

- `filing` 表示一份财报披露本体；
- `artifact` 表示围绕该 filing 产生的各种文件和中间产物。

推荐概念模型：

```text
filing
  -> original_pdf_artifact
  -> aliyun_raw_json_artifact
  -> parsed_markdown_artifact
  -> normalized_blocks_artifact
  -> chunks
  -> opensearch_index
```

未来可以继续扩展：

```text
filing
  -> edgar_html_artifact
  -> inline_xbrl_facts
  -> structured_tables
```

也就是说：

- `filing` 是业务对象；
- `pdf` 只是 V1 的一种输入 artifact；
- 阿里云解析 JSON、markdown、normalized blocks 都属于 artifact；
- 后续即便接入 HTML、XBRL、结构化表格，也仍然挂在同一个 filing 之下。

建议在命名上逐步采用：

- `sec_filing`
- `filing_artifact`

而不是把长期模型写死成 `sec_pdf_document`。

### 8.2 V1 文档粒度

V1 建议以“一份 SEC 财报 PDF = 一个 document”建模。

原因：

- 能直接复用当前表结构；
- 事实源最清晰；
- section 信息可以下沉到 chunk metadata；
- 后续重做 chunk 或重建索引更容易。

这只是 V1 的落地方式，不改变长期的 `filing + artifacts` 建模方向。

### 8.3 输入方式

支持两种输入：

1. 本地 PDF 文件路径；
2. 结构化 manifest。

建议 manifest 字段：

- `company_name`
- `ticker`
- `cik`
- `form_type`
- `filing_date`
- `fiscal_year`
- `fiscal_period`
- `accession_no`
- `pdf_path`
- `sec_url`

### 8.4 V1 当前确定范围

- 文档类型：先支持 `10-K` 和 `10-Q`
- 数据入口：本地 PDF 目录导入
- 语言：不做翻译存储，但 deepresearch 在回答阶段可自行处理中英表达

## 9. 阿里云 PDF 解析

建议增加解析客户端模块：

- `app/knowledge_base/parsers/alibaba_pdf.py`

职责：

- 发起解析任务；
- 轮询异步任务状态；
- 保存原始 JSON 响应；
- 标准化解析输出。

V1 采用阿里云 AI Search Open Platform 的异步文档解析接口：

- `service_id = ops-document-analyze-002`
- 输入方式：本地 PDF 转 Base64 后通过 `document.content` 上传
- 同时传 `document.file_name`
- `document.file_type = pdf`

采用异步接口的原因：

- 当前项目输入是本地 PDF；
- 官方文档明确提示同步接口存在 HTTP 超时风险，不建议生产使用；
- `ops-document-analyze-002` 更适合复杂 PDF，包括表格、图片、版面元素。

### 9.1 重要约束

根据阿里云官方文档：

- 请求体最大 `8MB`
- PDF 解析结果正文返回为 `markdown`
- 异步流程分两步：
  1. `POST /document-analyze/{service_id}/async`
  2. `GET /document-analyze/{service_id}/async/task-status?task_id=...`
- `strategy.enable_semantic` 会改善 markdown 层级，但会增加延迟，超长文档可能被服务端自动降级。

### 9.2 当前真实验证结果

`data/sec-pdf` 目录下的全部样例 PDF 已经通过真实阿里云接口解析成功，原始结果保存在：

- `data/sec-pdf/aliyun-raw`

这说明“本地 PDF -> 阿里云异步解析 -> 原始 JSON 落盘”这条链路已经打通。

### 9.3 内部标准化结构

标准化后的内部结构至少要保留：

- `page_number`
- `block_type`：`heading` / `paragraph` / `table` / `list` / `image`
- `block_text`
- `block_order`
- `section_heading`
- `section_path`
- `bbox`，如后续需要可保留

即便 V1 暂时不使用版面坐标做检索，也应保留这些字段，为后续更精准引用和图文增强做准备。

### 9.4 运行级记录表结构

除了保存最终产物，还应保存“这些产物是怎么生成的”。

原因：

- 解析参数会调整；
- 分块策略会调整；
- embedding 模型会切换；
- OpenSearch mapping 和索引版本会演进；
- 如果没有 run 级记录，后续很难比较不同实验结果，也很难排查质量波动。

建议增加以下运行级表：

#### `kb_parse_artifacts`

记录一次解析产物与其来源。

建议字段：

- `artifact_id`
- `filing_id`
- `artifact_type`
- `parser_vendor`
- `parser_model`
- `parser_version`
- `parse_config_json`
- `input_sha256`
- `raw_output_path`
- `normalized_output_path`
- `status`
- `created_at`

#### `kb_chunk_runs`

记录一次分块运行。

建议字段：

- `chunk_run_id`
- `filing_id`
- `parse_artifact_id`
- `chunk_profile_id`
- `chunker_version`
- `config_json`
- `chunk_count`
- `status`
- `created_at`

#### `kb_index_runs`

记录一次索引运行。

建议字段：

- `index_run_id`
- `source_id`
- `chunk_run_id`
- `index_name`
- `embedding_model`
- `embedding_dim`
- `opensearch_mapping_version`
- `status`
- `created_at`

这些表的核心作用不是替代 `documents/chunks/embeddings`，而是补充“运行上下文”和“版本追踪”。

换句话说：

- `document / chunk / embedding` 保存事实结果；
- `parse_run / chunk_run / index_run` 保存这些结果是如何产生的。

## 10. SEC 文档分块策略

Wikipedia 式分块对财报不够。SEC 财报具有强 section 结构、长段落、大表格、图片占位、法律语言重复等特点。

### 10.1 V1 总体策略

- 优先使用 heading-aware 边界；
- 尽量把表格与相邻解释文字放在一起；
- chunk 需要带页码范围；
- chunk 需要带 section path；
- overlap 适度，避免风险披露等内容过度重复。

### 10.2 推荐 V1 chunk profile

- `chunk_profile_id = sec_filing_medium_v1`
- `target_size = 1600`
- `soft_min_size = 900`
- `hard_max_size = 2400`
- `overlap_size = 200`

这个配置比现有 Wikipedia 的 800 字更大，原因是财报研究更依赖语义完整证据块，而不是极碎的事实块。

### 10.3 分块元数据

- `page_start`
- `page_end`
- `section_title`
- `section_path`
- `block_types`
- `table_title`
- `table_continued`
- `image_count`
- `company_name`
- `ticker`
- `form_type`
- `filing_date`
- `fiscal_year`
- `fiscal_period`

### 10.4 长表处理

V1 不应把所有长表整张塞进一个 oversized chunk，这会造成检索噪声和引用不稳定。

建议规则：

1. 短表完整保留；
2. 长表按“语义行组”拆分，而不是按字符数硬切；
3. 每个拆分后的表格 chunk 都重复表标题和列表头；
4. 尽量把表格附近的解释段落与表格保持关联。

优先的拆表边界：

- 年份区块
- 业务分部区块
- Note 内的自然小节
- subtotal 到 subtotal 的自然区间

每个表格子 chunk 至少保留：

- 原始表标题
- 重复后的表头
- 所属 section
- 页码范围
- `table_continued = true/false`

这样可以避免两个典型问题：

- 检索到表格碎片但没有表头；
- 检索到数字但丢失表格含义和说明上下文。

表格周边文本的建议行为：

- 表格前的说明段落可单独存为文本 chunk；
- 表格主体可拆成一个或多个表格 chunk；
- 表格后若有紧跟的短结论段，且长度允许，可以并入最后一个表格 chunk。

### 10.5 图片占位处理

阿里云返回的 markdown 中可能出现 `![IMAGE]...` 这类图片占位。

在 V1 中，这类内容应视为“结构信号”，而不是主要证据。

建议规则：

1. 标准化时识别为 `block_type = image`；
2. 默认不对纯图片块做向量化；
3. 如果图片块前后有解释文字，则并入邻近文本 chunk；
4. 如果整页基本都是图片，则保留 metadata，但不作为高优先级检索证据；
5. 原始图片占位在 raw parse artifact 中保留，为后续多模态升级做准备。

原因：

- 图片占位说明原 PDF 在该位置存在视觉内容；
- 对 SEC V1 而言，最有价值的研究证据仍然是 heading、段落、表格以及表格相邻说明；
- 如果完全删除图片占位，会丢失布局和边界信号。

### 10.6 重要 section 特殊处理

这些 section 是高价值检索区域，应保持可识别：

- `Risk Factors`
- `MD&A`
- `Business`
- `Notes to Consolidated Financial Statements`

建议：

- `Risk Factors`：按风险条目或子标题切；
- `MD&A`：按业务主题或自然段落组切；
- `Financial Statements / Notes`：允许更大的 chunk，并优先保留表格前后说明；
- `10-K` 和 `10-Q` 先共用一套 chunk profile，但 metadata 必须区分 `form_type/year/quarter`。

## 11. OpenSearch 索引设计

金融研究不应继续沿用当前 Wikipedia / 通用知识库索引，而应建设独立的 OpenSearch 索引。

原因：

- 金融研究语料与通用知识库语料结构差异很大；
- 财报检索高度依赖公司、财报类型、年份、季度、section、页码等元数据；
- 金融检索的过滤、排序、引用需求与 Wikipedia 类文本明显不同；
- 独立索引更利于后续演进为面向金融分析任务的专用检索体系。

建议增加字段：

- `source_type`
- `company_name`
- `ticker`
- `cik`
- `accession_no`
- `form_type`
- `filing_date`
- `fiscal_year`
- `fiscal_period`
- `page_start`
- `page_end`
- `section_title`
- `section_path`
- `block_types`
- `document_id`
- `chunk_run_id`
- `parse_artifact_id`
- `chunk_profile_id`
- `embedding_version`
- `content_hash`
- `section_level`
- `section_item_no`
- `is_table_chunk`
- `is_risk_factor_chunk`

建议索引命名：

- `kb_sec_filing_en_sec_filing_medium_v1`

推荐索引隔离策略：

- `kb_wikipedia_*`
- `kb_sec_*`

其中：

- `kb_wikipedia_*` 继续承载通用知识库或 Wikipedia 类语料；
- `kb_sec_*` 专门承载金融研究相关语料；
- 后续如接入财报电话会、研报、公告，也应优先考虑放入独立金融索引体系，而不是混入通用索引。

## 12. 检索要求

### 12.1 检索模式

继续保留：

- `bm25`
- `vector`
- `hybrid`

### 12.2 V1 过滤条件

- `ticker`
- `company_name`
- `form_type`
- `filing_date_from`
- `filing_date_to`
- `fiscal_year`
- `section_title`

### 12.3 检索返回字段

每个 hit 至少返回：

- `chunk_id`
- `doc_id`
- `score`
- `company_name`
- `ticker`
- `form_type`
- `filing_date`
- `section_title`
- `page_start`
- `page_end`
- `content`

## 13. DeepResearch Workflow

`deepresearch` 不应只是一个简单 search API，而应是检索之上的工作流。

### 13.1 V1 workflow

1. `question analysis`
   - 识别目标公司
   - 识别时间范围
   - 识别任务类型：总结、对比、变化跟踪、风险提取
2. `retrieval planning`
   - 生成 3 到 8 个子查询
   - 决定过滤条件与目标 form type
3. `evidence retrieval`
   - 对每个子查询跑 hybrid search
   - 去掉高度重复 chunk
4. `evidence organization`
   - 按公司 / filing date / section 分组
5. `answer synthesis`
   - 严格基于证据回答
   - 附上引用
6. `final report formatting`
   - 精简结论
   - 证据要点
   - 来源列表

### 13.2 V1 输出协议

最终结果至少包含：

- `answer`
- `findings`
- `citations`
- `uncertainty_note`

引用格式示例：

`[MSFT 10-K 2025-07-30, MD&A, p.42-43, chunk msft_2025_10k:chunk:0012]`

### 13.3 回答语言

底层证据以英文财报原文为准，不做翻译入库。  
但 deepresearch 在最终回答阶段可以根据用户问题语言，输出中文总结或中英混合表达。

## 14. 新增 API

### 14.1 SEC 解析

`POST /kb/sec/parse`

建议请求字段：

- `input_dir`
- `output_dir`
- `file_names`
- `force`
- `poll_interval_seconds`
- `timeout_seconds`
- `limit`

### 14.2 SEC ingest

`POST /kb/sec/ingest`

建议请求字段：

- `source_id`
- `manifest` 或 `file_paths`
- `chunk_profile_id`
- `parse_only`
- `reparse`
- `rechunk`
- `reindex`

### 14.3 SEC index

`POST /kb/sec/index`

建议请求字段：

- `source_id`
- `chunk_profile_id`
- `top_limit`

### 14.4 SEC search

`POST /kb/sec/search`

建议请求字段：

- `query`
- `mode`
- `top_k`
- `tickers`
- `form_types`
- `filing_date_from`
- `filing_date_to`
- `section_titles`

### 14.5 Deepresearch

`POST /research/sec`

建议请求字段：

- `question`
- `tickers`
- `form_types`
- `filing_date_from`
- `filing_date_to`
- `max_chunks`
- `max_subqueries`

建议响应字段：

- `answer`
- `findings`
- `citations`
- `queries_used`
- `retrieved_chunks`
- `research_trace`

## 15. 数据质量与评估

现有 KB eval 框架可以复用，但要增加 SEC 特有指标。

### 15.1 数据质量指标

- parse success rate
- empty block rate
- table text loss rate
- duplicate chunk rate
- oversize chunk rate
- section tagging coverage

### 15.2 检索指标

- Recall@5
- MRR
- chunk hit rate
- section hit rate
- citation correctness 抽样检查

### 15.3 研究质量评估

人工抽样检查：

- 回答是否真正基于证据；
- 是否错误混淆了不同期间或不同 form；
- 引用是否能对应正确公司、页码、section。

## 16. 实施阶段

### Phase 1：多数据源知识库基础

- 抽象当前 Wikipedia-only ingest；
- 增加 `source_type`；
- 增加 SEC manifest ingest；
- 持久化 SEC filing metadata；
- 建立 `filing + artifacts` 的基础数据模型；
- 预留 `parse_run / chunk_run / index_run` 运行记录结构。

### Phase 2：PDF 解析与原始数据标准化

- 接入阿里云解析客户端；
- 将 raw parse artifact 落盘；
- 把 markdown 解析成统一 block 结构；
- 保留 section / table / image 信号；
- 建立 parse artifact 与 parse run 的关联。

### Phase 3：分块、索引与检索

- 实现 SEC chunk profile；
- 建 SEC chunk metadata；
- 写入 OpenSearch；
- 跑 SEC 过滤检索；
- 建立 chunk run 与 index run 的可追踪链路。

### Phase 4：deepresearch workflow

- 实现问题分析与子查询生成；
- 实现 evidence grouping；
- 实现 citation-aware synthesis；
- 暴露 `/research/sec`。

### Phase 5：评估与调优

- 构建 SEC eval dataset；
- 对比 chunk profile；
- 调 hybrid 权重；
- 调整 citation 格式与过滤逻辑。

## 17. 验收标准

V1 满足以下条件时可认为合格：

1. 本地 SEC PDF 可以成功解析并保存原始结果；
2. 解析后的内容可以标准化为 page/block/section 结构；
3. chunk 保留公司、财报、section、页码信息；
4. 过滤检索能稳定返回公司级财报证据；
5. deepresearch 输出包含精简结论和精确引用；
6. 可以对同一公司跨年份财报，或不同公司财报做比较；
7. 可以在不重新解析原 PDF 的情况下，从原始解析结果或 SQLite 重建索引。

## 18. 风险

- 阿里云解析结果对复杂表格的保真度可能不足；
- SEC 财报长且重复，容易造成召回重复和总结噪声；
- 图片占位暂不做视觉理解，可能损失部分图表信息；
- 多公司、多期间比较时，若过滤不严，容易混淆不同年份或 form type；
- 当前 SQLite 连接生命周期管理尚未完全工程化，长生命周期服务后续应补显式关闭逻辑。

## 19. 已确认决策

1. V1 只做 `10-K / 10-Q`
2. 数据入口先做本地 PDF 目录导入
3. 阿里云解析模型使用 `ops-document-analyze-002`
4. 原始解析结果保存在 `data/` 目录，不提交 Git
5. 回答层可根据用户语言输出中文，但证据仍以原始英文财报为准
6. V1 先做“最短可行研究回答”，但架构允许以后扩展成长报告
7. OpenSearch 层面不复用当前通用知识库索引，金融研究使用独立索引体系
8. 长期数据模型采用 `filing + artifacts`，PDF 只是 V1 输入 artifact
9. 需要补 `parse_run / chunk_run / index_run` 用于版本追踪与可复现

## 20. 推荐的 V1 落地切法

如果目标是尽快落地一个可用版本，建议：

- 范围限定在 `10-K / 10-Q`
- 仅支持本地 PDF
- 先打通“解析 -> 标准化 -> 分块 -> 检索 -> 短回答”
- 先不做翻译入库
- 先不做图像理解
- 优先把表格与表格前后说明处理好
- 输出以“短答案 + 引用”为主，后续再扩展成长报告模板
