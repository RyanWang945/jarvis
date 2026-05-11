# Obsidian Everything About Me 升级设计

| 项目 | 内容 |
|------|------|
| 作者 | Codex |
| 日期 | 2026-05-05 |
| 状态 | 设计中 |
| 关联设计 | 【2026-5-2】 obsidian_wiki.md、【2026-05-03】Jarvis Mode Routing 与 Runtime Policy 设计、【2026-05-02】 context manager设计 |

---

## 1. 背景

当前 `obsidian_wiki` 已经能把 Jarvis 项目设计文档整理成 Obsidian 可读的 Wiki，并形成：

```text
index
-> projects/jarvis/index
-> projects/jarvis/designs/index
-> projects/jarvis/decisions/index
-> concepts/index
```

这说明项目知识库方向已经跑通，但它仍然偏向 **Jarvis 项目知识库**。

用户对 Obsidian 的目标更接近：

> Everything about me.

也就是 Obsidian 不只是项目文档仓库，而是个人长期知识、日记、项目、责任域、资料、决策、人物、资产、习惯、反思的统一工作台。

因此需要把当前 `obsidian_wiki` 从「项目 Wiki」升级为「个人知识操作系统」，同时保留 Jarvis 项目知识库作为其中一个 project domain。

---

## 2. 外部项目参考

本设计参考了几个开源项目的模式，但不直接照搬。

| 项目 | 关键能力 | 对 Jarvis 的启发 |
|------|----------|------------------|
| MindCache | Obsidian MCP server，提供 search/read/write/journal/connect/organize 等工具 | 工具边界可以按 Find / Read / Remember / Journal / Write / Connect / Organize 拆分 |
| EchOS | Capture / Search / Write 三段式个人 AI 知识系统，支持 Telegram、CLI、Web 输入 | Jarvis 应把「随手输入」作为第一等入口，先 capture，再 search/write |
| meld | AI 和用户共享同一个 Markdown 知识库，并能创建、连接、更新 notes | 个人 Wiki 应允许 AI 参与持续整理，但要有写入边界 |
| mneia | 本地、多连接器、知识图谱、实体关系抽取、自动后台循环 | 可参考实体/关系/时间维度，但 Jarvis v1 不应默认全自动改写 |
| Verity | Telegram + Obsidian agent，收集、研究、写入 Obsidian | 飞书可以承担类似 Telegram 的轻量 capture 入口 |
| Open Connections / Smart Connections | 基于语义相似度发现相关笔记 | 用于 suggested links，而不是直接生成全量互链 |
| Khoj | 开源个人 AI 搜索和研究助手 | 查询层应支持个人资料和 web/research 混合，但写入仍需独立治理 |

参考结论：

- 成熟方向不是「LLM 直接改 vault」，而是 **capture -> classify -> draft/update proposal -> apply -> maintain**。
- 自动化可以负责发现关系、生成草稿、维护索引，但正式知识页更新需要策略控制。
- 日记、原始输入、长期知识、项目文档、决策记录必须分层，否则 vault 会退化为混乱收件箱。

---

## 3. 产品定位

### 3.1 新定位

`personal_wiki` 是 Jarvis 的长期个人知识子系统。

它负责：

- 捕获用户输入的任意个人内容。
- 判断内容性质和目标位置。
- 将原文保存在时间线或 raw 层。
- 抽取项目、概念、人物、决策、资源、任务、情绪、事件。
- 生成可审核的组织建议。
- 在允许时更新 Obsidian vault。
- 维护 vault 的索引、链接、frontmatter、孤儿页和主题簇。

它不负责：

- 替代普通聊天。
- 替代项目执行 agent。
- 自动决定高风险个人事项。
- 静默重写用户手写的重要页面。

### 3.2 与当前 obsidian_wiki 的关系

当前 `obsidian_wiki` 更像 project wiki。

升级后建议拆成两层：

