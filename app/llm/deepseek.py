import json
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from app.tools.specs import ToolCallPlan, ToolSpec

LLMRole = Literal["system", "user", "assistant"]


class LLMMessage(BaseModel):
    role: LLMRole
    content: str


class DeepSeekClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        timeout_seconds: float = 180.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_seconds = timeout_seconds

    def chat(
        self,
        messages: list[LLMMessage],
        *,
        response_format: dict[str, str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [message.model_dump() for message in messages],
            "stream": False,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

        response = httpx.post(
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        message = body["choices"][0]["message"]
        return message if isinstance(message, dict) else {"content": str(message)}

    def plan_tasks(self, *, instruction: str, tools: list[ToolSpec]) -> list[ToolCallPlan]:
        message = self.chat(
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
        message = self.chat(
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
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return []
    body = json.loads(content)
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
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return {"decision": "success", "summary": "Worker completed successfully."}

    body = json.loads(content)
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
