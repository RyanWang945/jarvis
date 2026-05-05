from __future__ import annotations

from enum import Enum
from typing import Any


class TurnLoopProvider(str, Enum):
    REACT = "react"
    PLAN_EXECUTE = "plan_execute"
    RESEARCH = "research"
    CODING_REVIEW = "coding_review"


def resolve_turn_loop_provider(metadata: dict[str, Any] | None) -> TurnLoopProvider:
    if not isinstance(metadata, dict):
        return TurnLoopProvider.REACT
    runtime_profile = metadata.get("runtime_profile")
    value = None
    if isinstance(runtime_profile, dict):
        value = runtime_profile.get("loop_provider")
    if not isinstance(value, str) or not value.strip():
        return TurnLoopProvider.REACT
    try:
        return TurnLoopProvider(value.strip())
    except ValueError as exc:
        raise ValueError(f"Unsupported turn loop provider: {value}") from exc
