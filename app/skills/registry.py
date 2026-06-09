from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.skills.skill import Skill

SkillConfidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class SkillMatch:
    skill: Skill
    score: int
    confidence: SkillConfidence
    reason: str


class SkillRegistry:
    def __init__(self, skills: list[Skill]) -> None:
        self._skills: dict[str, Skill] = {}
        self._aliases: dict[str, str] = {}
        for skill in skills:
            if skill.skill_id in self._skills:
                raise ValueError(f"duplicate skill: {skill.skill_id}")
            self._skills[skill.skill_id] = skill
            display_name = (skill.manifest.name or "").strip()
            if display_name and display_name not in self._skills and display_name not in self._aliases:
                self._aliases[display_name] = skill.skill_id

    def get(self, name: str) -> Skill:
        key = self._aliases.get(name, name)
        try:
            return self._skills[key]
        except KeyError as exc:
            raise ValueError(f"unknown skill: {name}") from exc

    def list(self) -> list[Skill]:
        return list(self._skills.values())

    def select_for_query(self, text: str, *, limit: int = 3) -> list[Skill]:
        return [match.skill for match in self.select_matches(text, limit=limit)]

    def select_matches(self, text: str, *, limit: int = 3) -> list[SkillMatch]:
        query = (text or "").strip().lower()
        if not query:
            return []

        explicit = query.lstrip("/").split(maxsplit=1)[0]
        explicit_key = self._aliases.get(explicit, explicit)
        if query.startswith("/") and explicit_key in self._skills:
            skill = self._skills[explicit_key]
            return [
                SkillMatch(
                    skill=skill,
                    score=100,
                    confidence="high",
                    reason=f"explicit skill invocation: /{explicit}",
                )
            ]

        scored: list[SkillMatch] = []
        query_tokens = _tokens(query)
        for skill in self._skills.values():
            score = 0
            reasons: list[str] = []
            name = skill.skill_id.lower()
            display_name = (skill.display_name or "").lower()
            description = (skill.effective_description or skill.description or "").lower()
            manifest_text = " ".join(
                [
                    skill.manifest.when_to_use or "",
                    " ".join(skill.manifest.tags),
                    " ".join(skill.manifest.tools),
                    " ".join(skill.manifest.capabilities),
                ]
            ).lower()

            if name in query:
                score += 8
                reasons.append("name matched")
            if display_name and display_name in query:
                score += 6
                reasons.append("display name matched")
            if any(capability.lower() in query for capability in skill.manifest.capabilities):
                score += 5
                reasons.append("capability matched")
            if any(tool.lower() in query for tool in skill.manifest.tools):
                score += 4
                reasons.append("tool matched")
            if skill.manifest.when_to_use and skill.manifest.when_to_use.lower() in query:
                score += 3
                reasons.append("when_to_use matched")

            haystack_tokens = _tokens(" ".join([name, display_name, description, manifest_text]))
            overlap = query_tokens.intersection(haystack_tokens)
            score += len(overlap)
            if overlap:
                reasons.append("keyword overlap: " + ", ".join(sorted(overlap)[:5]))

            if score > 0:
                scored.append(
                    SkillMatch(
                        skill=skill,
                        score=score,
                        confidence=_confidence_for_score(score),
                        reason="; ".join(reasons) if reasons else "matched skill metadata",
                    )
                )

        scored.sort(key=lambda item: (-item.score, item.skill.skill_id))
        return scored[:limit]


def get_default_skill_registry() -> SkillRegistry:
    from app.skills.bootstrap import get_skill_registry

    return get_skill_registry()


def _tokens(text: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9_./-]+", text) if token}


def _confidence_for_score(score: int) -> SkillConfidence:
    if score >= 8:
        return "high"
    if score >= 4:
        return "medium"
    return "low"
