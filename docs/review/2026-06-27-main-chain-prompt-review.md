# Jarvis 主链路 Prompt Review

日期：2026-06-27

范围：本 review 覆盖 `TaskAgentRuntime -> PlanningRouter -> NodeExecutor -> ResultAggregator` 主链路中仍由 `prompt/config.json` 管理并实际参与运行的 prompt。`kb_eval_*` 属于知识库评估链路，按保留策略不纳入主链路问题清单。

## 主链路概览

当前主链路 prompt 大致分为五类：

- 路由与规划：`fast_intent:v2`、`heavy_plan:v7`
- 节点执行：`llm_node_execute:v3`、`react_node_execute:v4`、`coder_node_execute:v4`
- coder 辅助：`coder_temporal_context:v2`、`coder_worker:v2`、`coder_node_finalize:v2`
- 汇总收口：`result_aggregator:v6`
- 工具与 skill 辅助：`skill_listing:v2`、`loaded_skill_guidance:v2`、`tool_definitions:v2`

## Review 明细

| Prompt | 使用场景 | 调用位置 | 主要问题 |
|---|---|---|---|
| `fast_intent:v2` | 第一层快速路由。判断当前 turn 能否直接回复；不能则调用 `needs_plan` 虚拟工具进入 Planner。 | `app/task_runtime/fast_intent.py` | 规则总体清楚，但“普通聊天、简单观点、简单解释”允许直接回答，边界偏宽，可能把需要来源、上下文或当前信息的问题误判成 fast reply。它依赖模型主动调用 `needs_plan`，没有要求稳定结构化 reason，排查误路由时信息可能不稳定。 |
| `heavy_plan:v7` | 生成执行 DAG，决定 `react` / `coder`、node 粒度、artifact refs、finalization hint。 | `app/task_runtime/planner.py` | prompt 只允许 `react | coder`，但系统仍存在 `llm` runtime，fallback 和 fast reply 会用 `llm`。这会让 Planner 不能主动规划纯 LLM node，简单但被 fast intent 打到 needs_plan 的任务会被迫走 `react`。输出示例中 `finalization_hint` 只写 `user_facing`，代码模型里还有 `mode` 派生逻辑，prompt 与 runtime 心智不完全一致。 |
| `llm_node_execute:v3` | 执行 `llm` node，理论上是无工具节点，返回 node result JSON。 | `app/task_runtime/node_execute_runtime.py` | manifest 是 `response_format: text`，但 prompt 要求“返回 JSON”，runtime 又会在模型支持时强制 `json_object`。三处语义不一致。另一个冲突是 prompt 说“不使用工具”，但 runtime 实际会给它暴露 `Skill` 工具。 |
| `react_node_execute:v4` | 执行非仓库、可用工具的节点，例如搜索、提醒、artifact 交付、知识查询。 | `app/task_runtime/node_execute_runtime.py` | 职责边界基本正确，但它说“不要 shell / 仓库工作”，实际工具暴露策略如果不够严，模型仍可能看到本地文件或写文件类工具。prompt 要求工具后返回 JSON，但 manifest 是 `text`，只有最后一步 runtime 才可能要求 JSON，普通步骤没有强约束。 |
| `coder_node_execute:v4` | 把 planner 的 coder node 转成给 coder provider 的具体执行指令。 | `app/task_runtime/node_execute_runtime.py` | 和 `coder_worker` 有大量重复约束，分支、worktree、approval、manifest 规则分散在两层 prompt，后续维护容易不一致。它对 manifest 写入讲得清楚，但没有明确“如果没有 artifact 不要写 manifest”的成功边界，provider 可能过度产出报告。 |
| `coder_temporal_context:v2` | 插入 coder node 的当前日期/时间上下文。 | `app/task_runtime/node_execute_runtime.py` | 内容简单，问题不大。风险在于只服务 coder；React 和 Planner 各自使用 JSON temporal payload，时间约束表达分散，之后可能出现不同 runtime 对“最新/今天”的解释不一致。 |
| `coder_worker:v2` | 委派给本地 coder worker 的仓库执行契约，通常在工具层 `delegate_to_codex` / `delegate_to_claude_code` 使用。 | `app/tools/coder_common.py` | 和 `coder_node_execute` 重叠明显，且 commit/push 规则由工具参数驱动，和 runtime 管理 node commit/merge 的新机制有潜在心智冲突。它还提到 Codex approval 流程，若 provider 不是 Codex，措辞会偏具体。 |
| `coder_node_finalize:v2` | 可选 LLM finalizer，只有 `coder_node_finalizer_llm_enabled=true` 时使用，用 coder stdout/stderr/manifest 生成更规整的 NodeResult。 | `app/task_runtime/node_finalizer.py` | 可选路径，默认不启用。manifest 是 `response_format: text`，但 prompt 要求 JSON；如果启用，建议和其他 JSON prompt 统一。它还要求判断节点是否完成，但没有明确如何处理 provider 成功但验证失败、artifact 缺失这类灰区。 |
| `result_aggregator:v6` | 汇总 plan 和 node results，生成最终用户回复、status、artifact refs、approval requests。 | `app/task_runtime/result_aggregator.py` | 规则比较成熟。主要问题是只汇总，不 replan；prompt 也明确说 runtime 未实现 replan/resume。因此“部分完成但还差一步”的场景只能 `failed` / `needs_user_input`，不能自动补救。`artifact_refs` 规则依赖 node results 准确上报，Aggregator 本身不会校验 artifact 是否真实存在。 |
| `skill_listing:v2` | 在 LLM/React node 消息后追加可用 Skill 菜单，让模型选择 `Skill` 工具加载流程指引。 | `app/agent_react/context_manager.py` | 内容清楚，但它作为 user message 注入，可能和真实用户意图混在一起。它强调“不会替代 planner”，但注入发生在 node runtime 阶段，Planner 已经结束，这句对当前阶段帮助有限。 |
| `loaded_skill_guidance:v2` | `Skill` 工具加载成功后，把 skill 正文注入下一轮模型步骤。 | `app/task_runtime/node_execute_runtime.py` | 太轻，只说“相关时遵循”。如果 skill 内容和当前 node mode、权限、工具边界冲突，prompt 没有明确优先级。建议明确：runtime/tool policy、node objective、user request 优先于 skill guidance。 |
| `tool_definitions:v2` | 给所有 LLM 工具填充中文 description 和参数说明，实际内容来自 `catalog.json`。 | `app/tools/definitions.py` | `catalog.md` 只是占位，真正内容在 `catalog.json`，维护者不直观。工具描述整体偏长，尤其 `read_file`、`search_files`、`tavily_search`、`tool_search` 规则较密，模型可能忽略后半段。另一个风险是工具描述里写了策略性约束，但真正工具暴露由 runtime 控制；如果二者不一致，模型会收到混合信号。 |

## 非主链路保留项

`kb_eval_query_generation:v2` 和 `kb_eval_gold_span_refine:v2` 仍在 `prompt/config.json` 中，但不属于 agent 主链路。

- `kb_eval_query_generation:v2` 用于知识库评估 query 生成，调用点在 `app/knowledge_base/eval.py`。
- `kb_eval_gold_span_refine:v2` 用于评估 gold span refine 脚本，调用点在 `scripts/refine_eval_gold_spans_with_llm.py`。

这两个 prompt 当前按知识库评估功能保留。

## 优先处理建议

1. 明确 `heavy_plan` 是否应该恢复/保留 `llm` runtime 的规划能力。
2. 统一 `llm_node_execute`、`react_node_execute`、`coder_node_finalize` 的 `response_format` 与“返回 JSON”的 prompt 要求。
3. 收敛 `coder_node_execute` 与 `coder_worker` 中重复的分支、worktree、approval、manifest 规则。
4. 明确 skill guidance 的优先级，避免 skill 内容覆盖 node objective、runtime/tool policy 或用户约束。
5. 改善 `tool_definitions` 的维护形态，让 `catalog.json` 是明确的一等资源，减少 `catalog.md` 占位带来的误导。
