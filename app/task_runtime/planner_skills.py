from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from app.agent_react.context_manager import ConversationContext
from app.agent_react.session_state import ConversationSessionState
from app.llm.client import LLMMessage, parse_json_content
from app.llm.model_profiles import LLMNode
from app.llm.model_router import ModelRouter
from app.observability import (
    add_event,
    content_capture_enabled,
    record_exception,
    set_attributes,
    span_context,
    trace_preview,
)
from app.prompting import PromptRegistry
from app.runtime_usage import usage_record_from_response
from app.skills import get_skill_registry
from app.skills.skill import Skill
from app.task_runtime.runtime_context import RuntimeContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SelectedPlannerSkill:
    skill_id: str
    reason: str = ""
    guidance: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "reason": self.reason,
            "planning_guidance": self.guidance,
        }


@dataclass(frozen=True)
class PlannerSkillSelection:
    skill_id: str | None = None
    reason: str = ""
    guidance: str = ""
    selected_skills: tuple[SelectedPlannerSkill, ...] = ()
    usage_records: list[dict[str, Any]] | None = None

    @property
    def skills(self) -> tuple[SelectedPlannerSkill, ...]:
        if self.selected_skills:
            return self.selected_skills
        if not self.skill_id:
            return ()
        return (
            SelectedPlannerSkill(
                skill_id=self.skill_id,
                reason=self.reason,
                guidance=self.guidance,
            ),
        )

    def as_payload(self) -> dict[str, Any]:
        skills = self.skills
        if not skills:
            return {}
        return {
            "selected_planner_skills": [skill.as_payload() for skill in skills],
        }


