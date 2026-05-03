from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from app.tools.business_knowledge import run_business_knowledge_search
from app.tools.coder import run_coder_tool
from app.tools.codex import run_codex_coder_tool
from app.tools.common import ToolExecutionRequest, ToolExecutionResult
from app.tools.obsidian_wiki import (
    run_obsidian_wiki_apply,
    run_obsidian_wiki_draft,
    run_obsidian_wiki_maintain,
    run_obsidian_wiki_query,
)
from app.tools.shell import run_shell_command, run_shell_inspect
from app.tools.tavily import run_tavily_search
from app.tools.write_file import run_write_file

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


def builtin_tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="shell_inspect",
            description=(
                "Inspect the local environment and repository using read-only shell commands. "
                "Use this for listing files, reading file content, searching text, and checking git or tool status. "
                "Do NOT use this for web searches, factual lookups, or current events."
            ),
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
            description=(
                "Run one explicit local shell command after Jarvis safety checks. "
                "Use this for targeted commands such as tests, lint, build, or diagnostic commands. "
                "Do NOT use this for web searches, factual lookups, or current events. "
                "Do not use this for multi-step repository workflows, code editing, git commit, or git push; "
                "use delegate_to_codex for those."
            ),
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
            name="delegate_to_claude_code",
            description=(
                "High-privilege delegation tool for repository development workflows. "
                "Use this only for substantial code tasks such as multi-file edits, refactors, "
                "bug fixes, code review follow-up, test execution, and git workflows inside a repository. "
                "Do not use this for simple shell commands, factual questions, or lightweight search. "
                "Before calling it, gather enough context to issue one complete task contract."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": (
                            "Detailed development task for the coder worker, including file constraints, "
                            "verification expectations, and whether commit or push is permitted."
                        ),
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Absolute path to the target repository working directory.",
                    },
                    "verification_cmd": {
                        "type": "string",
                        "description": "Optional command the coder worker should run before finishing.",
                    },
                    "allow_commit": {
                        "type": "boolean",
                        "description": "Whether the coder worker may create a git commit.",
                        "default": False,
                    },
                    "allow_push": {
                        "type": "boolean",
                        "description": "Whether the coder worker may push to origin. Requires allow_commit=true.",
                        "default": False,
                    },
                },
                "required": ["instruction", "workdir"],
            },
            handler=run_coder_tool,
            risk_level="high",
            exposed_to_llm=False,
            execution_mode="proposal",
            can_modify_files=True,
            requires_workdir=True,
        ),
        ToolDefinition(
            name="delegate_to_codex",
            description=(
                "High-privilege delegation tool backed by Codex for repository development workflows. "
                "Use this only for substantial code tasks such as multi-file edits, refactors, "
                "bug fixes, code review follow-up, test execution, and git workflows inside a repository. "
                "Do not use this for simple shell commands, factual questions, or lightweight search. "
                "Before calling it, gather enough context to issue one complete task contract."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": (
                            "Detailed development task for the coder worker, including file constraints, "
                            "verification expectations, and whether commit or push is permitted."
                        ),
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Absolute path to the target repository working directory.",
                    },
                    "verification_cmd": {
                        "type": "string",
                        "description": "Optional command the coder worker should run before finishing.",
                    },
                    "allow_commit": {
                        "type": "boolean",
                        "description": "Whether the coder worker may create a git commit.",
                        "default": False,
                    },
                    "allow_push": {
                        "type": "boolean",
                        "description": "Whether the coder worker may push to origin. Requires allow_commit=true.",
                        "default": False,
                    },
                },
                "required": ["instruction", "workdir"],
            },
            handler=run_codex_coder_tool,
            risk_level="high",
            execution_mode="proposal",
            can_modify_files=True,
            requires_workdir=True,
        ),
        ToolDefinition(
            name="obsidian_wiki_query",
            description=(
                "Query the obsidian_wiki long-term knowledge store. "
                "Use this for project memory, concepts, decisions, and supporting raw evidence."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "query_mode": {
                        "type": "string",
                        "enum": ["wiki_only", "wiki_then_raw", "raw_only"],
                        "default": "wiki_then_raw",
                    },
                    "vault_path": {"type": "string"},
                },
                "required": ["query"],
            },
            handler=run_obsidian_wiki_query,
        ),
        ToolDefinition(
            name="obsidian_wiki_draft",
            description=(
                "Create a candidate obsidian_wiki draft for stable knowledge such as design, decisions, concepts, or playbooks. "
                "This does not write to the formal wiki page."
            ),
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
                    "vault_path": {"type": "string"},
                },
                "required": ["title", "page_type", "content"],
            },
            handler=run_obsidian_wiki_draft,
        ),
        ToolDefinition(
            name="obsidian_wiki_apply",
            description=(
                "Apply a previously created obsidian_wiki draft to the formal wiki. "
                "Use this only after user confirmation."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string"},
                    "target_page": {"type": "string"},
                    "vault_path": {"type": "string"},
                },
                "required": ["draft_id"],
            },
            handler=run_obsidian_wiki_apply,
        ),
        ToolDefinition(
            name="obsidian_wiki_maintain",
            description=(
                "Run lightweight maintenance on the obsidian_wiki vault, such as checking frontmatter, page types, source ids, and dead links."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "vault_path": {"type": "string"},
                },
            },
            handler=run_obsidian_wiki_maintain,
        ),
        ToolDefinition(
            name="business_knowledge_search",
            description=(
                "Query the OpenSearch-backed business knowledge store. "
                "Use this for business corpora such as deep research materials, imported documents, "
                "Wikipedia-style reference corpora, and SEC filings. "
                "Do not use this for personal memory, user notes, project decisions, or Obsidian wiki pages; "
                "use obsidian_wiki_query for those."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "mode": {
                        "type": "string",
                        "enum": ["bm25", "vector", "hybrid", "rrf", "rrf_v2"],
                        "default": "rrf_v2",
                    },
                    "language": {
                        "type": "string",
                        "description": "Corpus language, default zh; sec_filing defaults to en.",
                    },
                    "chunk_profile_id": {"type": "string"},
                    "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                    "source_type": {
                        "type": "string",
                        "description": (
                            "Optional business source type, for example wikipedia, "
                            "deep_research, sec_filing, or generic."
                        ),
                    },
                    "source_id": {"type": "string", "description": "Optional exact source id filter."},
                    "source_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of source ids to search within.",
                    },
                    "ticker": {"type": "string", "description": "SEC filing ticker filter."},
                    "company_name": {"type": "string", "description": "SEC filing company filter."},
                    "form_type": {"type": "string", "description": "SEC filing form type such as 10-K or 10-Q."},
                    "fiscal_year": {"type": "integer", "description": "SEC filing fiscal year filter."},
                    "section_title": {"type": "string", "description": "SEC filing section title filter."},
                },
                "required": ["query"],
            },
            handler=run_business_knowledge_search,
        ),
        ToolDefinition(
            name="write_file",
            description=(
                "Write a UTF-8 markdown file inside the local Jarvis workspace. "
                "Use this when the user explicitly asks to create or overwrite a markdown file in the repository. "
                "Always provide a workspace-relative .md path. Do not guess paths outside the project. "
                "If the target directory does not already exist, stop and ask the user before proceeding."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "relative_path": {
                        "type": "string",
                        "description": "Workspace-relative markdown path such as docs/research/2026-05/example.md",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full markdown content to write.",
                    },
                },
                "required": ["relative_path", "content"],
            },
            handler=run_write_file,
            risk_level="medium",
        ),
        ToolDefinition(
            name="tavily_search",
            description=(
                "Search the web using Tavily AI Search API. "
                "Use this when the user asks about current events, facts, or anything that requires up-to-date information from the internet. "
                "Search ONCE per question. After receiving results, summarize them and reply to the user immediately. "
                "Do NOT search the same topic multiple times."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query string.",
                    },
                    "search_depth": {
                        "type": "string",
                        "enum": ["basic", "advanced"],
                        "description": "'basic' for fast results (1-2s), 'advanced' for comprehensive research (5-10s).",
                        "default": "basic",
                    },
                    "topic": {
                        "type": "string",
                        "enum": ["general", "news"],
                        "description": "'general' for broad web search, 'news' for current events in the last 7 days.",
                        "default": "general",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return (1-10).",
                        "default": 5,
                    },
                    "include_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of domains to specifically include.",
                    },
                    "exclude_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of domains to exclude.",
                    },
                },
                "required": ["query"],
            },
            handler=run_tavily_search,
            risk_level="low",
            execution_mode="direct",
        ),
    ]
