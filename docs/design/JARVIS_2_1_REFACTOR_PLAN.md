# Jarvis 2.1 重构方案

版本：v2.1 proposal
状态：待评审
日期：2026-04-22
来源：项目级代码评审与质量门禁验证

## 1. 背景

Jarvis 当前已经具备 CA Agent、Worker、Skill、SQLite 持久化、CLI/API 入口、审批、资源锁、worker 恢复和外部 skill 注册等核心能力。整体设计方向正确，已经明显超过简单 agent demo 的成熟度。

但当前实现仍应视为“可运行的 v2 MVP baseline”，而不是稳定工业化版本。继续堆叠 Feishu、Obsidian、更多 Worker 或更复杂的长期记忆之前，需要先完成一轮 v2.1 重构，收敛安全边界、状态机复杂度、类型约束和文档/编码治理。

本方案的目标不是重写 Jarvis，而是在保持现有架构方向的前提下，把系统提升到更适合长期迭代和 AI 辅助开发的工程基线。

## 2. 当前结论

### 2.1 设计优点

- 分层方向清晰：CA Agent 负责决策，Worker 负责执行，Skill 负责能力封装，业务 DB 负责事实状态。
- Long-running agent 的关键基础设施已经起步：LangGraph checkpoint、`interrupt()` / `Command(resume=...)`、审批、worker callback、恢复扫描和审计日志均已接入。
- 能力注册开始从“工具名字符串”升级为 capability/tool/skill 三层映射，为后续插件生态打下基础。
- 测试覆盖了不少关键链路：审批、worker 回调、恢复、外部 skill、防覆盖、final answer synthesis、WorkPlan 顺序执行等。
- API 与 CLI 双入口已经成型，适合本地个人 assistant 的使用方式。

### 2.2 当前不足

- 执行层安全边界不够硬，`ShellSkill` 和 `CoderSkill` 过度依赖上层规划与审批。
- `app/agent/nodes.py` 职责过重，已经成为状态机、规划、调度、评估、fallback、启发式规则的混合模块。
- 中文启发式规则、测试用例和设计文档本身是 UTF-8 正常内容，但命令行、脚本和 CI 若未显式按 UTF-8 读取，容易出现误判和误读。
- 类型系统没有形成门禁，`mypy` 当前失败，`TypedDict` 与动态 dict 混用较多。
- SQLite schema 可用但不够可演进，缺少迁移体系、外键和关键索引。
- LLM assessment 失败时部分路径默认成功，可能掩盖真实未完成任务。
- 外部 Skill 加载模型接近“执行任意本地 Python 代码”，需要明确 trusted boundary。

## 3. 质量门禁现状

本次评审执行了以下命令：

```powershell
uv run pytest
uv run ruff check app tests
uv run mypy app
```

结果：

- `uv run pytest` 通过：70 个测试全部通过。
- `uv run ruff check app tests` 失败：2 个未使用 import。
- `uv run mypy app` 失败：24 个类型错误，集中在 subprocess 输出类型、TypedDict 动态字段、registry 类型推断和 LangGraph interrupt 返回结构。

v2.1 的基本验收要求应是：测试、ruff、mypy 三者全部通过，并进入 CI。

## 4. 重构目标

### 4.1 安全目标

- 执行层必须具备独立安全边界，不能只依赖 planner、risk gate 或审批。
- 高风险能力必须在 WorkOrder 层固化风险信息，审批后执行的必须是原始已审批 WorkOrder，不允许隐式重写。
- 外部 skill 默认视为不可信或半可信，必须通过显式 trusted path 或 allowlist 才能暴露给 LLM。

### 4.2 可维护性目标

- 拆分 Agent 节点逻辑，避免 `nodes.py` 继续膨胀。
- 将启发式规则、planner 适配、completion assessment、final answer synthesis 独立成模块。
- 用 Pydantic 或更严格的数据模型替代关键路径上的动态 dict。

### 4.3 可演进目标

- 引入数据库迁移机制，支持 schema 安全演进。
- 明确 Worker 生命周期、超时、取消、恢复、重复回调和部分完成策略。
- 为 Feishu、Obsidian、长期记忆、更多插件预留清晰边界，而不是继续塞进现有节点文件。

### 4.4 文档目标

- 统一命令行、脚本和 CI 的 UTF-8 读取/显示方式，避免误把正常中文内容看成乱码。
- v2.1 之后所有新文档必须使用 UTF-8。
- 架构文档要区分“已实现”“部分实现”“设计目标”，避免误导后续开发。