class PlannerSkillRouter:
    """Select planner skills before the generic heavy planner runs."""

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
        started = time.perf_counter()
        candidates = _planner_skill_candidates()
        candidate_ids = _skill_ids(candidates)
        with span_context(
            "planner.skill_router",
            **{
                "jarvis.planner_skill_candidate_count": len(candidates),
                "jarvis.planner_skill_candidates": candidate_ids,
            },
        ):
            if not candidates:
                logger.info("planner skill router skipped reason=no_planner_skills_registered")
                add_event(
                    "planner_skill_router.skipped",
                    **{"jarvis.reason": "no_planner_skills_registered"},
                )
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
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                logger.info(
                    "planner skill router skipped reason=missing_api_key profile=%s "
                    "candidate_count=%s candidates=%s elapsed_ms=%s",
                    resolved.profile.id,
                    len(candidates),
                    candidate_ids,
                    elapsed_ms,
                )
                set_attributes(
                    **{
                        "jarvis.planner_skill_router_status": "skipped",
                        "jarvis.planner_skill_router_reason": "missing_api_key",
                        "jarvis.planner_skill_router_elapsed_ms": elapsed_ms,
                    }
                )
                add_event(
                    "planner_skill_router.skipped",
                    **{
                        "jarvis.reason": "missing_api_key",
                        "jarvis.profile": resolved.profile.id,
                        "jarvis.elapsed_ms": elapsed_ms,
                    },
                )
                return PlannerSkillSelection(reason="missing api key", usage_records=[])

            prompt = self._prompt_registry.load("planner_skill_router", self._prompt_version)
            response_format = prompt.response_format if resolved.profile.supports_json_object else None
            logger.info(
                "planner skill router llm request profile=%s provider=%s response_format=%s "
                "prompt_version=%s candidate_count=%s candidates=%s input_preview=%s",
                resolved.profile.id,
                resolved.profile.provider,
                response_format,
                prompt.version,
                len(candidates),
                candidate_ids,
                trace_preview(content, limit=160),
            )
            try:
                response = resolved.client.chat_normalized(
                    prompt.render({"input_json": json.dumps(payload, ensure_ascii=False)}),
                    response_format=response_format,
                )
                parsed = parse_json_content({"content": response.content})
            except Exception as exc:
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                logger.exception(
                    "planner skill router failed candidate_count=%s candidates=%s elapsed_ms=%s",
                    len(candidates),
                    candidate_ids,
                    elapsed_ms,
                )
                record_exception(exc, **{"jarvis.stage": "planner_skill_router"})
                set_attributes(
                    **{
                        "jarvis.planner_skill_router_status": "failed",
                        "jarvis.planner_skill_router_elapsed_ms": elapsed_ms,
                    }
                )
                raise

            selected = _selection_from_payload(parsed, candidates)
            usage_record = usage_record_from_response(response, stage="planner_skill_router")
            usage_records = [usage_record] if usage_record is not None else []
            first = selected.skills[0] if selected.skills else None
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            selected_ids = _selected_skill_ids(selected)
            ignored_reason = selected.reason if not selected_ids else ""
            logger.info(
                "planner skill router selected selected_skills=%s reason=%s candidate_count=%s "
                "candidates=%s model=%s finish_reason=%s content_len=%s elapsed_ms=%s",
                selected_ids,
                selected.reason,
                len(candidates),
                candidate_ids,
                response.model,
                response.finish_reason,
                len(response.content or ""),
                elapsed_ms,
            )
            if content_capture_enabled():
                logger.debug("planner skill router raw payload=%s", trace_preview(parsed, limit=1200))
            set_attributes(
                **{
                    "jarvis.planner_skill_router_status": "completed",
                    "jarvis.planner_skill_selected": selected_ids,
                    "jarvis.planner_skill_reason": trace_preview(selected.reason, limit=240),
                    "jarvis.planner_skill_router_elapsed_ms": elapsed_ms,
                }
            )
            add_event(
                "planner_skill_router.completed",
                **{
                    "jarvis.selected_skills": selected_ids,
                    "jarvis.reason": trace_preview(selected.reason, limit=240),
                    "jarvis.no_selection_reason": trace_preview(ignored_reason, limit=240),
                    "jarvis.candidate_count": len(candidates),
                    "jarvis.elapsed_ms": elapsed_ms,
                },
            )
            return PlannerSkillSelection(
                skill_id=first.skill_id if first is not None else None,
                reason=selected.reason,
                guidance=first.guidance if first is not None else "",
                selected_skills=selected.skills,
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
    raw_selected_many = payload.get("selected_planner_skills")
    selected_items: list[dict[str, Any]] = []
    if isinstance(raw_selected_many, list):
        selected_items = [item for item in raw_selected_many if isinstance(item, dict)]
    elif isinstance(raw_selected_many, dict):
        selected_items = [raw_selected_many]

    selected = payload.get("selected_planner_skill")
    if not selected_items and isinstance(selected, dict):
        selected_items = [selected]
    elif not selected_items and selected is not None:
        selected_items = [{"skill_id": selected, "reason": payload.get("reason") or ""}]
    elif not selected_items and payload.get("skill_id"):
        selected_items = [{"skill_id": payload.get("skill_id"), "reason": payload.get("reason") or ""}]

    if not selected_items:
        reason = str(payload.get("reason") or "").strip()
        return PlannerSkillSelection(reason=reason or "no planner skill selected")

    selected_skills: list[SelectedPlannerSkill] = []
    ignored: list[str] = []
    seen: set[str] = set()
    for item in selected_items:
        selected_id = str(item.get("skill_id") or item.get("id") or "").strip()
        reason = str(item.get("reason") or payload.get("reason") or "").strip()
        if selected_id in {"", "none", "null", "general"}:
            continue
        skill = candidate_by_id.get(selected_id)
        if skill is None:
            ignored.append(selected_id)
            continue
        if skill.skill_id in seen:
            continue
        seen.add(skill.skill_id)
        selected_skills.append(
            SelectedPlannerSkill(
                skill_id=skill.skill_id,
                reason=reason,
                guidance=skill.manifest.planning_guidance,
            )
        )

    if selected_skills:
        reason = "; ".join(skill.reason for skill in selected_skills if skill.reason)
        first = selected_skills[0]
        return PlannerSkillSelection(
            skill_id=first.skill_id,
            reason=reason,
            guidance=first.guidance,
            selected_skills=tuple(selected_skills),
        )
    if ignored:
        return PlannerSkillSelection(reason=f"ignored unknown planner skill: {', '.join(ignored)}")
    return PlannerSkillSelection(reason=str(payload.get("reason") or "no planner skill selected").strip())


def _skill_ids(skills: list[Skill]) -> list[str]:
    return [skill.skill_id for skill in skills]


def _selected_skill_ids(selection: PlannerSkillSelection) -> list[str]:
    return [skill.skill_id for skill in selection.skills]
