from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Literal

from app.prompting import PromptRegistry
from app.tools.ask_user import run_ask_user
from app.tools.business_knowledge import run_business_knowledge_search
from app.tools.common import ToolExecutionRequest, ToolExecutionResult
from app.tools.deliver_file import run_deliver_file
from app.tools.file_read import run_read_file, run_search_files
from app.tools.obsidian_wiki import (
    run_obsidian_wiki_apply,
    run_obsidian_wiki_draft,
    run_obsidian_wiki_maintain,
    run_obsidian_wiki_query,
)
from app.tools.shell import run_shell_command, run_shell_inspect
from app.tools.scheduled_task import run_scheduled_task
from app.tools.skill_guidance import run_load_skill
from app.tools.tavily import run_tavily_search
from app.tools.tool_search import run_tool_search
from app.tools.write_file import run_write_file
from app.tools.x_search import run_x_search

RiskLevel = Literal["low", "medium", "high", "critical"]
ExecutionMode = Literal["direct", "proposal"]
ToolHandler = Callable[[ToolExecutionRequest], ToolExecutionResult]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    args_schema: dict[str, Any]
    handler: ToolHandler
    risk_level: RiskLevel = "low"
    exposed_to_llm: bool = True
    execution_mode: ExecutionMode = "direct"
    requires_explicit_user_command: bool = False
    can_modify_files: bool = False
    requires_workdir: bool = False


def _tool_description(tool_name: str) -> str:
    value = _tool_prompt_catalog().get(tool_name, {}).get("description")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing tool prompt description for {tool_name}")
    return value.strip()


