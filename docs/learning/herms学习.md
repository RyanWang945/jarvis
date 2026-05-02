# Hermes Agent 对 Jarvis 的可借鉴点总结

## 1. 总体判断

Hermes Agent 对 Jarvis 最大的参考价值，不是它的 ReAct 流程本身，而是它把一个长期运行的个人 Agent 所需要的工程底座做得比较完整。

Jarvis 当前的方向是：

- 飞书入口
- 本地运行
- 主图 + React 子图
- 工具调用与 observe
- OpenSearch / RAG
- Obsidian / LLMWiki
- 本地代码执行与自动化
- 个人知识和项目经验沉淀

这个方向是合理的。Jarvis 不需要整体迁移到 Hermes，也不需要把自己做成一个大而全的通用 Agent Runtime。

更合理的做法是：

```text
保留 Jarvis 当前主图 + React 子图架构
吸收 Hermes 的 Skill / Memory / Gateway / Sandbox / Self-improvement 思想
围绕飞书 + 本地环境 + RAG + Obsidian Wiki 做垂直个人助理
```

---

## 2. Hermes 最值得 Jarvis 借鉴的点

### 2.1 Skill 体系

Hermes 最值得借鉴的一点，是把 Skill 设计成了一等公民。

Skill 不是普通工具，也不是普通知识库文档，而是“过程性知识”。

它描述的是：

```text
什么时候使用
怎么执行
需要哪些工具
有哪些风险
有哪些坑
如何验证结果
```

Jarvis 现在也需要类似能力。

Jarvis 中应该明确区分：

```text
Memory：短小、稳定、会影响长期行为的事实和偏好
Wiki：结构化、可阅读、可检索的长期知识资产
Skill：可复用的做事流程、操作规范、踩坑经验
```

举例：

```text
Wiki：什么是 bge-reranker-v2-m3
Memory：用户本地机器是 Windows + 4070Ti
Skill：如何在本地 Docker 中部署 reranker 并验证健康状态
```

也就是说，Wiki 解决“知道什么”，Skill 解决“怎么做”。

---

### 2.2 Jarvis 应该引入 Skill Library

Jarvis 可以在 Obsidian 或本地文件系统中维护一个 Skill Library。

建议目录：

```text
JarvisWiki/
  20_Skills/
    Coding/
    Docker/
    RAG/
    Obsidian/
    Feishu/
    Deployment/
```

每个 Skill 可以是一个 Markdown 文件。

示例：

```md
---
name: local-reranker-deploy
description: 本地 Docker 部署 reranker 的标准流程
tags: [rag, docker, gpu]
requires_tools: [terminal, docker]
risk_level: medium
---

# Local Reranker Deploy

## When to Use

当用户需要部署、切换、排查 reranker 服务时使用。

## Procedure

1. 检查 GPU / CUDA / Docker 环境
2. 检查模型路径
3. 启动 Docker Compose
4. 调用健康检查接口
5. 跑最小 rerank case
6. 检查日志和显存占用

## Pitfalls

- Windows Docker 访问宿主机需要注意 host.docker.internal
- 模型首次加载较慢
- 显存释放可能有延迟
- 端口冲突容易导致健康检查失败

## Verification

- `/health` 返回正常
- `/rerank` 返回排序结果
- GPU 显存占用符合预期
```

对 Jarvis 的意义是：

```text
Jarvis 不只是积累资料，而是积累能力。
```

对话、排错、部署、评审、写文档这些经验，都可以逐渐沉淀为 Skill。

---

### 2.3 自我改进闭环

Hermes 的另一个重要启发是：Agent 执行任务之后，不应该只是返回结果，还应该判断这次任务是否产生了可复用经验。

Jarvis 也应该有任务后反思流程。

建议流程：

```text
任务完成
  ↓
判断是否有可沉淀内容
  ↓
分类为 memory / wiki / skill / project note
  ↓
生成 proposed draft
  ↓
用户确认
  ↓
写入长期资产
```

第一版不要全自动写入，应该采用半自动机制：

```text
Jarvis 发现可沉淀内容
  ↓
生成建议
  ↓
用户确认
  ↓
再写入 Obsidian / Memory / Skill
```

可沉淀内容包括：

```text
用户偏好
项目背景
环境配置
操作流程
排错经验
稳定结论
常用命令
设计决策
文档索引
```

示例：

```text
这次讨论中有两个内容适合沉淀：

1. 本地 reranker Docker 部署流程
   类型：Skill

2. Jarvis 当前 RAG 服务依赖 OpenSearch + reranker
   类型：Project Memory

是否生成草稿？
```

