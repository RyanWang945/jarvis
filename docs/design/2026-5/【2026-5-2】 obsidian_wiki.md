# Jarvis obsidian_wiki 设计

| 项目 | 内容 |
|------|------|
| 作者 | Codex |
| 日期 | 2026-05-02 |
| 版本 | v1.0 |
| 状态 | 设计中 |
| 依赖 | 【2026-5-1】Jarvis 长运行多轮对话 ReAct Agent 设计、【2026-5-1】Jarvis Token 窗口管理与上下文压缩设计 |

---

## 1. 背景

Jarvis 现在已经在往长运行、多轮对话、统一 ReAct runtime 的方向收敛，但“长期知识沉淀”还缺少一层稳定结构。

当前问题有三个：

- 对话历史保存在数据库里，适合审计和回放，但不适合作为长期可维护知识资产。
- 原始资料来源很多，包括对话、网页、文档、仓库、已有 Obsidian 笔记，缺少统一编译规则。
- LLM 直接查原始资料成本高、噪声大，而且容易把“事实层”和“推断层”混在一起。

因此需要引入一层 **LLM Wiki**：把分散的 raw sources 编译成结构化 Markdown 页面，再由 Jarvis 和用户共同维护。

这层设计的目标不是做一个复杂知识图谱系统，而是做一个 **可读、可写、可检索、可审计** 的本地知识工作区，并优先兼容 Obsidian 的使用习惯。

---

## 2. 核心思路

采用 LLM Wiki pattern：

```text
Raw Sources -> Wiki -> Retrieval / Reasoning
           \-> Schema /
```

四层定义如下：

- `Raw Sources`：原始事实输入，只追加、不直接改写。
- `Schema`：约束 Wiki 的目录、命名、页面类型、frontmatter 和链接规范。
- `Wiki`：LLM 编译后的知识页，是 Jarvis 默认优先查询的长期知识层。
- `Retrieval / Reasoning`：查询时优先读 Wiki，必要时回看 Raw Sources 验证证据。

核心原则：

- **raw 与 wiki 分层**：原始资料和知识页必须分开存放。
- **写入显式化**：Jarvis 只有在用户明确允许或策略允许时才写 Wiki。
- **查询默认读 wiki**：避免每次都把原始上下文重新喂给模型。
- **编译优先增量**：不是每次全量重建，而是按 source/page 增量更新。
- **人机共编**：Jarvis 可以生成、更新、整理页面，但 schema 和最终组织权仍由用户掌握。

---

## 3. 产品定位

Jarvis 里的 `obsidian_wiki` 不是一个独立产品，而是 ReAct runtime 的长期记忆子系统。

它承担三类职责：

- **知识沉淀**：把项目架构、设计决策、操作手册、术语说明沉淀为稳定页面。
- **上下文增强**：在后续对话中，为 `ContextAssembler` 或 `knowledge retrieval` 提供可控的长期背景。

它不承担的职责：

- 不替代数据库中的 message / turn / tool_call 审计事实。
- 不替代 git 仓库中的源码和 commit 历史。
- 不在 v1 直接做自动知识图谱、复杂双向同步和全自动页面去重。

---

## 4. v1 目标与非目标

### 4.1 v1 目标

- 提供一个 Obsidian 兼容的本地 Wiki 目录结构。
- 支持把对话、网页、文档、已有笔记导入到 `raw/`。
- 支持生成候选 draft，并在用户确认后将 draft 应用到正式 wiki 页面。
- 支持按 query 检索 wiki 页面，并可按模式回查 raw source。
- 支持后台维护，处理最基础的 frontmatter、page_type、source_ids 和死链问题。
- 支持把 Jarvis 的设计文档、决策记录、概念说明、操作手册持续沉淀进 Wiki。

### 4.2 v1 非目标

- 不做 Obsidian 插件开发。
- 不做复杂 GUI。
- 不做自动后台持续重写全部页面。
- 不做自动合并全部重复知识页。
- 不做真正意义上的图数据库或实体关系图谱。
- 不要求一开始就和 OpenSearch / 向量库深度绑定。

---

## 5. 信息模型

### 5.1 Raw Source

`raw/` 保存未经知识化编译的原始输入，按来源分类：

