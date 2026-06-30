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
  不引入 integration node。多个普通实现 node 的代码收口由 Jarvis runtime 合入 session branch；不要额外规划 integration/merge node 来做 session 内整合。
  普通 coder node 只负责实现、检查、测试或报告；不要让普通 coder node 发布到 master/main 或 push 远程。
  如果用户明确要求合并到 master/main、发布、上线、push，生成一个专用 publish coder node：runtime=coder，mode=publish，repo_id 必须来自 registered_repositories。
  publish coder node 的 objective 必须写明 source branch 和 target branch；如果用户要求 push，objective 必须写明 push 远程前需要走 approval。
  如果“刚才的分支”无法从 previous_node_results 或 conversation_context 中明确解析为具体 branch，先生成确认/检查节点，不要让 coder 猜。
  如果用户要求大型多区域代码工作的 review、质量检查或合并准备度，添加一个依赖实现 nodes 的 code-review coder node。
  当用户命名或引用已注册仓库时，在 coder nodes 上设置匹配 registered_repositories 的 repo_id。
---

这是仅供 Planner 使用的 skill。运行时 agent 不应通过 Skill 工具加载它。
