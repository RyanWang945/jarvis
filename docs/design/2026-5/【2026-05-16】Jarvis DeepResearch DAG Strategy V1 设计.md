# Jarvis DeepResearch DAG Strategy V1 设计

日期：2026-05-16

更新：2026-06-08

## 背景

这份文档原先把 DeepResearch 设计成独立 `DeepResearchSubagent` / `DeepResearchRuntime`。这个方向现在废弃。

新的判断是：

```text
DeepResearch 不是子 agent，也不是节点 runtime。
DeepResearch 是外层 TaskDAG 的一种规划策略和可展开子图。
```

原因很直接：DeepResearch 本质上也是 planner + DAG + evidence + synthesis。如果把它做成独立 subagent，它内部会重复实现一套调度、状态、恢复、权限、进度、产物流转和汇总逻辑，和外层 DAGRuntime 形成双 runtime。长期看，这会让复杂研究、复杂代码任务、研究后代码审查等混合任务无法共享同一个执行引擎。

## 核心结论

V1 采用统一 DAG 架构：

```text
Agent Runtime
  -> FastIntent
  -> Planner / PlanExpander
  -> TaskDAG
  -> DAGRuntime
     -> LLMWorker
     -> ReactWorker
     -> CodexWorker
  -> Aggregator
  -> EffectHandler
```

DeepResearch 的位置：

```text
DeepResearch = plan_kind / strategy / expandable subgraph
```

不是：

```text
DeepResearch != subagent
DeepResearch != NodeRuntime
DeepResearch != tool
```

ReactLoop 的位置：

```text
ReactLoop = 单个 research 节点的 worker 执行方式
```

一个 ReactWorker 只执行一个明确节点，例如“搜索并提取某个子问题的证据”。它不负责全局研究计划、不管理整个研究 DAG、不做最终报告汇总。

## 设计目标

1. DeepResearch 复用外层 DAGRuntime 的调度、状态、进度、恢复和产物机制。
2. 复杂研究和复杂代码任务可以在同一个 DAG 中混排。
3. ReactLoop 只作为叶子节点 worker，不成为全局总控。
4. Codex 只作为代码/仓库节点 worker，不作为普通工具暴露给 Planner。
5. 不允许 `tool runtime` 成为 DAG 节点执行类型；工具只能是 worker 内部 capability，或系统级 effect。
6. 所有研究结论都能追溯到 evidence/source。

## 非目标

V1 不做：

1. 独立 DeepResearch 子 agent。
2. 独立 DeepResearchRuntime。
3. DeepResearch 内部再套一层 planner + executor。
4. worker 之间直接通信。
5. 复杂 DAG 并行优化。
6. 论文级引用格式。
7. 自动长期 research session。

## 概念分层

### 1. Task Strategy

Task Strategy 描述整轮任务应该如何展开。

建议枚举：

```python
TaskStrategy = Literal[
    "fast_reply",
    "simple_plan",
    "deep_research",
    "code_project",
    "mixed_complex",
]
```

DeepResearch 是其中一种 strategy：

```json
{
  "strategy": "deep_research",
  "expansion_policy": "multi_source_parallel_research"
}
```

### 2. Node Worker Runtime

Node Worker Runtime 执行单个叶子节点。

建议 V1 只保留：

```python
NodeRuntime = Literal["llm", "react", "codex"]
```

语义：

```text
llm    无工具推理、整理、合成、改写
react  有界 ReAct loop，用于检索、知识查询、证据提取
codex  本地仓库阅读、修改、测试、review、报告
```

不再把 `deepresearch` 放进 `NodeRuntime`。

### 3. System Effect

确定性副作用不是 worker runtime。

建议结构：

```python
EffectType = Literal[
    "deliver_file",
    "schedule_task",
    "notify",
    "persist_artifact",
]
```

例如提醒和发送文件：

```json
{
  "effects": [
    {
      "type": "schedule_task",
      "depends_on": ["node:final_report"],
      "payload": {
        "time": "2026-06-08T23:00:00+08:00",
        "message": "看 DeepResearch 报告"
      }
    }
  ]
}
```

## DeepResearch 展开方式

用户请求：

```text
深度研究一下 agent runtime / task graph 的设计思路，再结合 jarvis 项目给出建议。
```

Planner 可以先生成高阶计划：