- 对话转录
- 文档快照
- 网页抓取结果
- 仓库说明
- 已有 Obsidian 笔记镜像

raw source 建议元数据：

```text
source_id
source_type: conversation | document | web | repo | obsidian_note
title
source_ref
created_at
collected_at
content_hash
status: active | superseded | ignored
tags[]
```

约束：

- raw 默认不可改写，只允许追加、标记 superseded 或重新导入。
- raw 必须保留来源信息，不能只保留正文。
- raw 是证据层，不承担最终组织职责。

### 5.2 Wiki Page

Wiki page 是编译后的结构化知识页，建议统一 frontmatter：

```yaml
---
title: Jarvis ReAct Runtime
page_type: design
status: draft
source_ids:
  - conv_2026_05_02_001
updated_at: 2026-05-02
tags:
  - jarvis
  - design
---
```

正文建议包含：

- 页面摘要
- 核心事实
- 决策或结论
- 相关页面链接
- 原始来源
- 待确认项

### 5.3 Schema

Schema 不是一份抽象说明，而是一组可执行约束：

- 允许哪些目录
- 页面怎么命名
- 每类页面需要哪些 frontmatter
- 哪些页面必须有 source 引用
- 哪些页面可以由 Jarvis 自动更新

---

## 6. Obsidian 目录设计

推荐目录如下：

```text
JarvisWiki/
  schema/
    wiki-schema.md
    naming.md
    page-types.md
    writing-rules.md

  raw/
    conversations/
    documents/
    web/
    repos/
    obsidian-notes/

  wiki/
    index.md
    inbox/
    projects/
      jarvis/
        index.md
        designs/
        decisions/
    concepts/
    tools/
    playbooks/

  drafts/
  templates/
  logs/
```

目录职责：

- `schema/`：规则层，由用户和 Jarvis 共同维护。
- `raw/`：证据层，只追加。
- `wiki/`：知识层，面向查询和长期维护。
- `drafts/`：编译过程中的候选稿，避免直接污染正式 wiki。
- `templates/`：页面模板。
- `logs/`：编译日志、lint 报告、冲突记录。

其中 `wiki/inbox/` 很重要，它用来承接暂时无法归类、但值得保留的知识页。这样可以避免 v1 因为“分类不完美”而阻塞写入。

---

## 7. 页面类型设计

建议 v1 先固定 5 种页面类型，避免过度抽象：

| page_type | 用途 | 示例 |
|-----------|------|------|
| `index` | 目录页 | `projects/jarvis/index.md` |
| `design` | 方案设计 | 某个新模块设计草案、架构说明 |
| `decision` | 决策记录 | 为什么选 ReAct 而不是 DAG |
| `playbook` | 操作手册 | 发布流程、排障步骤 |
| `concept` | 概念说明 | turn、conversation、active_summary |

收敛原则：

- `architecture` 并入 `design`，不单独设类型。
- `faq` 并入 `concept` 或 `playbook`。
- `postmortem` 等任务系统稳定后再引入。
- `people` 目录第一版不做，避免引入个人信息、权限和隐私治理问题。

页面命名原则：

- 目录层用英文、短语义名。
- 页面标题可以是中文。
- 文件名尽量稳定，避免频繁改名导致链接漂移。
- 决策页和复盘页允许带日期前缀。

---

## 8. Tool 接口设计

建议把 `obsidian_wiki` 能力抽象为 4 个高层 ReAct tools，对外隐藏更细的 `ingest / compile / lint / refresh` 流水线细节。

### 8.1 `obsidian_wiki_query(query, scope, query_mode)`

职责：

- 查询长期知识
- 返回命中的页面摘要、路径和相关 source 引用
- 按模式决定是否回查 raw

查询模式：

- `wiki_only`：只查 wiki
- `wiki_then_raw`：先查 wiki，不足时回查 raw
- `raw_only`：只查 raw，用于证据追溯

适用场景：

- 平时回答问题
- 为 ReAct loop 补长期背景
- 追溯结论的原始依据

### 8.2 `obsidian_wiki_draft(input_ref, draft_type, target_hint)`

职责：

