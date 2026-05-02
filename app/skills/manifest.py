from typing import Any

from pydantic import BaseModel, Field, model_validator


class SkillManifest(BaseModel):
    name: str
    description: str = ""
    version: str | None = None
    capabilities: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_metadata_wrappers(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        metadata = data.get("metadata")
        if isinstance(metadata, dict):
            data = dict(data)
            for key in ("name", "description", "version", "capabilities"):
                if key not in data and key in metadata:
                    data[key] = metadata[key]
        return data
