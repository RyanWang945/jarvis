# Jarvis ReactRuntime 网页搜索能力改造方案

| 项目   | 内容                                                              |
| ---- | --------------------------------------------------------------- |
| 主题   | 让 Jarvis 在网页搜索任务中更准确识别信息时效性、构造搜索策略、校验证据                         |
| 适用场景 | ReactRuntime 执行 Tavily/Web Search 工具                            |
| 典型问题 | 用户问“看看金价”时，Jarvis 应意识到这是当前行情问题，而不是普通网页搜索                        |
| 核心原则 | Planner 负责把问题变成好任务；React node 负责把任务执行成好证据；Aggregator 负责把证据变成好回复 |

---

## 1. 当前问题

当前 ReactRuntime system prompt 更像是运行时约束：

```text
你是 Jarvis ReactNodeExecuteRuntime。执行一个非仓库计划节点。
当任务需要外部信息、业务知识、项目记忆、提醒或 artifact 交付动作时，使用工具。
...
不要生成最终用户回复。
工具使用后，返回包含 summary、findings、sources、data 和 artifacts 的 JSON。
```

这套 prompt 的问题不是不完整，而是重点偏错了。

它强调了：

```text
1. 运行时身份
2. 能不能使用工具
3. 不能执行代码和 shell
4. read / write mode
5. 返回 JSON
6. workspace 路径
```

但它没有充分指导：

```text
1. 搜索前如何理解用户的信息需求
2. 如何判断问题是否天然具有强时效性
3. 如何避免裸搜用户原文
4. 如何构造更好的搜索 query
5. 如何判断网页结果是否新鲜、可信、可用
6. 如何把不确定性传给下游 aggregator
```

因此对于用户短查询：

```text
看看金价
```

ReactRuntime 容易退化成：

```text
搜“金价” -> 拿几个网页 -> 总结
```

这会导致搜索结果可能过旧、口径混乱、来源不稳，最终回答不可靠。

---

## 2. 设计目标

本次改造的目标不是写一堆硬规则，例如：

```text
if query contains 金价 then realtime
```

而是提升 ReactRuntime 的搜索执行能力，让它在搜索前主动推理：

```text
1. 用户到底想知道什么？
2. 这个信息是稳定信息、近期信息，还是快速变化信息？
3. 哪类网页更可靠？
4. 哪类网页容易误导？
5. 应该搜索哪些 query？
6. 搜到结果后，必须验证哪些证据？
```

也就是从：

```text
用户问题 -> 裸搜索 -> 总结
```

改成：

```text
用户问题 -> 信息需求推理 -> 搜索策略 -> 网页证据校验 -> 结构化 evidence -> Aggregator 回复
```

---

## 3. 总体分层

推荐拆成三层职责。

```text
Planner 层：
  负责判断是否需要外部信息，并生成边界清晰的 react read node。

ReactRuntime / Node 层：
  负责根据 node objective 选择 runtime skill，组装执行 prompt，执行搜索前推理、query 构造、结果校验、证据整理。

Aggregator 层：
  负责消费 evidence，生成最终用户回复，并显式处理不确定性。
```

一句话：

```text
Planner 负责“把问题变成好任务”；
React node 负责“把任务执行成好证据”；
Aggregator 负责“把证据变成好回复”。
```

---

## 4. Skill 应放在哪里

### 4.1 不建议把完整 web-search skill 放在 Planner

Planner 不应该学会：

```text
1. Tavily query 怎么写
2. 搜几个 query
3. 搜索结果是否过期
4. 哪个网页可信
5. 是否需要二次搜索
6. 如何解析网页证据
```

否则 Planner 会变成半个执行器，复杂度会快速膨胀。

### 4.2 推荐把 skill 选择放在 ReactRuntime 内部

不建议让 Planner 输出 `required_skills`。

原因是：

```text
1. Planner 的职责是任务拆分，不是执行策略选择。
2. skill 选择依赖当前 runtime、工具可用性和节点类型，更接近执行器职责。
3. 如果把 required_skills 放进 plan schema，会扩大 Planner 输出面，增加校验和兼容成本。
4. 对同一个 objective，runtime 可以根据当前工具和环境选择不同执行 skill，这比 Planner 静态指定更灵活。
```