- 将值得沉淀的信息整理为候选 draft
- 推测 page_type、标题和候选路径
- 生成可供用户确认的知识稿

输入来源可以是：

- 某个对话片段
- 某个 raw source
- 某次任务结论
- 某个已有页面的补充更新请求

约束：

- `draft` 只生成候选内容，不直接改正式 wiki。
- 如果需要大幅改写已有页面，也应先走 `draft`。

### 8.3 `obsidian_wiki_apply(draft_id, target_page)`

职责：

- 将用户确认过的 draft 应用到正式 wiki 页面
- 新建 page 或更新已有 page
- 保留 source 关系和 apply log

约束：

- `apply` 是唯一允许写正式 wiki 的高层工具。
- 未确认的 draft 不应直接落正式页。

### 8.4 `obsidian_wiki_maintain(scope, mode)`

职责：

- 做后台治理和轻维护
- 修复 frontmatter、page_type、source_ids、死链等基础问题
- 整理 inbox、做轻量 refresh、补充缺失元数据

约束：

- `maintain` 默认不做大规模语义重写。
- 涉及页面内容的大改，应该重新走 `draft -> apply`。

Phase 2 再补：

- 孤儿页检查
- 标题重复检查
- inbox 长期滞留治理

---

## 9. 写入与编译流程

### 9.1 Ingest 流程

```text
外部资料 / 对话 / 网页 / 文档
  -> 标准化
  -> 保存到 raw/
  -> 写 source metadata
  -> 记录 ingest log
```

这个阶段是模块内部能力，对外不一定单独暴露为工具。

### 9.2 Draft 流程

```text
读取 raw/wiki/schema
  -> 判断目标 page_type 和目标路径
  -> 生成 page draft
  -> 进行链接和 frontmatter 补全
  -> 冲突检测
  -> 写入 drafts/
  -> 记录 draft log
```

`obsidian_wiki_draft` 可以封装这条流程，不把 compile 细节暴露给 Jarvis 主流程。

### 9.3 Apply 流程

```text
draft
  -> 用户确认或策略允许
  -> apply 到 target page
  -> 记录 apply log
```

### 9.4 Query 流程

```text
用户问题
  -> 按 query_mode 选择 wiki / raw 查询路径
  -> 返回页面摘要或证据命中
  -> 组合给 LLM 作为长期上下文
```

这个设计和现有 token 窗口方案一致：**长期知识通过检索注入，不直接把整段历史塞进上下文窗口。**

### 9.5 Maintain 流程

```text
wiki / drafts / logs
  -> 扫描基础治理问题
  -> 修复 frontmatter / page_type / source_ids / 死链
  -> 整理 inbox 或做轻量 refresh
  -> 记录 maintain log
```

---

## 10. 与 Jarvis ReAct Runtime 的集成

### 10.1 在主图中的职责

主图负责：

- 判断当前请求是否允许写 wiki
- 识别这是“查询知识”还是“沉淀知识”
- 控制路径权限、写入范围和 commit 策略
- 在需要时触发索引刷新

### 10.2 在子图中的职责

ReAct loop 负责：

- 调用 `obsidian_wiki_query` 获取长期背景或原始证据
- 调用 `obsidian_wiki_draft` 生成候选沉淀稿
- 调用 `obsidian_wiki_apply` 在确认后写入正式 wiki
- 调用 `obsidian_wiki_maintain` 做后台治理和轻维护

### 10.3 和 ContextAssembler 的关系

这套设计不替代 `active_summary`，而是位于它之上：

- `active_summary`：对单个 conversation 的短中期压缩记忆。
- `obsidian_wiki`：跨会话、跨任务、跨来源的长期结构化知识。

建议 ContextAssembler 后续按如下顺序装配：

```text
system prompt
+ retrieved wiki knowledge
+ conversation.active_summary
+ recent turn blocks
+ current input
```

这样职责边界清晰，不会把“对话摘要”和“长期知识库”混成一层。

---

## 11. 与 Obsidian 的关系

Obsidian 在这里首先是 **文件系统和知识工作台**，不是同步协议中心。

设计上要坚持三点：

