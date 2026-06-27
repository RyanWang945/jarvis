from typing import Any

from pydantic import BaseModel, Field, model_validator


class SkillManifest(BaseModel):
    name: str | None = None
    description: str = ""
    effective_description: str | None = None
    when_to_use: str | None = None
    version: str | None = None
    tags: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    disable_model_invocation: bool = False
    user_invocable: bool = True
    allowed_tools: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def skill_type(self) -> str:
        value = self.metadata.get("skill_type", self.metadata.get("type"))
        return str(value or "runtime").strip().lower()

    @property
    def is_planner_skill(self) -> bool:
        return self.skill_type == "planner"

    @property
    def routing_summary(self) -> str:
        value = self.metadata.get("routing_summary")
        return str(value or "").strip()

    @property
    def planning_guidance(self) -> str:
        value = self.metadata.get("planning_guidance")
        return str(value or "").strip()

    @model_validator(mode="before")
    @classmethod
    def accept_metadata_wrappers(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        metadata = data.get("metadata")
        if isinstance(metadata, dict):
            data = dict(data)
            for key in (
                "name",
                "description",
                "effective_description",
                "when_to_use",
                "version",
                "tags",
                "tools",
                "capabilities",
                "disable_model_invocation",
                "user_invocable",
                "allowed_tools",
                "skill_type",
                "type",
                "routing_summary",
                "planning_guidance",
            ):
                if key not in data and key in metadata:
                    data[key] = metadata[key]
        if "when_to_use" not in data and "when-to-use" in data:
            data = dict(data)
            data["when_to_use"] = data["when-to-use"]
        if "disable_model_invocation" not in data and "disable-model-invocation" in data:
            data = dict(data)
            data["disable_model_invocation"] = data["disable-model-invocation"]
        if "user_invocable" not in data and "user-invocable" in data:
            data = dict(data)
            data["user_invocable"] = data["user-invocable"]
        allowed_tools = data.get("allowed-tools")
        if "allowed_tools" not in data and allowed_tools is not None:
            data = dict(data)
            existing_metadata = data.get("metadata")
            if not isinstance(existing_metadata, dict):
                existing_metadata = {}
            data["metadata"] = {**existing_metadata, "allowed-tools": allowed_tools}
            data["allowed_tools"] = allowed_tools
        metadata_keys = ("skill_type", "type", "routing_summary", "planning_guidance")
        if any(key in data for key in metadata_keys):
            data = dict(data)
            existing_metadata = data.get("metadata")
            if not isinstance(existing_metadata, dict):
                existing_metadata = {}
            data["metadata"] = {
                **existing_metadata,
                **{key: data[key] for key in metadata_keys if key in data},
            }
        for key in ("tags", "tools", "capabilities", "allowed_tools"):
            value = data.get(key)
            if isinstance(value, str):
                data = dict(data)
                data[key] = [item.strip() for item in value.split(",") if item.strip()]
        return data
