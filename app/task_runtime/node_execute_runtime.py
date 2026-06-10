from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.agent_react.context_manager import ContextManager
from app.config import get_settings
from app.llm.client import LLMMessage, parse_json_content
from app.llm.provider_adapters import NormalizedLLMResponse, NormalizedToolCall
from app.llm.model_profiles import LLMNode
from app.llm.model_router import ModelRouter
from app.repositories import RepositoryRegistryError, get_repository_registry
from app.skills.bootstrap import get_skill_registry
from app.skills.rendering import render_loaded_skill_guidance
from app.task_runtime.coder_provider import (
    ApprovalDecision,
    CoderAction,
    CoderPolicy,
    CoderProvider,
    CoderRunRequest,
    CoderRunResult,
    CodexCoderProvider,
    build_coder_provider,
)
from app.task_runtime.node_result import NodeArtifact, NodeError, NodeResult, ResolvedInput
from app.task_runtime.planner import PlanNode
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

                result = _llm_result_from_response(context, response, tool_calls)
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
        return _llm_result_from_response(context, response, tool_calls)

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
                result = _react_result_from_response(context, response, tool_calls)
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
        result = _react_result_from_response(context, response, tool_calls)
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

    def __init__(self, provider: CoderProvider | None = None) -> None:
        self._provider = provider or build_coder_provider(get_settings())

    def run(self, context: NodeExecutionContext) -> NodeResult:
        started = time.perf_counter()
        if context.node.runtime_hints:
            context = NodeExecutionContext(
                user_objective=context.user_objective,
                node=context.node,
                resolved_inputs=context.resolved_inputs,
                runtime_hints={**context.runtime_hints, **context.node.runtime_hints},
                instructions=context.instructions,
            )
        repo_id = _active_repo(context)
        if not repo_id:
            logger.info("coder node blocked node_id=%s reason=missing_active_repo", context.node.id)
            return _blocked(context.node, "missing_active_repo", "Coder runtime requires runtime_hints.active_repo.")
        try:
            repo = get_repository_registry().resolve_repo(repo_id)
        except RepositoryRegistryError as exc:
            logger.info("coder node blocked node_id=%s repo_id=%s reason=repository_error error=%s", context.node.id, repo_id, exc)
            return _blocked(context.node, "repository_not_available", str(exc))
        policy = _coder_policy(context)
        if policy.allow_push and not policy.allow_commit:
            return _failed(context.node, "invalid_coder_policy", "allow_push=true requires allow_commit=true.", retryable=False)
        request = CoderRunRequest(
            repo_id=repo.repo_id,
            workdir=repo.canonical_root_path,
            instruction=_coder_instruction(context),
            policy=policy,
            timeout_seconds=int(getattr(get_settings(), "coder_timeout_seconds", 1800)),
            metadata={"node_id": context.node.id},
        )
        try:
            logger.info(
                "coder node start node_id=%s repo_id=%s provider=%s access_mode=%s allow_commit=%s allow_push=%s",
                context.node.id,
                repo_id,
                self._provider.name,
                policy.access_mode,
                policy.allow_commit,
                policy.allow_push,
            )
            result = self._provider.run(request, decide_action=_decide_coder_action(policy))
        except Exception as exc:
            logger.exception("coder node failed node_id=%s elapsed_ms=%s", context.node.id, int((time.perf_counter() - started) * 1000))
            return _failed(context.node, "coder_runtime_error", str(exc), retryable=True)
        node_result = _node_result_from_coder(context.node, result, provider=self._provider.name)
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


class CodexNodeExecuteRuntime(CoderNodeExecuteRuntime):
    """Compatibility wrapper for older tests/callers that still name Codex directly."""

    def __init__(self, runner=None) -> None:
        provider = CodexCoderProvider() if runner is None else CodexCoderProvider(runner=runner)
        super().__init__(provider=provider)