```text
personal_wiki
  - everything about me 的 vault 治理服务
  - 包含 journal/projects/areas/resources/concepts/decisions

project_wiki / obsidian_wiki
  - personal_wiki 下的 projects/* 子域
  - Jarvis 项目只是 projects/jarvis
```

代码层可以先不拆项目，但概念上要区分：

- `personal_wiki`：面向个人全域知识。
- `obsidian_wiki`：当前文件系统和 Markdown 操作底座。

---

## 4. Vault 信息架构

推荐顶层目录：

```text
vault/
  index.md

  journal/
    index.md
    daily/
      2026/
        2026-05-05.md
    weekly/
    monthly/

  projects/
    index.md
    jarvis/
      index.md
      designs/
      decisions/
      logs/
      retrospectives/

  areas/
    index.md
    career/
    finance/
    health/
    learning/
    relationships/
    home/

  resources/
    index.md
    books/
    papers/
    articles/
    tools/
    people/
    companies/

  concepts/
    index.md
    ...

  decisions/
    index.md
    personal/
    career/
    technical/

  inbox/
    index.md

  archive/

  assets/
    files/
    images/
```

### 4.1 顶层目录职责

| 目录 | 职责 | 写入策略 |
|------|------|----------|
| `journal/` | 时间线，记录当天发生、想法、情绪、反思、待办 | 默认 append |
| `projects/` | 有目标和结束条件的事项，例如 Jarvis、求职、旅行、研究 | draft 后 apply |
| `areas/` | 长期责任域，例如职业、健康、财务、学习 | draft 后 apply |
| `resources/` | 外部资料、书、文章、论文、工具、人物、公司 | capture 后可自动建卡片 |
| `concepts/` | 抽象概念和长期理解 | draft 后 apply |
| `decisions/` | 重要选择和理由 | 需要确认 |
| `inbox/` | 无法归类但值得保留的输入 | 默认 append，定期整理 |
| `archive/` | 过期项目和历史资料 | 低频维护 |

### 4.2 不按来源做顶层组织

不建议顶层使用：

```text
notes/
web/
documents/
chat/
files/
```

来源应该是 metadata，而不是长期结构。

长期稳定结构应按信息性质组织：

- 时间：journal
- 目标：projects
- 责任：areas
- 资料：resources
- 理解：concepts
- 选择：decisions

---

## 5. Personal Wiki Curator Agent

需要新增一个专门 agent profile：

```text
personal_wiki_curator
```

### 5.1 核心职责

```text
User input
  -> capture raw input
  -> classify content
  -> route to journal / project / area / resource / concept / decision / inbox
  -> extract entities and relations
  -> generate write proposals
  -> apply low-risk writes
  -> ask_user for sensitive or structural writes
  -> maintain indexes and links
```

### 5.2 为什么需要专门 Agent

普通 ReAct agent 目标是完成当前 turn。

Curator agent 目标是维护长期知识资产。

这两者的优化目标不同：

- 普通 agent 追求当前问题完成。
- curator 追求长期可检索、可审计、可维护。

因此 curator 应有独立 runtime policy、工具集合和写入协议。

### 5.3 输入类型

Curator 应能处理：

- 日记
- 随笔
- 临时想法
- 项目记录
- 会议记录
- 网页链接
- 文档摘录
- 技术设计
- 决策记录
- 人物/公司/资源笔记
- 任务复盘
- 语音转文字文本

### 5.4 输出结构

每次整理应输出一份简短报告：

```yaml
capture_id: cap_20260505_xxx
content_type: journal_reflection
primary_location: journal/daily/2026/2026-05-05.md
entities:
  projects:
    - projects/jarvis
  concepts:
    - concepts/personal-knowledge-management
  decisions:
    - decisions/personal/use-obsidian-as-everything-about-me
proposed_writes:
  - append_journal
  - draft_concept_update
  - draft_decision
risk:
  journal_append: low
  concept_update: medium
  decision_create: medium
needs_confirmation:
  - draft_decision
```

