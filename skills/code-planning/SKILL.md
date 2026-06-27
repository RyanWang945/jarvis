---
name: 代码规划
description: 用于本地仓库代码任务的 planner skill。
when_to_use: 用户要求检查、评审、修改、测试、提交、推送，或以其他方式处理本地/已注册仓库。
skill_type: planner
user_invocable: false
disable_model_invocation: true
routing_summary: 适用于本地或已注册仓库任务，包括代码评审、实现、重构、测试、提交、推送，以及基于 workspace 的技术分析。
planning_guidance: |
  本地或已注册仓库读取、代码评审、代码编辑、测试、提交、推送，以及基于 workspace 的实现工作，使用 coder runtime。
  小型、目标单一的仓库任务通常使用一个粗粒度 coder node。
  coder node 可以在同一个 node 中检查仓库并产出 markdown 报告或建议。
  只有当用户请求跨多个业务域、模块、角色或可独立交付的工作区域时，大型代码任务才使用多个 coder nodes。
  大型代码工作按主要业务能力或角色拆分，不要按文件、命令、grep/read/patch/test 步骤或实现微步骤拆分。
  每个 coder node 的 output_hint 中写入聚焦检查和验证期望；不要额外创建只用于读文件、grep、patch 或运行已知测试的 node。
  如果多个实现 node 必须协调，或用户明确要求跨区域整合/合并，添加一个依赖实现 nodes 的 integration/merge coder node。
  如果用户要求大型多区域代码工作的 review、质量检查或合并准备度，添加一个依赖实现或整合 node 的 code-review coder node。
  当用户命名或引用已注册仓库时，在 coder nodes 上设置匹配 registered_repositories 的 repo_id。
---

这是仅供 Planner 使用的 skill。运行时 agent 不应通过 Skill 工具加载它。