def _property_description(tool_name: str, property_name: str) -> str:
    properties = _tool_prompt_catalog().get(tool_name, {}).get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    value = properties.get(property_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing tool prompt property description for {tool_name}.{property_name}")
    return value.strip()


def _tool_prompt_catalog() -> dict[str, Any]:
    bundle = PromptRegistry().load("tool_definitions")
    return _load_tool_prompt_catalog(str(bundle.version_dir), bundle.fingerprint)


@lru_cache(maxsize=16)
def _load_tool_prompt_catalog(version_dir: str, fingerprint: str) -> dict[str, Any]:
    del fingerprint
    path = Path(version_dir) / "catalog.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("tool_definitions catalog must be a JSON object")
    return payload


def builtin_tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="read_file",
            description=_tool_description("read_file"),
            args_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": _property_description("read_file", "path"),
                    },
                    "start_line": {
                        "type": "integer",
                        "description": _property_description("read_file", "start_line"),
                        "default": 1,
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": _property_description("read_file", "max_lines"),
                        "default": 200,
                    },
                },
                "required": ["path"],
            },
            handler=run_read_file,
            risk_level="low",
        ),
        ToolDefinition(
            name="search_files",
            description=_tool_description("search_files"),
            args_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": _property_description("search_files", "query"),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["path", "content"],
                        "description": _property_description("search_files", "mode"),
                        "default": "path",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": _property_description("search_files", "max_results"),
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
            handler=run_search_files,
            risk_level="low",
        ),
        ToolDefinition(
            name="shell_inspect",
            description=_tool_description("shell_inspect"),
            args_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "workdir": {"type": "string"},
                },
                "required": ["command"],
            },
            handler=run_shell_inspect,
        ),
        ToolDefinition(
            name="shell_run_command",
            description=_tool_description("shell_run_command"),
            args_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "workdir": {"type": "string"},
                },
                "required": ["command"],
            },
            handler=run_shell_command,
            risk_level="medium",
            requires_explicit_user_command=True,
        ),
        ToolDefinition(
            name="ask_user",
            description=_tool_description("ask_user"),
            args_schema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": _property_description("ask_user", "question"),
                    },
                    "reason": {
                        "type": "string",
                        "description": _property_description("ask_user", "reason"),
                    },
                    "expected_answer_type": {
                        "type": "string",
                        "enum": ["free_text", "yes_no", "choice"],
                        "default": "free_text",
                    },
                    "choices": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": _property_description("ask_user", "choices"),
                    },
                },
                "required": ["question"],
            },
            handler=run_ask_user,
            risk_level="low",
        ),
        ToolDefinition(
            name="tool_search",
            description=_tool_description("tool_search"),
            args_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": _property_description("tool_search", "query"),
                    },
                    "original_user_request": {
                        "type": "string",
                        "description": _property_description("tool_search", "original_user_request"),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": _property_description("tool_search", "max_results"),
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
            handler=run_tool_search,
            risk_level="low",
        ),
        ToolDefinition(
            name="Skill",
            description=_tool_description("Skill"),
            args_schema={
                "type": "object",
                "properties": {
                    "skill": {
                        "type": "string",
                        "description": _property_description("Skill", "skill"),
                    },
                    "args": {
                        "description": _property_description("Skill", "args"),
                    },
                },
                "required": ["skill"],
            },
            handler=run_load_skill,
            risk_level="low",
        ),
        ToolDefinition(
            name="obsidian_wiki_query",
            description=_tool_description("obsidian_wiki_query"),
            args_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "query_mode": {
                        "type": "string",
                        "enum": ["wiki_only", "wiki_then_raw", "raw_only"],
                        "default": "wiki_then_raw",
                    },
                    "workspace_path": {
                        "type": "string",
                        "description": _property_description("obsidian_wiki_query", "workspace_path"),
                    },
                    "vault_path": {
                        "type": "string",
                        "description": _property_description("obsidian_wiki_query", "vault_path"),
                    },
                },
                "required": ["query"],
            },
            handler=run_obsidian_wiki_query,
        ),
        ToolDefinition(
            name="obsidian_wiki_draft",
            description=_tool_description("obsidian_wiki_draft"),
            args_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "page_type": {
                        "type": "string",
                        "enum": ["index", "design", "decision", "concept", "playbook"],
                    },
                    "content": {"type": "string"},
                    "source_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "target_hint": {"type": "string"},
                    "workspace_path": {
                        "type": "string",
                        "description": _property_description("obsidian_wiki_draft", "workspace_path"),
                    },
                    "vault_path": {
                        "type": "string",
                        "description": _property_description("obsidian_wiki_draft", "vault_path"),
                    },
                },
                "required": ["title", "page_type", "content"],
            },
            handler=run_obsidian_wiki_draft,
        ),
        ToolDefinition(
            name="obsidian_wiki_apply",
            description=_tool_description("obsidian_wiki_apply"),
            args_schema={
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string"},
                    "target_page": {"type": "string"},
                    "workspace_path": {
                        "type": "string",
                        "description": _property_description("obsidian_wiki_apply", "workspace_path"),
                    },
                    "vault_path": {
                        "type": "string",
                        "description": _property_description("obsidian_wiki_apply", "vault_path"),
                    },
                },
                "required": ["draft_id"],
            },
            handler=run_obsidian_wiki_apply,
        ),
        ToolDefinition(
            name="obsidian_wiki_maintain",
            description=_tool_description("obsidian_wiki_maintain"),
            args_schema={
                "type": "object",
                "properties": {
                    "workspace_path": {
                        "type": "string",
                        "description": _property_description("obsidian_wiki_maintain", "workspace_path"),
                    },
                    "vault_path": {
                        "type": "string",
                        "description": _property_description("obsidian_wiki_maintain", "vault_path"),
                    },
                },
            },
            handler=run_obsidian_wiki_maintain,
        ),
        ToolDefinition(
            name="business_knowledge_search",
            description=_tool_description("business_knowledge_search"),
            args_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "mode": {
                        "type": "string",
                        "enum": ["bm25", "vector", "hybrid", "rrf", "rrf_v2", "rrf_v2_rerank"],
                        "default": "rrf_v2",
                    },
                    "language": {
                        "type": "string",
                        "description": _property_description("business_knowledge_search", "language"),
                    },
                    "chunk_profile_id": {"type": "string"},
                    "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                    "source_type": {
                        "type": "string",
                        "description": _property_description("business_knowledge_search", "source_type"),
                    },
                    "source_id": {"type": "string", "description": _property_description("business_knowledge_search", "source_id")},
                    "source_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": _property_description("business_knowledge_search", "source_ids"),
                    },
                    "ticker": {"type": "string", "description": _property_description("business_knowledge_search", "ticker")},
                    "company_name": {"type": "string", "description": _property_description("business_knowledge_search", "company_name")},
                    "form_type": {"type": "string", "description": _property_description("business_knowledge_search", "form_type")},
                    "fiscal_year": {"type": "integer", "description": _property_description("business_knowledge_search", "fiscal_year")},
                    "section_title": {"type": "string", "description": _property_description("business_knowledge_search", "section_title")},
                },
                "required": ["query"],
            },
            handler=run_business_knowledge_search,
        ),
        ToolDefinition(
            name="write_file",
            description=_tool_description("write_file"),
            args_schema={
                "type": "object",
                "properties": {
                    "relative_path": {
                        "type": "string",
                        "description": _property_description("write_file", "relative_path"),
                    },
                    "content": {
                        "type": "string",
                        "description": _property_description("write_file", "content"),
                    },
                },
                "required": ["relative_path", "content"],
            },
            handler=run_write_file,
            risk_level="medium",
        ),
        ToolDefinition(
            name="scheduled_task",
            description=_tool_description("scheduled_task"),
            args_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "list", "remove"]},
                    "job_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "prompt": {"type": "string"},
                    "run_at": {"type": "string"},
                    "source_time_text": {"type": "string"},
                    "timezone": {"type": "string"},
                },
                "required": ["action"],
            },
            handler=run_scheduled_task,
            risk_level="low",
        ),
        ToolDefinition(
            name="deliver_file",
            description=_tool_description("deliver_file"),
            args_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {
                        "type": "string",
                        "description": _property_description("deliver_file", "artifact_id"),
                    },
                    "path": {
                        "type": "string",
                        "description": _property_description("deliver_file", "path"),
                    },
                    "file_path": {
                        "type": "string",
                        "description": _property_description("deliver_file", "file_path"),
                    },
                    "filename": {
                        "type": "string",
                        "description": _property_description("deliver_file", "filename"),
                    },
                },
                "anyOf": [
                    {"required": ["artifact_id"]},
                    {"required": ["path"]},
                    {"required": ["file_path"]},
                ],
            },
            handler=run_deliver_file,
            risk_level="medium",
        ),
        ToolDefinition(
            name="x_search",
            description=_tool_description("x_search"),
            args_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": _property_description("x_search", "query"),
                    },
                    "handles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": _property_description("x_search", "handles"),
                    },
                    "exclude_handles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": _property_description("x_search", "exclude_handles"),
                    },
                    "date_from": {
                        "type": "string",
                        "description": _property_description("x_search", "date_from"),
                    },
                    "date_to": {
                        "type": "string",
                        "description": _property_description("x_search", "date_to"),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": _property_description("x_search", "max_results"),
                        "default": 10,
                    },
                    "model": {
                        "type": "string",
                        "description": _property_description("x_search", "model"),
                    },
                },
                "required": ["query"],
            },
            handler=run_x_search,
            risk_level="low",
            execution_mode="direct",
        ),
        ToolDefinition(
            name="tavily_search",
            description=_tool_description("tavily_search"),
            args_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": _property_description("tavily_search", "query"),
                    },
                    "search_depth": {
                        "type": "string",
                        "enum": ["basic", "advanced"],
                        "description": _property_description("tavily_search", "search_depth"),
                        "default": "basic",
                    },
                    "topic": {
                        "type": "string",
                        "enum": ["general", "news"],
                        "description": _property_description("tavily_search", "topic"),
                        "default": "general",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": _property_description("tavily_search", "max_results"),
                        "default": 5,
                    },
                    "include_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": _property_description("tavily_search", "include_domains"),
                    },
                    "exclude_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": _property_description("tavily_search", "exclude_domains"),
                    },
                },
                "required": ["query"],
            },
            handler=run_tavily_search,
            risk_level="low",
            execution_mode="direct",
        ),
    ]
