from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import Settings, get_settings
from app.llm.client import ChatClient
from app.llm.model_profiles import (
    LLMNode,
    LLMNodePolicy,
    ModelProfile,
    active_profile_id,
    default_profile_id,
    model_profiles,
    node_policy,
)


@dataclass(frozen=True)
class ResolvedLLM:
    node: LLMNode
    profile: ModelProfile
    policy: LLMNodePolicy
    client: ChatClient


class ModelRouter:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def resolve(self, node: LLMNode, conversation_metadata: dict[str, Any] | None = None) -> ResolvedLLM:
        profiles = model_profiles(self._settings)
        policy = node_policy(node, self._settings)
        profile_id = self._profile_id_for_node(node, conversation_metadata)
        profile = profiles.get(profile_id)
        if profile is None and policy.fallback_profile_id:
            profile = profiles.get(policy.fallback_profile_id)
        if profile is None:
            profile = profiles.get(default_profile_id(self._settings))
        if profile is None:
            raise ValueError(f"No model profile configured for node {node.value}")

        if policy.requires_tools and not profile.supports_tools:
            fallback = profiles.get(policy.fallback_profile_id or default_profile_id(self._settings))
            if fallback is None or not fallback.supports_tools:
                raise ValueError(f"Model profile {profile.id} does not support tools for node {node.value}")
            profile = fallback

        timeout = policy.timeout_seconds or self._settings.llm_timeout_seconds
        client = ChatClient(
            api_key=profile.api_key or "",
            base_url=profile.base_url,
            model=profile.model,
            timeout_seconds=timeout,
            provider=profile.provider,
            supports_reasoning_content=profile.supports_reasoning_content and policy.allows_reasoning_content,
        )
        return ResolvedLLM(node=node, profile=profile, policy=policy, client=client)

    def _profile_id_for_node(self, node: LLMNode, metadata: dict[str, Any] | None) -> str:
        override = _model_override(metadata, node)
        if override:
            return override
        return active_profile_id(metadata, self._settings)


def _model_override(metadata: dict[str, Any] | None, node: LLMNode) -> str | None:
    if not isinstance(metadata, dict):
        return None
    runtime_profile = metadata.get("runtime_profile")
    overrides = None
    if isinstance(runtime_profile, dict):
        overrides = runtime_profile.get("model_overrides")
    if overrides is None:
        overrides = metadata.get("model_overrides")
    if not isinstance(overrides, dict):
        return None
    value = overrides.get(node.value)
    return value.strip() if isinstance(value, str) and value.strip() else None