## 5. 关键风险与改造方案

### 5.1 执行层安全边界

问题：

- `ShellSkill` 使用 shell 字符串执行，天然扩大命令解释面。
- `CoderSkill` 使用 `bypassPermissions` 调用 Claude Code，并允许部分 Bash 能力。
- 当前安全性主要依赖 planner eligibility、risk pattern、approval，而执行层本身不是最后防线。

改造建议：

- 将 `ShellSkill` 拆成 `ShellCommandSkill` 和 `TestCommandSkill`。
- `TestCommandSkill` 只允许配置白名单，例如 `uv run pytest`、`pytest`、指定目录下的测试命令。
- `ShellCommandSkill` 继续支持显式用户命令，但必须记录完整风险判断、工作目录和审批状态。
- 默认关闭 `CoderSkill` 的 `bypassPermissions`，通过配置项显式开启。
- `CoderSkill` 的 allowed tools 从硬编码改为配置，并按任务风险级别选择不同权限集合。
- Worker 执行前再次校验 WorkOrder：风险级别、审批状态、workdir、timeout、capability 是否匹配。

验收标准：

- 高风险 shell/coder 命令无审批无法执行。
- verification command 只能走白名单或同一套审批机制。
- worker 直接收到未审批高风险 WorkOrder 时返回失败，而不是执行。

### 5.2 外部 Skill 插件边界

问题：

- Skill loader 通过 `exec_module` 加载外部 Python 文件。
- 当前适合本地可信插件，但不适合作为默认第三方插件生态。

改造建议：

- 在文档中明确 external skill 是 trusted local code。
- 默认仅加载 `data/skills` 下显式安装的 skill，不自动加载任意用户目录，或提供配置开关。
- Manifest 增加 `trust_level`、`permissions`、`exposed_to_llm` 审核字段。
- 外部 skill 默认不暴露给 LLM，除非 manifest 与配置同时允许。
- 长期方案中，将外部 skill 放入独立进程执行，通过 JSON RPC 或 CLI 协议通信。

验收标准：

- 外部 skill 无法覆盖内置 skill/tool。
- 未授权外部 skill 不进入 LLM candidate tools。
- 加载失败、重复注册、权限声明缺失都进入审计日志。

### 5.3 Agent 状态机拆分

问题：

- `app/agent/nodes.py` 同时承担过多职责。
- 启发式规则、planner 调用、completion assessment、final answer fallback 和 LangGraph 节点逻辑混杂。

建议拆分为：

- `app/agent/nodes.py`：只保留 LangGraph node 函数和 route 函数。
- `app/agent/intent.py`：意图识别、候选 capability 选择、中文/英文启发式。
- `app/agent/planning.py`：WorkPlan、WorkOrder 生成、replan context。
- `app/agent/dispatching.py`：dispatch queue、approval gate、active worker 状态。
- `app/agent/assessment.py`：规则评估、LLM completion assessment、retry/replan 决策。
- `app/agent/final_answer.py`：最终回答合成和 fallback。
- `app/agent/risk.py`：命令风险分类和风险合并。

验收标准：

- 单文件不超过 500 行，`nodes.py` 不超过 300 行。
- 每个模块有对应单元测试。
- `strategize`、`aggregate`、`summarize` 的核心逻辑可以脱离 LangGraph 单独测试。

### 5.4 数据模型收敛

问题：

- `TypedDict` 与动态 dict 混用较多。
- 部分字段通过 `type: ignore` 动态塞入，例如 `plan_step_id`。
- LangGraph interrupt 返回结构与 `AgentState` 类型不匹配。

改造建议：

- 将 `Task`、`PendingAction`、`IntentDecision`、`PlanStep`、`WorkPlan`、`AgentStateSnapshot` 迁移到 Pydantic model 或 dataclass。
- 定义明确的 `AgentStatePatch` 返回类型，避免所有节点都返回 `dict[str, Any]`。
- 对 LangGraph interrupt 结果建立专门解析函数，不直接把 `__interrupt__` 当作普通 state 字段访问。
- 将 `WorkOrder`、`WorkResult` 作为跨层唯一契约，避免重复定义同类结构。

验收标准：

- `uv run mypy app` 通过。
- 核心 TypedDict 不再需要 `type: ignore`。
- 所有 node 输入输出有明确模型或类型别名。

### 5.5 数据库与恢复机制

问题：

