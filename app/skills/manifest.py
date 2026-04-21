from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.tools.specs import IntentKind, RiskLevel


class SkillToolManifest(BaseModel):
    name: str
    capability_name: str | None = None
    description: str
    args_schema: dict[str, Any] = Field(default_factory=dict)
    skill: str | None = None
    worker_type: str | None = None
    action: str
    risk_level: RiskLevel = "low"
    exposed_to_llm: bool = False
    intent_kinds: list[IntentKind] = Field(default_factory=list)
    requires_explicit_user_command: bool = False
    can_modify_files: bool = False
    requires_workdir: bool = False

    @model_validator(mode="after")
    def default_skill_and_worker(self) -> "SkillToolManifest":
        if self.skill is None:
            self.skill = self.worker_type
        if self.worker_type is None:
            self.worker_type = self.skill
        return self


class JarvisSkillExtension(BaseModel):
    module: str = "skill"
    class_name: str
    tools: list[SkillToolManifest] = Field(default_factory=list)


class SkillManifest(BaseModel):
    name: str
    description: str = ""
    version: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    jarvis: JarvisSkillExtension | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_metadata_wrappers(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        metadata = data.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("jarvis"), dict) and "jarvis" not in data:
            data = dict(data)
            data["jarvis"] = metadata["jarvis"]
        return data
