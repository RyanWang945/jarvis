from __future__ import annotations

import json
from typing import Any

from app.skills.skill import Skill


def render_loaded_skill_guidance(skill: Skill, *, args: Any = None) -> str:
    body = skill.load_body().strip()
    skill_dir = str(skill.path)
    args_text = _stringify_args(args)
    had_arguments_placeholder = "$ARGUMENTS" in body

    body = body.replace("${JARVIS_SKILL_DIR}", skill_dir)
    if args_text is not None:
        body = body.replace("$ARGUMENTS", args_text)

    sections = [f"Base directory for this skill: {skill_dir}"]
    if body:
        sections.extend(["", body])
    if args_text is not None and not had_arguments_placeholder:
        sections.extend(["", f"ARGUMENTS: {args_text}"])
    return "\n".join(sections)


def expected_tools_for_skill(skill: Skill) -> list[str]:
    seen: set[str] = set()
    tools: list[str] = []
    for value in [*skill.manifest.allowed_tools, *skill.manifest.tools]:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            tools.append(text)
    return tools


def _stringify_args(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        text = str(value).strip()
        return text or None
