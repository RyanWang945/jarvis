from __future__ import annotations

import importlib.util
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml
from pydantic import ValidationError

from app.skills.base import Skill
from app.skills.manifest import SkillManifest
from app.tools.specs import ToolSpec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoadedSkillPackage:
    path: Path
    manifest: SkillManifest
    skill: Skill | None = None
    tools: list[ToolSpec] = field(default_factory=list)


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
        skill = _load_skill(path, manifest) if manifest.jarvis else None
        tools = [
            ToolSpec(
                name=tool.name,
                description=tool.description,
                args_schema=tool.args_schema,
                skill=tool.skill or manifest.name,
                worker_type=tool.worker_type or tool.skill or manifest.name,
                action=tool.action,
                risk_level=tool.risk_level,
                exposed_to_llm=tool.exposed_to_llm,
            )
            for tool in (manifest.jarvis.tools if manifest.jarvis else [])
        ]
        return LoadedSkillPackage(path=path, manifest=manifest, skill=skill, tools=tools)


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


def _load_skill(path: Path, manifest: SkillManifest) -> Skill:
    if manifest.jarvis is None:
        raise ValueError("manifest has no jarvis extension")
    module_path = _module_path(path, manifest.jarvis.module)
    module_name = f"jarvis_external_skill_{manifest.name}_{abs(hash(module_path))}"
    module = _load_module(module_name, module_path)
    skill_class = getattr(module, manifest.jarvis.class_name, None)
    if skill_class is None:
        raise ValueError(f"skill class not found: {manifest.jarvis.class_name}")
    skill = skill_class()
    if not hasattr(skill, "name") or not hasattr(skill, "run"):
        raise ValueError(f"{manifest.jarvis.class_name} is not a Skill")
    if str(skill.name) != manifest.name:
        logger.warning(
            "external skill name differs from manifest path=%s manifest=%s skill=%s",
            path,
            manifest.name,
            skill.name,
        )
    return skill


def _module_path(package_path: Path, module: str) -> Path:
    if module.endswith(".py"):
        candidate = package_path / module
    else:
        candidate = package_path / (module.replace(".", "/") + ".py")
    resolved_package = package_path.resolve()
    resolved_candidate = candidate.resolve()
    if resolved_package not in resolved_candidate.parents and resolved_candidate != resolved_package:
        raise ValueError(f"module path escapes skill package: {module}")
    if not resolved_candidate.exists() or not resolved_candidate.is_file():
        raise ValueError(f"skill module not found: {module}")
    return resolved_candidate


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot import skill module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
