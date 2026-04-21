from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

RiskLevel = Literal["low", "medium", "high", "critical"]
IntentKind = Literal[
    "code_write",
    "code_review",
    "search_summary",
    "explicit_shell",
    "test_only",
    "simple_chat",
    "unknown",
]


class ToolSpec(BaseModel):
    name: str
    capability_name: str | None = None
    description: str
    args_schema: dict[str, Any] = Field(default_factory=dict)
    skill: str
    worker_type: str = ""
    action: str
    risk_level: RiskLevel = "low"
    exposed_to_llm: bool = False
    intent_kinds: list[IntentKind] = Field(default_factory=list)
    requires_explicit_user_command: bool = False
    can_modify_files: bool = False
    requires_workdir: bool = False

    @model_validator(mode="after")
    def default_worker_type(self) -> "ToolSpec":
        if not self.worker_type:
            self.worker_type = self.skill
        return self


class ToolCallPlan(BaseModel):
    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)
    title: str | None = None
    description: str | None = None
    dod: str | None = None
    verification_cmd: str | None = None
    max_retries: int = 0
