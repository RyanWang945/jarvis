# Jarvis DeepResearch Subagent V1 设计

日期：2026-05-16

## 背景

Jarvis 当前主架构是：

```text
intent classifier
  -> runtime policy
  -> ReAct loop
  -> tool calls
  -> final answer
```

这个架构适合 chat bot、轻量检索、单轮工具调用和局部任务处理。它的优势是简单、弹性高、能按模型即时判断是否需要工具。

但 deep research 的任务形态不同。它不是“看到一步结果后再想下一步”的开放式聊天，而是任务级证据管线：

1. 先拆解问题。
2. 明确子问题之间的依赖。
3. 控制搜索预算。
4. 收集并标准化证据。
5. 去重、排序、发现冲突和缺口。
6. 必要时补充搜索。
7. 基于证据合成最终答案。

如果把 deep research 继续塞进主 ReAct loop，会出现以下问题：

1. 搜索路径容易漂移。
2. 工具调用次数不可预测。
3. 搜索结果会快速撑爆上下文。
4. 最终回答很难追溯每个结论来自哪个来源。
5. 缺少任务级停止条件，容易在“继续搜”和“可以回答”之间摇摆。

因此，deep research 应作为独立 subagent 接入 Jarvis。主 Jarvis 继续保持简单的 intent routing 和普通 ReAct loop，复杂的研究执行模型封装在 DeepResearchSubagent 内部。

## 核心结论

V1 采用：

```text
DeepResearchSubagent = deterministic orchestrator + LLM planner/synthesizer + Tavily search
```

不在 V1 把 research worker 做成完整 agent。第一版先验证 DAG 计划、搜索预算、证据结构化和引用链路。后续再把某些 DAG 节点替换成 agent worker。

边界如下：

```text
Jarvis main runtime:
  负责意图识别、路由、权限策略、会话写回。

DeepResearchSubagent:
  负责研究计划、DAG 执行、搜索、证据归一化、最终合成。

Tavily:
  只是 V1 的 evidence acquisition backend，不直接决定最终回答。
```

## 设计目标

1. 把 deep research 从主 ReAct loop 中隔离出来。
2. 第一版只支持 plan DAG + Tavily search。
3. 搜索过程可预算、可追踪、可测试。
4. 所有最终结论都能回到 evidence/source。
5. 保持 V1 实现足够小，后续可以把 worker 升级成 agent。
6. 不破坏现有 chat、coding、artifact delivery 和普通 research 流程。

## 非目标

V1 不做：

1. 多 agent worker。
2. 网页全文抓取和长文抽取。
3. PDF、代码仓库、表格、数据库等复杂工具接入。
4. 多轮长期 research session。
5. 自动写入知识库。
6. 复杂 DAG 并行调度器。
7. 自动引用格式规范化到论文级别。
8. 取代现有普通 ReAct research 场景。

## 架构边界

目标架构：

```text
User message
  -> turn classifier
  -> runtime policy
  -> loop provider resolver
     -> REACT: existing ReAct loop
     -> RESEARCH: DeepResearchRuntime
  -> final response
```

当前代码中已经存在 `TurnLoopProvider.RESEARCH` 枚举，但 runtime 目前只支持 `REACT`。因此 DeepResearchSubagent 的接入点应是新增一个 research loop provider，而不是在现有 ReAct prompt 中追加大量 deep research 规则。

建议演进：

```text
V1:
  DeepResearchRuntime 作为 TurnRuntime 的一个分支。

V1.5:
  支持 conversation metadata 或 runtime profile 选择 research loop provider。

V2:
  将 search/analyze 节点替换为 worker agent。
```

## V1 执行流程

```text
DeepResearchRuntime.invoke
  -> prepare_context
  -> plan_research
  -> validate_dag
  -> execute_dag
  -> normalize_evidence
  -> optional_gap_check
  -> synthesize_final_answer
  -> finalize_turn_success
```

### 1. prepare_context

输入：

1. 当前用户问题。
2. 会话摘要。
3. runtime policy。
4. 当前时间。
5. 可选的 recent artifacts 或 repository context。

V1 只使用用户问题、会话摘要和时间上下文。不要在 V1 自动读取仓库或历史 artifacts。

### 2. plan_research

调用 planner LLM，输出一个小型 ResearchDAG。

规划原则：

1. DAG 节点数量默认 3 到 6 个。
2. 搜索节点最多 6 个。
3. 每个 search 节点只允许一次 Tavily 调用。
4. query 必须具体，避免宽泛搜索。
5. synthesize 节点只依赖已有节点，不直接调用外部工具。

### 3. validate_dag

执行前必须验证：

1. 节点 id 唯一。
2. `depends_on` 指向存在节点。
3. DAG 无环。
4. search 节点数量不超过预算。
5. 节点类型属于 V1 允许集合。
6. 所有 search 节点都有非空 query。

校验失败时，不应直接把坏计划交给执行器。V1 可以做一次 plan repair；repair 失败则退化为单 query Tavily search + 直接 synthesis。

### 4. execute_dag

V1 可以按拓扑排序顺序执行，不必做真正并行。即使 DAG 结构支持依赖，执行器先保持同步顺序，降低复杂度。

