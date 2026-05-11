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

### 3.1 v1 产品结论

v1 不做“自动生长的长期大脑”，而做“可控的知识沉淀管道”。

也就是说，v1 的重点是：

- 用户或 Agent 显式决定哪些内容值得沉淀
- 先生成候选 draft
- 再由用户确认写入正式 wiki

而不是：

- 自动把每段 conversation 都导入 raw
- 自动全量扫描 raw 并批量生成 wiki
- 自动后台重写正式页面

---

## 4. v1 目标与非目标

### 4.1 v1 目标

- 提供一个 Obsidian 兼容的本地 Wiki 目录结构。
- 支持把被显式选中的对话片段、网页、文档、已有笔记导入到 `raw/`。
- 支持生成候选 draft，并在用户确认后将 draft 应用到正式 wiki 页面。
- 支持按 query 检索 wiki 页面，并可按模式回查 raw source。
- 支持后台维护，处理最基础的 frontmatter、page_type、source_ids 和死链问题。
- 支持把 Jarvis 的设计文档、决策记录、概念说明、操作手册持续沉淀进 Wiki。

### 4.2 v1 非目标

- 不做 Obsidian 插件开发。
- 不做复杂 GUI。
- 不做 conversation 自动全量导入 raw。
- 不做后台定期扫描 raw 自动生成 draft。
- 不做自动后台持续重写全部页面。
- 不做自动合并全部重复知识页。
- 不做真正意义上的图数据库或实体关系图谱。
- 不要求一开始就和 OpenSearch / 向量库深度绑定。

### 4.3 v1 路线选择

v1 明确选择：

- **A. 先只支持显式触发的 `draft -> 用户确认 -> apply`**

不选：

- B. conversation 自动进 raw，再由后台任务扫描 raw
- C. 只做查询，不做写入链路

原因：

- A 的链路最短，最容易先跑通。
- A 最符合当前 Jarvis 的成熟度和权限边界。
- A 能先验证 wiki 工具链本身，不会因为自动化过早污染知识库。

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
- v1 不自动全量导入 conversation，只有被显式选中的知识片段才进入 raw。
- raw 的 `source_id / source_ref / created_at / content_hash` 全部由代码生成，不由 LLM 负责填写。

### 5.1.1 Conversation -> Raw 规则

v1 对 Conversation -> Raw 采用保守策略：

- 不在每个 turn 结束后自动扫描对话
- 不对所有 conversation 做自动全量导出
- 只在以下场景创建 raw source：
  - 用户明确说“记下来”“写进 wiki”“沉淀这段内容”
  - Agent 在当前任务流里显式决定“这段内容值得沉淀”

raw 的存储粒度也不按整段 thread，也不按单 turn 全拆，而是：

- 一条 raw source 对应一段被显式选中的知识片段
- 必要时可包含若干相关 turn
- 如果结论依赖 tool result，则相关 tool result 一并纳入 raw

这样可以避免 raw 在 v1 过早变成噪音堆。

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

## 6. Workspace 与 Vault 目录设计

v1 需要明确区分两个概念：

- `data/obsidian_wiki/`：默认模块工作区根目录，包含系统内部状态和人类可读知识页
- `vault/`：真正给 Obsidian 打开的目录，只包含人类可读页面

也就是说，**不要把 raw / drafts / schema 直接暴露在 Obsidian 主 vault 里**。否则图谱会被 `src_*`、`draft_*`、规则文件和日志污染，不适合人类使用。

推荐目录如下：

```text
data/obsidian_wiki/
  README.md

  vault/
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
    .obsidian/

  system/
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
    drafts/
    templates/
    logs/
```

目录职责：

- `vault/`：人类主视图，也是 Obsidian 应该打开的目录。
- `system/schema/`：规则层，由用户和 Jarvis 共同维护，但不进入主图谱。
- `system/raw/`：证据层，只追加，不直接暴露给 Obsidian 图谱。
- `system/drafts/`：编译过程中的候选稿，避免直接污染正式 wiki。
- `system/templates/`：页面模板。
- `system/logs/`：编译日志、lint 报告、冲突记录。

其中：

- Obsidian 客户端应打开 `data/obsidian_wiki/vault/`
- Jarvis 模块应持有 workspace 根路径，默认是 `data/obsidian_wiki/`
- `vault/` 与 `system/` 必须一起管理，但面向不同角色

这样做的直接收益是：

- 人类图谱里只出现正式知识页
- `src_*`、`draft_*`、schema 文件不会污染图谱
- `maintain` 仍然可以检查 `source_ids`、raw 和 drafts，只是这些不再占据人类工作台

其中 `vault/inbox/` 很重要，它用来承接暂时无法归类、但值得保留的知识页。这样可以避免 v1 因为“分类不完美”而阻塞写入。

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

v1 的沉淀标准必须收敛。默认只允许沉淀：

- 设计决策
- 概念定义
- 稳定操作步骤
- 用户明确确认过的事实
- 后续会重复复用的项目知识

默认不沉淀：

