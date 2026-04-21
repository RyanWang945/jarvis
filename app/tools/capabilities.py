from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.tools.registry import ToolRegistry, get_default_tool_registry
from app.tools.specs import IntentKind, RiskLevel, ToolSpec


class WorkerCapability(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    worker_type: str
    provider: str | None = None
    action: str
    description: str
    intents: list[IntentKind] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    risk_level: RiskLevel = "low"
    exposed_to_llm: bool = False
    requires_explicit_user_command: bool = False
    can_modify_files: bool = False
    requires_workdir: bool = False
    priority: int = 100

    @classmethod
    def from_tool_spec(cls, tool: ToolSpec, *, priority: int = 100) -> "WorkerCapability":
        canonical_name = tool.capability_name or _canonical_name(tool.name)
        aliases = [tool.name] if tool.name != canonical_name else []
        return cls(
            name=canonical_name,
            aliases=aliases,
            worker_type=tool.worker_type,
            provider=tool.skill,
            action=tool.action,
            description=tool.description,
            intents=list(tool.intent_kinds),
            input_schema=dict(tool.args_schema),
            risk_level=tool.risk_level,
            exposed_to_llm=tool.exposed_to_llm,
            requires_explicit_user_command=tool.requires_explicit_user_command,
            can_modify_files=tool.can_modify_files,
            requires_workdir=tool.requires_workdir,
            priority=priority,
        )

    def to_tool_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.llm_tool_name,
            capability_name=self.name,
            description=self.description,
            args_schema=self.input_schema,
            skill=self.provider or self.worker_type,
            worker_type=self.worker_type,
            action=self.action,
            risk_level=self.risk_level,
            exposed_to_llm=self.exposed_to_llm,
            intent_kinds=list(self.intents),
            requires_explicit_user_command=self.requires_explicit_user_command,
            can_modify_files=self.can_modify_files,
            requires_workdir=self.requires_workdir,
        )

    @property
    def llm_tool_name(self) -> str:
        return self.aliases[0] if self.aliases else self.name


class CapabilityRegistry:
    def __init__(self, capabilities: list[WorkerCapability]) -> None:
        self._capabilities = {capability.name: capability for capability in capabilities}
        self._aliases: dict[str, str] = {}
        for capability in capabilities:
            for alias in capability.aliases:
                if alias in self._aliases:
                    raise ValueError(f"duplicate capability alias: {alias}")
                self._aliases[alias] = capability.name

    @classmethod
    def from_tool_registry(cls, registry: ToolRegistry) -> "CapabilityRegistry":
        capabilities = [
            WorkerCapability.from_tool_spec(tool, priority=_default_priority(tool))
            for tool in registry.list()
        ]
        return cls(capabilities)

    def get(self, name: str) -> WorkerCapability:
        resolved_name = self._aliases.get(name, name)
        try:
            return self._capabilities[resolved_name]
        except KeyError as exc:
            raise ValueError(f"unknown capability: {name}") from exc

    def resolve_name(self, name: str) -> str:
        return self.get(name).name

    def list(
        self,
        *,
        exposed_to_llm: bool | None = None,
        intent_kinds: list[IntentKind] | None = None,
    ) -> list[WorkerCapability]:
        capabilities = list(self._capabilities.values())
        if exposed_to_llm is not None:
            capabilities = [
                capability
                for capability in capabilities
                if capability.exposed_to_llm is exposed_to_llm
            ]
        if intent_kinds is not None:
            intent_set = set(intent_kinds)
            capabilities = [
                capability
                for capability in capabilities
                if intent_set.intersection(capability.intents)
            ]
        return sorted(capabilities, key=lambda capability: (capability.priority, capability.name))

    def tool_specs(
        self,
        *,
        exposed_to_llm: bool | None = None,
        intent_kinds: list[IntentKind] | None = None,
    ) -> list[ToolSpec]:
        return [
            capability.to_tool_spec()
            for capability in self.list(
                exposed_to_llm=exposed_to_llm,
                intent_kinds=intent_kinds,
            )
        ]

    def names_for_intent(self, intent_kind: IntentKind) -> list[str]:
        return [
            capability.name
            for capability in self.list(exposed_to_llm=True, intent_kinds=[intent_kind])
        ]

    def default_name_for_intent(self, intent_kind: IntentKind) -> str | None:
        names = self.names_for_intent(intent_kind)
        return names[0] if names else None


def get_default_capability_registry() -> CapabilityRegistry:
    return CapabilityRegistry.from_tool_registry(get_default_tool_registry())


def _default_priority(tool: ToolSpec) -> int:
    priorities = {
        "coder.claude_code": 10,
        "search.tavily": 10,
        "shell.command": 10,
        "shell.test": 10,
        "answer.echo": 10,
    }
    return priorities.get(_canonical_name(tool.name), 100)


def _canonical_name(tool_name: str) -> str:
    aliases = {
        "delegate_to_claude_code": "coder.claude_code",
        "tavily_search": "search.tavily",
        "run_shell_command": "shell.command",
        "run_tests": "shell.test",
        "echo": "answer.echo",
    }
    return aliases.get(tool_name, tool_name)
