from functools import lru_cache

from app.skills.base import Skill
from app.skills.echo import EchoSkill
from app.skills.shell import ShellSkill
from app.skills.tavily import TavilySearchSkill


class SkillRegistry:
    def __init__(self, skills: list[Skill]) -> None:
        self._skills = {skill.name: skill for skill in skills}

    def get(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError as exc:
            raise ValueError(f"unknown skill: {name}") from exc

    def list(self) -> list[Skill]:
        return list(self._skills.values())


@lru_cache
def get_default_skill_registry() -> SkillRegistry:
    return SkillRegistry(
        [
            EchoSkill(),
            ShellSkill(),
            TavilySearchSkill(),
        ]
    )
