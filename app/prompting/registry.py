from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import get_settings
from app.llm.client import LLMMessage, LLMRole

_VARIABLE_PATTERN = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")


PromptResponseFormat = Literal["json_object", "text"]


class PromptMessageTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: LLMRole
    template: str


class PromptManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    scenario: str
    version: str
    description: str = ""
    model_profile: str | None = None
    response_format: PromptResponseFormat = "text"
    variables: list[str] = Field(default_factory=list)
    messages: list[PromptMessageTemplate]

    @field_validator("id", "scenario", "version")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("value must not be empty")
        return text

    @field_validator("variables")
    @classmethod
    def _dedupe_variables(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        return result

    @model_validator(mode="after")
    def _id_matches_scenario_version(self) -> PromptManifest:
        expected = f"{self.scenario}:{self.version}"
        if self.id != expected:
            raise ValueError(f"prompt manifest id must be {expected}")
        if not self.messages:
            raise ValueError("prompt manifest requires at least one message")
        return self


class PromptBundle:
    def __init__(
        self,
        *,
        manifest: PromptManifest,
        scenario_dir: Path,
        version_dir: Path,
        schema_path: Path | None,
    ) -> None:
        self.manifest = manifest
        self.scenario_dir = scenario_dir
        self.version_dir = version_dir
        self.schema_path = schema_path

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def scenario(self) -> str:
        return self.manifest.scenario

    @property
    def version(self) -> str:
        return self.manifest.version

    @property
    def response_format(self) -> dict[str, str] | None:
        if self.manifest.response_format == "json_object":
            return {"type": "json_object"}
        return None

    @property
    def fingerprint(self) -> str:
        parts = [
            _read_bytes(self.version_dir / "manifest.json"),
            *[_read_bytes(self.version_dir / message.template) for message in self.manifest.messages],
        ]
        if self.schema_path is not None and self.schema_path.exists():
            parts.append(_read_bytes(self.schema_path))
        digest = hashlib.sha256()
        for part in parts:
            digest.update(part)
            digest.update(b"\n")
        return digest.hexdigest()

    def render(self, context: dict[str, Any]) -> list[LLMMessage]:
        missing = [variable for variable in self.manifest.variables if variable not in context]
        if missing:
            raise ValueError(f"Missing prompt variables for {self.id}: {', '.join(missing)}")
        return [
            LLMMessage(
                role=message.role,
                content=_render_template((self.version_dir / message.template).read_text(encoding="utf-8"), context),
            )
            for message in self.manifest.messages
        ]

    def schema(self) -> dict[str, Any]:
        if self.schema_path is None or not self.schema_path.exists():
            return {}
        return json.loads(self.schema_path.read_text(encoding="utf-8"))

    def metadata(self) -> dict[str, Any]:
        return {
            "prompt_id": self.id,
            "prompt_scenario": self.scenario,
            "prompt_version": self.version,
            "prompt_sha256": self.fingerprint,
        }


class PromptRegistry:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or get_settings().workspace_root / "prompt"
        self._cache: dict[tuple[str, str], PromptBundle] = {}

    def load(self, scenario: str, version: str | None = None) -> PromptBundle:
        scenario_name = scenario.strip()
        if not scenario_name:
            raise ValueError("scenario must not be empty")
        selected_version = version or self.active_version(scenario_name)
        cache_key = (scenario_name, selected_version)
        if cache_key in self._cache:
            return self._cache[cache_key]
        scenario_dir = self.root / "scenarios" / scenario_name
        version_dir = scenario_dir / "versions" / selected_version
        manifest_path = version_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"Prompt manifest not found: {manifest_path}")
        manifest = PromptManifest.model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))
        schema_path = scenario_dir / "schema.json"
        bundle = PromptBundle(
            manifest=manifest,
            scenario_dir=scenario_dir,
            version_dir=version_dir,
            schema_path=schema_path if schema_path.exists() else None,
        )
        self._cache[cache_key] = bundle
        return bundle

    def active_version(self, scenario: str) -> str:
        env_name = "JARVIS_PROMPT_" + re.sub(r"[^A-Z0-9]+", "_", scenario.upper()).strip("_") + "_VERSION"
        return os.environ.get(env_name, "v1").strip() or "v1"


def _render_template(template: str, context: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in context:
            raise ValueError(f"Missing prompt variable: {key}")
        value = context[key]
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    return _VARIABLE_PATTERN.sub(replace, template).strip()


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()
