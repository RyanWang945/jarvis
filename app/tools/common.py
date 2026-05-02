from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolExecutionRequest:
    tool_name: str
    workdir: str | None
    args: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 30


@dataclass(frozen=True)
class ToolExecutionResult:
    ok: bool
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    artifacts: list[str] = field(default_factory=list)
    summary: str = ""