执行策略：

1. `search` 节点调用 Tavily。
2. `analyze` 节点基于依赖节点的 evidence 做局部归纳。
3. `synthesize` 节点合成最终 answer。

V1 也可以暂时省略 `analyze` 节点，只支持 `search` 和最终 `synthesize`。如果保留 `analyze` 类型，建议它只调用 LLM，不调用工具。

### 5. normalize_evidence

Tavily 返回结果不能直接喂给最终回答。需要先标准化为 evidence。

标准化目标：

1. 保留 URL、标题、snippet、query、node id。
2. 抽取有限数量 claim。
3. 标记不确定性。
4. 去除重复 URL。
5. 保留来源和结论之间的映射。

### 6. optional_gap_check

V1 gap check 只做一轮，且默认不开复杂补查。

允许补查的条件：

1. 关键子问题没有任何来源。
2. 主要来源明显冲突。
3. 用户明确要求最新、最近、全面比较。

补查预算建议最多 2 次 Tavily search。

### 7. synthesize_final_answer

最终回答必须基于 evidence，而不是基于 planner 的原始设想。

合成要求：

1. 先回答用户问题。
2. 清楚区分事实、推断和建议。
3. 标注主要来源。
4. 对冲突或证据不足的地方明确说明。
5. 不暴露内部 DAG 细节，除非用户要求。

## ResearchDAG 数据结构

V1 建议定义内部 dataclass 或 Pydantic model。

```python
from typing import Literal

NodeType = Literal["search", "analyze", "synthesize"]

class ResearchNode:
    id: str
    type: NodeType
    query: str | None
    instruction: str | None
    depends_on: list[str]

class ResearchDAG:
    objective: str
    budget: ResearchBudget
    nodes: list[ResearchNode]

class ResearchBudget:
    max_searches: int = 6
    max_followup_searches: int = 2
    max_results_per_search: int = 5
```

示例：

```json
{
  "objective": "比较 A 和 B 的最新架构进展",
  "budget": {
    "max_searches": 4,
    "max_followup_searches": 1,
    "max_results_per_search": 5
  },
  "nodes": [
    {
      "id": "search_a",
      "type": "search",
      "query": "A latest architecture 2026",
      "depends_on": []
    },
    {
      "id": "search_b",
      "type": "search",
      "query": "B latest architecture 2026",
      "depends_on": []
    },
    {
      "id": "compare",
      "type": "synthesize",
      "instruction": "Compare architecture, tradeoffs, maturity, and uncertainty.",
      "depends_on": ["search_a", "search_b"]
    }
  ]
}
```

## Evidence 数据结构

Evidence 是 V1 的关键设计。没有 evidence 层，deep research 会退化成“多搜几次再总结”。

建议结构：

```python
class ResearchEvidence:
    evidence_id: str
    node_id: str
    query: str
    title: str
    url: str
    published_date: str | None
    snippet: str
    claims: list[ResearchClaim]
    score: float | None

class ResearchClaim:
    text: str
    confidence: Literal["low", "medium", "high"]
    source_url: str
    notes: str | None
```

V1 的 claim 抽取可以先简单：

1. 每条 Tavily result 生成 0 到 2 条 claim。
2. claim 文本不得超过 240 字。
3. 不能从 snippet 中没有的信息外推。
4. 没有足够信息时 claims 为空，但 source 仍保留。

## Tavily 使用策略

V1 继续复用现有 `tavily_search` 工具能力，但 DeepResearchSubagent 内部不应依赖主 ReAct 模型自行决定是否搜索。

建议封装一个内部 adapter：

```text
TavilySearchClient.search(query, depth, topic, max_results)
  -> RawSearchResult[]
```

然后由 deepresearch runtime 将 raw result 转成 evidence。

默认参数：

```text
search_depth = "advanced" for deepresearch
topic = "general"
max_results = 5
```

当用户明确要求新闻、最近 7 天或当前动态时：

```text
topic = "news"
```

V1 需要沿用全局 Tavily API key 和错误处理，不新增独立配置。

## 与主 ReAct 的关系

DeepResearchSubagent 不应该作为主 ReAct 内部的一串工具调用实现。

推荐边界：

```text
主 ReAct:
  适合普通聊天、轻量搜索、一次性工具调用。

DeepResearchRuntime:
  适合明确 deep research、多来源对比、需要证据链的长答案。
```

主 Jarvis 可以把 deepresearch 看作一个 subagent：

```text
input: user question + context
output: final answer + sources + optional trace metadata
```

但 subagent 内部应该有自己的执行模型，不要把每个 DAG 节点暴露给主 ReAct 决策。

## 路由策略

V1 可以先使用显式触发：

```text
/research <question>
```

或 conversation metadata：

```json
{
  "runtime_profile": {
    "loop_provider": "research"
  }
}
```

后续再由 classifier 自动区分普通 research 和 deepresearch。

自动路由可以考虑这些信号：

1. 用户明确说“深度调研”、“deep research”、“全面比较”。
2. 问题要求多来源、多维度对比。
3. 用户要求引用来源。
4. 用户要求最新事实并做综合判断。
5. 问题明显不能由一次搜索回答。