这个能力可以让 Jarvis 从“会执行任务”逐渐变成“越用越懂你的系统”。

---

## 3. Gateway 抽象

Hermes 的多平台接入能力对 Jarvis 很有参考价值。

Jarvis 当前主要通过飞书交互，但飞书不应该侵入 Agent Core。

推荐结构：

```text
Feishu / CLI / Web / Telegram / WeCom
  ↓
Gateway Adapter
  ↓
Unified MessageEvent
  ↓
Auth / Session / Policy
  ↓
Jarvis Agent Runtime
  ↓
Tool / Subgraph / Skill / Memory
  ↓
Response Renderer
  ↓
Delivery Adapter
```

Jarvis 不应该这样设计：

```text
Feishu Handler 里直接写 Agent 逻辑
Feishu Handler 里直接调用工具
Feishu Handler 里直接管理上下文
Feishu Handler 里直接判断权限
```

应该这样设计：

```text
FeishuAdapter：
  只负责飞书消息解析和回复发送

MessageNormalizer：
  把平台消息转为统一事件

SessionResolver：
  判断属于哪个 conversation / user / group

PolicyEngine：
  判断用户是否有权限调用某些能力

JarvisRuntime：
  真正执行 Agent 主流程

Renderer：
  把结果渲染成飞书卡片、Markdown、纯文本等形式
```

这样做的收益：

```text
后续可以接 CLI
后续可以接 Web UI
后续可以接企业微信 / 钉钉 / Telegram
群聊和私聊可以统一建模
权限和安全策略可以平台无关
Agent Core 不被飞书污染
```

---

## 4. 安全与沙箱

Hermes 对 Jarvis 最大的现实启发之一是：一旦 Agent 可以执行 shell、改代码、部署服务，就必须有安全边界。

Jarvis 未来如果要做到：

```text
通过飞书控制本地电脑
修改代码
执行测试
提交代码
部署服务
操作数据库
访问本地文件
```

就必须设计安全与沙箱机制。

Jarvis 至少需要四层安全：

```text
L1：用户与群组权限
L2：工具权限
L3：命令风险审批
L4：执行环境隔离
```

---

### 4.1 用户与群组权限

需要区分：

```text
个人私聊
可信群聊
普通群聊
陌生用户
管理员
只读用户
```

不同用户能调用的能力不同。

例如：

```text
普通群成员：
  - 可以问答
  - 可以查知识库
  - 不可以执行 shell
  - 不可以改代码
  - 不可以写 Obsidian

管理员：
  - 可以执行高风险操作
  - 可以确认写入 Wiki
  - 可以触发部署
```

---

### 4.2 工具权限

工具需要分级：

```text
read_only：
  - obsidian_wiki_query
  - search_docs
  - list_files

write_low_risk：
  - create_draft
  - save_note_draft

write_high_risk：
  - apply_wiki_draft
  - modify_code
  - update_config

execution：
  - shell
  - docker
  - git
  - deploy
```

不是所有工具都应该对所有场景开放。

---

### 4.3 命令风险审批

对 shell / git / docker / deploy 这类工具，要做风险判断。

例如：

```text
低风险：
  - ls
  - cat
  - grep
  - git status
  - docker ps

中风险：
  - npm install
  - docker compose up
  - git checkout
  - mv file

高风险：
  - rm -rf
  - git reset --hard
  - docker system prune
  - mysql update/delete
  - deploy production
```

高风险命令必须要求用户确认。

---

### 4.4 执行环境隔离

Jarvis 第一版可以先简单做：

```text
工作目录 allowlist
禁止访问敏感目录
命令 blocklist
环境变量脱敏
操作日志持久化
高风险命令二次确认
```

后续再升级到：

```text
Docker sandbox
WSL sandbox
临时工作区
只读挂载
网络限制
密钥隔离
```

---

## 5. Memory / Wiki / Skill 分层

Hermes 的长期记忆设计对 Jarvis 很有借鉴价值，但 Jarvis 不应该把所有东西都叫 memory。

建议 Jarvis 明确分层。

---

### 5.1 Raw Message

原始对话消息，完整存储。

用途：

```text
审计
回放
历史追溯
重新总结
训练测试集
```

特点：

```text
不直接全部进入 LLM 上下文
只在必要时检索
```

---

### 5.2 Conversation Summary

会话摘要。

用途：

```text
多轮对话上下文压缩
避免 context 过大
保留当前任务状态
```

特点：

```text
短期有效
随着对话推进持续更新
可以替代大量历史消息进入上下文
```