推荐流程：

```text
Planner:
  生成 runtime=react、mode=read、objective 清晰的 node。

ReactRuntime:
  在执行前基于 node objective / mode / available_tools 做一次轻量 skill selection。
  将选中的 runtime skill 内容拼接进 Claude Agent 的 prompt。
```

### 4.3 Runtime Web Search Skill

#### `skills/runtime/web-search.md`

给 ReactRuntime 用，负责真正搜索执行。

内容重点：

```text
1. 搜索前生成 search brief
2. 判断信息新鲜度
3. 对短查询做 query expansion
4. 避免裸搜用户原文
5. 优先合适的来源类型
6. 检查时间、单位、来源、数据口径
7. 不确定时返回 uncertainty
```

这个 skill 不要求 Planner 显式指定。ReactRuntime 可以通过内部 LLM 选择器或启发式先筛选候选 runtime skills，再把匹配 skill 拼进 prompt。

### 4.4 Planner Skill 可选，不作为第一阶段必需项

可以保留一个轻量 planner skill 帮助 Planner 写出更好的 objective，但它不应引入新字段。

内容重点：

```text
1. 什么时候创建 react read node
2. 如何写 objective
3. 如何把用户意图、歧义、默认口径和时效要求压进 objective
4. 避免让 React node 直接“面向用户回复”
```

---

## 5. Planner 改动

### 5.1 Planner 只输出任务，不输出执行细节

Planner 应信任自己的任务拆分和意图解释能力。React node 应局限在 Planner 交给它的工作内容内，不应该再拿原始用户消息重新解释用户目标。

因此不建议新增这些 plan 字段：

```json
{
  "original_user_message": "...",
  "required_skills": ["runtime/web-search"],
  "evidence_contract": {},
  "output_hint": "..."
}
```

原因是：

```text
1. original_user_message 会让 React node 绕过 Planner 重新解释用户意图。
2. required_skills 属于执行器策略，应由 ReactRuntime 基于 objective 选择。
3. evidence_contract 的通用要求应沉淀到 runtime skill，而不是每个 node 重复携带。
4. output_hint 容易和 objective 重复，价值有限。
5. finalization_hint 应由 runtime 根据 plan 自动推导，不应交给 Planner 输出。
```

### 5.2 Planner 的核心责任是写好 objective

当前 objective：

```text
查询当前金价（例如现货黄金或国内金价）并将结果以可读形式呈现给用户
```

问题有两个：

```text
1. “呈现给用户”与 ReactRuntime 的“不要生成最终用户回复”冲突。
2. 没有说明口径、默认假设、时效要求和旧数据处理方式。
```

建议改成：

```text
收集当前黄金行情证据，供最终回复使用。用户未指定口径，默认优先查看国际现货黄金 XAU/USD；如能获取可靠来源，再补充国内人民币/克口径。必须判断数据时效性，旧数据不能包装成当前数据。不要直接生成最终用户回复。
```

### 5.3 Objective 应包含哪些信息

好的 react read node objective 应尽量包含：

```text
1. 本 node 要完成的证据任务。
2. 默认口径或关键假设。
3. 已知歧义的处理方式。
4. 时效性要求，尤其是市场价格、新闻、政策、天气等快速变化信息。
5. 失败或不确定时的表达边界，例如“旧数据不能包装成当前数据”。
6. 不生成最终用户回复。
```

### 5.4 “看看金价”的理想 Planner 输出

Planner 输出保持简洁：

```json
{
  "user_objective": "查看当前金价",
  "nodes": [
    {
      "id": "check_gold_price",
      "runtime": "react",
      "mode": "read",
      "objective": "收集当前黄金行情证据，供最终回复使用。用户未指定口径，默认优先查看国际现货黄金 XAU/USD；如能获取可靠来源，再补充国内人民币/克口径。必须判断数据时效性，旧数据不能包装成当前数据。不要直接生成最终用户回复。",
      "input_refs": []
    }
  ]
}
```

