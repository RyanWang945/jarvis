from __future__ import annotations

import json
from typing import Any

from app.tools.common import ToolExecutionRequest, ToolExecutionResult


def run_ask_user(request: ToolExecutionRequest) -> ToolExecutionResult:
    args = request.args
    question = str(args.get("question") or "").strip()
    if not question:
        return ToolExecutionResult(
            ok=False,
            exit_code=2,
            stderr="ask_user requires a non-empty question.",
            summary="Missing question.",
        )

    choices = _coerce_choices(args.get("choices"))
    payload: dict[str, Any] = {
        "status": "waiting_for_user",
        "question": question,
        "reason": _optional_str(args.get("reason")),
        "expected_answer_type": _expected_answer_type(args.get("expected_answer_type")),
        "choices": choices,
    }
    payload = {key: value for key, value in payload.items() if value not in (None, [], "")}
    return ToolExecutionResult(
        ok=True,
        exit_code=0,
        stdout=json.dumps(payload, ensure_ascii=False),
        summary=question,
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _expected_answer_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"free_text", "yes_no", "choice"}:
        return text
    return "free_text"


def _coerce_choices(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    choices: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in choices:
            choices.append(text)
        if len(choices) >= 8:
            break
    return choices