---

## 6. 工具设计

### 6.1 工具分层

参考 MindCache 的工具分组，但收敛为 Jarvis v1 可实现的少数工具。

第一阶段建议暴露：

```text
personal_wiki_capture
personal_wiki_query
personal_wiki_draft_updates
personal_wiki_apply_update
personal_wiki_maintain
```

第二阶段再扩展：

```text
personal_wiki_suggest_links
personal_wiki_extract_entities
personal_wiki_move_note
personal_wiki_update_properties
personal_wiki_daily_digest
```

### 6.2 `personal_wiki_capture`

职责：

- 接收任意输入。
- 保存原文。
- 自动追加到 daily journal 或 inbox。
- 返回 capture id。

输入：

```json
{
  "content": "...",
  "source_type": "feishu_text | voice_transcript | web_clip | manual",
  "timestamp": "2026-05-05T22:30:00+08:00",
  "hint": "journal | project | resource | decision | auto"
}
```

默认行为：

- `journal` 内容 append 到当天 daily note。
- 无法判断时 append 到 `inbox/`.
- 所有输入保留 raw/capture 记录。

### 6.3 `personal_wiki_draft_updates`

职责：

- 根据 capture 生成结构化更新提案。
- 不直接改长期知识页。

可能的 update 类型：

```text
create_concept
update_concept
create_project_note
append_project_log
create_decision
create_resource_card
add_links
move_from_inbox
```

### 6.4 `personal_wiki_apply_update`

职责：

- 应用已确认的 update proposal。
- 记录 apply log。
- 更新相关索引和 frontmatter。

约束：

- 不允许无 proposal 直接写长期页。
- 涉及 `decisions/`、`people/`、`finance/`、`health/` 时默认需要确认。

### 6.5 `personal_wiki_query`

职责：

- 查询 personal vault。
- 支持按 scope 查询。

scope：

```text
all
journal
projects
areas
resources
concepts
decisions
```

query mode：

```text
wiki_only
journal_then_wiki
raw_only
evidence_trace
```

---

## 7. HTTP / 外部入口

不建议第一阶段拆成独立项目。

应先在 Jarvis 内部实现 service + tools，然后提供轻量 HTTP endpoint。

### 7.1 推荐 API

```http
POST /api/personal-wiki/capture
POST /api/personal-wiki/query
POST /api/personal-wiki/apply
GET  /api/personal-wiki/status
```

第一阶段只需要：

```http
POST /api/personal-wiki/capture
```

用途：

- 飞书快捷入口
- 手机快捷指令
- 浏览器插件
- CLI
- 未来语音输入

### 7.2 为什么不单独项目

当前 Jarvis 已有：

- Feishu channel
- runtime policy
- LLM client
- ask_user
- tool audit
- Obsidian workspace
- Context Manager

Curator agent 强依赖这些基础设施。

因此第一阶段做成 Jarvis 子模块更合适：

```text
app/personal_wiki/
  service.py
  models.py
  classifier.py
  curator.py
  tools.py
```

只有当需要独立 UI、独立部署、多客户端共享或开源成通用产品时，才拆独立项目。

---

## 8. 写入策略

### 8.1 风险分级

| 写入类型 | 默认策略 | 原因 |
|----------|----------|------|
| append daily journal | 自动允许 | 时间线记录，低风险 |
| append inbox | 自动允许 | 暂存，不污染知识图谱 |
| create resource card | 可自动，但要保留来源 | 资料卡片风险较低 |
| update project log | 可自动 | 项目过程记录 |
| update concept | draft -> apply | 影响长期理解 |
| create decision | ask_user 确认 | 决策代表稳定选择 |
| update people / health / finance | ask_user 确认 | 隐私和高敏感 |
| rename / move note | ask_user 确认 | 影响链接稳定性 |
| delete note | 默认禁止 | 不符合证据保留原则 |

