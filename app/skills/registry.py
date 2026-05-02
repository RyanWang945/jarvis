from __future__ import annotations

import re

from app.skills.skill import Skill


class SkillRegistry:
    def __init__(self, skills: list[Skill]) -> None:
        self._skills: dict[str, Skill] = {}
        for skill in skills:
            if skill.name in self._skills:
                raise ValueError(f"duplicate skill: {skill.name}")
            self._skills[skill.name] = skill

    def get(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError as exc:
            raise ValueError(f"unknown skill: {name}") from exc

    def list(self) -> list[Skill]:
        return list(self._skills.values())

    def select_for_query(self, text: str, *, limit: int = 3) -> list[Skill]:
        query = (text or "").strip().lower()
        if not query:
            return []

        explicit = query.lstrip("/").split(maxsplit=1)[0]
        if query.startswith("/") and explicit in self._skills:
            return [self._skills[explicit]]

        scored: list[tuple[int, Skill]] = []
        query_tokens = _tokens(query)
        for skill in self._skills.values():
            score = 0
            name = skill.name.lower()
            description = (skill.description or "").lower()
            manifest_text = " ".join(skill.manifest.capabilities).lower()

            if name in query:
                score += 8
            if any(capability.lower() in query for capability in skill.manifest.capabilities):
                score += 5

            haystack_tokens = _tokens(" ".join([name, description, manifest_text]))
            overlap = query_tokens.intersection(haystack_tokens)
            score += len(overlap)

            if score > 0:
                scored.append((score, skill))

        scored.sort(key=lambda item: (-item[0], item[1].name))
        return [skill for _, skill in scored[:limit]]


def get_default_skill_registry() -> SkillRegistry:
    from app.skills.bootstrap import get_skill_registry

    return get_skill_registry()


def _tokens(text: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9_./-]+", text) if token}