`finalization_hint` 不需要 Planner 输出。Runtime 可以根据 nodes 自动判断是否需要 Aggregator；react/coder/multi-node 默认走 Aggregator。

---

## 6. ReactRuntime System Prompt 改动

当前 ReactRuntime system prompt 可以保留运行时约束，但需要增加搜索行为规范。

建议改成：

```text
你是 Jarvis ReactNodeExecuteRuntime。
你负责执行一个非代码计划节点，并通过可用工具收集、验证、整理证据。

你的职责不是生成最终用户回复。
你的职责是为下游 Aggregator 返回可用 evidence。

当任务需要外部信息、业务知识、项目记忆、提醒或 artifact 交付动作时，使用工具。

不要执行代码编辑、shell 命令、仓库工作流或代码 agent 委派。
代码和 shell 工作属于 coder runtime nodes。

temporal_context / runtime_context 中的 current_time 是权威当前时间。

遵循 node.mode：
- read：只收集和分析证据；不要创建或修改文件/artifacts。
- write：只通过可用的 Jarvis artifact/file 工具创建用户请求的非代码 artifacts，并在结果中包含 artifact metadata。

Web Search Behavior：
当节点需要网页搜索时，不要盲目搜索用户原话。
在第一次搜索前，先生成一个 compact search brief，包括：
- information_need：用户真正需要的信息
- freshness_requirement：stable / recent / fast_changing
- freshness_reason：为什么这么判断
- source_preferences：优先来源类型
- misleading_source_risks：容易误导的来源类型
- query_candidates：候选搜索 query
- evidence_required：回答前必须拿到的证据

有些信息即使用户没有显式说“今天 / 当前 / 最新 / 现在”，也天然具有强时效性。
例如：
- 市场价格
- 金价、油价、股价、加密货币价格
- 汇率
- 天气
- 体育比分
- 当前新闻
- 法律法规政策
- 商品价格和库存
- 软件版本
- 活动时间、开放时间、赛程、航班、签证政策等

对于 fast_changing 信息：
- 使用包含 live / current / today / latest / 具体日期的 query。
- 优先找数据页、行情页、官方页、源头页，而不是旧新闻或 SEO 文章。
- 必须检查结果中是否有价格、单位、更新时间、交易日期、发布日期或其他 update context。
- 如果无法确认新鲜度，必须在 uncertainties 中说明。
- 如果用户问题很短或有歧义，应尝试多个 query phrasing，而不是只搜原始短语。

工具使用后，返回 JSON。
不要生成最终用户回复。

返回结构：
{
  "status": "completed | failed | blocked",
  "summary": string,
  "findings": array,
  "sources": array,
  "data": {
    "search_brief": object,
    "uncertainties": array,
    "...": "other structured evidence"
  },
  "artifacts": array
}

保持简洁，但保留下游 Aggregator 有用的证据。
```

---

## 7. Runtime Web Search Skill

建议新增文件：

```text
skills/runtime/web-search.md
```

内容如下：

```text
# Runtime Skill: Web Search

Use this skill when a React node needs external web information.

## Core Principle

Do not blindly search the user's exact words.

Before searching, infer the user's information need:
- What is the user probably trying to know?
- Is the answer stable, recent, or fast-changing?
- What source type would be reliable?
- What source type would be misleading?
- What evidence must be present before the result can be used?

## Freshness Reasoning

Some information is fast-changing even when the user does not explicitly say "today", "latest", "current", or "now".

Examples:
- market prices
- gold price, oil price, stock price, crypto price
- exchange rates
- weather
- sports scores
- current events
- laws, policies, regulations
- product prices, availability, inventory
- software versions
- schedules, events, transport, visa rules

For fast-changing information:
- Use query terms like "live", "current", "today", "latest", or the concrete current date.
- Prefer source/data pages over old articles.
- Verify timestamp, trading date, publication date, update time, or other update context.
- Prefer results that contain concrete values, units, and timestamps.
- If freshness cannot be verified, return this as an uncertainty.

## Query Construction

For short or ambiguous user queries:
- Generate multiple query candidates.
- Use different phrasings.
- Prefer English queries for global market, finance, and technical information when likely to retrieve better sources.
- Prefer Chinese queries for China-specific or local information.
- Do not rely on one raw query.

For example, for "看看金价":
- "XAU USD live gold price today"
- "spot gold price live USD per ounce"
- "COMEX gold futures price today"
- "上海黄金交易所 Au99.99 今日价格"

## Source Evaluation

Prefer:
- official or primary source pages
- exchange pages
- finance quote pages
- market data pages
- pages with explicit update time or trading date

Be careful with:
- old news articles
- SEO pages
- pages without timestamp
- summaries without source
- brand retail pages unless the user asks for retail price
- pages that mix different units or asset scopes

## Evidence Contract

Before returning, check:
- Did I identify the asset or topic scope?
- Did I collect a concrete value if the user asked for a value?
- Did I include unit?
- Did I include timestamp, trading date, publication date, or update context?
- Did I include source?
- Did I preserve ambiguity and uncertainty?

Return uncertainty explicitly instead of hiding it.
```

