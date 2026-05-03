from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import get_settings


class RepositoryRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class RepositoryRef:
    repo_id: str
    name: str
    root_path: Path
    canonical_root_path: Path
    status: str = "active"
    permission_level: str = "coder"


class RepositoryRegistry:
    def __init__(self, repositories: list[RepositoryRef]) -> None:
        self._repositories = list(repositories)
        _assert_unique_repositories(self._repositories)
        self._by_id = {repo.repo_id: repo for repo in self._repositories}
        self._by_path = {_path_key(repo.canonical_root_path): repo for repo in self._repositories}

    @classmethod
    def default(cls) -> RepositoryRegistry:
        settings = get_settings()
        root = _canonicalize_existing_dir(settings.workspace_root)
        repositories = [
            RepositoryRef(
                repo_id="jarvis",
                name="Jarvis",
                root_path=settings.workspace_root,
                canonical_root_path=root,
                status="active",
                permission_level="coder",
            )
        ]
        repositories.extend(cls.from_config_file(settings.repositories_config_path).list_repositories())
        return cls(repositories)

    @classmethod
    def from_config_file(cls, path: str | Path | None) -> RepositoryRegistry:
        if path is None:
            return cls([])
        config_path = Path(path)
        if not config_path.exists():
            return cls([])
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RepositoryRegistryError(f"Repository config is invalid: {config_path}") from exc

        raw_repositories = payload.get("repositories") if isinstance(payload, dict) else None
        if not isinstance(raw_repositories, list):
            raise RepositoryRegistryError("Repository config must contain a repositories list.")
        return cls([_repository_from_config(item, index) for index, item in enumerate(raw_repositories)])

    def list_repositories(self) -> list[RepositoryRef]:
        return list(self._repositories)

    def resolve_repo(self, repo_id: str) -> RepositoryRef:
        repo = self._by_id.get(repo_id)
        if repo is None:
            raise RepositoryRegistryError(f"Repository is not registered or not authorized: {repo_id}")
        if repo.status != "active":
            raise RepositoryRegistryError(f"Repository is not active: {repo_id}")
        return repo

    def find_by_workdir(self, workdir: str | Path) -> RepositoryRef | None:
        try:
            canonical = _canonicalize_existing_dir(Path(workdir))
        except RepositoryRegistryError:
            return None
        repo = self._by_path.get(_path_key(canonical))
        if repo is None or repo.status != "active":
            return None
        return repo


@lru_cache
def get_repository_registry() -> RepositoryRegistry:
    return RepositoryRegistry.default()


def render_repository_report(registry: RepositoryRegistry | None = None) -> str:
    registry = registry or get_repository_registry()
    repositories = registry.list_repositories()
    if not repositories:
        return "Registered repositories:\n\n(none)"
    lines = ["Registered repositories:"]
    for repo in repositories:
        lines.extend(
            [
                "",
                f"- {repo.repo_id}",
                f"  name: {repo.name}",
                f"  path: {repo.canonical_root_path}",
                f"  permission: {repo.permission_level}",
                f"  status: {repo.status}",
            ]
        )
    return "\n".join(lines)


def _canonicalize_existing_dir(path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise RepositoryRegistryError(f"Repository path does not exist: {path}") from exc
    if not resolved.is_dir():
        raise RepositoryRegistryError(f"Repository path is not a directory: {path}")
    return resolved


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path))


def _repository_from_config(item: Any, index: int) -> RepositoryRef:
    if not isinstance(item, dict):
        raise RepositoryRegistryError(f"Repository config item #{index + 1} must be an object.")
    repo_id = str(item.get("repo_id") or "").strip()
    root_path_raw = str(item.get("root_path") or "").strip()
    if not repo_id:
        raise RepositoryRegistryError(f"Repository config item #{index + 1} is missing repo_id.")
    if not root_path_raw:
        raise RepositoryRegistryError(f"Repository config item #{index + 1} is missing root_path.")

    root_path = Path(root_path_raw)
    return RepositoryRef(
        repo_id=repo_id,
        name=str(item.get("name") or repo_id),
        root_path=root_path,
        canonical_root_path=_canonicalize_existing_dir(root_path),
        status=str(item.get("status") or "active"),
        permission_level=str(item.get("permission_level") or "coder"),
    )


def _assert_unique_repositories(repositories: list[RepositoryRef]) -> None:
    ids: set[str] = set()
    paths: set[str] = set()
    for repo in repositories:
        if repo.repo_id in ids:
            raise RepositoryRegistryError(f"Duplicate repository id: {repo.repo_id}")
        ids.add(repo.repo_id)
        path_key = _path_key(repo.canonical_root_path)
        if path_key in paths:
            raise RepositoryRegistryError(f"Duplicate repository path: {repo.canonical_root_path}")
        paths.add(path_key)