不要把所有 `scene=research` 都路由到 DeepResearchRuntime。普通“查一下今天某个信息”仍应走现有 ReAct + Tavily。

## 状态与持久化

V1 最小持久化可以只写最终 assistant message 和 tool call trace。

但建议在 final raw_payload 中保存 research trace 摘要：

```json
{
  "source": "deepresearch_runtime",
  "research": {
    "objective": "...",
    "node_count": 4,
    "search_count": 3,
    "source_count": 12,
    "evidence_count": 10
  },
  "sources": [
    {
      "title": "...",
      "url": "..."
    }
  ]
}
```

详细 DAG 和 evidence 可以后续再落表。V1 若要方便调试，也可以先把完整 trace 放进 raw_payload，但需要注意体积。

## 错误处理

### Planner 失败

策略：

1. 重试一次 JSON plan。
2. 失败后退化为单搜索计划。
3. 单搜索也失败则返回明确错误。

### DAG 校验失败

策略：

1. 尝试 repair 一次。
2. repair 后仍失败则退化为单搜索计划。

### Tavily 失败

策略：

1. 单个 search 节点失败不立即终止。
2. 记录 node error。
3. 如果所有 search 都失败，最终回复说明外部搜索不可用。
4. 如果部分成功，基于已有 evidence 回答并说明覆盖不足。

### Evidence 不足

策略：

1. 不编造。
2. 明确说明没有找到足够来源。
3. 给出可回答部分。
4. 如有必要，说明哪些问题需要进一步搜索。

## 测试计划

### 单元测试

1. planner 输出合法 DAG 后可通过校验。
2. 重复 node id 会被拒绝。
3. 环形依赖会被拒绝。
4. search 节点超过预算会被拒绝。
5. Tavily result 能转成 evidence。
6. 重复 URL 会去重。
7. Tavily 部分失败时仍能 synthesis。

### 契约测试

1. `/research` 可以进入 DeepResearchRuntime。
2. 普通 chat 不进入 DeepResearchRuntime。
3. 普通 current lookup 仍可走 ReAct + Tavily。
4. DeepResearchRuntime 的 final raw_payload 包含 research summary。
5. final answer 包含来源 URL。

### 回归测试

1. 不影响 `delegate_to_codex`。
2. 不影响 artifact delivery。
3. 不影响现有 Tavily search budget。
4. 不影响 session mode writeback。

## 文件组织建议

建议新增：

```text
app/deepresearch_agent/
  __init__.py
  models.py
  planner.py
  dag.py
  tavily_client.py
  evidence.py
  synthesizer.py
  runtime.py
```

职责：

```text
models.py:
  ResearchDAG / ResearchNode / ResearchEvidence / ResearchResult

planner.py:
  LLM plan generation and repair

dag.py:
  validation and topological ordering

tavily_client.py:
  internal adapter around existing Tavily API/tool behavior

evidence.py:
  result normalization, dedupe, claim extraction

synthesizer.py:
  final answer generation from evidence

runtime.py:
  DeepResearchRuntime.invoke
```

`app/agent_react/runtime.py` 只负责选择 loop provider，不承载 deepresearch 细节。

## 后续演进

### V2: SearchWorker agent

将 search 节点从确定性 Tavily call 升级为 SearchWorker：

```text
SearchWorker tools:
  - tavily_search
  - fetch_page
  - x_search
```

仍要求输出统一 evidence schema。

### V3: AnalystWorker agent

将 analyze 节点升级为 AnalystWorker：

```text
AnalystWorker tools:
  - code/read docs
  - table extraction
  - calculation
  - knowledge_base_search
```

适用于技术调研、代码库对比、财报/论文类分析。

### V4: Critic 和 GapChecker

新增 critic 节点：

```text
planner -> workers -> critic -> followup search -> synthesizer
```

critic 不直接生成最终答案，只负责发现：

1. 来源是否单薄。
2. 是否有明显冲突。
3. 是否遗漏用户要求的维度。
4. 是否有过度推断。

## 开放问题

1. DeepResearchRuntime 是否默认绑定强模型，还是沿用 conversation active model？
2. research trace 是否需要单独落表，还是先放在 turn raw_payload？
3. V1 是否允许并行执行 search 节点？
4. `/research` 是否总是 deepresearch，还是保留普通 research mode？
5. Tavily 是否需要开启 raw content，还是等 fetch_page 工具再做全文证据？

## 推荐实现顺序

1. 定义 `models.py` 和 `dag.py`。
2. 实现 Tavily adapter 和 evidence normalization。
3. 实现 planner prompt 和 plan validation。
4. 实现 deterministic `DeepResearchRuntime`。
5. 在 `TurnLoopProvider.RESEARCH` 接入 runtime 分支。
6. 加单元测试和最小 contract 测试。
7. 再考虑自动路由和 worker agent 化。

第一版的关键验收标准不是“像人一样深度研究”，而是：

```text
同一个问题能稳定生成小 DAG；
按预算完成搜索；
每个结论有来源；
失败时可降级；
主 ReAct loop 不被 deepresearch 复杂性污染。
```
