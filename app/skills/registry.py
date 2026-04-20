from app.skills.base import Skill


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


def get_default_skill_registry() -> SkillRegistry:
    from app.skills.bootstrap import get_skill_registry

    return get_skill_registry()
