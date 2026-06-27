from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.agent_react.context_manager import ConversationContext
from app.agent_react.session_state import ConversationSessionState
from app.llm.client import LLMMessage, parse_json_content
from app.llm.model_profiles import LLMNode
from app.llm.model_router import ModelRouter
from app.prompting import PromptRegistry
from app.runtime_usage import usage_record_from_response
from app.skills import get_skill_registry
from app.skills.skill import Skill
from app.task_runtime.runtime_context import RuntimeContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlannerSkillSelection:
    skill_id: str | None = None
    reason: str = ""
    guidance: str = ""
    usage_records: list[dict[str, Any]] | None = None

    def as_payload(self) -> dict[str, Any]:
        if not self.skill_id:
            return {}
        return {
            "skill_id": self.skill_id,
            "reason": self.reason,
            "planning_guidance": self.guidance,
        }


class PlannerSkillRouter:
    """Select at most one planner skill before the generic heavy planner runs."""

    def __init__(self, *, prompt_registry: PromptRegistry | None = None, prompt_version: str | None = None) -> None:
        self._prompt_registry = prompt_registry or PromptRegistry()
        self._prompt_version = prompt_version

    def prompt_metadata(self) -> dict[str, Any]:
        return self._prompt_registry.load("planner_skill_router", self._prompt_version).metadata()

    def select(
        self,
        *,
        content: str,
        session_state: ConversationSessionState | None = None,
        conversation_metadata: dict[str, Any] | None = None,
        conversation_context: ConversationContext | None = None,
        runtime_context: RuntimeContext | None = None,
    ) -> PlannerSkillSelection:
        candidates = _planner_skill_candidates()
        if not candidates:
            return PlannerSkillSelection(reason="no planner skills registered", usage_records=[])

        payload = _router_input(
            content=content,
            candidates=candidates,
            session_state=session_state,
            conversation_context=conversation_context,
            runtime_context=runtime_context,
        )
        resolved = ModelRouter().resolve(LLMNode.PLANNER, conversation_metadata)
        if not resolved.profile.api_key:
            logger.info("planner skill router skipped reason=missing_api_key profile=%s", resolved.profile.id)
            return PlannerSkillSelection(reason="missing api key", usage_records=[])

        prompt = self._prompt_registry.load("planner_skill_router", self._prompt_version)
        response_format = prompt.response_format if resolved.profile.supports_json_object else None
        response = resolved.client.chat_normalized(
            prompt.render({"input_json": json.dumps(payload, ensure_ascii=False)}),
            response_format=response_format,
        )
        parsed = parse_json_content({"content": response.content})
        selected = _selection_from_payload(parsed, candidates)
        usage_record = usage_record_from_response(response, stage="planner_skill_router")
        usage_records = [usage_record] if usage_record is not None else []
        return PlannerSkillSelection(
            skill_id=selected.skill_id,
            reason=selected.reason,
            guidance=selected.guidance,
            usage_records=usage_records,
        )


def _planner_skill_candidates() -> list[Skill]:
    try:
        skills = get_skill_registry().list()
    except Exception:
        logger.warning("planner skill registry unavailable", exc_info=True)
        return []
    return [
        skill
        for skill in skills
        if skill.manifest.is_planner_skill and skill.manifest.planning_guidance
    ]


def _router_input(
    *,
    content: str,
    candidates: list[Skill],
    session_state: ConversationSessionState | None,
    conversation_context: ConversationContext | None,
    runtime_context: RuntimeContext | None,
) -> dict[str, Any]:
    context_payload = (
        conversation_context.planner_payload()
        if conversation_context is not None
        else {"has_history": False, "context_reference_detected": False, "messages": []}
    )
    return {
        "current_user_input": content,
        "conversation_context": context_payload,
        "session": {
            "mode": getattr(session_state, "session_mode", None),
        },
        "runtime_context": (runtime_context or RuntimeContext.from_hints({})).to_legacy_hints(),
        "planner_skills": [
            {
                "skill_id": skill.skill_id,
                "description": skill.effective_description,
                "when_to_use": skill.manifest.when_to_use or "",
                "routing_summary": skill.manifest.routing_summary or skill.effective_description,
            }
            for skill in candidates
        ],
    }


def _selection_from_payload(payload: dict[str, Any], candidates: list[Skill]) -> PlannerSkillSelection:
    candidate_by_id = {skill.skill_id: skill for skill in candidates}
    selected = payload.get("selected_planner_skill")
    if isinstance(selected, dict):
        selected_id = str(selected.get("skill_id") or selected.get("id") or "").strip()
        reason = str(selected.get("reason") or payload.get("reason") or "").strip()
    else:
        selected_id = str(selected or payload.get("skill_id") or "").strip()
        reason = str(payload.get("reason") or "").strip()
    if selected_id in {"", "none", "null", "general"}:
        return PlannerSkillSelection(reason=reason or "no planner skill selected")
    skill = candidate_by_id.get(selected_id)
    if skill is None:
        return PlannerSkillSelection(reason=f"ignored unknown planner skill: {selected_id}")
    return PlannerSkillSelection(
        skill_id=skill.skill_id,
        reason=reason,
        guidance=skill.manifest.planning_guidance,
    )