- 临时探索过程
- 未确认猜测
- 一次性报错过程
- 普通闲聊
- 重复背景信息

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

### 8.5 Draft 判断原则

如果由 LLM 判断当前内容是否值得沉淀，system prompt 或 tool 使用说明中必须明确写入这些原则：

- 只沉淀后续仍会复用的知识
- 只沉淀已经相对稳定的信息
- 只写事实、定义、决策、步骤
- 不写临时推测和探索轨迹
- 不确定归类时，允许进入 `inbox`，不要强行写正式页

---

## 9. 写入与编译流程

### 9.1 Ingest 流程

```text
外部资料 / 对话 / 网页 / 文档
  -> 显式选择值得沉淀的片段
  -> 标准化
  -> 保存到 raw/
  -> 写 source metadata
  -> 记录 ingest log
```

这个阶段是模块内部能力，对外不一定单独暴露为工具。

v1 不做“每轮 conversation 自动进 raw”，而是先要求显式触发。

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
  -> 在当前对话流中请求用户确认
  -> apply 到 target page
  -> 记录 apply log
```

v1 不做异步审批流，也不做事后批量审批。写入确认应直接发生在当前对话里。

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

其中 `inbox` 的整理在 v1 默认由 `obsidian_wiki_maintain` 负责，也允许用户手工整理，但不要求自动分类完美。

---

## 10. 与 Jarvis ReAct Runtime 的集成

### 10.1 在主图中的职责

主图负责：

- 判断当前请求是否允许写 wiki
- 识别这是“查询知识”还是“沉淀知识”
- 控制路径权限、写入范围和 commit 策略
- 在需要时触发索引刷新
- 在 `apply` 前向用户同步确认

### 10.2 在子图中的职责

ReAct loop 负责：

- 调用 `obsidian_wiki_query` 获取长期背景或原始证据
- 调用 `obsidian_wiki_draft` 生成候选沉淀稿
- 调用 `obsidian_wiki_apply` 在确认后写入正式 wiki
- 调用 `obsidian_wiki_maintain` 做后台治理和轻维护

其中查询不是每个 turn 默认执行。v1 建议只在以下场景触发 `obsidian_wiki_query`：

- 用户明确询问“之前怎么定的”“这个概念是什么”“依据是什么”
- 当前任务明显依赖长期项目背景
- Agent 判断短期上下文不足，需要长期知识补充

不要把 `obsidian_wiki_query` 作为每轮固定步骤，否则会引入不必要的成本和噪声。

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

### 10.4 写入确认与冲突处理

v1 对正式 wiki 的写入采用同步、保守策略：

- `apply` 前必须在当前对话流中请求用户确认
- 不做异步审批队列
- 不做静默覆盖

如果目标 page 在用户手工编辑后发生变化，而 Jarvis 持有旧 draft，则：

- `apply` 先做冲突检测
- 若检测到 page 内容已变化，则不直接覆盖
- 输出当前 page 摘要、draft 摘要和冲突提示
- 由用户决定是否覆盖、放弃或重新 draft

v1 不做自动 merge。

---

## 11. 与 Obsidian 的关系

Obsidian 在这里首先是 **知识工作台**，不是系统内部状态目录。

设计上要坚持三点：

- Jarvis 操作的是标准 Markdown 文件，而不是依赖 Obsidian 私有能力。
- 即使用户不用 Obsidian 客户端，这套 Wiki 也仍然可工作。
- 如果用户已经在 Obsidian 中手工维护笔记，Jarvis 应把它视为一类 `system/raw/obsidian-notes` 或受控 `vault/` 输入，而不是无条件覆盖。

同时要补一条更具体的界面原则：

- Obsidian 默认只打开 `vault/`
- `system/` 不应作为人类主工作台，也不应进入默认图谱关注范围
- 人类图谱表达的是正式知识网络，不是系统执行现场

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

### 13.1 Source 追溯与审计链

v1 对 source 审计采用“完整优先、优化后置”的策略。

规则如下：

- page frontmatter 允许保留 `source_ids`
- 如果某个 page 经历多次增量更新，`source_ids` 不要求无限增长到前端可读；必要时可以只保留当前有效 source，历史来源进入日志或 sidecar metadata
- 用户手工创建或修改的页面允许 `source_ids: []`
- 但这类页面建议增加额外标记，例如：

```yaml
source_mode: manual | generated | mixed
```

这样可以明确说明该页面的审计链是否完整，而不是假装所有页面都来源清晰。

### 13.2 Raw 生命周期

v1 对 raw source 不做物理清理策略，只做逻辑标记：

- `active`
- `superseded`
- `ignored`

也就是说：

- 允许把旧 raw 标记为 `superseded`
- 但不在 v1 自动删除 raw 文件

原因是 v1 更强调保留证据链，而不是提前做存储优化。

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
- 跑通显式触发的 `draft -> 用户确认 -> apply`

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

- 评估是否引入 conversation -> raw 半自动导入
- 评估是否引入 raw -> draft 的后台扫描任务
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