- 当前业务 DB schema 可用，但缺少外键、索引和迁移。
- Resource lock 只有独占锁，没有读/写模式、TTL、worker owner。
- 恢复策略目前是最小闭环，尚未覆盖重复回调、部分完成、超时 worker 等复杂场景。

改造建议：

- 引入 Alembic 或轻量 migration runner。
- 给 `tasks.run_id`、`work_orders.ca_thread_id`、`work_orders.task_id`、`work_results.order_id`、`audit_logs.thread_id` 加索引。
- 增加外键约束，减少孤儿数据。
- Resource lock schema 增加 `mode`、`owner_event_id`、`worker_id`、`expires_at`。
- Worker result 持久化增加幂等字段，重复回调必须可安全忽略。
- 增加 worker timeout 与 stale lock 清理策略。

验收标准：

- 新环境可通过 migration 创建完整 schema。
- 重复 worker callback 不会重复推进任务。
- 进程重启后能够区分 waiting approval、monitoring、failed、stale worker。

### 5.6 LLM 降级策略

问题：

- Completion assessment 异常时默认 success，会掩盖复杂任务未完成。
- Final answer synthesis 失败时 fallback 质量依赖规则提取，搜索场景可接受，但代码任务需要更严格。

改造建议：

- 对低风险 echo/search 可继续 fallback。
- 对 coder/high-risk 任务，LLM assessment 不可用时返回 `needs_review` 或 `blocked`。
- 增加 `completed_unverified` 状态，区分“worker 成功退出”和“目标已验证达成”。
- Final answer synthesis 输入中必须包含 completed/pending/failed steps，禁止把 worker 建议当成已完成事实。

验收标准：

- Coder worker 成功退出但缺少验证时，不自动标记为完全完成。
- LLM assessment 失败不会导致高风险任务默认成功。
- 用户最终回答准确区分“已完成”“未验证”“建议后续处理”。

### 5.7 UTF-8 工具链与文档治理

问题：

- 项目中的中文文档、测试和 intent marker 使用 UTF-8 打开时是正常的。
- 在 Windows PowerShell 等环境中，如果命令未显式指定 UTF-8，读取结果会显示为 mojibake，容易造成误判。
- 后续脚本、CI、报告导出和文档处理需要统一使用 UTF-8，避免工具链层面的显示问题。

改造建议：

- 明确全仓文本文件以 UTF-8 保存和读取。
- 文档读取命令、导出脚本和 CI 检查显式使用 UTF-8。
- 对 PowerShell 示例补充 `-Encoding UTF8`，避免本地查看误读。
- 保留中文与英文双语 marker，继续覆盖中文输入识别测试。

验收标准：

- 新增或补充文档说明：Windows/PowerShell 下读取 Markdown 和源码应显式使用 UTF-8。
- CI 中涉及文本读取的脚本统一指定 UTF-8。
- 核心文档和中文测试在 UTF-8 环境下可正常阅读。

## 6. 分阶段实施计划

### Phase 0：立即修复质量门禁

目标：让当前 baseline 恢复基本工程健康。

任务：

- 修复 ruff 的 2 个 unused import。
- 修复 mypy 的 24 个类型错误。
- 将 `ruff check app tests`、`mypy app`、`pytest` 加入 CI。
- 补充 `pyproject.toml` 中 ruff、mypy、pytest 配置。

验收：

```powershell
uv run ruff check app tests
uv run mypy app
uv run pytest
```

三者全部通过。

### Phase 1：UTF-8 工具链与文档读取规范

目标：统一文档、脚本和 CI 的 UTF-8 读取方式，避免工具链显示误判。

任务：

- 在 README 或开发文档中说明 PowerShell 下使用 `Get-Content -Encoding UTF8`。
- 检查文档生成、报告导出、测试 fixture 是否显式使用 UTF-8。
- 保留并补充中文输入识别测试，确保真实中文 intent marker 可用。
- 如新增编码检查脚本，应检查文件是否可按 UTF-8 解码，而不是检查正常中文字符。

验收：

- 中文用户指令能够被稳定识别为 code write、search、test、review 等意图。
- 核心文档在 UTF-8 工具链下可读，命令行示例不会误导开发者。

### Phase 2：拆分 Agent 核心模块

目标：降低 `nodes.py` 复杂度，建立可维护边界。

任务：

- 抽取 intent、planning、assessment、final_answer、risk 模块。
- 为每个模块建立单元测试。
- 保持 LangGraph 图结构不大改，降低迁移风险。