### 8.2 原文保留

所有 capture 都应有原文记录。

建议放在：

```text
system/raw/captures/
```

或在 daily note 中保留完整输入。

知识化页面只承载提炼后的长期内容，不能替代原始输入。

### 8.3 双写关系

一条输入可能同时产生：

```text
journal/daily/2026/2026-05-05.md
projects/jarvis/logs/2026-05.md
concepts/personal-knowledge-management.md
decisions/personal/use-obsidian-as-everything-about-me.md
```

但 v1 不应全部自动 apply。

推荐：

- journal 自动 append。
- 其他页面生成 proposal。
- 用户确认后 apply。

---

## 9. 链接与图谱治理

### 9.1 链接原则

不要生成全量互链。

每个页面控制在：

- 1-2 个索引链接
- 2-5 个核心语义链接
- 0-3 个来源/决策/项目链接

### 9.2 链接类型

建议在 frontmatter 或正文中表达关系类型。

示例：

```yaml
related:
  - target: concepts/runtime-policy
    relation: uses
  - target: projects/jarvis/decisions/runtime-policy-controls-tool-exposure
    relation: decided_by
```

正文仍可保留 Obsidian wikilink：

```md
# Related

- [[concepts/runtime-policy]] - uses
- [[projects/jarvis/decisions/runtime-policy-controls-tool-exposure]] - decided by
```

### 9.3 suggested links

语义相似度工具只能生成建议，不直接写入：

```yaml
suggested_links:
  - target: concepts/tool-exposure
    confidence: 0.83
    reason: "mentions runtime tool exposure and intent pruning"
```

这借鉴 Open Connections / Smart Connections 的思路：发现相关性，但不把相关性直接等同于正式链接。

### 9.4 Maintain 任务

`personal_wiki_maintain` 应检查：

- missing frontmatter
- dead links
- orphan notes
- too many outgoing links
- too many incoming hub links
- inbox aging
- duplicate titles
- missing source/capture refs
- daily note 未汇总
- decision 缺少 rationale

---

## 10. Context Manager 集成

当前 Context Manager 应支持 personal wiki retrieval。

建议 context 装配顺序：

```text
system prompt
+ runtime policy
+ retrieved personal wiki knowledge
+ retrieved project knowledge
+ active summary
+ recent messages
+ current user input
```

注意：

- 普通聊天不应每轮查询整个 vault。
- 只有用户明确问个人历史、项目背景、之前决定、日记回顾时才查询。
- Curator mode 可以主动查询近期 journal / inbox / related concepts。

---

## 11. Runtime Policy

新增 capability：

```text
personal.capture
personal.organize
personal.query
personal.apply_write
```

工具暴露建议：

| 场景 | 工具 |
|------|------|
| “记一下...” | `personal_wiki_capture` |
| “我之前怎么想的...” | `personal_wiki_query` |
| “整理今天的内容” | `personal_wiki_draft_updates` |
| “确认写入” | `personal_wiki_apply_update` |
| “整理 inbox” | `personal_wiki_maintain` |

`personal_wiki_apply_update` 不应在普通 chat 默认暴露。

---

## 12. 数据模型

### 12.1 Capture

```yaml
capture_id: cap_20260505_abcd
source_type: feishu_text
created_at: 2026-05-05T22:30:00+08:00
raw_path: system/raw/captures/2026/cap_20260505_abcd.md
primary_note: journal/daily/2026/2026-05-05.md
content_hash: ...
status: captured | organized | ignored
```

### 12.2 Update Proposal

```yaml
update_id: upd_20260505_abcd
capture_ids:
  - cap_20260505_abcd
update_type: create_concept
target_page: concepts/personal-knowledge-management.md
risk_level: medium
requires_confirmation: true
status: proposed | applied | rejected
```

### 12.3 Page Frontmatter

