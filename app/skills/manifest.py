from typing import Any

from pydantic import BaseModel, Field, model_validator


class SkillManifest(BaseModel):
    name: str
    description: str = ""
    when_to_use: str | None = None
    version: str | None = None
    tags: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def accept_metadata_wrappers(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        metadata = data.get("metadata")
        if isinstance(metadata, dict):
            data = dict(data)
            for key in ("name", "description", "when_to_use", "version", "tags", "tools", "capabilities"):
                if key not in data and key in metadata:
                    data[key] = metadata[key]
        allowed_tools = data.get("allowed-tools")
        if "allowed_tools" not in data and allowed_tools is not None:
            data = dict(data)
            existing_metadata = data.get("metadata")
            if not isinstance(existing_metadata, dict):
                existing_metadata = {}
            data["metadata"] = {**existing_metadata, "allowed-tools": allowed_tools}
        return data
