# Jarvis Trace 驱动真实对话评测与优化闭环

日期：2026-06-25

## 1. 目标

把 Jarvis 的真实对话运行记录转化为可观测、可抽取、可回归评测、可持续优化的数据闭环。

目标链路：

```text
真实对话
-> trace / log / raw_payload 记录
-> 评测候选样本抽取
-> 人工轻标注或规则确认
-> 自动评测
-> 失败归因
-> prompt / runtime / tool 策略优化
-> 回归验证
```

核心不是单纯“能看到 trace”，而是让真实运行过程成为可复现、可分析、可改进的数据资产。

## 2. 基本原则

1. 先可观测，再自动化。
   先保证每个 turn 能解释清楚：为什么这样路由、生成了什么 plan、调用了哪些 node/tool、哪里耗时、哪里失败。

2. trace 面向机器抽取，log 面向人工排障。
   关键业务字段应进入标准化 span attribute / event，而不是只存在于日志文本。

3. 先做候选池，不直接把真实对话变成正式 eval。
   真实对话噪声高，需要经过筛选、轻标注或人工确认。

4. 评测要分层。
   结构评测、行为评测、答案质量评测分开做，避免一个 pass/fail 掩盖失败原因。

5. 自动优化必须有回归门禁。
   先自动生成失败报告和修改建议，再考虑自动 patch。任何自动修改都必须经过 eval 回归。

## 3. 阶段路径

### Phase 1：Trace Schema 收敛

统一关键 span 和字段：

- `feishu.agent_run`
- `turn.run`
- `node.execute`
- `llm.call`
- `tool.call`
- `coder.run`

每个 turn 至少能关联：

- `conversation_id`
- `turn_id`
- `trace_id`
- route / status
- planner 输出
- node 结果
- tool 调用
- token / latency / error

### Phase 2：Trace-to-Eval 抽取

从真实运行记录中抽取评测候选样本。

候选样本应包含：

- 用户输入
- 必要对话上下文摘要
- trace_id / turn_id / conversation_id
- route 和 plan
- tool_calls / node_results
- final_answer
- status / error / usage
- 初步 failure signals

候选样本先进入 `candidate_eval_cases`，不直接进入正式评测集。

### Phase 3：轻标注与正式 Eval Case

人工或规则确认候选样本，生成正式 eval case。

优先标注：

- `should_route`
- `expected_tools`
- `forbidden_tools`
- `required_status`
- `success_criteria`
- `risk_tags`

第一阶段不强求所有样本都有标准答案。Agent 行为评测优先关注路径、工具选择、状态和约束是否正确。

### Phase 4：自动评测

评测分三层：

1. 结构评测：基于 trace 直接判断 route、status、tool、token、latency。
2. 行为评测：判断规划、工具选择、权限处理、fallback 是否合理。
3. 答案质量评测：用 judge LLM 判断最终回答是否满足用户目标。

评测输出必须保留失败归因，而不是只有通过率。

### Phase 5：优化闭环

基于评测失败聚合优化方向：

- fast intent 误判
- planner 过度规划或漏规划
- tool 选择错误
- node runtime 失败
- aggregation 丢信息
- coder provider / finalizer 不稳定
- prompt 约束不足

优化流程：

```text
eval 失败
-> 失败类型聚合
-> 生成优化建议
-> 人工确认或自动 patch
-> 跑回归 eval
-> 通过后合入
```

## 4. 近期优先级

近期只做三件事：

1. 固化 trace 字段，让真实 turn 可稳定抽取。
2. 建立真实对话候选样本池。
3. 打通候选样本到 eval runner 的最小闭环。

自动优化放在后面，等评测集和失败归因稳定后再做。
