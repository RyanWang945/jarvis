from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.task_runtime.approval_types import approval_request_dicts
from app.task_runtime.planner import NodeRuntime

NodeStatus = Literal["completed", "failed", "blocked"]
ExecutionStatus = Literal["completed", "failed", "blocked"]


class NodeArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ref: str
    artifact_id: str | None = None
    kind: str = "artifact"
    name: str | None = None
    description: str = ""
    path: str | None = None
    session_relative_path: str | None = None
    mime_type: str | None = None
    filename: str | None = None
    size_bytes: int | None = None
    source_tool: str = ""
    publish: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ref")
    @classmethod
    def _ref_not_empty(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("artifact ref must not be empty")
        return text.removeprefix("artifact:")

    @field_validator("artifact_id")
    @classmethod
    def _artifact_id_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class NodeError(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class NodeResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    node_id: str
    runtime: NodeRuntime
    status: NodeStatus
    summary: str = ""
    artifacts: list[NodeArtifact] = Field(default_factory=list)
    approval_requests: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    usage_records: list[dict[str, Any]] = Field(default_factory=list)
    git: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)
    error: NodeError | None = None

    @field_validator("node_id")
    @classmethod
    def _node_id_not_empty(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("node_id must not be empty")
        return text

    @field_validator("approval_requests", mode="before")
    @classmethod
    def _approval_requests_list(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return approval_request_dicts(value)

    @field_validator("tool_calls", "usage_records", mode="before")
    @classmethod
    def _dict_list(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]


class ResolvedInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ref: str
    kind: Literal["artifact", "node_result"]
    summary: str = ""
    artifacts: list[NodeArtifact] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    source_status: NodeStatus | None = None


class ExecutionReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: ExecutionStatus
    node_results: list[NodeResult]
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def by_node_id(self) -> dict[str, NodeResult]:
        return {result.node_id: result for result in self.node_results}