```json
{
  "strategy": "deep_research",
  "user_objective": "深度研究 agent runtime / task graph 的设计思路，并结合 jarvis 项目给出建议",
  "nodes": [
    {
      "id": "expand_deep_research",
      "kind": "expandable",
      "strategy": "deep_research",
      "objective": "展开多来源研究 DAG"
    }
  ]
}
```

PlanExpander 将其展开成普通 TaskDAG：

```json
{
  "strategy": "deep_research",
  "nodes": [
    {
      "id": "scope",
      "runtime": "llm",
      "capability": "research_scoping",
      "objective": "拆解研究问题、维度和关键词",
      "input_refs": [],
      "expected_output": "研究范围、子问题、搜索计划"
    },
    {
      "id": "search_runtime_patterns",
      "runtime": "react",
      "capability": "evidence_search",
      "objective": "搜索 agent runtime / task graph 的近期设计资料并提取证据",
      "input_refs": ["node:scope"],
      "expected_output": "带来源的证据列表"
    },
    {
      "id": "search_code_agent_patterns",
      "runtime": "react",
      "capability": "evidence_search",
      "objective": "搜索代码 agent / Codex 类执行模型的设计资料并提取证据",
      "input_refs": ["node:scope"],
      "expected_output": "带来源的证据列表"
    },
    {
      "id": "evidence_synthesis",
      "runtime": "llm",
      "capability": "evidence_synthesis",
      "objective": "合并证据、识别共识、冲突和缺口",
      "input_refs": ["node:search_runtime_patterns", "node:search_code_agent_patterns"],
      "expected_output": "结构化研究结论"
    },
    {
      "id": "repo_assessment",
      "runtime": "codex",
      "capability": "repo_review",
      "objective": "结合研究结论 review jarvis 当前架构并给出重构建议",
      "input_refs": ["node:evidence_synthesis"],
      "expected_output": "面向 jarvis 的 markdown 架构建议"
    }
  ]
}
```

注意：展开后的每个节点仍由统一 DAGRuntime 调度。DeepResearch 不创建自己的 executor。

## ReactWorker 边界

ReactWorker 执行一个 bounded ReAct loop。

输入：

```text
- node objective
- resolved input refs
- allowed tool capabilities
- evidence output schema
- search budget
- temporal context
```

输出：

```text
- summary
- evidence[]
- sources[]
- tool_calls trace
- artifacts
```

ReactWorker 可以调用搜索、网页读取、知识库等工具，但工具选择受节点 capability、PermissionGuard 和 budget 约束。

ReactWorker 不做：

```text
- 不创建新 DAG
- 不调度其他节点
- 不直接最终回答用户
- 不执行代码仓库修改
- 不执行提醒、发文件等系统 effect
```

## CodexWorker 边界

CodexWorker 执行代码/仓库节点。

适合：

```text
- repo inspect
- code edit
- code review
- test run
- patch summary
- architecture report
```

复杂代码任务也可以是同一个 DAG 的多个 Codex 节点：

```json
[
  {
    "id": "implement",
    "runtime": "codex",
    "capability": "code_edit",
    "objective": "实现 runtime 改造"
  },
  {
    "id": "review",
    "runtime": "codex",
    "capability": "code_review",
    "objective": "review implement 节点的改动并指出风险",
    "input_refs": ["node:implement"]
  }
]
```

## Evidence 数据结构

DeepResearch 的核心不是“多搜几次”，而是 evidence 结构化。

建议结构：

```python
class ResearchEvidence:
    evidence_id: str
    node_id: str
    query: str | None
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

ReactWorker 输出的 evidence 由 Aggregator 或后续 LLM synthesis 节点消费。外层 DAGRuntime 只保存和传递，不理解 evidence 语义。

## Plan IR 建议

`PlanNode` 建议包含：

```python
class PlanNode(BaseModel):
    id: str
    runtime: Literal["llm", "react", "codex"]
    capability: str
    objective: str
    input_refs: list[str] = []
    expected_output: str
    budget: dict = {}
```

`ExecutionPlan` 建议包含：

```python
class ExecutionPlan(BaseModel):
    user_objective: str
    strategy: Literal["fast_reply", "simple_plan", "deep_research", "code_project", "mixed_complex"]
    nodes: list[PlanNode]
    effects: list[SystemEffect] = []
    finalization_hint: FinalizationHint
