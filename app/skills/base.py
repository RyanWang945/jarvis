from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

RiskLevel = Literal["low", "medium", "high", "critical"]


@dataclass(frozen=True)
class SkillRequest:
    skill: str
    action: str
    workdir: str | None
    args: dict[str, Any] = field(default_factory=dict)
    risk_level: RiskLevel = "low"
    timeout_seconds: int = 30


@dataclass(frozen=True)
class SkillResult:
    ok: bool
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    artifacts: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "artifacts": self.artifacts,
            "summary": self.summary,
        }


class Skill(Protocol):
    name: str

    def run(self, request: SkillRequest) -> SkillResult:
        ...