---

## 8. ReactRuntime Skill Selection

ReactRuntime 应在执行 node 前做一次轻量 skill selection，而不是依赖 Planner 输出 `required_skills`。

输入可以是：

```text
node.id
node.runtime
node.mode
node.objective
available_tools
current_time / timezone
```

输出可以是内部结构，不进入 plan schema：

```json
{
  "selected_skills": ["runtime/web-search"],
  "reason": "objective requests current market price evidence and requires freshness/source validation"
}
```

Skill selection 可以先用 LLM 做，也可以用简单候选规则缩小范围后再让 LLM 判断。关键点是：选中 skill 后，ReactRuntime 必须把 skill 内容拼进 Claude Agent prompt，而不是让模型再通过 `Skill` 工具碰运气加载。

---

## 9. User Prompt 瘦身

当前 user prompt 塞入了大量 runtime path：

```text
session_workspace_dir
session_artifacts_dir
session_approvals_dir
session_nodes_dir
node_workspace_dir
node_repo_dir
node_task_path
node_progress_path
node_result_markdown_path
node_state_path
node_artifacts_dir
node_input_snapshot_path
node_output_path
node_result_path
node_manifest_path
provider_run_dir
```

对 read mode 的网页搜索任务，这些基本都是噪声。

建议：不要把精简 JSON 直接裸传给 Claude Agent，而是组装成面向 agent 的 user prompt。JSON 只作为必要上下文的一部分。

### 9.1 read mode 默认不暴露完整 workspace path

React node 的 read mode prompt 只保留任务语义、时间上下文和必要输入：

```md
你正在执行一个 Jarvis React 节点。只完成本节点，不生成最终用户回复。

## Task

节点 ID：check_gold_price
执行模式：read

节点目标：
收集当前黄金行情证据，供最终回复使用。用户未指定口径，默认优先查看国际现货黄金 XAU/USD；如能获取可靠来源，再补充国内人民币/克口径。必须判断数据时效性，旧数据不能包装成当前数据。不要直接生成最终用户回复。

## Time Context

当前日期：2026-07-03
当前时间：2026-07-03T21:24:55+08:00
时区：Asia/Shanghai

## Selected Runtime Skills

以下 skill 已由 ReactRuntime 根据节点目标选择并注入到 system prompt 或本 prompt 前置上下文：

- runtime/web-search

## Resolved Inputs

无。

## Output

返回符合 schema 的 JSON：status、summary、findings、sources、data、artifacts。
```

### 9.2 write mode 或 artifact node 再暴露路径

只有在以下场景才把 workspace/artifact path 注入 prompt：

```text
1. node.mode = write
2. 用户明确要求生成 artifact
3. ReactRuntime 需要读取或写入文件型产物
4. 下游节点需要基于文件路径继续处理
```

否则路径下沉到 runtime 环境，不进入 LLM 主上下文。

---

## 10. Aggregator 改动

Aggregator 不需要学会搜索，但必须学会消费 evidence。

建议增加约束：

```text
当 React node 返回 data.uncertainties 时，最终用户回复必须体现这些不确定性。
当 evidence 缺少 timestamp/source/update context 时，不要把结果包装成确定的实时信息。
当用户问题存在口径歧义时，最终回复应说明默认口径。
```

