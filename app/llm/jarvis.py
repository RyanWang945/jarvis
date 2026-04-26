from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.llm.client import ChatClient, LLMMessage, parse_json_content
from app.tools.specs import PlannerDecision, ToolCallPlan, ToolSpec


class JarvisLLM:
    def __init__(self, chat_client: ChatClient) -> None:
        self._chat = chat_client

    def plan_tasks(self, *, instruction: str, tools: list[ToolSpec]) -> list[ToolCallPlan]:
        return self.plan_decision(instruction=instruction, tools=tools).tool_calls

    def plan_decision(self, *, instruction: str, tools: list[ToolSpec]) -> PlannerDecision:
        if type(self).plan_tasks is not _ORIGINAL_PLAN_TASKS:
            return PlannerDecision(tool_calls=self.plan_tasks(instruction=instruction, tools=tools))
        message = self._chat.chat(
            [
                LLMMessage(
                    role="system",
                    content=_load_prompt("planner_system").strip(),
                ),
                LLMMessage(
                    role="user",
                    content=_load_prompt("planner_user").replace("{instruction}", instruction).strip(),
                ),
            ],
            tools=[_tool_to_chat_tool(tool) for tool in tools],
            tool_choice="auto",
        )
        plans = _tool_calls_from_message(message)
        if plans:
            return PlannerDecision(tool_calls=plans, raw_output=_raw_output(message))
        return _legacy_json_decision(message)

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
                    content=_load_prompt("assessor_system").strip(),
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

    def synthesize_final_answer(
        self,
        *,
        instruction: str,
        tasks: list[dict[str, Any]],
        worker_results: list[dict[str, Any]],
    ) -> str:
        message = self._chat.chat(
            [
                LLMMessage(
                    role="system",
                    content=_load_prompt("synthesizer_system").strip(),
                ),
                LLMMessage(
                    role="user",
                    content=json.dumps(
                        {
                            "instruction": instruction,
                            "tasks": tasks,
                            "worker_results": worker_results,
                        },
                        ensure_ascii=False,
                    ),
                ),
            ],
        )
        content = message.get("content")
        return str(content).strip() if content else ""


_ORIGINAL_PLAN_TASKS = JarvisLLM.plan_tasks

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


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


def _legacy_json_decision(message: dict[str, Any]) -> PlannerDecision:
    body = parse_json_content(message)
    if not body:
        return PlannerDecision(raw_output=_raw_output(message))
    tasks = body.get("tasks", [])
    if not isinstance(tasks, list):
        tasks = []

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
    return PlannerDecision(
        confidence=_clean_confidence(body.get("confidence")),
        needs_clarification=bool(body.get("needs_clarification") or False),
        clarification_question=_clean_string(body.get("clarification_question")),
        tool_calls=plans,
        raw_output=body,
    )


def _raw_output(message: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(message, ensure_ascii=False, default=str))


def _clean_confidence(value: object) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(1.0, confidence))


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
