from __future__ import annotations

import logging
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
    def from_default_paths(cls, *, workspace_root: Path, extra_paths: list[Path] | None = None) -> "SkillPackageLoader":
        paths = [
            workspace_root / "skills",
        ]
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
        content_path = (path / "SKILL.md") if (path / "SKILL.md").exists() else None
        skill_id = path.name
        effective_description = _effective_description(manifest, content_path)
        skill = Skill(
            skill_id=skill_id,
            description=manifest.description,
            effective_description=effective_description,
            path=path,
            manifest=manifest,
            content_path=content_path,
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
    if not (content.startswith("---\n") or content.startswith("---\r\n")):
        raise ValueError("SKILL.md must start with YAML frontmatter")
    end = content.find("\n---", 4)
    if end == -1:
        raise ValueError("SKILL.md frontmatter is not closed")
    raw = yaml.safe_load(content[4:end]) or {}
    if not isinstance(raw, dict):
        raise ValueError("SKILL.md frontmatter must be a mapping")
    return raw


def _effective_description(manifest: SkillManifest, content_path: Path | None) -> str:
    if manifest.effective_description:
        return manifest.effective_description.strip()
    if manifest.description:
        return manifest.description.strip()
    if content_path is None or not content_path.exists():
        return ""

    from app.skills.skill import _strip_frontmatter

    body = _strip_frontmatter(content_path.read_text(encoding="utf-8")).strip()
    for paragraph in body.split("\n\n"):
        text = paragraph.strip()
        if not text:
            continue
        if text.startswith("#"):
            return text.lstrip("#").strip()
        return " ".join(line.strip() for line in text.splitlines() if line.strip())
    return ""
