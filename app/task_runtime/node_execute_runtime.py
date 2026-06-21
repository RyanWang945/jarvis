from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Protocol

from app.agent_react.context_manager import ContextManager
from app.config import get_settings
from app.llm.client import LLMMessage, parse_json_content
from app.llm.provider_adapters import NormalizedLLMResponse, NormalizedToolCall
from app.llm.model_profiles import LLMNode
from app.llm.model_router import ModelRouter
from app.prompting import PromptRegistry
from app.repositories import RepositoryRegistry, RepositoryRegistryError, get_repository_registry
from app.runtime_usage import usage_record_from_response
from app.skills.bootstrap import get_skill_registry
from app.skills.rendering import render_loaded_skill_guidance
from app.task_runtime.approval_runtime import runtime_git_merge_approval
from app.task_runtime.approval_types import approval_request_to_dict
from app.task_runtime.coder_provider import (
    CoderProvider,
    CoderRunRequest,
    CoderRunResult,
    build_coder_provider,
)
from app.task_runtime.node_finalizer import CodeNodeFinalizer, LLMCodeNodeFinalizerAgent
from app.task_runtime.node_result import NodeArtifact, NodeError, NodeResult, ResolvedInput
from app.task_runtime.planner import PlanNode
from app.task_runtime.runtime_context import (
    BranchRuntimeContext,
    RepoRuntimeContext,
    TemporalRuntimeContext,
    WorkspaceRuntimeContext,
)
from app.task_runtime.session_workspace import (
    NodeRepoCommit,
    NodeRepoMerge,
    NodeRepoWorkspace,
    commit_node_repo,
    merge_node_repo_to_target,
    prepare_node_repo_workspace,
)
from app.tools.common import ToolExecutionResult
from app.tools.runtime import build_llm_tools, check_tool_policy, execute_tool, get_tool_definition

logger = logging.getLogger(__name__)


_REACT_CODER_ONLY_TOOLS = {
    "delegate_to_claude_code",
    "delegate_to_codex",
    "shell_inspect",
    "shell_run_command",
}
_SKILL_TOOL_NAMES = {"Skill"}
_MAX_REACT_SELECTED_SKILLS = 3
_MAX_REACT_SKILL_GUIDANCE_CHARS = 12000


@dataclass(frozen=True)
class NodeExecutionContext:
    user_objective: str
    node: PlanNode
    resolved_inputs: list[ResolvedInput] = field(default_factory=list)
    runtime_hints: dict[str, Any] = field(default_factory=dict)
    instructions: list[str] = field(default_factory=list)


class NodeExecuteRuntime(Protocol):
    def run(self, context: NodeExecutionContext) -> NodeResult: ...


class LLMNodeExecuteRuntime:
    """Plain LLM runtime for one node, with only the Skill loader tool exposed."""

    def __init__(
        self,
        *,
        model_resolver=None,
        skill_tool_runner=execute_tool,
        max_skill_steps: int = 3,
        skill_tool_timeout_seconds: int = 30,
    ) -> None:
        self._model_resolver = model_resolver or (lambda context: ModelRouter().resolve(LLMNode.AGENT_STEP, None))
        self._skill_tool_runner = skill_tool_runner
        self._max_skill_steps = max(1, int(max_skill_steps))
        self._skill_tool_timeout_seconds = max(1, int(skill_tool_timeout_seconds))

    def run(self, context: NodeExecutionContext) -> NodeResult:
        started = time.perf_counter()
        resolved = self._model_resolver(context)
        if not resolved.profile.api_key:
            logger.info("llm node skipped node_id=%s reason=missing_api_key profile=%s", context.node.id, getattr(resolved.profile, "id", None))
            return _blocked(context.node, "missing_api_key", "LLM runtime API key is not configured.")
        messages = _llm_messages(context)
        tools = build_llm_tools(allowed_tools={"Skill"})
        tool_calls: list[dict[str, Any]] = []
        loaded_skill_names: set[str] = set()
        response: NormalizedLLMResponse | None = None
        usage_records: list[dict[str, Any]] = []
        response_format = {"type": "json_object"} if resolved.profile.supports_json_object else None
        try:
            for step_index in range(1, self._max_skill_steps + 1):
                force_final = step_index == self._max_skill_steps
                logger.info(
                    "llm node request node_id=%s step=%s model_profile=%s response_format=%s resolved_input_count=%s skill_tool_count=%s force_final=%s",
                    context.node.id,
                    step_index,
                    getattr(resolved.profile, "id", None),
                    response_format,
                    len(context.resolved_inputs),
                    len(tool_calls),
                    force_final,
                )
                response = resolved.client.chat_normalized(
                    messages,
                    tools=None if force_final else tools,
                    tool_choice=None if force_final else "auto",
                    response_format=response_format,
                )
                usage_record = usage_record_from_response(response, stage="llm_node")
                if usage_record is not None:
                    usage_records.append(usage_record)
                if response.tool_calls:
                    messages.append(_assistant_tool_call_message(response))
                    for tool_call in response.tool_calls:
                        observation, record = self._run_skill_tool_call(tool_call)
                        tool_calls.append(record)
                        messages.append(
                            LLMMessage(
                                role="tool",
                                tool_call_id=tool_call.id,
                                content=json.dumps(observation, ensure_ascii=False),
                            )
                        )
                    skill_message = _skill_guidance_message_from_tool_records(tool_calls, loaded_skill_names)
                    if skill_message is not None:
                        messages.append(skill_message)
                    continue

                result = _llm_result_from_response(context, response, tool_calls, usage_records)
                logger.info(
                    "llm node completed node_id=%s model=%s finish_reason=%s skill_tool_count=%s summary_len=%s elapsed_ms=%s",
                    context.node.id,
                    response.model,
                    response.finish_reason,
                    len(tool_calls),
                    len(result.summary),
                    int((time.perf_counter() - started) * 1000),
                )
                return result
        except Exception as exc:
            logger.exception("llm node failed node_id=%s elapsed_ms=%s", context.node.id, int((time.perf_counter() - started) * 1000))
            return _failed(context.node, "llm_runtime_error", str(exc), retryable=True)
        if response is None:
            return _failed(context.node, "llm_runtime_no_response", "LLM runtime finished without a response.", retryable=True)
        return _llm_result_from_response(context, response, tool_calls, usage_records)

    def _run_skill_tool_call(self, tool_call: NormalizedToolCall) -> tuple[dict[str, Any], dict[str, Any]]:
        started = time.perf_counter()
        record: dict[str, Any] = {
            "id": tool_call.id,
            "tool_name": tool_call.name,
            "args": tool_call.args,
        }
        if tool_call.name not in _SKILL_TOOL_NAMES:
            message = "Rejected: LLM runtime nodes can only call the Skill tool."
            record.update({"status": "rejected", "summary": message})
            return {"ok": False, "status": "rejected", "error": message}, record
        try:
            tool = get_tool_definition(tool_call.name)
            result = self._skill_tool_runner(
                tool,
                tool_call.args,
                timeout_seconds=self._skill_tool_timeout_seconds,
            )
        except Exception as exc:
            message = str(exc)
            record.update({"status": "failed", "summary": message})
            logger.exception("llm node skill tool failed tool_call_id=%s elapsed_ms=%s", tool_call.id, int((time.perf_counter() - started) * 1000))
            return {"ok": False, "status": "failed", "error": message}, record

        status = "completed" if result.ok else "failed"
        record.update(
            {
                "status": status,
                "summary": result.summary,
                "exit_code": result.exit_code,
            }
        )
        loaded_skill = _loaded_skill_from_tool_result(tool.name, result.stdout)
        if loaded_skill is not None:
            record["loaded_skill"] = loaded_skill
        observation = {
            "ok": result.ok,
            "status": status,
            "tool_name": tool.name,
            "summary": result.summary,
            "stdout": _truncate(_tool_observation_stdout(tool.name, result.stdout)),
            "stderr": _truncate(result.stderr),
        }
        return observation, record


