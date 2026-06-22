# Jarvis 复杂代码任务 Skill 与多节点 Rebuttal 设计

| 项目 | 内容 |
|------|------|
| 日期 | 2026-06-23 |
| 状态 | Draft |
| 相关模块 | `app/skills`, `app/task_runtime`, `prompt/scenarios/heavy_plan`, `app/task_runtime/node_executor.py` |
| 目标 | 对复杂代码任务引入稳定的多节点执行与 review/rebuttal 机制，避免依赖单个 coder node 一次性完成任务 |

---

## 1. 背景

Jarvis 当前已经将 skill 收敛为 Claude/Codex 风格的 procedural guidance：

```text
skill listing -> model calls Skill({ skill }) -> runtime injects loaded skill guidance
```

这条链路适合把“复杂代码任务应该如何规划”作为一个 skill 提供给 planner。复杂代码任务的核心问题不是某个 coder provider 能否改代码，而是：

1. 单个 coder node 容易把实现、验证、review、合并混在一起。
2. 单节点自我验证缺少反驳机制，容易漏掉设计问题和回归风险。
3. 大任务需要拆成多个明确 scope，减少上下文污染和过度修改。
4. review 结果应该进入后续修复节点，而不是只作为最终摘要。

因此需要一个多节点执行模式，让 planner 在识别复杂代码任务后生成带 review/rebuttal 的 DAG。

---

## 2. 设计目标

### 2.1 目标

1. 复杂代码任务不再默认交给单个 coder node 一次性完成。
2. planner 可通过 `complex-code-task` skill 获得规划策略。
3. 复杂代码任务 DAG 至少包含实现、review、修复/整合、验证节点。
4. review 节点只做代码审查，不直接修改代码。
5. 修复节点必须基于 review findings 行动。
6. 合并节点只负责整合已完成代码、处理冲突、跑最终验证。
7. 第一版使用固定执行模板和有限 rebuttal 轮次，不做无限动态 DAG。

### 2.2 非目标

1. 第一版不实现任意动态 DAG 续写。
2. 第一版不允许 review 节点直接改代码。
3. 第一版不让 skill 直接创建节点或执行工具。
4. 第一版不引入复杂投票、多 agent 自由辩论或无限循环。

---

## 3. 核心判断

“动态 DAG”不是第一阶段的必要能力。真正需要的是有限、可审计的反驳闭环：

```text
implement -> review -> fix -> verify
```

如果 verify 失败，可以允许最多 1 到 2 轮：

```text
fix -> review -> verify
```

超过上限后停止，并把剩余风险报告给用户。这样能获得多节点 rebuttal 的收益，又不会让 planner 产物变成不可控的自生长流程。

---

## 4. Skill 角色

新增 skill 建议命名：

```text
skills/complex-code-task-1.0.0/SKILL.md
```

该 skill 只提供 planner guidance，不执行代码、不授权工具、不直接生成节点。它应说明：

1. 何时把任务判定为复杂代码任务。
2. 应选择哪个 execution pattern。
3. 各节点职责边界。
4. review findings 的输出格式。
5. fix node 如何消费 review findings。
6. verify node 如何判定完成或阻塞。

planner 看到复杂代码请求时：

```text
skill listing -> Skill("complex-code-task-1.0.0") -> 按 guidance 生成多节点 DAG
```

---

## 5. 推荐执行模板

### 5.1 标准模板

```text
N1 plan_scope
  runtime: llm
  objective: clarify implementation scopes, risks, and expected files

N2 implement_primary
  runtime: coder
  objective: implement scoped business/code changes

N3 review_code
  runtime: react 或 llm
  input_refs: [node:implement_primary]
  objective: review code only; produce actionable findings

N4 apply_review_fixes
  runtime: coder
  input_refs: [node:implement_primary, node:review_code]
  objective: apply fixes for accepted review findings only

N5 final_verify
  runtime: react 或 coder
  input_refs: [node:apply_review_fixes]
  objective: run tests/checks or explain why they cannot run

N6 integrate
  runtime: coder
  input_refs: [node:final_verify]
  objective: merge/integrate completed node work when policy allows
```

### 5.2 并行业务分支模板

当用户明确要求多个相对独立业务模块，例如“写业务 A 和业务 B”：

```text
N1 plan_scope
N2 implement_business_a
N3 implement_business_b
N4 review_code
N5 apply_review_fixes
N6 final_verify
N7 integrate
```

`implement_business_a` 和 `implement_business_b` 可以并行，但 review 必须依赖两个实现节点。

