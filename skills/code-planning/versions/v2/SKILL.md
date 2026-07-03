---
name: 代码规划
description: 用于本地或已注册仓库相关任务的 planner skill。
when_to_use: 用户要求读取、分析、评审、修改、实现、重构、测试、提交、合并、发布、推送，或以其他方式处理本地/已注册代码仓库、配置、prompt、脚本、测试、日志、CI、构建文件。
skill_type: planner
user_invocable: false
disable_model_invocation: true

routing_summary: >
  仓库相关任务使用具备 repo 能力的 runtime，通常是 coder。
  本 skill 只负责代码仓库规划，不负责生产运维、云资源操作或外部系统发布；这些应由 ops 类 skill 处理。

planning_guidance: |
  ## Runtime

  - 涉及本地或已注册仓库的读取、分析、评审、实现、修改、测试、脚本执行、配置调整、prompt 文件调整、提交、分支操作、合并或推送，使用 coder runtime。
  - 当任务命名或引用已注册仓库时，在 coder node 上设置匹配的 repo_id。
  - 如果任务提到 Jarvis、jarvis、planner、planner prompt、heavy_plan、当前系统、本项目，且 registered_repositories 中存在 jarvis，相关 coder node 使用 repo_id=jarvis。
  - 不要使用 react/deepresearch/ops 读取或修改仓库文件，除非 runtime_context 明确声明它们具备对应 repo 能力。

  ## Mode

  - 用户明确要求只检查、只分析、只评审、只出方案、先不改代码、不要动文件时，使用 mode=read。
  - 用户要求修改、实现、修复、新增、删除、重构、更新测试、更新配置、更新 prompt、更新脚本、生成或修改仓库内文档时，使用 mode=write。
  - 用户说“按刚才的方案改”“开始做”“可以改了”“继续实现”“没了”这类确认短句时，结合 conversation_context 解析此前的具体代码任务；如果此前限制是“先不改”，当前确认通常表示解除限制并使用 mode=write。
  - 用户要求 git push、merge 到 master/main、创建 release、打 tag、发布代码包、推送远程分支时，使用 mode=publish。
  - 普通 write node 不得 push、merge、release、deploy 或发布到远程高影响目标。

  ## 默认拆分

  - 小型、目标单一、单 repo 的代码任务默认生成一个 coder node。
  - 读取、搜索、理解文件、修改文件、运行相关测试、修复局部问题，都是同一个 write node 的内部工作，不要拆成多个 nodes。
  - 不要按文件、目录、命令、grep、read、patch、lint、test、commit message 或最终总结拆分 nodes。

  ## 什么时候拆多个 coder nodes

  只有满足以下条件之一时才拆分：
  - 涉及多个 repo，且每个 repo 可独立交付。
  - 涉及多个可独立业务能力，例如 auth、billing、search、notification、admin 等。
  - 涉及明显不同技术角色且可以并行，例如 backend API、frontend UI、infra config、data migration。
  - 用户明确要求先产出审计/设计/迁移方案，再等确认后修改。
  - 用户明确要求实现完成后做独立 review、合并准备度检查或质量审计。
  - 存在发布边界，例如先实现，再 merge/push/release，必须拆出 publish node。
  - 前一个 node 的产物是后一个 node 的必要输入，例如先 deep research 竞品/规范，再更新仓库代码或 prompt。

  ## 实现 node

  - 实现 node 应该自包含：检查相关文件、理解现有风格、做最小必要修改、更新或补充测试、运行或说明相关验证。
  - objective 必须写清用户要实现/修复/修改的具体目标，以及不能做的事情，例如不要 push、不要 merge、不要发布。
  - output_hint 应要求输出：修改摘要、验证结果、未完成风险、测试无法运行时的原因。

  ## Review node

  - 用户明确要求代码 review、质量检查、合并准备度检查，或 planner 已生成多个实现 nodes 且需要收口检查时，添加 review_code_changes node。
  - review node 使用 coder runtime，通常 mode=read。
  - review node 依赖相关实现 nodes。
  - review node 只做检查和报告，不做发布、不做 merge、不做远程 push。
  - 如果 review 需要修复问题，除非用户明确要求自动修复，否则只报告问题；用户明确要求“检查并修复”时，应使用 write node 或额外 write node。

  ## Publish node

  - 用户明确要求 merge、push、release、tag、发布代码包或推送远程时，生成专用 publish coder node。
  - publish node 必须依赖实现或 review node，除非用户明确指定已有 source branch。
  - publish node objective 必须写明 source branch 和 target branch；如果无法从 conversation_context、previous_node_results 或 input_refs 中确定具体 branch，不要让 coder 猜。
  - 如果用户要求 push 远程，objective 必须写明 push 远程前需要走 approval。
  - publish node 不负责新增功能实现；它只做发布、合并、推送或发布前检查。

  ## Output hint 模板

  对 write node：
  "Implement the requested repository change in the specified repo. Inspect relevant files as needed inside this node. Keep changes minimal and consistent with existing style. Add or update relevant tests when appropriate. Run relevant validation if possible; if not possible, explain why. Output changed areas, validation performed, remaining risks, and follow-up suggestions. Do not push, merge, release, or publish."

  对 read review node：
  "Inspect the relevant repository state and produce a concise technical report. Do not modify files. Include findings, evidence, risks, and recommended next steps."

  对 publish node：
  "Perform the requested publish/merge/push/release action only for the specified source and target branches. Run appropriate preflight checks. Require approval before remote push or high-impact publish if the runtime supports approval gates. Output actions performed, validation, and any failure/rollback notes."
---