---

### 5.3 User Memory

用户长期偏好和稳定事实。

例如：

```text
用户主要使用 Windows
用户熟悉 Java 和 MySQL
用户正在开发 Jarvis
用户倾向于先做简单 MVP
用户喜欢设计先收敛再实现
```

特点：

```text
短
稳定
高价值
会注入系统上下文
```

---

### 5.4 Project Memory

项目级记忆。

例如：

```text
Jarvis 当前使用 OpenSearch
Jarvis 入口是飞书
Jarvis 已摒弃 CAAgent + worker 固定工作流
Jarvis 当前倾向主图 + React 子图
Jarvis 准备集成 Obsidian Wiki
```

特点：

```text
围绕具体项目
可被项目相关任务自动加载
```

---

### 5.5 Wiki

结构化知识资产。

例如：

```text
RAG 评测设计
reranker 部署方案
Jarvis 多轮对话表设计
Obsidian Wiki 集成方案
DeepResearch 架构
```

特点：

```text
Markdown
人类可读
可被 Obsidian 打开
可被 Jarvis 检索
不一定注入 prompt
```

---

### 5.6 Skill

做事流程和操作规范。

例如：

```text
如何评审 Jarvis 设计
如何生成 Wiki 草稿
如何部署 reranker
如何跑 RAG 评测
如何处理 Docker 代理问题
```

特点：

```text
过程性知识
面向执行
由 Agent 在需要时加载
```

---

## 6. 对 Jarvis 工具设计的启发

Hermes 的经验说明：工具不是越多越好。

Jarvis 应该避免把内部流水线的每一步都暴露成工具。

例如，Obsidian Wiki 不建议暴露成：

```text
obsidian_wiki_ingest
obsidian_wiki_compile_source
obsidian_wiki_apply_draft
obsidian_wiki_refresh_page
obsidian_wiki_query
obsidian_wiki_lint
```

这些步骤作为内部 service 方法可以存在，但不适合全部暴露给 LLM。

推荐第一版只暴露：

```text
obsidian_wiki_query
obsidian_wiki_draft
obsidian_wiki_apply
```

可选：

```text
obsidian_wiki_maintain
```

内部 service 可以更细：

```text
ObsidianWikiService
  - ingest_source()
  - find_related_pages()
  - compile_to_draft()
  - render_markdown()
  - apply_draft()
  - refresh_index()
  - lint_pages()
```

工具抽象原则：

```text
用户意图层：工具少、语义清晰
系统实现层：模块细、职责清晰
```

也就是说：

```text
LLM 看到的是 “生成 Wiki 草稿”
代码内部可以拆成 ingest / compile / link / render / diff
```

---

## 7. 对 Jarvis Agent 架构的启发

Jarvis 当前放弃 CAAgent + worker 固定工作流，转向主图 + React 子图，是合理的。

推荐结构：

```text
Main Graph
  - 接收用户输入
  - 识别任务类型
  - 加载用户/项目上下文
  - 选择子图
  - 汇总结果
  - 更新状态

React Subgraph
  - 自主规划
  - 工具调用
  - observe
  - 中间结果修正
  - 生成最终回答
```

Hermes 的 AIAgent runtime 可以作为参考，但 Jarvis 没必要完全模仿。

---

### 7.1 适合固定图控制的部分

```text
消息接入
权限判断
上下文加载
会话摘要
用户确认
高风险审批
结果渲染
状态持久化
```

这些适合主图控制。

---

### 7.2 适合 React 子图处理的部分

```text
查资料
代码排查
DeepResearch
复杂问答
设计评审
Wiki 草稿生成
多工具组合任务
```

这些适合让子图动态调用工具。

---

## 8. 对上下文管理的启发

Hermes 的思路是：不要把全部历史消息塞进上下文，而是通过摘要、检索、记忆、Skill 来管理长期上下文。

Jarvis 应该采用类似策略。

上下文来源：

```text
当前用户消息
最近几轮对话
会话摘要
用户长期记忆
项目长期记忆
相关 Wiki 页面
相关 Skill
工具执行结果
```

推荐上下文组装顺序：

```text
1. System Prompt
2. 用户长期偏好
3. 项目上下文
4. 当前会话摘要
5. 最近几轮原始消息
6. 相关 Wiki 检索结果
7. 相关 Skill
8. 当前用户输入
```

上下文控制原则：

```text
短事实进 Memory
长知识进 Wiki
流程经验进 Skill
原始消息进数据库
当前任务状态进 Summary
```

---

## 9. 对模型 Provider 的启发