```yaml
---
title: Personal Knowledge Management
page_type: concept
status: active
source_mode: mixed
capture_ids:
  - cap_20260505_abcd
updated_at: 2026-05-05
tags:
  - pkm
---
```

---

## 13. 交互样例

### 13.1 随手日记

用户：

```text
记一下，今天我意识到 Obsidian 对我来说应该是 everything about me，不只是 Jarvis 项目知识库。
```

Jarvis：

```text
已追加到 journal/daily/2026/2026-05-05.md。

我还识别出 2 个可沉淀项：
- concepts/personal-knowledge-management
- decisions/personal/use-obsidian-as-everything-about-me

是否生成草稿？
```

### 13.2 项目复盘

用户：

```text
把刚才 Obsidian 图谱改造记录整理一下。
```

Jarvis：

```text
已生成 3 个更新提案：
- append projects/jarvis/logs/2026-05.md
- update concepts/knowledge-writeback.md
- create decision: keep-monthly-index-as-archive-only
```

---

## 14. 分阶段落地

### Phase 1：Capture + Journal

- 建立 `journal/`、`inbox/`、`areas/`、`resources/` 顶层目录。
- 实现 `personal_wiki_capture`。
- 支持 append daily note。
- 支持 capture raw。
- 支持基础 frontmatter。

### Phase 2：Classify + Draft

- 实现内容分类。
- 提取 project / area / concept / decision / resource。
- 生成 update proposal。
- 生成整理报告。

### Phase 3：Apply + Maintain

- 实现 `personal_wiki_apply_update`。
- 实现索引自动维护。
- 实现 dead links / orphan / inbox aging 检查。
- 高风险写入接入 ask_user。

### Phase 4：Semantic Graph

- 接入 embedding 或 OpenSearch。
- 支持 suggested links。
- 支持 concept clustering。
- 支持定期 digest 和 resurfacing。

### Phase 5：External Inputs

- HTTP capture endpoint。
- 飞书快捷 capture。
- 浏览器插件 / CLI。
- 语音转文字入口。

---

## 15. 风险

### 15.1 自动整理污染 vault

风险：

- Agent 把临时想法写成长期概念。
- Agent 过度链接导致图谱噪声。

缓解：

- journal/inbox 自动写。
- concepts/decisions draft 后 apply。
- link suggestions 不自动落地。

### 15.2 隐私与敏感信息

风险：

- 健康、财务、人际关系等内容被错误暴露或错误总结。

缓解：

- sensitive scope 默认不进入普通检索。
- 高敏感写入必须确认。
- 查询结果标注来源和时间。

### 15.3 结构过早复杂化

风险：

- 顶层目录和模板过多，用户不愿意写。

缓解：

- v1 固定 7 个顶层目录。
- 不确定就进 journal/inbox。
- 周期性整理，而不是写入时强制完美分类。

---

## 16. 结论

Jarvis 的 Obsidian 能力不应停留在「项目设计文档入库」。

目标应升级为：

```text
Everything about me
= journal timeline
+ projects
+ areas
+ resources
+ concepts
+ decisions
+ archive
```

对应实现不应是一个普通笔记写入工具，而应是一个独立的 `personal_wiki_curator` agent profile。

第一阶段最重要的不是复杂 RAG 或知识图谱，而是建立正确边界：

- 原文先 capture。
- 日记可自动 append。
- 长期知识先 draft。
- 重要决策要确认。
- 链接先建议，后落地。

这样 Obsidian 才能既承载「所有和我有关的内容」，又不变成不可维护的信息垃圾场。

---

## 17. 参考

- MindCache: https://usemindcache.com/
- EchOS: https://echos.sh/
- meld: https://meld.kizz.me/
- mneia: https://mneia.app/
- Verity: https://verity.salient.community/
- Open Connections: https://github.com/GoBeromsu/open-connections
- Khoj: https://www.opensourcealternatives.to/item/khoj
