from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from app.tools.ask_user import run_ask_user
from app.tools.business_knowledge import run_business_knowledge_search
from app.tools.coder import run_coder_tool
from app.tools.codex import run_codex_coder_tool
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
from app.tools.skill_guidance import run_load_skill_guidance
from app.tools.tavily import run_tavily_search
from app.tools.tool_search import run_tool_search
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
            name="read_file",
            description=(
                "Read a known local workspace file by path for lightweight inspection. "
                "Use this for file content snippets, logs, configuration files, and existence checks when a specific path is known. "
                "Use it when the user's final goal is to inspect, verify, or deliver a specific file or artifact. "
                "This tool is read-only and returns bounded text content plus file metadata. "
                "Do not use it as the primary strategy when the user needs repository-level judgment such as "
                "architecture analysis, integration planning, code review, design recommendations, execution-chain analysis, "
                "or multi-file reasoning; use delegate_to_codex for those."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file path such as app/tools/runtime.py.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "1-based line number to start reading from. Defaults to 1.",
                        "default": 1,
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Maximum lines to return, capped by the runtime. Defaults to 200.",
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
            description=(
                "Search local workspace files without invoking shell commands. "
                "Use mode=path to find files by path/name or check exact path existence; use mode=content for bounded text search. "
                "Use this when the user's final goal is to locate, verify, read, or deliver a specific known or guessable file/path/artifact. "
                "This tool is read-only and returns matching paths plus small metadata or previews. "
                "Do not use it as the primary strategy when the user needs repository-level judgment such as "
                "architecture analysis, integration planning, code review, design recommendations, execution-chain analysis, "
                "or multi-file reasoning; use delegate_to_codex for those."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Path/name/content query to search inside the workspace.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["path", "content"],
                        "description": "path searches file paths and exact existence; content searches file text.",
                        "default": "path",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results to return, capped by the runtime.",
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
                    "repo_id": {
                        "type": "string",
                        "description": "Registered repository id, such as jarvis. Prefer this over workdir.",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Deprecated compatibility field. Must match a registered repository root.",
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
                "required": ["instruction"],
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
                "High-privilege delegation tool backed by Codex for local repository workflows. "
                "Use this for multi-file repository reasoning, architecture review, code review, reports, tests, "
                "code edits, refactors, bug fixes, git workflows, and repo-local visual artifacts "
                "such as architecture diagrams or generated PNG/WebP/SVG files inside a registered repository. "
                "Prefer this as the first tool for open-ended repository investigation, integration planning, design recommendations, "
                "execution-chain analysis, or understanding how an external technology should fit into a local project. "
                "Do not use this to list files, check whether a file exists, read a known file, locate a specific artifact for delivery, or run bounded text search; "
                "use read_file or search_files for those lightweight workspace file tasks. "
                "For image generation requests, instruct Codex to preserve the requested output format and "
                "copy any final raster image from Codex's generated-images area into the repository workspace "
                "before finishing, so Jarvis can discover and deliver it as an artifact. "
                "Do not use this for general factual questions or lightweight web search. "
                "Pass one compact outcome-oriented task: user goal, repo_id, constraints, permissions, "
                "and verification expectations. Codex owns planning, repository reasoning, command selection, "
                "retry strategy, and approval requests. Do not turn the task into a step-by-step shell script "
                "or prescribe recovery commands unless the user explicitly requested exact commands. "
                "Preserve the user's full repository outcome: if the user asks to edit/update/create, the "
                "instruction must remain an execution task, not a request to read files and ask what to do. "
                "Do not ask Codex to confirm commit messages or other routine execution details unless the "
                "user explicitly requested that confirmation."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": (
                            "Outcome-oriented task contract for Codex. Include the goal, constraints, "
                            "expected artifact path/format when generating a visual artifact, "
                            "verification expectations, and whether commit or push is permitted; avoid "
                            "enumerating shell commands or recovery steps, and avoid routine pre-confirmation prompts. "
                            "Do not downgrade explicit edit/commit/push requests into read-only inspection. "
                            "Do not silently change raster image-generation requests into SVG-only artifacts. "
                            "For lightweight file existence, path lookup, known-file reading, locating a specific deliverable artifact, or bounded text search, "
                            "do not use Codex; use read_file or search_files."
                        ),
                    },
                    "repo_id": {
                        "type": "string",
                        "description": "Registered repository id, such as jarvis. Prefer this over workdir.",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Deprecated compatibility field. Must match a registered repository root.",
                    },
                    "verification_cmd": {
                        "type": "string",
                        "description": "Optional command the coder worker should run before finishing.",
                    },
                    "allow_commit": {
                        "type": "boolean",
                        "description": (
                            "Whether the coder worker may create a git commit. Set true when the user asks to commit, "
                            "create a commit, save changes in git, or push."
                        ),
                        "default": False,
                    },
                    "allow_push": {
                        "type": "boolean",
                        "description": (
                            "Whether the coder worker may push to origin. Set true when the user asks to push or "
                            "publish to a remote; requires allow_commit=true."
                        ),
                        "default": False,
                    },
                },
                "required": ["instruction"],
            },
            handler=run_codex_coder_tool,
            risk_level="high",
            execution_mode="proposal",
            can_modify_files=True,
            requires_workdir=True,
        ),
        ToolDefinition(
            name="ask_user",
            description=(
                "Ask the user one concise clarification question when required information is missing. "
                "Use this when Jarvis cannot safely continue without a user choice, such as an ambiguous target, "
                "unclear time, missing recipient, missing repository, or a required preference. "
                "Do not use ask_user for permission to perform Codex operations; Codex approvals have their own flow. "
                "Ask only the minimum necessary question, then stop and wait for the user's reply."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The exact short question to show to the user.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Brief internal reason why the answer is needed.",
                    },
                    "expected_answer_type": {
                        "type": "string",
                        "enum": ["free_text", "yes_no", "choice"],
                        "default": "free_text",
                    },
                    "choices": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional concise choices when expected_answer_type is choice.",
                    },
                },
                "required": ["question"],
            },
            handler=run_ask_user,
            risk_level="low",
        ),
        ToolDefinition(
            name="tool_search",
            description=(
                "Search Jarvis's tool catalog when the currently exposed low-risk tools cannot satisfy the user's request. "
                "This tool only discovers candidate tools; it never executes them and never grants permission by itself. "
                "Tool discovery is internal: never tell the user you are searching available tools or looking up related tools. "
                "Use it before asking for a hidden capability such as reminders, web search, repository work, or wiki writes. "
                "If the request can be answered from the current conversation without any tool, or no suitable tool exists, "
                "return no_capable_tool rather than forcing a tool. Never use tool_search as a retry mechanism after an "
                "unrelated tool failure unless the original user request still clearly needs another capability."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Capability search query, expanded from the user's request without adding new intent.",
                    },
                    "original_user_request": {
                        "type": "string",
                        "description": "The user's original request or the relevant quoted part of it.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum candidate tools to return. Defaults to 3.",
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
            handler=run_tool_search,
            risk_level="low",
        ),
        ToolDefinition(
            name="load_skill_guidance",
            description=(
                "Load task-specific procedural guidance from Jarvis skills. "
                "Use this before delegating to Codex, generating artifacts, operating on external channels, "
                "or performing a multi-step workflow when no relevant <system-reminder> skill is already present. "
                "This tool only loads guidance for the current turn; it does not execute the task, change files, "
                "call Codex, upload attachments, or grant permissions."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The user task or workflow capability to match against installed Jarvis skills.",
                    },
                    "intent": {
                        "type": "string",
                        "description": "Optional normalized intent, such as image artifact generation or repository workflow.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum skills to load. Defaults to 3.",
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
            handler=run_load_skill_guidance,
            risk_level="low",
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
                    "workspace_path": {
                        "type": "string",
                        "description": "Obsidian wiki workspace root containing vault/ and system/. Defaults to data/obsidian_wiki.",
                    },
                    "vault_path": {
                        "type": "string",
                        "description": "Deprecated alias for workspace_path.",
                    },
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
                    "workspace_path": {
                        "type": "string",
                        "description": "Obsidian wiki workspace root containing vault/ and system/. Defaults to data/obsidian_wiki.",
                    },
                    "vault_path": {
                        "type": "string",
                        "description": "Deprecated alias for workspace_path.",
                    },
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
                    "workspace_path": {
                        "type": "string",
                        "description": "Obsidian wiki workspace root containing vault/ and system/. Defaults to data/obsidian_wiki.",
                    },
                    "vault_path": {
                        "type": "string",
                        "description": "Deprecated alias for workspace_path.",
                    },
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
                    "workspace_path": {
                        "type": "string",
                        "description": "Obsidian wiki workspace root containing vault/ and system/. Defaults to data/obsidian_wiki.",
                    },
                    "vault_path": {
                        "type": "string",
                        "description": "Deprecated alias for workspace_path.",
                    },
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
                        "enum": ["bm25", "vector", "hybrid", "rrf", "rrf_v2", "rrf_v2_rerank"],
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
            name="scheduled_task",
            description=(
                "Create, list, or remove Jarvis reminder tasks when the user asks to be reminded, "
                "notified later, woken up, or shown/cancelled existing reminders. "
                "Use natural-language time_text such as 今天10点 or in 20 minutes; "
                "Jarvis parses and validates the concrete schedule. "
                "The runtime supplies conversation_id, platform, and external_chat_id automatically; "
                "do not ask the user for those fields."
            ),
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
            description=(
                "Deliver a previously generated local artifact or an explicitly requested workspace file "
                "to the current conversation channel. Use only when the user explicitly asks to send, "
                "resend, upload, or deliver a file/image. Do not use this as part of ordinary image "
                "generation; generated artifacts are delivered automatically by the runtime. Prefer "
                "artifact_id for historical artifacts and use path only when the user explicitly names a file. "
                "The runtime supplies conversation_id, turn_id, platform, and external_chat_id automatically."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {
                        "type": "string",
                        "description": "Preferred artifact id to deliver or redeliver.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Workspace-local file path fallback when artifact_id is unavailable.",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Deprecated alias for path.",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Optional display filename.",
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
            name="tavily_search",
            description=(
                "Search the web using Tavily AI Search API. "
                "Use this when the user asks about current events, facts, or anything that requires up-to-date information from the internet. "
                "It can also find indexed X/Twitter pages or web coverage of X/Twitter activity when a general web/news search is enough. "
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