Hermes 支持多模型 Provider，这对 Jarvis 也很重要。

Jarvis 不应该把模型调用写死在业务逻辑里。

推荐抽象：

```text
ModelProvider
  - OpenAIProvider
  - DeepSeekProvider
  - ClaudeProvider
  - QwenProvider
  - OpenRouterProvider
  - LocalVLLMProvider
  - AliyunBailianProvider
```

调用层统一：

```text
LLMClient.chat()
LLMClient.stream()
LLMClient.embed()
LLMClient.rerank()
LLMClient.vision()
```

配置示例：

```yaml
models:
  default_chat:
    provider: deepseek
    model: deepseek-chat

  strong_reasoning:
    provider: openai
    model: gpt-5.5-thinking

  cheap_summary:
    provider: qwen
    model: qwen-turbo

  local_coding:
    provider: local_vllm
    model: qwen-coder
```

收益：

```text
方便切换模型
方便按任务选择模型
降低成本
避免业务代码绑定具体厂商
支持本地模型和云模型混用
```

---

## 10. 对插件和工具系统的启发

Hermes 的插件化能力说明，Jarvis 的工具系统也应该有明确元数据。

工具不应该只是函数。

每个工具应该有元信息：

```yaml
name: shell_exec
description: 执行 shell 命令
risk_level: high
requires_confirmation: true
allowed_scopes:
  - local_dev
permissions:
  - shell
  - filesystem
timeout_seconds: 60
```

工具需要声明风险：

```text
safe_read
low_risk_write
medium_risk_exec
high_risk_exec
critical
```

工具需要声明作用域：

```text
workspace_only
vault_only
readonly_filesystem
network_allowed
network_denied
database_readonly
database_write
```

收益：

```text
便于权限控制
便于审批
便于审计
便于不同场景加载不同工具
便于防止 LLM 误用工具
```

---

## 11. 对后台任务的启发

Hermes 有长期运行和后台任务思想。Jarvis 也需要，但第一版要克制。

Jarvis 可以有的后台任务：

```text
定期压缩会话摘要
定期维护 Obsidian Wiki 索引
定期检查 Wiki 死链
定期整理未应用 draft
定期清理临时文件
定期生成项目状态摘要
定期检查失败任务
```

第一版不建议做太复杂：

```text
全自动 self-improvement
全自动修改 Skill
全自动重写 Wiki
全自动删除旧内容
全自动合并文档
```

推荐策略：

```text
后台任务只生成建议
高风险变更需要用户确认
重要变更要留 diff
所有写入要有审计日志
```

---

## 12. 对 Obsidian / LLMWiki 的启发

Hermes 的 Skill 和 Memory 思路可以帮助 Jarvis 更好地设计 LLMWiki。

LLMWiki 不应该只是 RAG 文档库，而应该是 Jarvis 的长期知识资产层。

包括：

```text
设计决策
技术方案
踩坑记录
操作手册
项目背景
评测结果
架构演进
```

Obsidian 的价值不是存储本身，而是：

```text
本地 Markdown
人类可读
可双链
可手动编辑
可版本管理
可被 Jarvis 检索
可作为长期知识资产
```

推荐目录结构：

```text
JarvisWiki/
  00_Inbox/
  10_Projects/
    Jarvis/
    RAG/
    LLMWiki/
  20_Skills/
    Coding/
    Docker/
    RAG/
    Obsidian/
  30_Concepts/
  40_Decisions/
  50_References/
  90_Archive/
```

推荐写入流程：

```text
对话 / 网页 / 文档
  ↓
生成 Draft
  ↓
用户确认
  ↓
写入 Obsidian
  ↓
更新索引
  ↓
后续 query 可检索
```

---

## 13. Jarvis 应该避免照搬 Hermes 的地方

### 13.1 不要一开始做全平台

Jarvis 当前最重要的是飞书和本地环境。

不需要一开始支持：

```text
Telegram
Discord
Slack
WhatsApp
Signal
Email
```

但需要把 Gateway 抽象做好，避免未来难扩展。

---

### 13.2 不要一开始做复杂自演化

Hermes 的自我改进机制很强，但 Jarvis 第一版不应该过度自动化。

尤其不要让 Agent 自动：

```text
修改长期记忆
修改 Skill
改写 Wiki
删除文档
重构知识库
```

第一版应该以用户确认为主。

---

### 13.3 不要一开始做过多工具

工具太多会让 LLM 决策复杂，降低可靠性。

原则：

```text
工具对 LLM 暴露要少
内部 service 可以细
```

---

### 13.4 不要把 Jarvis 做成通用大框架