验收：

- `nodes.py` 只保留节点函数和路由函数。
- 单元测试覆盖规划、候选工具选择、风险分类、completion assessment、final answer fallback。

### Phase 3：执行安全加固

目标：让 Worker/Skill 层成为独立安全防线。

任务：

- Shell test 命令白名单化。
- Coder permission mode 配置化。
- Worker 执行前校验审批状态。
- verification command 走统一风险与审批机制。

验收：

- 未审批高风险 WorkOrder 无法执行。
- Coder worker 权限可通过配置降级。
- 安全相关路径有负面测试。

### Phase 4：状态模型与类型系统收敛

目标：让状态变更可推理、可类型检查。

任务：

- 将关键 TypedDict 迁移到 Pydantic/dataclass。
- 定义节点 patch 类型。
- 消除 `type: ignore`。
- 明确 LangGraph interrupt 解析边界。

验收：

- mypy 严格通过。
- 状态字段新增/删除不会悄悄破坏运行链路。

### Phase 5：持久化与恢复增强

目标：为长跑 agent 提供可靠数据底座。

任务：

- 引入 migration。
- 增加 FK 和索引。
- 扩展 resource lock。
- 增强 worker recovery、重复回调和 stale worker 处理。

验收：

- 重启、重复回调、worker 失败、等待审批、资源锁冲突均有明确恢复行为。
- DB schema 可版本化迁移。

### Phase 6：插件治理

目标：让 skill 插件机制可长期扩展。

任务：

- Manifest 增加权限声明。
- 默认不暴露未授权外部 skill。
- 增加 trusted path 和 allowlist。
- 设计外部 skill 进程隔离协议。

验收：

- 外部 skill 注册、拒绝、权限不足都有审计。
- 插件无法绕过 capability/risk/approval 边界。

## 7. 建议文件结构

目标结构：

```text
app/
  agent/
    graph.py
    nodes.py
    state.py
    intent.py
    planning.py
    dispatching.py
    assessment.py
    final_answer.py
    risk.py
    runner.py
  workers/
    base.py
    executor.py
    inline.py
    threaded.py
    events.py
  skills/
    base.py
    shell.py
    coder.py
    loader.py
    manifest.py
  persistence/
    db.py
    migrations/
    repositories.py
  tools/
    specs.py
    capabilities.py
    registry.py
```

测试结构：

```text
tests/
  unit/
    agent/
    skills/
    tools/
    persistence/
  integration/
    test_thread_manager.py
    test_worker_recovery.py
    test_external_skills.py
  e2e/
    test_cli_agent.py
    test_api_agent.py
```

## 8. 优先级排序

P0：

- 修复 ruff 和 mypy。
- 统一 UTF-8 工具链和文档读取方式。
- 拆分 `nodes.py`。
- 加固 shell/coder 执行边界。
- 修复 LLM assessment 失败默认成功的问题。

P1：

- 数据模型迁移到 Pydantic/dataclass。
- 数据库 migration、FK、索引。
- Resource lock 扩展为读/写锁和 TTL。
- 外部 skill trust/permission 治理。

P2：

- Worker 进程隔离。
- OpenTelemetry tracing。
- 更完整的插件签名与版本兼容策略。
- 更复杂的多 worker 并行调度。

## 9. v2.1 完成定义

Jarvis v2.1 完成时应满足：

- `pytest`、`ruff`、`mypy` 全部通过，并进入 CI。
- 核心文档和中文测试在 UTF-8 读取方式下正常显示。
- `nodes.py` 被拆分，核心策略模块可独立测试。
- 高风险 shell/coder/verification command 无法绕过审批。
- LLM assessment 不可用时不会把高风险或 coder 任务默认标记为成功。
- 业务 DB 有迁移机制、关键索引和更清晰的恢复策略。
- 外部 skill 有明确 trust boundary 和权限声明。

## 10. 推荐下一步

建议从最小闭环开始：

1. 修复 ruff 和 mypy，让质量门禁变绿。
2. 补充 UTF-8 读取说明和必要的编码检查，避免 PowerShell 默认编码导致误读。
3. 拆出 `risk.py`、`intent.py`、`assessment.py` 三个模块，这是 `nodes.py` 降复杂度的最低成本切入点。
4. 给 `ShellSkill` 和 `CoderSkill` 增加执行前防线，先覆盖未审批高风险 WorkOrder 的负面测试。

完成这四步后，再推进数据库 migration 和外部 skill 权限治理。