class ReactNodeExecuteRuntime:
    """Tool-using research runtime for one plan node."""

    def __init__(
        self,
        *,
        model_resolver=None,
        tool_runner=execute_tool,
        max_steps: int = 6,
        tool_timeout_seconds: int = 60,
    ) -> None:
        self._model_resolver = model_resolver or (lambda context: ModelRouter().resolve(LLMNode.AGENT_STEP, None))
        self._tool_runner = tool_runner
        self._max_steps = max(2, int(max_steps))
        self._tool_timeout_seconds = max(1, int(tool_timeout_seconds))

    def run(self, context: NodeExecutionContext) -> NodeResult:
        started = time.perf_counter()
        resolved = self._model_resolver(context)
        if not resolved.profile.api_key:
            logger.info("react node skipped node_id=%s reason=missing_api_key profile=%s", context.node.id, getattr(resolved.profile, "id", None))
            return _blocked(context.node, "missing_api_key", "React runtime LLM API key is not configured.")
        messages = _react_messages(context)
        tools = build_llm_tools()
        tool_calls: list[dict[str, Any]] = []
        loaded_skill_names: set[str] = set()
        response: NormalizedLLMResponse | None = None
        usage_records: list[dict[str, Any]] = []
        try:
            for step_index in range(1, self._max_steps + 1):
                force_final = step_index == self._max_steps
                response_format = {"type": "json_object"} if force_final and resolved.profile.supports_json_object else None
                logger.info(
                    "react node llm step start node_id=%s step=%s force_final=%s exposed_tool_count=%s tool_observation_count=%s",
                    context.node.id,
                    step_index,
                    force_final,
                    len(tools),
                    len(tool_calls),
                )
                response = resolved.client.chat_normalized(
                    messages,
                    tools=None if force_final else tools,
                    tool_choice=None if force_final else "auto",
                    response_format=response_format,
                )
                usage_record = usage_record_from_response(response, stage="react_node")
                if usage_record is not None:
                    usage_records.append(usage_record)
                if response.tool_calls:
                    logger.info(
                        "react node llm proposed tools node_id=%s step=%s tools=%s",
                        context.node.id,
                        step_index,
                        [{"id": item.id, "name": item.name, "args": item.args} for item in response.tool_calls],
                    )
                    messages.append(_assistant_tool_call_message(response))
                    for tool_call in response.tool_calls:
                        observation, record = self._run_tool_call(context, tool_call)
                        tool_calls.append(record)
                        messages.append(
                            LLMMessage(
                                role="tool",
                                tool_call_id=tool_call.id,
                                content=json.dumps(observation, ensure_ascii=False),
                            )
                        )
                    skill_message = _skill_guidance_message_from_tool_records(
                        tool_calls,
                        loaded_skill_names,
                    )
                    if skill_message is not None:
                        messages.append(skill_message)
                    continue
                result = _react_result_from_response(context, response, tool_calls, usage_records)
                logger.info(
                    "react node completed node_id=%s status=%s tool_call_count=%s summary_len=%s elapsed_ms=%s",
                    context.node.id,
                    result.status,
                    len(tool_calls),
                    len(result.summary),
                    int((time.perf_counter() - started) * 1000),
                )
                return result
        except Exception as exc:
            logger.exception("react node failed node_id=%s elapsed_ms=%s", context.node.id, int((time.perf_counter() - started) * 1000))
            return _failed(context.node, "react_runtime_error", str(exc), retryable=True)
        if response is None:
            return _failed(context.node, "react_runtime_no_response", "React runtime finished without an LLM response.", retryable=True)
        result = _react_result_from_response(context, response, tool_calls, usage_records)
        logger.info(
            "react node completed node_id=%s status=%s tool_call_count=%s summary_len=%s elapsed_ms=%s",
            context.node.id,
            result.status,
            len(tool_calls),
            len(result.summary),
            int((time.perf_counter() - started) * 1000),
        )
        return result

    def _run_tool_call(self, context: NodeExecutionContext, tool_call: NormalizedToolCall) -> tuple[dict[str, Any], dict[str, Any]]:
        started = time.perf_counter()
        record: dict[str, Any] = {
            "id": tool_call.id,
            "tool_name": tool_call.name,
            "args": tool_call.args,
        }
        boundary_rejection = _check_react_action_permission(context, tool_call.name)
        if boundary_rejection is not None:
            record.update({"status": "rejected", "summary": boundary_rejection})
            logger.info(
                "react node tool rejected tool=%s tool_call_id=%s node_id=%s reason=%s",
                tool_call.name,
                tool_call.id,
                context.node.id,
                boundary_rejection,
            )
            return {"ok": False, "status": "rejected", "error": boundary_rejection}, record
        try:
            tool = get_tool_definition(tool_call.name)
            if not tool.exposed_to_llm:
                message = f"Rejected: hidden tool is not callable by React runtime: {tool.name}"
                record.update({"status": "rejected", "summary": message})
                return {"ok": False, "status": "rejected", "error": message}, record
        except Exception as exc:
            message = str(exc)
            record.update({"status": "failed", "summary": message})
            logger.warning("react node tool definition failed tool=%s tool_call_id=%s error=%s", tool_call.name, tool_call.id, message)
            return {"ok": False, "status": "failed", "error": message}, record
        rejection = check_tool_policy(tool, tool_call.args, [])
        if rejection is not None:
            record.update({"status": "rejected", "summary": rejection})
            logger.info("react node tool rejected tool=%s tool_call_id=%s reason=%s", tool_call.name, tool_call.id, rejection)
            return {"ok": False, "status": "rejected", "error": rejection}, record
        try:
            logger.info("react node tool start tool=%s tool_call_id=%s args=%s", tool_call.name, tool_call.id, tool_call.args)
            result = self._tool_runner(tool, tool_call.args, timeout_seconds=self._tool_timeout_seconds)
        except Exception as exc:
            message = str(exc)
            record.update({"status": "failed", "summary": message})
            logger.exception("react node tool failed tool=%s tool_call_id=%s elapsed_ms=%s", tool_call.name, tool_call.id, int((time.perf_counter() - started) * 1000))
            return {"ok": False, "status": "failed", "error": message}, record

        status = "completed" if result.ok else "failed"
        logger.info(
            "react node tool finished tool=%s tool_call_id=%s status=%s exit_code=%s artifact_count=%s elapsed_ms=%s summary=%s",
            tool_call.name,
            tool_call.id,
            status,
            result.exit_code,
            len(result.tool_artifacts) + len(result.artifacts),
            int((time.perf_counter() - started) * 1000),
            _truncate(result.summary, limit=300),
        )
        record.update(
            {
                "status": status,
                "summary": result.summary,
                "exit_code": result.exit_code,
                "artifacts": list(result.artifacts),
                "tool_artifacts": [_tool_artifact_dict(item) for item in result.tool_artifacts],
            }
        )
        loaded_skill = _loaded_skill_from_tool_result(tool.name, result.stdout)
        if loaded_skill is not None:
            record["loaded_skill"] = loaded_skill
        observation = {
            "ok": result.ok,
            "status": status,
            "tool_name": tool.name,
            "summary": result.summary,
            "stdout": _truncate(_tool_observation_stdout(tool.name, result.stdout)),
            "stderr": _truncate(result.stderr),
            "artifacts": list(result.artifacts),
            "tool_artifacts": [_tool_artifact_dict(item) for item in result.tool_artifacts],
        }
        return observation, record


