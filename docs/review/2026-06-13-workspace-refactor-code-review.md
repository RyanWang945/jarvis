# 2026-06-13 Workspace Refactor Code Review

| Item | Value |
| --- | --- |
| Branch | `feat/refact` |
| Review date | 2026-06-13 |
| Scope | prompt scenarios, task runtime workspace, coder provider run dirs, artifact delivery, planner fallback, related tests |
| Verdict | Do not merge the whole workspace change as-is. Split or fix the blocking coder-workspace gap first. |

## Summary

This change moves Jarvis in a good direction: prompts are versioned under `prompt/scenarios`, coder provider logs are tied to per-node run directories, and session artifacts get a clearer delivery boundary.

The blocking issue is the new coder workspace model. Production task runtime now sends coder nodes into isolated `sessions/.../nodes/.../repo/...` worktrees, but there is no implemented path that merges or promotes code changes back to the registered repository. For a user-facing "modify this repo" task, the runtime can report success while the actual project checkout remains unchanged.

## Blocking Findings

### P0: Coder write tasks do not update the canonical repository

Evidence:

- `TaskAgentRuntime` creates a session workspace and passes it into execution: `app/task_runtime/agent_runtime.py:224`, `app/task_runtime/agent_runtime.py:242`.
- `NodeExecutor` injects session and node workspace hints into every node: `app/task_runtime/node_executor.py:88`, `app/task_runtime/node_executor.py:90`.
- `CoderNodeExecuteRuntime` uses `prepare_node_repo(...)` and sends the provider to that `workdir`: `app/task_runtime/node_execute_runtime.py:392`, `app/task_runtime/node_execute_runtime.py:416`.
- `prepare_node_repo` creates a detached worktree under `sessions/{session}/nodes/{node}/repo/{repo_id}`: `app/task_runtime/session_workspace.py:203`, `app/task_runtime/session_workspace.py:224`.
- No merge, cherry-pick, patch-apply, or promotion path was found from node repo back to the registered repo.

Impact:

- Normal write tasks modify only the node repo, not the user's actual project checkout.
- Commits created in `--detach` worktrees are detached from the branch the user expects.
- `allow_push` becomes ambiguous or broken because the provider is not on the registered repo branch.
- User-facing summaries can claim completion while the workspace the user sees is unchanged.

Recommendation:

- Pick one explicit write strategy before merge:
  - run write tasks on a managed integration branch/worktree and merge back to the canonical repo after approval;
  - require coder nodes to emit patch artifacts, then apply patches in a controlled integration step;
  - keep isolated node repos, but make the final deliverable a patch/artifact and never claim the canonical repo was changed.
- Add an end-to-end test where a coder node writes a file and the expected final location is asserted.
- Add a commit/push test that verifies branch semantics, not only permission flags.

### P0: Multi-node code decomposition cannot actually integrate code results

Evidence:

- The planner fallback creates multiple writer nodes plus `integrate_business_code`: `app/task_runtime/planner.py:756`, `app/task_runtime/planner.py:785`.
- Each coder node gets its own independent node repo by design: `app/task_runtime/session_workspace.py:249`, `tests/test_session_workspace.py:37`.
- The integration node receives prior node summaries through `input_refs`, but not their actual diffs.

Impact:

- The integration node starts from the original `HEAD`, not from the implementation nodes' code.
- It can summarize or reimplement work, but cannot reliably merge the actual previous node changes.

Recommendation:

- Disable multi-writer code fallback until diff handoff exists, or make each implementation node emit a patch artifact consumed by the integration node.
- Add a test where node A and node B change different files, and the integration node must see both diffs.

## Non-Blocking Findings

### P1: Runtime path hints need defensive containment checks

Evidence:

- `_runtime_workdir` accepts any existing directory: `app/tools/codex.py:352`.
- `_runtime_run_dir` accepts any path: `app/tools/codex.py:362`, `app/tools/coder.py:242`.

Current LLM exposure is reduced because coder tools are no longer model-facing, but these hidden args are still direct tool API inputs. Future callers, tests, or scripts can accidentally run providers outside the intended repo/session boundary.

Recommendation:

- Validate `_runtime_workdir` is either the registered canonical repo or the expected node repo for the current session and `repo_id`.
- Validate provider `run_dir` is under the node `provider_run/` directory or the legacy `data/coder_runs/` fallback.
- Add rejection tests for outside paths.

### P1: `deliver_file` contract no longer matches attachment resolution

Evidence:

- Tool prompt still says an explicitly requested workspace file can be delivered: `prompt/scenarios/tool_definitions/versions/v1/catalog.json:118`.
- `run_deliver_file` passes the path directly to `resolve_channel_attachments`: `app/tools/deliver_file.py:62`.
- Attachment resolution now allows only artifact preview roots, coder runs, and session artifact roots: `app/agent_react/artifacts.py:425`.
- Existing test intentionally rejects normal repo files: `tests/test_agent_react_artifacts.py:37`.

Impact:

- If a user explicitly asks to send a workspace file such as a generated repo image or markdown file, the tool can reject it as `path_outside_allowed_roots`.

Recommendation:

- Either update the tool prompt to say only registered artifacts/session artifacts are deliverable, or copy explicit workspace files into `session/artifacts/` with `source_repo_id`, `source_relative_path`, and `source_commit` metadata before resolving.
- Add a test for explicit `deliver_file(path=...)` on a normal workspace file.

### P2: Node path components are sanitized but not length-bounded

Evidence:

- `_safe_component` strips unsafe characters but does not cap length: `app/task_runtime/session_workspace.py:263`.

Impact:

- Very long planner node ids can produce deep Windows paths under `sessions/.../nodes/.../provider_run`, increasing path-length and filesystem failure risk.

Recommendation:

- Cap safe node/session components, for example `64 chars + 8-char hash`.
- Add a test with a long node id.

## Mergeable Areas

These parts are reasonable to merge after the blocking workspace behavior is split or fixed:

- `PromptRegistry` and `prompt/scenarios` migration. The version/profile/env override model is clean and test-covered.
- Tool description catalog extraction into `prompt/scenarios/tool_definitions`.
- Provider run-dir logging into per-node `provider_run/`, once path containment is added.
- Session artifact delivery whitelist, if the `deliver_file` contract is clarified.
- The CardKit JSON 2.0 design document as documentation-only work.

## Suggested Merge Plan

1. Merge prompt externalization as a standalone change with the current prompt tests.
2. Merge provider log/run-dir work with containment validation.
3. Keep session node repos behind a feature flag or read-only mode until write-back semantics are implemented.
4. Add a dedicated integration change for coder write-back:
   - single writer path;
   - multi-writer patch handoff;
   - commit/push branch policy.
5. Only then enable isolated node repos for write tasks by default.

## Verification

Command run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_prompting.py tests/test_session_workspace.py tests/test_task_runtime_e2e.py tests/test_tools_codex.py tests/test_tools_claude_coder.py tests/test_agent_react_artifacts.py tests/test_tools_deliver_file.py tests/test_task_planner.py tests/test_task_planner_eval_runner.py -q
```

Result:

```text
74 passed, 2 skipped
```

The passing tests validate the implemented mechanics, but they do not cover canonical-repo write-back or multi-node diff integration. Those gaps should be treated as required tests before merging the full workspace runtime behavior.
