from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from app.prompting import PromptRegistry
from app.task_runtime.planner import TurnPlanner
from app.tools.runtime import build_llm_tools


def test_prompt_registry_loads_project_prompt_and_renders_messages() -> None:
    bundle = PromptRegistry().load("fast_intent", "v1")

    messages = bundle.render({"input_json": json.dumps({"message": "hello"}, ensure_ascii=False)})

    assert bundle.id == "fast_intent:v1"
    assert bundle.response_format is None
    assert len(bundle.fingerprint) == 64
    assert messages[0].role == "system"
    assert "FastIntentNode" in messages[0].content
    assert "needs_plan virtual routing tool" in messages[0].content
    assert json.loads(messages[1].content)["message"] == "hello"


def test_prompt_registry_uses_project_default_versions() -> None:
    bundle = PromptRegistry().load("heavy_plan")

    assert bundle.id == "heavy_plan:v4"


def test_project_prompt_config_defaults_are_loadable() -> None:
    registry = PromptRegistry()

    for scenario, version in registry.config.default_versions.items():
        bundle = registry.load(scenario, version)
        assert bundle.id == f"{scenario}:{version}"
        assert len(bundle.fingerprint) == 64


def test_prompt_resources_are_included_in_wheel_build() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]

    assert force_include["prompt"] == "prompt"


def test_turn_planner_default_prompt_version_follows_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_PROMPT_PROFILE", "planner_v1")

    metadata = TurnPlanner().prompt_metadata()

    assert metadata["prompt_id"] == "heavy_plan:v1"


def test_tool_definitions_prompt_catalog_populates_llm_tool_schema() -> None:
    tools = build_llm_tools(allowed_tools={"read_file"})
    read_file = tools[0]["function"]

    assert PromptRegistry().load("tool_definitions").id == "tool_definitions:v1"
    assert read_file["description"].startswith("Read a known local workspace file")
    assert read_file["parameters"]["properties"]["path"]["description"].startswith("Workspace-relative file path")


def test_prompt_registry_uses_active_version_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_prompt_version(tmp_path, scenario="demo_scene", version="v2", system_text="Demo {{name}}")
    monkeypatch.setenv("JARVIS_PROMPT_DEMO_SCENE_VERSION", "v2")

    bundle = PromptRegistry(root=tmp_path).load("demo_scene")
    messages = bundle.render({"name": "prompt"})

    assert bundle.id == "demo_scene:v2"
    assert messages[0].content == "Demo prompt"


def test_prompt_registry_uses_prompt_profile_from_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_prompt_version(tmp_path, scenario="demo_scene", version="v1", system_text="Demo v1", variables=[])
    _write_prompt_version(tmp_path, scenario="demo_scene", version="v2", system_text="Demo v2", variables=[])
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "default_versions": {"demo_scene": "v1"},
                "profiles": {"candidate": {"demo_scene": "v2"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVIS_PROMPT_PROFILE", "candidate")

    bundle = PromptRegistry(root=tmp_path).load("demo_scene")

    assert bundle.id == "demo_scene:v2"
    assert bundle.render({})[0].content == "Demo v2"


def test_prompt_registry_uses_prompt_versions_mapping(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_prompt_version(tmp_path, scenario="demo_scene", version="v1", system_text="Demo v1", variables=[])
    _write_prompt_version(tmp_path, scenario="demo_scene", version="v2", system_text="Demo v2", variables=[])
    (tmp_path / "config.json").write_text(
        json.dumps({"default_versions": {"demo_scene": "v1"}, "profiles": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVIS_PROMPT_VERSIONS", "demo_scene=v2")

    bundle = PromptRegistry(root=tmp_path).load("demo_scene")

    assert bundle.id == "demo_scene:v2"


def test_prompt_registry_rejects_missing_variables(tmp_path: Path) -> None:
    _write_prompt_version(tmp_path, scenario="demo_scene", version="v1", system_text="Demo {{name}}")
    bundle = PromptRegistry(root=tmp_path).load("demo_scene", "v1")

    with pytest.raises(ValueError, match="Missing prompt variables"):
        bundle.render({})


def test_prompt_registry_renders_conditional_sections(tmp_path: Path) -> None:
    _write_prompt_version(
        tmp_path,
        scenario="demo_scene",
        version="v1",
        system_text="{{#enabled}}Enabled {{name}}{{/enabled}}{{^enabled}}Disabled{{/enabled}}",
        variables=["name", "enabled"],
    )
    bundle = PromptRegistry(root=tmp_path).load("demo_scene", "v1")

    assert bundle.render({"name": "prompt", "enabled": True})[0].content == "Enabled prompt"
    assert bundle.render({"name": "prompt", "enabled": False})[0].content == "Disabled"


def _write_prompt_version(
    tmp_path: Path,
    *,
    scenario: str,
    version: str,
    system_text: str,
    variables: list[str] | None = None,
) -> None:
    scenario_dir = tmp_path / "scenarios" / scenario
    version_dir = scenario_dir / "versions" / version
    version_dir.mkdir(parents=True, exist_ok=True)
    (scenario_dir / "schema.json").write_text("{}", encoding="utf-8")
    (version_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": f"{scenario}:{version}",
                "scenario": scenario,
                "version": version,
                "response_format": "text",
                "variables": ["name"] if variables is None else variables,
                "messages": [{"role": "system", "template": "system.md"}],
            }
        ),
        encoding="utf-8",
    )
    (version_dir / "system.md").write_text(system_text, encoding="utf-8")