Jarvis 的优势是垂直：

```text
个人助理
本地电脑
飞书控制
RAG
代码执行
Obsidian Wiki
```

不要为了模仿 Hermes，把它做成一个维护成本巨大的通用 Agent 平台。

---

## 14. 建议 Jarvis 吸收 Hermes 后的目标形态

### 14.1 Jarvis 核心层

```text
JarvisRuntime
  - MainGraph
  - ReactSubgraph
  - ContextManager
  - MemoryManager
  - SkillManager
  - ToolRegistry
  - PolicyEngine
  - SessionManager
```

---

### 14.2 接入层

```text
Gateway
  - FeishuAdapter
  - CLIAdapter
  - WebAdapter
```

---

### 14.3 知识层

```text
Knowledge
  - OpenSearch RAG
  - Obsidian Wiki
  - Conversation Store
  - Project Memory
  - User Memory
  - Skill Library
```

---

### 14.4 执行层

```text
Execution
  - Shell Tool
  - Docker Tool
  - Git Tool
  - Code Tool
  - Browser/Search Tool
  - Obsidian Tool
```

---

### 14.5 安全层

```text
Security
  - User Auth
  - Group Policy
  - Tool Permission
  - Risk Approval
  - Workspace Sandbox
  - Audit Log
```

---

## 15. Jarvis 推荐演进路线

### Phase 1：收敛核心能力

目标：

```text
飞书可用
多轮对话稳定
知识库查询稳定
Obsidian Wiki 可查询
Wiki 草稿可生成
用户确认后可写入
```

重点做：

```text
obsidian_wiki_query
obsidian_wiki_draft
obsidian_wiki_apply
conversation/message 持久化
session summary
基础权限
```

---

### Phase 2：加入 Skill 系统

目标：

```text
Jarvis 可以沉淀做事流程
后续任务自动复用 Skill
```

重点做：

```text
Skill 文件结构
Skill 检索
Skill 加载
Skill 草稿生成
用户确认后写入 Skill
```

---

### Phase 3：加入安全与沙箱

目标：

```text
Jarvis 可以更安全地执行本地任务
```

重点做：

```text
工具风险分级
命令审批
工作目录限制
审计日志
Docker/WSL sandbox
```

---

### Phase 4：加入自我改进闭环

目标：

```text
Jarvis 能从任务中持续积累经验
```

重点做：

```text
任务后反思
proposed memory
proposed skill
proposed wiki draft
定期 lint
定期 summary
```

---

### Phase 5：扩展 Gateway 和插件

目标：

```text
Jarvis 不再绑定飞书
```

重点做：

```text
CLI Adapter
Web UI
更多平台接入
插件化工具注册
模型 Provider 抽象
```

---

## 16. 针对 Jarvis 当前阶段的最小落地建议

当前阶段不要贪多，建议优先做这几件事：

```text
1. 保留主图 + React 子图
2. 飞书只做 Gateway Adapter，不侵入 Agent Core
3. Obsidian Wiki 工具只暴露 query / draft / apply
4. 引入 Memory / Wiki / Skill 三分法
5. 每次复杂任务结束后，生成“可沉淀内容建议”
6. 高风险工具必须有权限和确认机制
7. 工具系统增加 risk_level / permission / scope 元数据
```

最小 MVP 可以是：

```text
飞书消息进入 Jarvis
  ↓
主图加载用户/项目上下文
  ↓
判断是否需要 RAG / Wiki / Skill
  ↓
React 子图调用工具
  ↓
返回结果
  ↓
任务结束后判断是否值得沉淀
  ↓
生成 Wiki/Skill 草稿
  ↓
用户确认后写入 Obsidian
```

---

## 17. 最终结论

Hermes 对 Jarvis 最值得借鉴的不是某个单点功能，而是一套长期个人 Agent 的工程范式：

```text
平台入口统一化
上下文管理分层化
长期知识资产化
工具能力插件化
技能经验可复用化
执行环境沙箱化
任务完成后可反思沉淀
```

Jarvis 不应该照搬 Hermes，但应该吸收它的核心设计。

最优路线是：

```text
保留 Jarvis 当前主图 + React 子图架构
吸收 Hermes 的 Skill / Memory / Gateway / Sandbox / Self-improvement 思想
围绕飞书 + 本地环境 + RAG + Obsidian Wiki 做垂直个人助理
```

一句话总结：

```text
Hermes 是通用个人 Agent Runtime 的参考样板；
Jarvis 应该做成更垂直、更可控、更贴合自己工作流的本地个人操作系统。
```