def _llm_messages(context: NodeExecutionContext) -> list[LLMMessage]:
    payload = {
        "user_objective": context.user_objective,
        "node": context.node.model_dump(mode="json"),
        "resolved_inputs": [item.model_dump(mode="json", exclude_none=True) for item in context.resolved_inputs],
        "temporal_context": _temporal_context(context.runtime_hints),
        "runtime_hints": context.runtime_hints,
        "instructions": context.instructions,
    }
    messages = [
        LLMMessage(
            role="system",
            content=(
                "You are Jarvis LLMNodeExecuteRuntime. Execute one plan node without tools. "
                "Do not produce a final user reply unless the node objective itself is the whole answer. "
                "Use the temporal_context payload as the authoritative current date/time for relative-time wording. "
                "Write summary in the user's language when the node output is user-facing. "
                "Return JSON with summary, optional data, and optional artifacts."
            ),
        ),
        LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
    ]
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
    messages = [
        LLMMessage(
            role="system",
            content=(
                "You are Jarvis ReactNodeExecuteRuntime. Execute one non-repository plan node. "
                "Use tools when external, business, project-memory, reminder, or artifact-delivery action is needed. "
                "Use the temporal_context payload as the authoritative current date/time; convert relative terms "
                "such as today, current, latest, recent, 今天, 当前, 最新, 最近 into concrete date constraints when searching. "
                "Do not perform code edits, shell commands, repository workflows, or code-agent delegation; "
                "code and shell work belongs to coder runtime nodes. "
                "You may use lightweight file and artifact tools for explicit non-code document, report, artifact, or delivery work. "
                "Do not produce a final user reply. "
                "After tool use, return JSON with summary, findings, sources, and data. "
                "Be concise and preserve useful evidence for downstream nodes."
            ),
        ),
        LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
    ]
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
    content = (
        "Loaded skills for this turn. Follow their procedural guidance when relevant.\n\n"
        + "\n\n".join(sections)
    )
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
) -> NodeResult:
    payload = parse_json_content({"content": response.content})
    summary = str(payload.get("summary") or payload.get("answer") or response.content or "").strip()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if tool_calls:
        data = dict(data)
        data["tool_calls"] = tool_calls
    return NodeResult(
        node_id=context.node.id,
        runtime="llm",
        status="completed",
        summary=summary or "LLM runtime completed.",
        data=data,
        artifacts=_artifacts_from_payload(payload),
    )


