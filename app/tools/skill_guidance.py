from __future__ import annotations

import json
from typing import Any

from app.skills.bootstrap import get_skill_registry
from app.tools.common import ToolExecutionRequest, ToolExecutionResult


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
                "name": match.skill.name,
                "description": match.skill.description,
                "confidence": match.confidence,
                "score": match.score,
                "reason": match.reason,
            }
            for match in matches
        ],
        "selection_instruction": (
            "Jarvis runtime will inject the selected skill guidance as a turn-scoped "
            "<system-reminder>. Follow it before delegating to tools."
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
