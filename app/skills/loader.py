from __future__ import annotations

import logging
import json
import os
import re
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


@dataclass(frozen=True)
class _DiscoveredSkillPackage:
    path: Path
    skill_id: str


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
        for package in self._discover_package_specs():
            try:
                packages.append(self.load_package(package.path, skill_id=package.skill_id))
            except Exception as exc:
                logger.warning("skipping invalid skill package path=%s error=%s", package.path, exc)
        return packages

    def discover(self) -> list[Path]:
        return [package.path for package in self._discover_package_specs()]

    def _discover_package_specs(self) -> list[_DiscoveredSkillPackage]:
        specs: list[_DiscoveredSkillPackage] = []
        for root in self._search_paths:
            if not root.exists() or not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                versions_dir = child / "versions"
                if versions_dir.exists() and versions_dir.is_dir():
                    selected = _active_skill_version(root, child.name)
                    version_path = versions_dir / selected if selected else _latest_version_dir(versions_dir)
                    if version_path is None:
                        logger.warning("skipping versioned skill with no versions path=%s", child)
                        continue
                    if not _is_skill_package(version_path):
                        logger.warning(
                            "skipping versioned skill with missing selected version skill=%s version=%s path=%s",
                            child.name,
                            selected or version_path.name,
                            version_path,
                        )
                        continue
                    specs.append(_DiscoveredSkillPackage(path=version_path, skill_id=child.name))
                    continue
                if _is_skill_package(child):
                    specs.append(_DiscoveredSkillPackage(path=child, skill_id=child.name))
        return specs

    def load_package(self, path: Path, *, skill_id: str | None = None) -> LoadedSkillPackage:
        manifest = _read_manifest(path)
        manifest = _with_inferred_version(manifest, path)
        content_path = (path / "SKILL.md") if (path / "SKILL.md").exists() else None
        resolved_skill_id = skill_id or path.name
        effective_description = _effective_description(manifest, content_path)
        skill = Skill(
            skill_id=resolved_skill_id,
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


def _is_skill_package(path: Path) -> bool:
    return (path / "manifest.yaml").exists() or (path / "SKILL.md").exists()


def _with_inferred_version(manifest: SkillManifest, path: Path) -> SkillManifest:
    if manifest.version:
        return manifest
    if path.parent.name == "versions":
        return manifest.model_copy(update={"version": path.name})
    return manifest


def _active_skill_version(root: Path, skill_id: str) -> str | None:
    env_name = "JARVIS_SKILL_" + re.sub(r"[^A-Z0-9]+", "_", skill_id.upper()).strip("_") + "_VERSION"
    env_version = os.environ.get(env_name)
    if env_version is not None and env_version.strip():
        return env_version.strip()

    mapped_version = _parse_version_mapping(os.environ.get("JARVIS_SKILL_VERSIONS")).get(skill_id)
    if mapped_version:
        return mapped_version

    config = _read_skill_config(root)
    profile_name = os.environ.get("JARVIS_SKILL_PROFILE", "").strip()
    if profile_name:
        profile = config.get("profiles", {}).get(profile_name, {})
        if isinstance(profile, dict):
            profile_version = profile.get(skill_id)
            if profile_version:
                return str(profile_version).strip()

    default_version = config.get("default_versions", {}).get(skill_id)
    if default_version:
        return str(default_version).strip()
    return None


def _read_skill_config(root: Path) -> dict[str, Any]:
    config_path = root / "config.json"
    if not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("ignoring invalid skill config path=%s error=%s", config_path, exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_version_mapping(value: str | None) -> dict[str, str]:
    if not value or not value.strip():
        return {}
    text = value.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        return {
            str(key).strip(): str(version).strip()
            for key, version in payload.items()
            if str(key).strip() and str(version).strip()
        }
    result: dict[str, str] = {}
    for item in text.split(","):
        key, separator, version = item.partition("=")
        if not separator:
            key, separator, version = item.partition(":")
        if separator and key.strip() and version.strip():
            result[key.strip()] = version.strip()
    return result


def _latest_version_dir(versions_dir: Path) -> Path | None:
    candidates = [path for path in versions_dir.iterdir() if path.is_dir() and _is_skill_package(path)]
    if not candidates:
        return None
    return sorted(candidates, key=lambda path: _version_sort_key(path.name))[-1]


def _version_sort_key(version: str) -> tuple[int, int | str]:
    match = re.fullmatch(r"v(\d+)", version.strip().lower())
    if match:
        return (1, int(match.group(1)))
    return (0, version)


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