def _react_result_from_response(
    context: NodeExecutionContext,
    response: NormalizedLLMResponse,
    tool_calls: list[dict[str, Any]],
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
    data["tool_calls"] = tool_calls
    return NodeResult(
        node_id=context.node.id,
        runtime="react",
        status="completed",
        summary=summary or "React runtime completed.",
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
    sections = [
        "Execute one Jarvis plan node as CoderNodeExecuteRuntime.",
        "",
        _temporal_context_text(context.runtime_hints),
        "",
        f"User objective: {context.user_objective}",
        f"Node id: {context.node.id}",
        f"Node objective: {context.node.objective}",
        f"Expected output: {context.node.expected_output or 'Repository task result.'}",
    ]
    if context.resolved_inputs:
        sections.extend(["", "Resolved inputs:"])
        for item in context.resolved_inputs:
            sections.append(f"- {item.ref} ({item.kind}, status={item.source_status or 'n/a'}): {item.summary}")
            if item.data:
                sections.append(f"  data: {json.dumps(item.data, ensure_ascii=False)[:2000]}")
            if item.artifacts:
                artifact_refs = ", ".join(f"artifact:{artifact.ref}" for artifact in item.artifacts)
                sections.append(f"  artifacts: {artifact_refs}")
    if context.instructions:
        sections.extend(["", "Additional instructions:", *[f"- {item}" for item in context.instructions]])
    sections.extend(
        [
            "",
            "Return a concise result suitable for a NodeResult summary.",
            "Do not ask for routine confirmation. Respect permission limits and request approval only when required.",
        ]
    )
    return "\n".join(sections)


def _temporal_context(runtime_hints: dict[str, Any]) -> dict[str, str]:
    current_date = str(runtime_hints.get("current_date") or "").strip()
    current_time = str(runtime_hints.get("current_time") or "").strip()
    timezone = str(runtime_hints.get("timezone") or "").strip()
    return {
        key: value
        for key, value in {
            "current_date": current_date,
            "current_time": current_time,
            "timezone": timezone,
        }.items()
        if value
    }


def _temporal_context_text(runtime_hints: dict[str, Any]) -> str:
    temporal = _temporal_context(runtime_hints)
    if not temporal:
        return "Temporal context: unavailable; do not infer current dates from model memory."
    lines = ["Temporal context:"]
    if temporal.get("current_date"):
        lines.append(f"- Current date: {temporal['current_date']}")
    if temporal.get("current_time"):
        lines.append(f"- Current time: {temporal['current_time']}")
    if temporal.get("timezone"):
        lines.append(f"- Timezone: {temporal['timezone']}")
    lines.append("- Interpret today/current/latest/recent and 今天/当前/最新/最近 relative to this context.")
    return "\n".join(lines)


def _node_result_from_coder(node: PlanNode, result: CoderRunResult, *, provider: str) -> NodeResult:
    approval_required = bool(result.approval_requests)
    status = "blocked" if approval_required else "completed" if result.ok else "failed"
    summary = result.stdout or result.summary or result.stderr or f"Coder provider {provider} finished."
    data = {
        "provider": provider,
        "approval_required": approval_required,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
    }
    if approval_required:
        data["approval_requests"] = [_coder_approval_dict(item) for item in result.approval_requests]
        first = result.approval_requests[0]
        data["approval_id"] = first.approval_id
        data["action_kind"] = first.action_kind
        if first.command:
            data["command"] = first.command
        if first.path:
            data["path"] = first.path
        if first.reason:
            data["reason"] = first.reason
    data.update(result.metadata)
    return NodeResult(
        node_id=node.id,
        runtime="coder",
        status=status,
        summary=summary,
        artifacts=[_artifact_from_tool_string(item) for item in result.artifacts],
        data=data,
        error=(
            NodeError(code="coder_approval_required", message=summary, retryable=False)
            if approval_required
            else None if result.ok else NodeError(code="coder_provider_failed", message=summary, retryable=False)
        ),
    )


def _coder_approval_dict(value: Any) -> dict[str, Any]:
    return {
        "approval_id": value.approval_id,
        "action_kind": value.action_kind,
        "command": value.command,
        "path": value.path,
        "reason": value.reason,
        "raw_provider_payload": value.raw_provider_payload,
    }


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
    return NodeArtifact(ref=ref, kind=kind or "artifact", name=ref)


def _artifacts_from_payload(payload: dict[str, Any]) -> list[NodeArtifact]:
    raw = payload.get("artifacts")
    if not isinstance(raw, list):
        return []
    artifacts: list[NodeArtifact] = []
    for item in raw:
        if isinstance(item, str):
            artifacts.append(_artifact_from_tool_string(item))
        elif isinstance(item, dict):
            ref = str(item.get("ref") or item.get("id") or item.get("artifact_id") or "").strip()
            if ref:
                artifacts.append(
                    NodeArtifact(
                        ref=ref,
                        kind=str(item.get("kind") or item.get("type") or "artifact"),
                        name=_optional_text(item.get("name") or item.get("filename") or item.get("title")),
                        description=str(item.get("description") or item.get("summary") or ""),
                        path=_optional_text(item.get("path")),
                        metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
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
        "mime_type": artifact.mime_type,
        "filename": artifact.filename,
        "size_bytes": artifact.size_bytes,
        "source_tool": artifact.source_tool,
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


def _active_repo(context: NodeExecutionContext) -> str | None:
    value = context.runtime_hints.get("active_repo")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _coder_policy(context: NodeExecutionContext) -> CoderPolicy:
    access_mode = str(context.runtime_hints.get("access_mode") or "").strip().lower()
    if access_mode not in {"read", "write"}:
        access_mode = "read"
    allow_commit = bool(context.runtime_hints.get("allow_commit")) if access_mode == "write" else False
    allow_push = bool(context.runtime_hints.get("allow_push")) if access_mode == "write" else False
    return CoderPolicy(access_mode=access_mode, allow_commit=allow_commit, allow_push=allow_push)


def _decide_coder_action(policy: CoderPolicy):
    def decide(action: CoderAction) -> ApprovalDecision:
        if action.kind in {"read_file", "search", "git_status", "git_diff", "git_log"}:
            return ApprovalDecision("allow")
        if action.kind == "edit_file":
            return ApprovalDecision("allow" if policy.access_mode == "write" else "deny", reason="read mode does not allow edits")
        if action.kind == "commit":
            if policy.access_mode != "write" or not policy.allow_commit:
                return ApprovalDecision("deny", reason="commit is not allowed by coder policy")
            return ApprovalDecision("ask", reason="commit requires approval")
        if action.kind == "push":
            if policy.access_mode != "write" or not policy.allow_push:
                return ApprovalDecision("deny", reason="push is not allowed by coder policy")
            return ApprovalDecision("strong_ask", reason="push requires strong approval")
        if action.kind in {"secret_read", "dangerous_command", "outside_workspace_write"}:
            return ApprovalDecision("deny", reason=f"{action.kind} is denied by default")
        return ApprovalDecision("ask", reason="unknown external action requires approval")

    return decide


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