class CoderNodeExecuteRuntime:
    """Provider-backed code execution runtime for one plan node."""

    def __init__(
        self,
        provider: CoderProvider | None = None,
        finalizer: CodeNodeFinalizer | None = None,
        git_context_resolver: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self._provider = provider or build_coder_provider(get_settings())
        self._finalizer = finalizer or _default_code_node_finalizer()
        self._git_context_resolver = git_context_resolver or _llm_coder_git_context

    def run(self, context: NodeExecutionContext) -> NodeResult:
        started = time.perf_counter()
        try:
            registry = get_repository_registry()
        except RepositoryRegistryError as exc:
            logger.info("coder node blocked node_id=%s reason=repository_registry_error error=%s", context.node.id, exc)
            return _blocked(context.node, "repository_not_available", str(exc))
        try:
            context = _context_with_git_workspace_hints(
                context,
                registry=registry,
                resolver=self._git_context_resolver,
            )
        except Exception as exc:
            logger.exception("coder git context resolution failed node_id=%s", context.node.id)
            return _failed(context.node, "coder_git_context_failed", str(exc), retryable=True)
        repo_id = _active_repo(context, registry=registry)
        if not repo_id:
            logger.info("coder node blocked node_id=%s reason=missing_active_repo", context.node.id)
            return _blocked(context.node, "missing_active_repo", "Coder runtime requires runtime_hints.active_repo.")
        try:
            repo = registry.resolve_repo(repo_id)
        except RepositoryRegistryError as exc:
            logger.info("coder node blocked node_id=%s repo_id=%s reason=repository_error error=%s", context.node.id, repo_id, exc)
            return _blocked(context.node, "repository_not_available", str(exc))
        repo_context = RepoRuntimeContext.from_hints(context.runtime_hints)
        run_dir = repo_context.provider_run_dir
        try:
            repo_workspace = prepare_node_repo_workspace(
                repo_id=repo.repo_id,
                project_path=repo.canonical_root_path,
                runtime_hints=context.runtime_hints,
                node_id=context.node.id,
            )
            workdir = repo_workspace.repo_path if repo_workspace is not None else repo.canonical_root_path
        except (RuntimeError, ValueError, OSError) as exc:
            logger.info("coder node blocked node_id=%s repo_id=%s reason=node_repo_prepare_failed error=%s", context.node.id, repo_id, exc)
            return _blocked(context.node, "node_repo_prepare_failed", str(exc))
        request_metadata = {
            "node_id": context.node.id,
            "project_path": str(repo.canonical_root_path),
            "workdir": str(workdir),
        }
        git_context_usage = context.runtime_hints.get("git_context_usage")
        if isinstance(git_context_usage, dict):
            request_metadata["usage_records"] = [git_context_usage]
        if repo_workspace is not None:
            request_metadata["repo_workspace"] = repo_workspace.metadata()
            request_metadata["source_branch"] = repo_workspace.source_branch
            request_metadata["target_branch"] = repo_workspace.target_branch
            request_metadata["node_branch"] = repo_workspace.node_branch
            context = NodeExecutionContext(
                user_objective=context.user_objective,
                node=context.node,
                resolved_inputs=context.resolved_inputs,
                runtime_hints={
                    **context.runtime_hints,
                    "source_branch": repo_workspace.source_branch,
                    "target_branch": repo_workspace.target_branch,
                    "node_branch": repo_workspace.node_branch,
                    "worktree_mode": BranchRuntimeContext.from_hints(context.runtime_hints).worktree_mode or "node_branch_worktree",
                },
                instructions=context.instructions,
            )
        workspace_context = WorkspaceRuntimeContext.from_hints(context.runtime_hints)
        if workspace_context.manifest_path_text:
            request_metadata["node_manifest_path"] = workspace_context.manifest_path_text
        if run_dir is not None:
            request_metadata["run_dir"] = str(run_dir)
        if workspace_context.session_id:
            request_metadata["session_id"] = workspace_context.session_id
        instruction = _coder_instruction(context)
        request = CoderRunRequest(
            repo_id=repo.repo_id,
            workdir=workdir,
            instruction=instruction,
            timeout_seconds=int(getattr(get_settings(), "coder_timeout_seconds", 1800)),
            run_dir=run_dir,
            metadata=request_metadata,
        )
        try:
            logger.info(
                "coder node start node_id=%s repo_id=%s provider=%s workdir=%s run_dir=%s",
                context.node.id,
                repo_id,
                self._provider.name,
                str(workdir),
                str(run_dir) if run_dir is not None else "",
            )
            result = self._provider.run(request)
            result = replace(result, metadata={**request.metadata, **result.metadata})
            if result.ok and repo_workspace is not None:
                try:
                    result = _commit_coder_node_worktree(
                        result,
                        workdir=workdir,
                        node_id=context.node.id,
                        objective=context.node.objective,
                        repo_workspace=repo_workspace,
                    )
                except (RuntimeError, OSError) as exc:
                    logger.exception("coder node worktree commit/merge failed node_id=%s workdir=%s", context.node.id, workdir)
                    return _failed(context.node, "node_repo_commit_failed", str(exc), retryable=True)
        except Exception as exc:
            logger.exception("coder node failed node_id=%s elapsed_ms=%s", context.node.id, int((time.perf_counter() - started) * 1000))
            return _failed(context.node, "coder_runtime_error", str(exc), retryable=True)
        node_result = _finalize_coder_node_result(
            self._finalizer,
            context=context,
            instruction=instruction,
            result=result,
            provider=self._provider.name,
        )
        logger.info(
            "coder node finished node_id=%s provider=%s status=%s exit_code=%s artifact_count=%s elapsed_ms=%s summary=%s",
            context.node.id,
            self._provider.name,
            node_result.status,
            result.exit_code,
            len(result.artifacts),
            int((time.perf_counter() - started) * 1000),
            _truncate(node_result.summary, limit=300),
        )
        return node_result


def _default_code_node_finalizer() -> CodeNodeFinalizer:
    settings = get_settings()
    if bool(getattr(settings, "coder_node_finalizer_llm_enabled", False)):
        return CodeNodeFinalizer(llm_agent=LLMCodeNodeFinalizerAgent())
    return CodeNodeFinalizer()


def _llm_messages(context: NodeExecutionContext) -> list[LLMMessage]:
    payload = {
        "user_objective": context.user_objective,
        "node": context.node.model_dump(mode="json"),
        "resolved_inputs": [item.model_dump(mode="json", exclude_none=True) for item in context.resolved_inputs],
        "temporal_context": _temporal_context(context.runtime_hints),
        "runtime_hints": context.runtime_hints,
        "instructions": context.instructions,
    }
    messages = PromptRegistry().load("llm_node_execute").render(
        {"input_json": json.dumps(payload, ensure_ascii=False)}
    )
    skill_listing = _skill_listing_message()
    if skill_listing is not None:
        messages.append(skill_listing)
    return messages


def _react_messages(context: NodeExecutionContext) -> list[LLMMessage]:
    payload = {
        "user_objective": context.user_objective,
        "node": context.node.model_dump(mode="json"),
        "resolved_inputs": [item.model_dump(mode="json", exclude_none=True) for item in context.resolved_inputs],
        "temporal_context": _temporal_context(context.runtime_hints),
        "runtime_hints": context.runtime_hints,
        "instructions": context.instructions,
    }
    messages = PromptRegistry().load("react_node_execute").render(
        {"input_json": json.dumps(payload, ensure_ascii=False)}
    )
    skill_listing = _skill_listing_message()
    if skill_listing is not None:
        messages.append(skill_listing)
    return messages


def _skill_listing_message() -> LLMMessage | None:
    try:
        message = ContextManager().build_skill_listing_message()
    except Exception:
        logger.warning("node runtime failed to render skill listing", exc_info=True)
        return None
    if message is None:
        return None
    return LLMMessage(role="user", content=str(message.content or ""))


def _skill_guidance_message_from_tool_records(
    tool_calls: list[dict[str, Any]],
    loaded_skill_names: set[str],
) -> LLMMessage | None:
    invocations: list[dict[str, Any]] = []
    for record in tool_calls:
        loaded = record.get("loaded_skill")
        if not isinstance(loaded, dict):
            continue
        name = str(loaded.get("name") or "").strip()
        if not name or name in loaded_skill_names:
            continue
        loaded_skill_names.add(name)
        invocations.append(loaded)
    return _render_skill_guidance_message(invocations)


def _render_skill_guidance_message(invocations: list[dict[str, Any]]) -> LLMMessage | None:
    content = _render_skill_guidance_text(invocations)
    if content is None:
        return None
    return LLMMessage(role="user", content=content)


def _render_skill_guidance_text(invocations: list[dict[str, Any]]) -> str | None:
    if not invocations:
        return None
    try:
        registry = get_skill_registry()
    except Exception:
        logger.warning("node runtime failed to access skill registry", exc_info=True)
        return None

    sections: list[str] = []
    seen: set[str] = set()
    for invocation in invocations:
        if len(sections) >= _MAX_REACT_SELECTED_SKILLS:
            break
        name = str(invocation.get("name") or invocation.get("skill") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        content = invocation.get("content")
        if isinstance(content, str) and content.strip():
            sections.append(f"[Skill: {name}]\n{content.strip()}")
            continue
        try:
            skill = registry.get(name)
        except ValueError:
            continue
        body = render_loaded_skill_guidance(skill, args=invocation.get("args")).strip()
        if not body:
            continue
        sections.append(f"[Skill: {skill.skill_id}]\n{body}")

    if not sections:
        return None
    content = PromptRegistry().load("loaded_skill_guidance").render_text({"skill_sections": "\n\n".join(sections)})
    if len(content) > _MAX_REACT_SKILL_GUIDANCE_CHARS:
        content = content[:_MAX_REACT_SKILL_GUIDANCE_CHARS].rstrip() + "\n\n[Skill content truncated by Jarvis runtime.]"
    return content


def _loaded_skill_from_tool_result(tool_name: str, stdout: str) -> dict[str, Any] | None:
    if tool_name not in _SKILL_TOOL_NAMES:
        return None
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("status") != "loaded":
        return None
    skill_payload = payload.get("skill")
    name = ""
    if isinstance(skill_payload, dict):
        name = str(skill_payload.get("skill_id") or "").strip()
    if not name:
        skills = payload.get("skills")
        if isinstance(skills, list) and skills and isinstance(skills[0], dict):
            name = str(skills[0].get("name") or "").strip()
    if not name:
        return None
    loaded: dict[str, Any] = {"name": name}
    if "args" in payload:
        loaded["args"] = payload.get("args")
    content = payload.get("content")
    if isinstance(content, str) and content.strip():
        loaded["content"] = content
    return loaded


def _tool_observation_stdout(tool_name: str, stdout: str) -> str:
    if tool_name not in _SKILL_TOOL_NAMES:
        return stdout
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return stdout
    if not isinstance(payload, dict):
        return stdout
    if "content" in payload:
        payload = dict(payload)
        payload["content"] = "[injected as turn-scoped skill guidance]"
    return json.dumps(payload, ensure_ascii=False)


def _assistant_tool_call_message(response: NormalizedLLMResponse) -> LLMMessage:
    return LLMMessage(
        role="assistant",
        content=response.content or "",
        tool_calls=[
            {
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "arguments": json.dumps(tool_call.args, ensure_ascii=False),
                },
            }
            for tool_call in response.tool_calls
        ],
        reasoning_content=response.reasoning_content,
    )


def _llm_result_from_response(
    context: NodeExecutionContext,
    response: NormalizedLLMResponse,
    tool_calls: list[dict[str, Any]],
    usage_records: list[dict[str, Any]],
) -> NodeResult:
    payload = parse_json_content({"content": response.content})
    reply = _first_payload_text(payload, ("reply", "final_answer", "final", "answer", "result"))
    summary = str(payload.get("summary") or reply or response.content or "").strip()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if reply:
        data = dict(data)
        data.setdefault("reply", reply)
    return NodeResult(
        node_id=context.node.id,
        runtime="llm",
        status="completed",
        summary=summary or "LLM runtime completed.",
        tool_calls=tool_calls,
        usage_records=usage_records,
        data=data,
        artifacts=_artifacts_from_payload(payload),
    )


def _first_payload_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    data = payload.get("data")
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _react_result_from_response(
    context: NodeExecutionContext,
    response: NormalizedLLMResponse,
    tool_calls: list[dict[str, Any]],
    usage_records: list[dict[str, Any]],
) -> NodeResult:
    payload = parse_json_content({"content": response.content})
    summary = _react_summary(payload, response.content) or _react_summary_from_tool_calls(tool_calls)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    data.update(_react_extra_data(payload))
    findings = payload.get("findings")
    sources = payload.get("sources")
    if isinstance(findings, list):
        data.setdefault("findings", findings)
    else:
        data.setdefault("findings", [])
    if isinstance(sources, list):
        data.setdefault("sources", sources)
    else:
        data.setdefault("sources", [])
    return NodeResult(
        node_id=context.node.id,
        runtime="react",
        status="completed",
        summary=summary or "React runtime completed.",
        tool_calls=tool_calls,
        tool_artifacts=_tool_artifacts_from_tool_calls(tool_calls),
        usage_records=usage_records,
        data=data,
        artifacts=_artifacts_from_payload(payload),
    )


def _check_react_action_permission(context: NodeExecutionContext, tool_name: str) -> str | None:
    if tool_name not in _REACT_CODER_ONLY_TOOLS:
        return None
    node_id = context.node.id
    return (
        f"Rejected: React runtime node {node_id} cannot execute coder-only actions. "
        "Use a coder node for shell commands, repository workflows, and code-agent delegation."
    )


def _react_summary(payload: dict[str, Any], response_content: str) -> str:
    for key in ("summary", "answer", "result", "final_answer"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    findings = payload.get("findings")
    if isinstance(findings, list):
        summary = _summary_from_list(findings)
        if summary:
            return summary
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("summary", "answer", "result", "final_answer"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        summary = _summary_from_mapping(data)
        if summary:
            return summary
    summary = _summary_from_mapping(_react_extra_data(payload))
    if summary:
        return summary
    text = str(response_content or "").strip()
    if text in {"{}", "[]", "null"}:
        return ""
    return text


def _tool_artifacts_from_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for call in tool_calls:
        call_artifacts = call.get("tool_artifacts")
        if isinstance(call_artifacts, list):
            artifacts.extend(item for item in call_artifacts if isinstance(item, dict))
    return artifacts


def _react_summary_from_tool_calls(tool_calls: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for record in tool_calls:
        if record.get("status") != "completed":
            continue
        tool_name = str(record.get("tool_name") or "tool").strip()
        summary = str(record.get("summary") or "").strip()
        if summary:
            lines.append(f"- {tool_name}: {_truncate(summary, limit=500)}")
        if len(lines) >= 5:
            break
    return "\n".join(lines)


def _react_extra_data(payload: dict[str, Any]) -> dict[str, Any]:
    reserved = {
        "summary",
        "answer",
        "result",
        "final_answer",
        "data",
        "findings",
        "sources",
        "artifacts",
    }
    return {key: value for key, value in payload.items() if key not in reserved}


def _summary_from_mapping(value: dict[str, Any]) -> str:
    for key in ("findings", "candidates", "items", "results"):
        items = value.get(key)
        if isinstance(items, list):
            summary = _summary_from_list(items)
            if summary:
                return summary
    if not value:
        return ""
    return _truncate(json.dumps(value, ensure_ascii=False), limit=1200)


def _summary_from_list(items: list[Any]) -> str:
    lines: list[str] = []
    for item in items[:5]:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = _summary_from_item(item)
        else:
            text = str(item).strip()
        if text:
            lines.append(f"- {text}")
    return "\n".join(lines)


def _summary_from_item(item: dict[str, Any]) -> str:
    label = _first_item_text(item, ("name", "title", "candidate"))
    summary = _first_item_text(item, ("summary", "answer", "claim", "result"))
    if label and summary and label != summary:
        return f"{label}: {summary}"
    if label or summary:
        return label or summary or ""
    for key in ("summary", "answer", "title", "name", "candidate", "claim", "result"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    compact = {key: value for key, value in item.items() if key not in {"url", "source_url"}}
    return _truncate(json.dumps(compact or item, ensure_ascii=False), limit=400)


def _first_item_text(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _coder_instruction(context: NodeExecutionContext) -> str:
    resolved_inputs_lines: list[str] = []
    if context.resolved_inputs:
        for item in context.resolved_inputs:
            resolved_inputs_lines.append(f"- {item.ref} ({item.kind}, status={item.source_status or 'n/a'}): {item.summary}")
            if item.data:
                resolved_inputs_lines.append(f"  data: {json.dumps(item.data, ensure_ascii=False)[:2000]}")
            if item.artifacts:
                artifact_refs = ", ".join(f"artifact:{artifact.ref}" for artifact in item.artifacts)
                resolved_inputs_lines.append(f"  artifacts: {artifact_refs}")
    additional_instructions = "\n".join(f"- {item}" for item in context.instructions)
    return PromptRegistry().load("coder_node_execute").render_text(
        {
            "temporal_context": _temporal_context_text(context.runtime_hints),
            "user_objective": context.user_objective,
            "node_id": context.node.id,
            "node_objective": context.node.objective,
            "output_hint": context.node.output_hint or "Repository task result.",
            "node_manifest_path": WorkspaceRuntimeContext.from_hints(context.runtime_hints).manifest_name(),
            "coder_workspace_section": _coder_workspace_section(context.runtime_hints),
            "resolved_inputs_section": "\n".join(resolved_inputs_lines),
            "additional_instructions_section": additional_instructions,
        }
    )


def _coder_workspace_section(runtime_hints: dict[str, Any]) -> str:
    lines: list[str] = []
    branch_context = BranchRuntimeContext.from_hints(runtime_hints)
    if branch_context.source_branch:
        lines.append(f"- Source branch: {branch_context.source_branch}")
    if branch_context.target_branch:
        lines.append(f"- Target branch: {branch_context.target_branch}")
    if branch_context.node_branch:
        lines.append(f"- Node branch: {branch_context.node_branch}")
    if branch_context.worktree_mode:
        lines.append(f"- Worktree mode: {branch_context.worktree_mode}")
    if branch_context.target_branch:
        lines.append("- Branch checkout is managed by Jarvis runtime before provider execution.")
    return "\n".join(lines)


def _temporal_context(runtime_hints: dict[str, Any]) -> dict[str, str]:
    return TemporalRuntimeContext.from_hints(runtime_hints).as_payload()


def _temporal_context_text(runtime_hints: dict[str, Any]) -> str:
    temporal = _temporal_context(runtime_hints)
    return PromptRegistry().load("coder_temporal_context").render_text(
        {
            "has_temporal": bool(temporal),
            "current_date": temporal.get("current_date", ""),
            "current_time": temporal.get("current_time", ""),
            "timezone": temporal.get("timezone", ""),
        }
    )


def _finalize_coder_node_result(
    finalizer: CodeNodeFinalizer,
    *,
    context: NodeExecutionContext,
    instruction: str,
    result: CoderRunResult,
    provider: str,
) -> NodeResult:
    approval_required = bool(result.approval_requests)
    approval_requests = [approval_request_to_dict(item) for item in result.approval_requests]
    workspace_context = WorkspaceRuntimeContext.from_hints(context.runtime_hints)
    return finalizer.finalize(
        node=context.node,
        user_objective=context.user_objective,
        instruction=instruction,
        provider=provider,
        provider_ok=result.ok,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        provider_summary=result.summary,
        legacy_artifacts=result.artifacts,
        metadata=result.metadata,
        approval_required=approval_required,
        approval_requests=approval_requests,
        session_root=workspace_context.session_root,
        node_workspace=workspace_context.node_workspace,
        manifest_path=workspace_context.manifest_path,
    )


def _commit_coder_node_worktree(
    result: CoderRunResult,
    *,
    workdir: Path,
    node_id: str,
    objective: str,
    repo_workspace: NodeRepoWorkspace | None = None,
) -> CoderRunResult:
    node_commit = commit_node_repo(workdir, node_id=node_id, objective=objective)
    if node_commit is None:
        return result

    metadata = dict(result.metadata)
    metadata["node_commit"] = node_commit.metadata()
    artifacts = _with_node_commit_artifacts(result.artifacts, node_commit)
    stdout = result.stdout
    if stdout:
        stdout = stdout.rstrip() + f"\n\n[JARVIS_NODE_COMMIT]\n{node_commit.short_hash} {node_commit.subject}\n"
    if repo_workspace is not None:
        if _is_protected_target_branch(repo_workspace.target_branch):
            approval = runtime_git_merge_approval(
                repo_workspace=repo_workspace,
                node_commit=node_commit,
                node_id=node_id,
            )
            return replace(
                result,
                stdout=stdout,
                artifacts=artifacts,
                approval_requests=[*result.approval_requests, approval],
                metadata=metadata,
            )
        node_merge = merge_node_repo_to_target(repo_workspace, node_commit=node_commit)
        metadata["node_merge"] = node_merge.metadata()
        artifacts = _with_node_merge_artifacts(artifacts, node_merge)
        if stdout:
            stdout = stdout.rstrip() + (
                f"\n\n[JARVIS_NODE_MERGE]\n"
                f"{node_merge.merge_commit[:12]} {node_merge.node_branch} -> {node_merge.target_branch}\n"
            )
    return replace(result, stdout=stdout, artifacts=artifacts, metadata=metadata)

def _with_node_commit_artifacts(artifacts: list[str], node_commit: NodeRepoCommit) -> list[str]:
    result = list(artifacts)
    commit_artifact = f"git_commit:{node_commit.short_hash}"
    node_commit_artifact = f"node_git_commit:{node_commit.short_hash}"
    if commit_artifact not in result:
        result.append(commit_artifact)
    if node_commit_artifact not in result:
        result.append(node_commit_artifact)
    if "git_worktree:clean" not in result:
        result = [item for item in result if item != "git_worktree:dirty"]
        result.append("git_worktree:clean")
    for path in node_commit.files:
        file_artifact = f"git_file:{path}"
        if file_artifact not in result:
            result.append(file_artifact)
    return result


def _with_node_merge_artifacts(artifacts: list[str], node_merge: NodeRepoMerge) -> list[str]:
    result = [item for item in artifacts if not item.startswith("git_commit:")]
    merge_artifacts = [
        f"git_commit:{node_merge.merge_commit[:12]}",
        f"git_branch:{node_merge.target_branch}",
        f"node_git_branch:{node_merge.node_branch}",
    ]
    for artifact in merge_artifacts:
        if artifact not in result:
            result.append(artifact)
    return result


def _codex_node_summary(result: ToolExecutionResult) -> str:
    if result.ok:
        return result.stdout or result.summary or "Codex runtime finished."
    return result.summary or result.stderr or result.stdout or "Codex runtime failed."


def _artifact_from_tool_string(value: str) -> NodeArtifact:
    text = str(value)
    kind, _, ref = text.partition(":")
    if not ref:
        kind = "artifact"
        ref = text
    return NodeArtifact(ref=ref, kind=kind or "artifact", name=ref, publish=False, metadata={"legacy": text})


def _artifacts_from_payload(payload: dict[str, Any]) -> list[NodeArtifact]:
    raw = payload.get("artifacts")
    if not isinstance(raw, list):
        return []
    artifacts: list[NodeArtifact] = []
    for item in raw:
        if isinstance(item, str):
            artifacts.append(_artifact_from_tool_string(item))
        elif isinstance(item, dict):
            raw_path = _optional_text(item.get("path") or item.get("session_relative_path"))
            ref = str(
                item.get("ref")
                or item.get("id")
                or item.get("artifact_id")
                or _ref_from_path(raw_path)
                or ""
            ).strip()
            if ref:
                metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
                delivery = str(item.get("delivery") or metadata.get("delivery") or "").strip().lower()
                artifacts.append(
                    NodeArtifact(
                        ref=ref,
                        artifact_id=_optional_text(item.get("artifact_id") or item.get("id")),
                        kind=str(item.get("kind") or item.get("type") or "artifact"),
                        name=_optional_text(item.get("name") or item.get("filename") or item.get("title")),
                        description=str(item.get("description") or item.get("summary") or ""),
                        path=raw_path,
                        session_relative_path=_optional_text(item.get("session_relative_path")),
                        mime_type=_optional_text(item.get("mime_type")),
                        filename=_optional_text(item.get("filename")),
                        size_bytes=_optional_int(item.get("size_bytes")),
                        source_tool=_optional_text(item.get("source_tool")) or "",
                        publish=_optional_bool(item.get("publish"), default=delivery not in {"internal", "none"}),
                        metadata=metadata,
                    )
                )
    return artifacts


def _tool_artifact_dict(artifact: Any) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "kind": artifact.kind,
        "turn_id": artifact.turn_id,
        "tool_call_id": artifact.tool_call_id,
        "path": artifact.path,
        "session_relative_path": getattr(artifact, "session_relative_path", None),
        "mime_type": artifact.mime_type,
        "filename": artifact.filename,
        "size_bytes": artifact.size_bytes,
        "source_tool": artifact.source_tool,
        "node_id": getattr(artifact, "node_id", None),
        "publish": getattr(artifact, "publish", True),
        "metadata": artifact.metadata,
    }


def _truncate(value: str, *, limit: int = 4000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "external", "publish"}:
        return True
    if text in {"0", "false", "no", "internal", "none"}:
        return False
    return default


def _ref_from_path(path: str | None) -> str:
    if not path:
        return ""
    text = path.replace("\\", "/").strip("/")
    if not text:
        return ""
    stem = Path(text).stem or "artifact"
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in stem).strip("_-")
    return safe or "artifact"


def _context_with_git_workspace_hints(
    context: NodeExecutionContext,
    *,
    registry: RepositoryRegistry,
    resolver: Callable[..., dict[str, Any]],
) -> NodeExecutionContext:
    initial_repo = None
    initial_repo_id = _active_repo(context, registry=registry)
    if initial_repo_id:
        try:
            initial_repo = registry.resolve_repo(initial_repo_id)
        except RepositoryRegistryError:
            initial_repo = None
    resolved_context = resolver(context=context, registry=registry, repo=initial_repo)
    hints = _clean_git_context_hints(resolved_context)
    if not hints:
        return context
    logger.info("coder git context node_id=%s hints=%s", context.node.id, json.dumps(hints, ensure_ascii=False, default=str))
    return NodeExecutionContext(
        user_objective=context.user_objective,
        node=context.node,
        resolved_inputs=context.resolved_inputs,
        runtime_hints={**context.runtime_hints, **hints},
        instructions=context.instructions,
    )


def _llm_coder_git_context(
    *,
    context: NodeExecutionContext,
    registry: RepositoryRegistry,
    repo: Any | None,
) -> dict[str, Any]:
    try:
        resolved = ModelRouter().resolve(LLMNode.PLANNER, None)
    except Exception as exc:
        logger.info("coder git context skipped node_id=%s reason=model_resolve_failed error=%s", context.node.id, exc)
        return {}
    if not resolved.profile.api_key:
        logger.info("coder git context skipped node_id=%s reason=missing_api_key profile=%s", context.node.id, getattr(resolved.profile, "id", None))
        return {}

    payload = {
        "user_objective": context.user_objective,
        "node": {
            "id": context.node.id,
            "objective": context.node.objective,
            "output_hint": context.node.output_hint,
        },
        "merged_runtime_hints": context.runtime_hints,
        "selected_repo": getattr(repo, "repo_id", None),
        "repositories": _registered_repo_facts(registry),
    }
    messages = [
        LLMMessage(
            role="system",
            content=(
                "You resolve Git workspace intent for one Jarvis coder node. "
                "Return JSON only. Do not plan code changes. "
                "Jarvis runtime will perform checkout/worktree/merge; the coder must not."
            ),
        ),
        LLMMessage(
            role="user",
            content=(
                "Choose the registered repository and Git branch context for this coder node.\n"
                "Set target_branch to the branch the user wants Jarvis runtime to integrate work into when one is named. "
                "Do not decide approval here; Jarvis permission gates check protected Git actions at execution time.\n"
                "Output exactly this shape with empty strings for unknown optional fields:\n"
                '{"repo_id":"", "source_branch":"", "target_branch":"", "worktree_mode":"node_branch_worktree|", '
                '"confidence":0.0}\n\n'
                f"Input:\n{json.dumps(payload, ensure_ascii=False, default=str)}"
            ),
        ),
    ]
    try:
        response = resolved.client.chat_normalized(
            messages,
            response_format={"type": "json_object"} if resolved.profile.supports_json_object else None,
        )
        parsed = parse_json_content({"content": response.content})
        usage = usage_record_from_response(response, stage="coder_git_context")
        if usage is not None:
            parsed["usage_record"] = usage
        return parsed
    except Exception as exc:
        logger.info("coder git context llm failed node_id=%s error=%s", context.node.id, exc)
        return {}


def _registered_repo_facts(registry: RepositoryRegistry) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for repo in registry.list_repositories():
        if repo.status != "active":
            continue
        item: dict[str, Any] = {
            "repo_id": repo.repo_id,
            "name": repo.name,
            "path": str(repo.canonical_root_path),
            "permission_level": repo.permission_level,
        }
        item.update(_repo_git_facts(repo.canonical_root_path))
        facts.append(item)
    return facts


def _repo_git_facts(path: Path) -> dict[str, Any]:
    current = _git_stdout_safe(path, "branch", "--show-current")
    branches = _git_stdout_safe(path, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    remote_branches = _git_stdout_safe(path, "for-each-ref", "--format=%(refname:short)", "refs/remotes")
    return {
        "current_branch": current.strip(),
        "local_branches": [line.strip() for line in branches.splitlines() if line.strip()],
        "remote_branches": [line.strip() for line in remote_branches.splitlines() if line.strip()],
    }


def _git_stdout_safe(path: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=path,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def _clean_git_context_hints(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    hints: dict[str, Any] = {}
    repo_id = _plain_text(payload.get("repo_id") or payload.get("project") or payload.get("active_repo"))
    if repo_id:
        hints["active_repo"] = repo_id
    source_branch = _clean_branch_hint(payload.get("source_branch"))
    if source_branch:
        hints["source_branch"] = source_branch
    target_branch = _clean_branch_hint(payload.get("target_branch") or payload.get("active_branch") or payload.get("git_branch"))
    if target_branch:
        hints["target_branch"] = target_branch
        hints["worktree_mode"] = "node_branch_worktree"
    worktree_mode = _plain_text(payload.get("worktree_mode"))
    if worktree_mode == "node_branch_worktree":
        hints["worktree_mode"] = worktree_mode
    usage_record = payload.get("usage_record")
    if isinstance(usage_record, dict):
        hints["git_context_usage"] = usage_record
    return hints


def _clean_branch_hint(value: Any) -> str:
    branch = _plain_text(value).strip("`'\"，。,.；;:：")
    if not branch or len(branch) > 160:
        return ""
    if branch in {".", ".."} or branch.endswith((".lock", ".", "/")):
        return ""
    if branch.startswith(("-", "/", ".")) or any(ch.isspace() for ch in branch):
        return ""
    if any(token in branch for token in ("..", "~", "^", ":", "?", "*", "[", "\\", "@{")):
        return ""
    return branch


def _plain_text(value: Any) -> str:
    return str(value or "").strip() if value is not None else ""


def _active_repo(context: NodeExecutionContext, *, registry: RepositoryRegistry | None = None) -> str | None:
    repo_context = RepoRuntimeContext.from_hints(context.runtime_hints)
    if repo_context.active_repo:
        return repo_context.active_repo
    if registry is not None:
        active_repos = [repo for repo in registry.list_repositories() if repo.status == "active"]
        if len(active_repos) == 1:
            return active_repos[0].repo_id
    return None


def _is_protected_target_branch(branch: str) -> bool:
    return str(branch or "").strip().lower() in {"main", "master"}


def _blocked(node: PlanNode, code: str, message: str) -> NodeResult:
    return NodeResult(node_id=node.id, runtime=node.runtime, status="blocked", summary=message, error=NodeError(code=code, message=message))


def _failed(node: PlanNode, code: str, message: str, *, retryable: bool = False) -> NodeResult:
    return NodeResult(
        node_id=node.id,
        runtime=node.runtime,
        status="failed",
        summary=message,
        error=NodeError(code=code, message=message, retryable=retryable),
    )
