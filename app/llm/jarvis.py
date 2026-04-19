from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from app.config import get_settings
from app.llm.client import ChatClient, LLMMessage, parse_json_content
from app.tools.specs import ToolCallPlan, ToolSpec


class JarvisLLM:
    def __init__(self, chat_client: ChatClient) -> None:
        self._chat = chat_client

    def plan_tasks(self, *, instruction: str, tools: list[ToolSpec]) -> list[ToolCallPlan]:
        message = self._chat.chat(
            [
                LLMMessage(
                    role="system",
                    content=(
                        "You are Jarvis Planner. Convert the user's instruction into one or more "
                        "tool calls. Choose the most appropriate provided tool yourself. Do not ask "
                        "the user to select a tool. Prefer the lowest-risk tool that can complete "
                        "the task, and rely on Jarvis risk checks for unsafe local actions."
                    ),
                ),
                LLMMessage(
                    role="user",
                    content=f"Instruction:\n{instruction}",
                ),
            ],
            tools=[_tool_to_chat_tool(tool) for tool in tools],
            tool_choice="auto",
        )
        plans = _tool_calls_from_message(message)
        if plans:
            return plans
        return _legacy_json_plans(message)

    def assess_completion(
        self,
        *,
        task: dict[str, Any],
        result: dict[str, Any],
        can_retry: bool,
    ) -> dict[str, str]:
        message = self._chat.chat(
            [
                LLMMessage(
                    role="system",
                    content=(
                        "You are Jarvis Completion Assessor. Decide whether a completed worker "
                        "result satisfies the task's definition of done. Return strict JSON only. "
                        "Allowed decisions: success, retry, replan, failed, blocked. Use retry only when "
                        "the issue is likely fixable by rerunning the same work and can_retry is true. "
                        "Use replan when the same work order is unlikely to satisfy the DoD and a different "
                        "strategy or tool is needed. "
                        "Use failed when the worker output shows the task did not satisfy the DoD. "
                        "Use blocked when human input or missing external context is required."
                    ),
                ),
                LLMMessage(
                    role="user",
                    content=json.dumps(
                        {
                            "task": task,
                            "worker_result": result,
                            "can_retry": can_retry,
                            "response_schema": {
                                "decision": "success | retry | replan | failed | blocked",
                                "summary": "short reason",
                            },
                        },
                        ensure_ascii=False,
                    ),
                ),
            ],
            response_format={"type": "json_object"},
        )
        return _completion_assessment_from_message(message, can_retry=can_retry)


@lru_cache
def get_jarvis_llm() -> JarvisLLM:
    settings = get_settings()
    provider = settings.llm_provider.lower()
    api_key = _provider_api_key(provider)
    if not api_key:
        raise ValueError(f"JARVIS_{provider.upper()}_API_KEY is required when JARVIS_PLANNER_TYPE=llm.")
    return JarvisLLM(
        ChatClient(
            api_key=api_key,
            base_url=_provider_base_url(provider),
            model=_provider_model(provider),
            timeout_seconds=_provider_timeout_seconds(provider),
        )
    )


def _provider_api_key(provider: str) -> str | None:
    settings = get_settings()
    if provider == "deepseek":
        return settings.deepseek_api_key
    if provider == "kimi":
        return settings.kimi_api_key
    if provider == "gemini":
        return settings.gemini_api_key
    raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")


def _provider_base_url(provider: str) -> str:
    settings = get_settings()
    if provider == "deepseek":
        return settings.deepseek_base_url
    if provider == "kimi":
        return settings.kimi_base_url
    if provider == "gemini":
        return settings.gemini_base_url
    raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")


def _provider_model(provider: str) -> str:
    settings = get_settings()
    if provider == "deepseek":
        return settings.deepseek_model
    if provider == "kimi":
        return settings.kimi_model
    if provider == "gemini":
        return settings.gemini_model
    raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")


def _provider_timeout_seconds(provider: str) -> float:
    settings = get_settings()
    if provider == "deepseek" and settings.deepseek_timeout_seconds is not None:
        return settings.deepseek_timeout_seconds
    return settings.llm_timeout_seconds


def _tool_to_chat_tool(tool: ToolSpec) -> dict[str, Any]:
    parameters = tool.args_schema or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": parameters,
        },
    }


def _tool_calls_from_message(message: dict[str, Any]) -> list[ToolCallPlan]:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []

    plans: list[ToolCallPlan] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = _parse_arguments(function.get("arguments"))
        plans.append(ToolCallPlan(tool_name=name, tool_args=arguments))
    return plans


def _legacy_json_plans(message: dict[str, Any]) -> list[ToolCallPlan]:
    body = parse_json_content(message)
    tasks = body.get("tasks", [])
    if not isinstance(tasks, list):
        return []

    plans: list[ToolCallPlan] = []
    for item in tasks:
        if not isinstance(item, dict):
            continue
        tool_name = item.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            continue
        tool_args = item.get("tool_args") if isinstance(item.get("tool_args"), dict) else {}
        plans.append(
            ToolCallPlan(
                tool_name=tool_name,
                tool_args=tool_args,
                title=_clean_string(item.get("title")),
                description=_clean_string(item.get("description")),
                dod=_clean_string(item.get("dod")),
                verification_cmd=_clean_string(item.get("verification_cmd")),
                max_retries=int(item.get("max_retries") or 0),
            )
        )
    return plans


def _completion_assessment_from_message(message: dict[str, Any], *, can_retry: bool) -> dict[str, str]:
    body = parse_json_content(message)
    if not body:
        return {"decision": "success", "summary": "Worker completed successfully."}

    decision = str(body.get("decision") or "success").strip().lower()
    if decision not in {"success", "retry", "replan", "failed", "blocked"}:
        decision = "success"
    if decision == "retry" and not can_retry:
        decision = "failed"
    summary = _clean_string(body.get("summary")) or "Completion assessment finished."
    return {"decision": decision, "summary": summary}


def _parse_arguments(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _clean_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
