from __future__ import annotations

import json
from typing import Any

from app.skills.bootstrap import get_skill_registry
from app.skills.rendering import expected_tools_for_skill, render_loaded_skill_guidance
from app.tools.common import ToolExecutionRequest, ToolExecutionResult


def run_load_skill(request: ToolExecutionRequest) -> ToolExecutionResult:
    raw_skill = str(request.args.get("skill") or "").strip()
    skill_name = raw_skill.lstrip("/")
    if not skill_name:
        payload = {
            "status": "invalid_request",
            "reason": "No skill id was provided.",
            "skills": [],
        }
        return _json_result(payload)

    try:
        skill = get_skill_registry().get(skill_name)
    except ValueError:
        payload = {
            "status": "unknown_skill",
            "reason": f"Unknown Jarvis skill: {raw_skill}",
            "skills": [],
        }
        return _json_result(payload)

    if skill.manifest.disable_model_invocation:
        payload = {
            "status": "disabled_skill",
            "reason": f"Jarvis skill is disabled for model invocation: {skill.skill_id}",
            "skills": [],
        }
        return _json_result(payload)

    args = request.args.get("args")
    expected_tools = expected_tools_for_skill(skill)
    content = render_loaded_skill_guidance(skill, args=args)
    payload: dict[str, Any] = {
        "status": "loaded",
        "skill": {
            "skill_id": skill.skill_id,
            "display_name": skill.display_name,
            "effective_description": skill.effective_description or skill.description,
            "content_path": str(skill.content_path) if skill.content_path is not None else None,
        },
        "expected_tools": expected_tools,
        "skills": [
            {
                "name": skill.skill_id,
                "description": skill.effective_description or skill.description,
                "expected_tools": expected_tools,
                "reason": f"explicit skill load: {skill.skill_id}",
            }
        ],
        "selection_instruction": (
            "Jarvis runtime will inject the selected skill guidance as turn-scoped loaded skill content. "
            "Follow it before delegating to tools."
        ),
        "content": content,
    }
    if args is not None:
        payload["args"] = args
    return _json_result(payload)


def run_load_skill_guidance(request: ToolExecutionRequest) -> ToolExecutionResult:
    query = str(request.args.get("query") or "").strip()
    intent = str(request.args.get("intent") or "").strip()
    max_results = _coerce_max_results(request.args.get("max_results"))
    text = " ".join(part for part in (query, intent) if part).strip()
    if not text:
        payload = {
            "status": "no_matching_skill",
            "reason": "No query or intent was provided.",
            "skills": [],
        }
        return _json_result(payload)

    matches = get_skill_registry().select_matches(text, limit=max_results)
    if not matches:
        payload = {
            "status": "no_matching_skill",
            "reason": "No Jarvis skill matched the provided task.",
            "skills": [],
        }
        return _json_result(payload)

    payload: dict[str, Any] = {
        "status": "loaded",
        "skills": [
            {
                "name": match.skill.skill_id,
                "description": match.skill.effective_description or match.skill.description,
                "confidence": match.confidence,
                "score": match.score,
                "reason": match.reason,
            }
            for match in matches
        ],
        "selection_instruction": (
            "Jarvis runtime will inject the selected skill guidance as turn-scoped loaded skill content. "
            "Follow it before delegating to tools."
        ),
    }
    return _json_result(payload)


def _json_result(payload: dict[str, Any]) -> ToolExecutionResult:
    stdout = json.dumps(payload, ensure_ascii=False)
    return ToolExecutionResult(ok=True, exit_code=0, stdout=stdout, summary=str(payload.get("status") or "completed"))


def _coerce_max_results(value: object) -> int:
    try:
        return max(0, min(int(value), 3))
    except (TypeError, ValueError):
        return 3
