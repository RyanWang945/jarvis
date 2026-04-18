from typing import Any, Literal

from pydantic import BaseModel, Field

RiskLevel = Literal["low", "medium", "high", "critical"]


class ToolSpec(BaseModel):
    name: str
    description: str
    args_schema: dict[str, Any] = Field(default_factory=dict)
    skill: str
    action: str
    risk_level: RiskLevel = "low"
    exposed_to_llm: bool = False


class ToolCallPlan(BaseModel):
    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)
    title: str | None = None
    description: str | None = None
    dod: str | None = None
    verification_cmd: str | None = None
    max_retries: int = 0
