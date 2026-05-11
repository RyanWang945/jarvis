from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.config import Settings


class LLMNode(str, Enum):
    AGENT_STEP = "agent_step"
    INTENT_CLASSIFIER = "intent_classifier"
    PLANNER = "planner"
    PLAN_REPAIR = "plan_repair"
    VERIFIER = "verifier"
    CONTEXT_COMPRESSION = "context_compression"
    SUMMARY = "summary"


@dataclass(frozen=True)
class ModelProfile:
    id: str
    label: str
    provider: str
    model: str
    base_url: str
    api_key: str | None
    supports_tools: bool
    supports_json_object: bool
    supports_reasoning_content: bool
    supports_usage: bool


@dataclass(frozen=True)
class LLMNodePolicy:
    node: LLMNode
    requires_tools: bool = False
    prefers_json_object: bool = False
    allows_reasoning_content: bool = False
    timeout_seconds: float | None = None
    fallback_profile_id: str | None = None


def node_policy(node: LLMNode, settings: Settings) -> LLMNodePolicy:
    if node is LLMNode.AGENT_STEP:
        return LLMNodePolicy(
            node=node,
            requires_tools=True,
            allows_reasoning_content=True,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    if node is LLMNode.INTENT_CLASSIFIER:
        return LLMNodePolicy(
            node=node,
            prefers_json_object=True,
            timeout_seconds=min(float(settings.llm_timeout_seconds), 30.0),
            fallback_profile_id=getattr(settings, "default_classifier_profile", None),
        )
    if node is LLMNode.PLANNER:
        return LLMNodePolicy(
            node=node,
            prefers_json_object=True,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    return LLMNodePolicy(node=node, timeout_seconds=settings.llm_timeout_seconds)


def model_profiles(settings: Settings) -> dict[str, ModelProfile]:
    profiles: list[ModelProfile] = []
    for model in _available_models(settings.deepseek_available_models, settings.deepseek_model):
        profiles.append(
            ModelProfile(
                id=model,
                label=f"DeepSeek {model}",
                provider="deepseek",
                model=model,
                base_url=str(settings.deepseek_base_url),
                api_key=settings.deepseek_api_key,
                supports_tools=True,
                supports_json_object=True,
                supports_reasoning_content=True,
                supports_usage=True,
            )
        )
    for model in _available_models(settings.kimi_available_models, settings.kimi_model):
        profiles.append(
            ModelProfile(
                id=model,
                label=f"Kimi {model}",
                provider="kimi",
                model=model,
                base_url=str(settings.kimi_base_url),
                api_key=settings.kimi_api_key,
                supports_tools=True,
                supports_json_object=True,
                supports_reasoning_content=False,
                supports_usage=True,
            )
        )
    for model in _available_models(settings.gemini_available_models, settings.gemini_model):
        profiles.append(
            ModelProfile(
                id=model,
                label=f"Gemini {model}",
                provider="gemini",
                model=model,
                base_url=str(settings.gemini_base_url),
                api_key=settings.gemini_api_key,
                supports_tools=True,
                supports_json_object=False,
                supports_reasoning_content=False,
                supports_usage=True,
            )
        )
    result: dict[str, ModelProfile] = {}
    for profile in profiles:
        if profile.id not in result:
            result[profile.id] = profile
    return result


def default_profile_id(settings: Settings) -> str:
    configured = getattr(settings, "default_model_profile", None)
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    provider = (settings.llm_provider or "deepseek").strip().lower()
    model = getattr(settings, f"{provider}_model", None)
    if isinstance(model, str) and model.strip():
        return model.strip()
    return str(settings.deepseek_model)


def active_profile_id(metadata: dict[str, Any] | None, settings: Settings) -> str:
    if isinstance(metadata, dict):
        value = metadata.get("active_model_profile")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default_profile_id(settings)


def render_model_status(metadata: dict[str, Any] | None, settings: Settings) -> str:
    profiles = model_profiles(settings)
    active_id = active_profile_id(metadata, settings)
    active = profiles.get(active_id) or profiles.get(default_profile_id(settings))
    active_text = active.id if active is not None else active_id
    agent_step = _override_or_active(metadata, LLMNode.AGENT_STEP, active_text)
    classifier = _override_or_classifier_default(metadata, settings, active_text)
    planner = _override_or_active(metadata, LLMNode.PLANNER, active_text)
    loop = runtime_loop_provider(metadata)
    return (
        f"LLM: {active_text}\n"
        f"Loop: {loop}\n"
        f"Agent step: {agent_step}\n"
        f"Intent classifier: {classifier}\n"
        f"Planner: {planner}"
    )


def render_model_list(metadata: dict[str, Any] | None, settings: Settings) -> str:
    active = active_profile_id(metadata, settings)
    lines = ["可用模型:"]
    available = [profile for profile in model_profiles(settings).values() if profile.api_key]
    for profile in available:
        marker = "*" if profile.id == active else "-"
        lines.append(f"{marker} {profile.id} ({profile.provider})")
    if not available:
        lines.append("- 当前没有配置可用模型 API key")
    lines.append("")
    lines.append("使用 `/model <id>` 切换模型。")
    return "\n".join(lines)


def model_command_response(
    content: str,
    metadata: dict[str, Any] | None,
    settings: Settings,
) -> tuple[str, str, dict[str, Any] | None]:
    parts = content.strip().split(maxsplit=1)
    if len(parts) == 1:
        return "model_report", render_model_list(metadata, settings), None

    requested = parts[1].strip()
    profiles = model_profiles(settings)
    profile = profiles.get(requested)
    if profile is None:
        reply = f"未知模型: {requested}\n\n{render_model_list(metadata, settings)}"
        return "model_report", reply, None
    if not profile.api_key:
        reply = f"模型 `{requested}` 尚未配置 API key，不能切换。\n\n{render_model_list(metadata, settings)}"
        return "model_report", reply, None
    patch = {"active_model_profile": profile.id}
    return "model_updated", f"已切换到模型: {profile.id}", patch


def runtime_preferences_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    kept: dict[str, Any] = {}
    for key in ("active_model_profile", "runtime_profile", "model_overrides"):
        value = metadata.get(key)
        if value is not None:
            kept[key] = value
    return kept


def runtime_loop_provider(metadata: dict[str, Any] | None) -> str:
    if not isinstance(metadata, dict):
        return "react"
    runtime_profile = metadata.get("runtime_profile")
    if isinstance(runtime_profile, dict):
        loop = runtime_profile.get("loop_provider")
        if isinstance(loop, str) and loop.strip():
            return loop.strip()
    return "react"


def _override_or_active(metadata: dict[str, Any] | None, node: LLMNode, active: str) -> str:
    if not isinstance(metadata, dict):
        return active
    runtime_profile = metadata.get("runtime_profile")
    overrides = None
    if isinstance(runtime_profile, dict):
        overrides = runtime_profile.get("model_overrides")
    if overrides is None:
        overrides = metadata.get("model_overrides")
    if isinstance(overrides, dict):
        value = overrides.get(node.value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return active


def _override_or_classifier_default(metadata: dict[str, Any] | None, settings: Settings, active: str) -> str:
    override = _override_or_active(metadata, LLMNode.INTENT_CLASSIFIER, "")
    if override:
        return override
    configured = getattr(settings, "default_classifier_profile", None)
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return active


def _available_models(configured: str | None, fallback: str) -> list[str]:
    raw = configured if isinstance(configured, str) and configured.strip() else fallback
    models: list[str] = []
    for item in raw.split(","):
        model = item.strip()
        if model and model not in models:
            models.append(model)
    return models