---

## 6. 节点职责约束

### 6.1 Implement Node

实现节点负责具体代码改动：

1. 只处理 planner 分配的 scope。
2. 不负责最终合并。
3. 必须记录改动摘要、涉及文件、测试结果或未测试原因。
4. 不应自称 review 已完成。

### 6.2 Review Node

review 节点只审查，不改代码：

1. 检查行为回归、边界条件、接口契约、测试缺口。
2. findings 必须可行动，避免泛泛建议。
3. 每条 finding 应包含位置、风险、建议修复方式。
4. 如果没有问题，明确输出 no blocking findings。

建议输出：

```json
{
  "summary": "review summary",
  "findings": [
    {
      "severity": "high|medium|low",
      "file": "path",
      "line": 123,
      "issue": "problem",
      "recommendation": "fix"
    }
  ],
  "blocking": true
}
```

### 6.3 Fix Node

fix node 消费 review findings：

1. 只修复 review 指出的明确问题。
2. 可以拒绝不成立的 finding，但必须说明理由。
3. 修复后输出 findings 处理清单。

### 6.4 Verify Node

verify node 负责最终检查：

1. 优先运行项目已有测试、lint、类型检查。
2. 如果不能运行，说明原因和剩余风险。
3. verify 失败时允许进入有限 rebuttal 轮次。

### 6.5 Integrate Node

integrate node 负责整合：

1. 合并已完成节点产物。
2. 处理冲突。
3. 不引入新需求。
4. 受现有 Git/runtime policy 约束。

---

## 7. 有限 Rebuttal 机制

第一版不做无限动态 DAG，而是在 executor 或 planner contract 中定义：

```text
max_rebuttal_rounds = 1
```

流程：

```text
implement -> review -> fix -> verify
```

如果 verify 失败且还有 rebuttal 预算：

```text
fix_again -> verify_again
```

如果预算耗尽：

```text
return blocked/failed with remaining risks
```

这样可以保证系统不会因为 review 或 verify 反复追加任务而失控。

---

## 8. Runtime 约束

不要完全相信 planner 自由生成的 DAG。runtime 应对 `execution_pattern = "multi_node_code_review"` 做结构校验：

1. 至少一个 coder implementation node。
2. 至少一个 review node。
3. review node 必须依赖 implementation node。
4. fix/integration node 必须依赖 review node。
5. review node 不允许使用 coder runtime 执行代码修改。
6. integrate node 必须依赖 verify 或 fix node。

如果 planner 输出不满足这些约束，runtime 应修正为标准模板或拒绝执行。

---

## 9. Planner 输出建议

在 plan schema 中增加可选字段：

```json
{
  "execution_pattern": "multi_node_code_review",
  "rebuttal_policy": {
    "max_rounds": 1,
    "stop_on_no_blocking_findings": true
  }
}
```

节点也可增加角色字段：

```json
{
  "id": "review_code",
  "runtime": "react",
  "role": "review",
  "objective": "Review implementation only and return actionable findings.",
  "input_refs": ["node:implement_primary"]
}
```

如果暂时不改 schema，也可以把 role 写入 `objective`，由 runtime 通过约定识别。

---

## 10. 分阶段计划

### P0：Skill + Planner Guidance

1. 新增 `complex-code-task-1.0.0` skill。
2. 在 planner prompt 中强调复杂代码任务应调用该 skill。
3. 让 planner 生成固定多节点模板。

### P1：Runtime Pattern 校验

1. 增加 `execution_pattern` 字段。
2. 对多节点代码 review pattern 做结构校验。
3. 禁止 review 节点使用 coder runtime。

### P2：有限 Rebuttal

1. 支持 verify 失败后追加一次 fix/verify。
2. 记录 rebuttal round。
3. 超限后停止并报告风险。

### P3：动态 DAG

仅在固定模板稳定后考虑：

1. review 产生新的明确子任务时追加节点。
2. verify 发现明确缺口时追加节点。
3. 每次追加必须消耗 rebuttal budget。
4. 禁止无上限自我扩展。

---

## 11. 推荐结论

复杂代码任务需要多节点 rebuttal，这个目标是合理的；但第一版不应直接做动态 DAG。

推荐落地顺序：

```text
fixed multi-node template
-> runtime pattern validation
-> one-round review/fix/verify rebuttal
-> optional dynamic DAG later
```

这样既能避免单 coder node 自证正确，又能保持执行可控、可测试、可恢复。
