from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.skills.loader import SkillPackageLoader
from app.skills.registry import SkillRegistry
from app.skills.skill import Skill

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Registries:
    skill_registry: SkillRegistry


_registries: Registries | None = None


def bootstrap_registries(*, external_paths: list[Path] | None = None, force: bool = False) -> Registries:
    global _registries
    if _registries is not None and not force:
        return _registries

    settings = get_settings()
    skills = _load_builtin_skills()
    skill_names = {skill.skill_id for skill in skills}

    loader = SkillPackageLoader.from_default_paths(
        workspace_root=settings.workspace_root,
        extra_paths=external_paths,
    )
    for package in loader.load():
        duplicate_skill = package.skill.skill_id in skill_names
        if duplicate_skill:
            logger.warning(
                "skipping skill package with duplicate registrations path=%s skill=%s",
                package.path,
                package.skill.skill_id,
            )
            continue
        skills.append(package.skill)
        skill_names.add(package.skill.skill_id)

    _registries = Registries(
        skill_registry=SkillRegistry(skills),
    )
    return _registries


def get_skill_registry() -> SkillRegistry:
    return bootstrap_registries().skill_registry


def reset_registries_for_tests() -> None:
    global _registries
    _registries = None


def _load_builtin_skills() -> list[Skill]:
    return []
