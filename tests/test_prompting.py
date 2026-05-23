from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.prompting import PromptRegistry


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


def test_prompt_registry_uses_active_version_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_prompt_version(tmp_path, scenario="demo_scene", version="v2", system_text="Demo {{name}}")
    monkeypatch.setenv("JARVIS_PROMPT_DEMO_SCENE_VERSION", "v2")

    bundle = PromptRegistry(root=tmp_path).load("demo_scene")
    messages = bundle.render({"name": "prompt"})

    assert bundle.id == "demo_scene:v2"
    assert messages[0].content == "Demo prompt"


def test_prompt_registry_rejects_missing_variables(tmp_path: Path) -> None:
    _write_prompt_version(tmp_path, scenario="demo_scene", version="v1", system_text="Demo {{name}}")
    bundle = PromptRegistry(root=tmp_path).load("demo_scene", "v1")

    with pytest.raises(ValueError, match="Missing prompt variables"):
        bundle.render({})


def _write_prompt_version(tmp_path: Path, *, scenario: str, version: str, system_text: str) -> None:
    scenario_dir = tmp_path / "scenarios" / scenario
    version_dir = scenario_dir / "versions" / version
    version_dir.mkdir(parents=True)
    (scenario_dir / "schema.json").write_text("{}", encoding="utf-8")
    (version_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": f"{scenario}:{version}",
                "scenario": scenario,
                "version": version,
                "response_format": "text",
                "variables": ["name"],
                "messages": [{"role": "system", "template": "system.md"}],
            }
        ),
        encoding="utf-8",
    )
    (version_dir / "system.md").write_text(system_text, encoding="utf-8")
