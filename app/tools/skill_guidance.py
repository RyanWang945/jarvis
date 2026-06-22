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


def _json_result(payload: dict[str, Any]) -> ToolExecutionResult:
    stdout = json.dumps(payload, ensure_ascii=False)
    return ToolExecutionResult(ok=True, exit_code=0, stdout=stdout, summary=str(payload.get("status") or "completed"))