例如 React 返回：

```json
{
  "data": {
    "asset_scope": "XAU/USD spot gold",
    "price": "xxx",
    "unit": "USD/oz",
    "timestamp": null,
    "source": "example finance page",
    "uncertainties": [
      "source page did not expose a clear update time",
      "用户未指定国际现货黄金、国内金价、黄金期货或品牌首饰金，Planner objective 默认采用国际现货黄金口径。"
    ]
  }
}
```

Aggregator 不能回复：

```text
当前金价是 xxx 美元/盎司。
```

而应该回复：

```text
我默认看的是国际现货黄金 XAU/USD，不是品牌首饰金。网页显示约为 xxx 美元/盎司，但该页面没有明确更新时间，实时性需要打折。
```

---

## 11. Tavily 工具描述改动

如果工具描述只是：

```json
{
  "query": "string"
}
```

模型会把它当普通搜索框。

建议 Tavily 工具描述改成：

```text
Use this tool to search the web.

Do not blindly copy the user's wording when the user query is short, ambiguous, or time-sensitive.

For time-sensitive facts, construct the query yourself with concrete freshness terms such as:
- today
- current
- live
- latest
- the concrete current date

For prices, market quotes, exchange rates, schedules, policies, software versions, and product availability:
- prefer source pages, data pages, finance quote pages, official pages, or pages with explicit update time
- avoid old news articles and SEO pages unless they are being used only for background
- after receiving results, inspect title, snippet, date, source type, and whether the result actually contains the requested value

Return enough metadata for the agent to judge freshness and source quality.
```

---

## 12. “看看金价”的理想执行链路

### 12.1 用户输入

```text
看看金价
```

### 12.2 Planner 输出

```json
{
  "user_objective": "查看当前金价",
  "nodes": [
    {
      "id": "check_gold_price",
      "runtime": "react",
      "mode": "read",
      "objective": "收集当前黄金行情证据，供最终回复使用。用户未指定口径，默认优先查看国际现货黄金 XAU/USD；如能获取可靠来源，再补充国内人民币/克口径。必须判断数据时效性，旧数据不能包装成当前数据。不要直接生成最终用户回复。",
      "input_refs": []
    }
  ]
}
```

### 12.3 ReactRuntime 内部 Skill Selection

```json
{
  "selected_skills": ["runtime/web-search"],
  "reason": "当前黄金行情属于 fast-changing market price，需要网页搜索、来源优先级和 freshness 校验。"
}
```

### 12.4 组装后的 Claude Agent User Prompt

```md
你正在执行一个 Jarvis React 节点。只完成本节点，不生成最终用户回复。

## Task

节点 ID：check_gold_price
执行模式：read

节点目标：
收集当前黄金行情证据，供最终回复使用。用户未指定口径，默认优先查看国际现货黄金 XAU/USD；如能获取可靠来源，再补充国内人民币/克口径。必须判断数据时效性，旧数据不能包装成当前数据。不要直接生成最终用户回复。

## Time Context

当前日期：2026-07-03
当前时间：2026-07-03T21:24:55+08:00
时区：Asia/Shanghai

## Selected Runtime Skills

- runtime/web-search

## Output

返回符合 schema 的 JSON：status、summary、findings、sources、data、artifacts。
```

### 12.5 ReactRuntime 内部 Search Brief

```json
{
  "information_need": "current gold market price",
  "freshness_requirement": "fast_changing",
  "freshness_reason": "gold is a traded market asset and intraday prices change frequently",
  "source_preferences": [
    "live quote pages",
    "market data pages",
    "exchange or finance sites"
  ],
  "misleading_source_risks": [
    "old news articles",
    "SEO pages without timestamp",
    "retail jewelry brand pages unless user asks for jewelry price"
  ],
  "query_candidates": [
    "XAU USD live gold price today",
    "spot gold price live USD per ounce",
    "COMEX gold futures price today",
    "上海黄金交易所 Au99.99 今日价格"
  ],
  "evidence_required": [
    "price",
    "unit",
    "timestamp or trading date",
    "source"
  ]
}
```

### 12.6 ReactRuntime 返回 evidence

