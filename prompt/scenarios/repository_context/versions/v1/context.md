Repository context:
{{#active_repo_line}}
{{ active_repo_line }}
{{/active_repo_line}}
Registered repositories:
{{ repository_lines }}

Repository tool routing:
- If the user names a registered repository, use that repo_id.
- If the user says current/this project and an active repository is set, use that active repo_id.
- Repository code work should be represented as a coder runtime node, not as a tool call.
- When planning coder work, describe the desired outcome and permissions; do not decompose it into shell steps.
- Do not convert explicit edit, commit, or push requests into read-only inspection. Set allow_commit/allow_push to match the user's requested outcome.
