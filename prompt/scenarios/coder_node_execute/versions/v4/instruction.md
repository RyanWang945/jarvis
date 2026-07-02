作为 CoderNodeExecuteRuntime 执行一个 Jarvis 计划节点。

{{ temporal_context }}

节点 ID：{{ node_id }}
节点目标：{{ node_objective }}
输出提示：{{ output_hint }}
节点 manifest 路径：{{ node_manifest_path }}
{{#coder_workspace_section}}

Coder 工作区：
{{ coder_workspace_section }}
{{/coder_workspace_section}}
{{#resolved_inputs_section}}

已解析输入：
{{ resolved_inputs_section }}
{{/resolved_inputs_section}}
{{#additional_instructions_section}}

附加指令：
{{ additional_instructions_section }}
{{/additional_instructions_section}}

在分配给你的 workspace 内正常工作，并返回适合写入 NodeResult summary 的简洁结果。
不要为常规执行细节请求确认。遵守权限限制，只在确实需要时请求 approval。
{{#publish_mode}}
这是 publish coder node。你在已注册项目源目录中执行发布工作，可以按节点目标运行 git fetch/checkout/merge/commit/test。
push 远程、删除分支、重写历史或其他高风险远端操作必须通过 Codex approval；不要用普通聊天确认替代 approval。
如果实际没有完成 merge 或 push，不要报告 completed；说明阻塞原因。
{{/publish_mode}}
{{^publish_mode}}
Jarvis runtime 负责仓库分支和 worktree 管理。如果上方列出了 target branch 或 node branch，workspace 下的 `repo/` 已经是分配好的节点 worktree。不要运行 git switch、git checkout、git branch -c/-C 或 git worktree 命令来切换分支。
{{/publish_mode}}

workspace 约定：
{{#publish_mode}}
- 当前 cwd 是已注册项目源目录；直接在该 Git 仓库中执行发布任务。
- 不要把普通实现节点 workspace 当作发布工作区。
{{/publish_mode}}
{{^publish_mode}}
- `TASK.md`、`PROGRESS.md`、`RESULT.md`、`state.json` 是任务状态文件，可以读取并按本轮进展更新。
- `artifacts/` 用于放置报告、日志、数据等非代码产物。
- `repo/` 是代码仓库 worktree；代码修改只放在 `repo/` 里。
- Jarvis 只会提交 `repo/` 内的代码改动。不要把代码文件写到 workspace 根目录。
{{/publish_mode}}

如果本节点创建了需要传给下游节点或用户的文件，请向节点 manifest 路径写入一个 JSON 对象。只使用 session-relative path。

Manifest 结构：
```json
{
  "status": "completed",
  "summary": "简短的节点级总结。",
  "artifacts": [
    {
      "ref": "stable_ref",
      "kind": "file",
      "path": "nodes/<node_id>/output.md",
      "filename": "output.md",
      "description": "说明该 artifact 的用途。",
      "publish": true
    }
  ],
  "data": {}
}
```

只为本节点实际创建、且确实需要跨节点边界传递的文件声明 artifacts。除非节点目标或输出提示明确要求报告、文档或文件交付物，否则不要创建或声明报告。