```json
{
  "summary": "已收集当前黄金行情证据。",
  "findings": [
    {
      "asset_scope": "international spot gold XAU/USD",
      "price": "...",
      "unit": "USD/oz",
      "change": "...",
      "timestamp_or_trading_date": "...",
      "source": "...",
      "freshness_assessment": "fresh enough / uncertain / stale"
    }
  ],
  "sources": [
    {
      "name": "...",
      "url": "...",
      "source_type": "finance quote page",
      "observed_date_or_update_context": "..."
    }
  ],
  "data": {
    "default_scope": "international spot gold XAU/USD",
    "market_snapshot": [],
    "search_brief": {
      "information_need": "current gold market price",
      "freshness_requirement": "fast_changing",
      "freshness_reason": "gold is a traded market asset and intraday prices change frequently"
    },
    "uncertainties": [
      "用户未说明是国际现货黄金、国内金价、黄金期货还是品牌首饰金，Planner objective 默认采用国际现货黄金口径。"
    ]
  },
  "artifacts": []
}
```

### 12.7 Aggregator 最终回复

```text
我默认看的是国际现货黄金 XAU/USD，不是品牌首饰金。

当前约为 xxx 美元/盎司，较前一交易日 xxx。
数据时间：xxx。
来源：xxx。

如果你想看国内金价，需要另外看上海金 Au99.99 或人民币/克口径。
```

---

## 13. 最小落地版本

不需要一次性大改。最小版本只做五件事。

### 13.1 Planner 只强化 objective

不新增 plan 字段。只要求 Planner 对需要网页证据的 react read node 写出更清晰的 objective：

```text
1. 明确这是证据收集任务，不是最终回复任务。
2. 写出默认口径和必要假设。
3. 写出时效性要求。
4. 写出旧数据、不确定数据的处理边界。
```

### 13.2 ReactRuntime 内部选择 runtime skill

在 Claude Agent 启动前，对 `node.objective` 做一次轻量 skill selection。选中 `runtime/web-search` 后动态拼接：

```text
base_react_runtime_prompt
+ selected_runtime_skills
+ node_prompt
```

### 13.3 read mode prompt 瘦身并改成任务说明

read mode 不再塞完整 workspace path，也不裸传整块 JSON。改成 Markdown 任务说明，只保留：

```text
node_id
mode
objective
current_time
timezone
resolved_inputs
selected_runtime_skills
```

### 13.4 Aggregator 显式处理 uncertainties

增加一条硬约束：

```text
如果 evidence.data 中有 uncertainties，最终回复必须体现。
如果时效性信息缺少 timestamp/source，不要伪装成确定实时数据。
```

### 13.5 finalization 由 runtime 推导

Planner 不再输出 `finalization_hint`。Runtime 根据 nodes 自动决定：

```text
react/coder/multi-node -> Aggregator
明确 pass-through 的单节点直接回复 -> pass_through
```

---

## 14. 非目标

本次改造不做：

```text
1. 不引入专门行情 API
2. 不在代码里硬编码“金价=实时”
3. 不让 Planner 执行搜索
4. 不把所有 web-search 细节塞进 Planner
5. 不让 ReactRuntime 生成最终用户回复
6. 不让 read node 暴露大量 workspace 路径
7. 不新增 original_user_message / required_skills / evidence_contract / output_hint 等 plan 字段
```

---

## 15. 核心结论

当前 Jarvis 的 ReactRuntime prompt 长在 runtime bookkeeping 上，但短在搜索执行能力上。

要解决“看看金价”这类问题，不应该靠硬规则：

```text
金价 -> realtime
```

而应该靠三层能力：

```text
Planner：
  把短问题塑造成一个好的 evidence collection node。

ReactRuntime：
  根据 objective 选择 web-search skill，组装 Claude Agent prompt，先做信息需求推理，再构造 query，最后校验证据。

Aggregator：
  根据 evidence 生成最终回复，并显式处理口径和不确定性。
```

最终目标是让 Jarvis 从：

```text
会调用搜索工具
```

进化到：

```text
会判断自己要找什么、该怎么搜、搜到的东西能不能用。
```