```

## 路由策略

FastIntent 只负责：

```text
- 是否能直接快回答
- 是否需要进入 Planner
```

FastIntent 不选择 DeepResearch 节点，也不选择工具。

Planner / PlanExpander 根据以下信号选择 `strategy=deep_research`：

```text
- 用户明确说 deepresearch、深度研究、深入研究
- 任务要求多来源证据链
- 任务要求比较多个方案并给建议
- 任务需要先研究外部资料，再结合本地仓库或已有 artifacts
- 单次搜索明显不足以满足目标
```

普通“查一下今天某个信息”仍然是 `simple_plan` + 单个 `react` 节点，不是 DeepResearch。

## 状态与持久化

统一写外层 DAG 状态，不单独维护 DeepResearch 状态。

最小状态：

```text
dag_run_id
strategy
node_id
runtime
capability
status
summary
output_json
artifact_refs
error
```

final raw_payload 可包含：

```json
{
  "source": "task_runtime",
  "strategy": "deep_research",
  "research": {
    "node_count": 5,
    "source_count": 12,
    "evidence_count": 20
  },
  "sources": [
    {
      "title": "...",
      "url": "..."
    }
  ]
}
```

## 错误处理

DeepResearch 的失败处理仍由统一 DAGRuntime 管理。

建议：

```text
- scope 节点失败：退化为普通 react research 或询问用户
- 单个 search 节点失败：允许继续，标记 coverage gap
- 多数 evidence 节点失败：Aggregator 明确说明覆盖不足
- synthesis 节点失败：使用 evidence summary fallback
- codex 节点 blocked：按统一 needs_user / approval 机制暂停
```

## 测试计划

### 单元测试

1. `strategy=deep_research` 的 expandable node 可展开为普通 DAG。
2. 展开后的 DAG 只包含 `llm/react/codex` runtime。
3. DeepResearch 计划不会生成 `runtime=deepresearch`。
4. DeepResearch 计划不会生成 `runtime=tool`。
5. ReactWorker 输出 evidence/source schema。
6. CodexWorker 可以消费 research synthesis 节点结果。

### 契约测试

1. “深度研究 X” 进入 `strategy=deep_research`。
2. “查一下今天 X” 不进入 `strategy=deep_research`。
3. 研究 + 仓库评估生成 `react -> llm -> codex` DAG。
4. 研究报告 + 定时提醒生成 DAG nodes + `effects.schedule_task`。
5. final raw_payload 包含 strategy、node results 和 source summary。

### 回归测试

1. 不影响 fast reply。
2. 不影响普通 react research。
3. 不影响 Codex 代码任务。
4. 不影响 artifact delivery。
5. 不引入独立 DeepResearchRuntime。

## 文件组织建议

不再新增 `app/deepresearch_agent/runtime.py`。

建议把 DeepResearch 相关逻辑放在 Task Runtime 内部：

```text
app/task_runtime/
  planner.py
  plan_expander.py
  deepresearch_strategy.py
  node_executor.py
  node_execute_runtime.py
```

职责：

```text
deepresearch_strategy.py:
  deep_research expandable graph templates
  research budget defaults
  evidence-oriented node expansion

plan_expander.py:
  expand high-level strategy nodes into normal PlanNode list
  validate expanded DAG

node_execute_runtime.py:
  LLMWorker / ReactWorker / CodexWorker
```

## 推荐实现顺序

1. 修改 Plan IR：增加 `strategy`、`capability`、`effects`。
2. 删除 Planner 对 `runtime=deepresearch` 和 `runtime=tool` 的输出要求。
3. 新增 `PlanExpander`，支持 `strategy=deep_research` 展开。
4. 将提醒、发文件迁移到 `effects`。
5. 收紧 ReactWorker：只执行单节点 bounded ReAct loop。
6. 收紧 CodexWorker：支持 `code_edit` / `code_review` / `repo_review` capability。
7. 增加 DeepResearch strategy eval cases。

## 一句话总结

DeepResearch 是外层 DAG 的一种规划策略，不是子 agent、不是 runtime、不是工具。ReactLoop 和 Codex 只负责执行叶子节点；统一 Agent Runtime 负责整张 DAG 的调度、状态、恢复、权限、产物和最终汇总。
