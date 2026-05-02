from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.skills.manifest import SkillManifest
from app.skills.skill import Skill

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoadedSkillPackage:
    path: Path
    manifest: SkillManifest
    skill: Skill


class SkillPackageLoader:
    def __init__(self, search_paths: list[Path]) -> None:
        self._search_paths = search_paths

    @classmethod
    def from_default_paths(cls, *, data_dir: Path, extra_paths: list[Path] | None = None) -> "SkillPackageLoader":
        paths = [
            data_dir / "skills",
            Path.home() / ".jarvis" / "skills",
        ]
        env_path = os.environ.get("JARVIS_SKILL_PATH")
        if env_path:
            paths.extend(Path(item).expanduser() for item in env_path.split(os.pathsep) if item.strip())
        if extra_paths:
            paths.extend(extra_paths)
        return cls(paths)

    def load(self) -> list[LoadedSkillPackage]:
        packages: list[LoadedSkillPackage] = []
        for path in self.discover():
            try:
                packages.append(self.load_package(path))
            except Exception as exc:
                logger.warning("skipping invalid skill package path=%s error=%s", path, exc)
        return packages

    def discover(self) -> list[Path]:
        packages: list[Path] = []
        for root in self._search_paths:
            if not root.exists() or not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                if (child / "manifest.yaml").exists() or (child / "SKILL.md").exists():
                    packages.append(child)
        return packages

    def load_package(self, path: Path) -> LoadedSkillPackage:
        manifest = _read_manifest(path)
        skill = Skill(
            name=manifest.name,
            description=manifest.description,
            path=path,
            manifest=manifest,
            content_path=(path / "SKILL.md") if (path / "SKILL.md").exists() else None,
        )
        return LoadedSkillPackage(path=path, manifest=manifest, skill=skill)


def _read_manifest(path: Path) -> SkillManifest:
    manifest_path = path / "manifest.yaml"
    skill_md_path = path / "SKILL.md"
    if manifest_path.exists():
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    elif skill_md_path.exists():
        raw = _read_skill_md_frontmatter(skill_md_path)
    else:
        raise ValueError("skill package must contain manifest.yaml or SKILL.md")
    try:
        return SkillManifest.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"invalid skill manifest: {exc}") from exc


def _read_skill_md_frontmatter(path: Path) -> dict[str, Any]:
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---\n"):
        raise ValueError("SKILL.md must start with YAML frontmatter")
    end = content.find("\n---", 4)
    if end == -1:
        raise ValueError("SKILL.md frontmatter is not closed")
    raw = yaml.safe_load(content[4:end]) or {}
    if not isinstance(raw, dict):
        raise ValueError("SKILL.md frontmatter must be a mapping")
    return raw