- Jarvis 操作的是标准 Markdown 文件，而不是依赖 Obsidian 私有能力。
- 即使用户不用 Obsidian 客户端，这套 Wiki 也仍然可工作。
- 如果用户已经在 Obsidian 中手工维护笔记，Jarvis 应把它视为一类 `raw/obsidian-notes` 或受控 `wiki/` 输入，而不是无条件覆盖。

换句话说，Obsidian 是最佳使用界面，但不是架构依赖。

---

## 12. 索引与检索策略

v1 建议分层推进：

### 12.1 第一阶段

- 先基于文件路径、标题、标签、简单全文检索工作。
- `obsidian_wiki_query` 支持 `wiki_only / wiki_then_raw / raw_only` 三种模式。
- 查询结果返回页面摘要、命中段落、路径和 source 引用。
- `draft / apply / maintain` 先基于本地文件系统工作，不依赖复杂索引。

### 12.2 第二阶段

- 接入 OpenSearch 或向量库。
- 以 wiki page 为主索引对象，raw source 作为回查层。
- 支持按 `project / tags / page_type / recency` 做过滤。

为什么不先索引 raw：

- raw 噪声太大。
- raw 里包含大量过程性、低密度信息。
- wiki 是经过结构化编译后的高信号层，更适合先检索。

---

## 13. 权限与治理

Wiki 写入比普通检索更敏感，建议 v1 明确区分三类动作：

- `read`：查询页面，默认允许。
- `propose_write`：生成草稿或更新提案，默认允许。
- `apply_write`：真正写入正式 wiki，按策略或显式指令执行。

建议规则：

- 未经明确授权，不要批量改写大量页面。
- 对已有正式页面的结构性重写，优先重新生成 `draft`。
- 自动写入必须记录 `updated_at`、`source_ids` 和变更原因。
- 用户手工写的页面，不应被无提示覆盖。

---

## 14. v1 风险与取舍

### 14.1 风险

- 页面去重困难，同一事实可能落在多个页面。
- LLM 编译可能引入“看起来合理但无证据”的总结。
- 用户手工改写和 Jarvis 自动更新可能冲突。
- 一开始 schema 过细，会显著增加维护成本。

### 14.2 取舍

因此 v1 应坚持：

- 先固定 5 个 page types，不做全能 taxonomy。
- 先允许 `inbox/` 和 `drafts/` 存在，不追求一次归类完美。
- 先保留 source_ids 和原文引用，降低幻觉风险。
- 先收敛到 `query / draft / apply / maintain` 4 个高层工具，不暴露更细流水线。

---

## 15. 推荐落地顺序

### Phase 1：文件结构与基础工具

- 建立 `JarvisWiki/` 目录
- 定义 schema、templates、page types
- 实现模块内部的 `init / ingest` 基础能力

### Phase 2：编译与查询

- 实现 `obsidian_wiki_query`
- 实现 `obsidian_wiki_draft`
- 实现 `obsidian_wiki_apply`
- 支持 `drafts/` 工作流
- 支持 source_ids 和 draft/apply logs

### Phase 3：质量与检索增强

- 实现 `obsidian_wiki_maintain`
- 接入全文检索 / OpenSearch / 向量检索
- 增补孤儿页、标题重复、inbox 滞留等高级治理
- 和 ContextAssembler 打通长期知识注入

### Phase 4：运行时自动化

- 设计文档讨论后自动生成 decision/design 草稿
- 对高频知识页做增量 refresh

---

## 16. 最终结论

我建议 Jarvis 的 `obsidian_wiki` 设计不要停留在“读写笔记”层，而要明确升级为一套 **Raw Source -> Schema -> Wiki -> Retrieval** 的长期知识架构。

其中最关键的不是 Obsidian 本身，而是三个边界：

- **raw 是证据层**
- **wiki 是知识层**
- **schema 是治理层**

在这个边界清晰之后，Jarvis 才能把多轮对话、任务复盘、设计沉淀、长期记忆和后续检索统一起来，而且不会把数据库历史、临时上下文和长期知识混成一锅。

如果只做“让 Agent 往 Obsidian 写 Markdown”，最终会退化成另一个杂乱收件箱；如果按 LLM Wiki 的思路建设，Obsidian 才会真正成为 Jarvis 的长期大脑外部化存储。
