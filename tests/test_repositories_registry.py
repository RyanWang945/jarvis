from pathlib import Path

import pytest

from app.repositories import (
    RepositoryRef,
    RepositoryRegistry,
    RepositoryRegistryError,
    render_repository_report,
)


def test_repository_registry_resolves_active_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = RepositoryRegistry(
        [
            RepositoryRef(
                repo_id="jarvis",
                name="Jarvis",
                root_path=repo,
                canonical_root_path=repo.resolve(),
            )
        ]
    )

    resolved = registry.resolve_repo("jarvis")

    assert resolved.repo_id == "jarvis"
    assert registry.find_by_workdir(repo).repo_id == "jarvis"


def test_repository_registry_rejects_unknown_repo(tmp_path: Path) -> None:
    registry = RepositoryRegistry([])

    with pytest.raises(RepositoryRegistryError):
        registry.resolve_repo("unknown")


def test_repository_registry_does_not_match_unregistered_workdir(tmp_path: Path) -> None:
    registered = tmp_path / "registered"
    unknown = tmp_path / "unknown"
    registered.mkdir()
    unknown.mkdir()
    registry = RepositoryRegistry(
        [
            RepositoryRef(
                repo_id="jarvis",
                name="Jarvis",
                root_path=registered,
                canonical_root_path=registered.resolve(),
            )
        ]
    )

    assert registry.find_by_workdir(unknown) is None


def test_repository_report_lists_registered_repositories(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = RepositoryRegistry(
        [
            RepositoryRef(
                repo_id="jarvis",
                name="Jarvis",
                root_path=repo,
                canonical_root_path=repo.resolve(),
            )
        ]
    )

    report = render_repository_report(registry)

    assert "Registered repositories:" in report
    assert "- jarvis" in report
    assert "permission: coder" in report


def test_repository_registry_loads_json_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "repositories.json"
    config.write_text(
        f"""
        {{
          "repositories": [
            {{
              "repo_id": "project-a",
              "name": "Project A",
              "root_path": "{repo.as_posix()}",
              "permission_level": "coder",
              "status": "active"
            }}
          ]
        }}
        """,
        encoding="utf-8",
    )

    registry = RepositoryRegistry.from_config_file(config)

    resolved = registry.resolve_repo("project-a")
    assert resolved.name == "Project A"
    assert resolved.canonical_root_path == repo.resolve()


def test_repository_registry_rejects_duplicate_repo_ids(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()

    with pytest.raises(RepositoryRegistryError, match="Duplicate repository id"):
        RepositoryRegistry(
            [
                RepositoryRef(repo_id="dup", name="One", root_path=one, canonical_root_path=one.resolve()),
                RepositoryRef(repo_id="dup", name="Two", root_path=two, canonical_root_path=two.resolve()),
            ]
        )
