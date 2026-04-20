from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.skills.base import Skill
from app.skills.coder import CoderSkill
from app.skills.echo import EchoSkill
from app.skills.loader import SkillPackageLoader
from app.skills.registry import SkillRegistry
from app.skills.shell import ShellSkill
from app.tools.registry import ToolRegistry
from app.tools.specs import ToolSpec


@dataclass(frozen=True)
class Registries:
    skill_registry: SkillRegistry
    tool_registry: ToolRegistry


_registries: Registries | None = None


def bootstrap_registries(*, external_paths: list[Path] | None = None, force: bool = False) -> Registries:
    global _registries
    if _registries is not None and not force:
        return _registries

    settings = get_settings()
    skills: list[Skill] = _load_builtin_skills()
    tools = _load_builtin_tools()

    loader = SkillPackageLoader.from_default_paths(data_dir=settings.data_dir, extra_paths=external_paths)
    for package in loader.load():
        if package.skill is not None:
            skills.append(package.skill)
        tools.extend(package.tools)

    _registries = Registries(
        skill_registry=SkillRegistry(skills),
        tool_registry=ToolRegistry(tools),
    )
    return _registries


def get_skill_registry() -> SkillRegistry:
    return bootstrap_registries().skill_registry


def get_tool_registry() -> ToolRegistry:
    return bootstrap_registries().tool_registry


def reset_registries_for_tests() -> None:
    global _registries
    _registries = None


def _load_builtin_skills() -> list[Skill]:
    return [
        EchoSkill(),
        ShellSkill(),
        CoderSkill(),
    ]


def _load_builtin_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="echo",
            description="Echo the task instruction without external side effects.",
            args_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            skill="echo",
            worker_type="echo",
            action="echo",
            risk_level="low",
            exposed_to_llm=True,
        ),
        ToolSpec(
            name="run_shell_command",
            description=(
                "Run a simple local shell command after Jarvis risk checks. "
                "Do not use this for multi-step code editing, git commit, or git push workflows; "
                "use delegate_to_claude_code for those."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "workdir": {"type": "string"},
                },
                "required": ["command"],
            },
            skill="shell",
            worker_type="shell",
            action="run",
            risk_level="medium",
            exposed_to_llm=True,
        ),
        ToolSpec(
            name="run_tests",
            description="Run a configured project test command.",
            args_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "enum": ["uv run pytest", "pytest"],
                    },
                    "workdir": {"type": "string"},
                },
                "required": ["command"],
            },
            skill="shell",
            worker_type="shell",
            action="run",
            risk_level="low",
            exposed_to_llm=True,
        ),
        ToolSpec(
            name="delegate_to_claude_code",
            description=(
                "Delegate a repository development workflow to the local Claude Code CLI. "
                "Use this for code/file edits, README updates, tests, "
                "git status/diff review, git commit, and git push when the user explicitly asks "
                "to commit or push. The worker runs in the provided workdir and should complete "
                "the workflow end-to-end instead of asking the user to type shell commands."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": (
                            "Detailed development task for the coder worker, including whether "
                            "to commit and/or push. Include file constraints and commit message "
                            "requirements if provided by the user."
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
                },
                "required": ["instruction", "workdir"],
            },
            skill="claude_code",
            worker_type="coder",
            action="run",
            risk_level="high",
            exposed_to_llm=True,
        ),
    ]
