仓库上下文：
{{#active_repo_line}}
{{ active_repo_line }}
{{/active_repo_line}}
已注册仓库：
{{ repository_lines }}

仓库工具路由：
- 如果用户命名了已注册仓库，使用对应 repo_id。
- 如果用户说“当前项目”“这个项目”，且存在 active repository，使用该 active repo_id。
- 仓库代码工作应表示为 coder runtime node，而不是工具调用。
- 规划 coder 工作时，描述期望结果和权限；不要拆成 shell 步骤。
- 不要把明确的编辑、commit 或 push 请求转换为只读检查。allow_commit/allow_push 应匹配用户请求的结果。
