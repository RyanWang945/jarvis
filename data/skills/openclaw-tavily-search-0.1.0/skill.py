from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path

from app.config import get_settings
from app.skills.base import SkillRequest, SkillResult


class TavilySearchSkill:
    name = "tavily-search"

    def run(self, request: SkillRequest) -> SkillResult:
        query = str(request.args.get("query") or "").strip()
        if not query:
            return SkillResult(ok=False, exit_code=None, stderr="missing query", summary="Missing search query.")

        max_results = _bounded_int(request.args.get("max_results"), default=5, minimum=1, maximum=10)
        include_answer = bool(request.args.get("include_answer", False))
        search_depth = str(request.args.get("search_depth") or "basic")
        output_format = str(request.args.get("format") or "brave")

        if search_depth not in {"basic", "advanced"}:
            return SkillResult(ok=False, exit_code=None, summary=f"Unsupported search_depth: {search_depth}")
        if output_format not in {"raw", "brave", "md"}:
            return SkillResult(ok=False, exit_code=None, summary=f"Unsupported format: {output_format}")

        script = Path(__file__).resolve().parent / "scripts" / "tavily_search.py"
        command = [
            sys.executable,
            str(script),
            "--query",
            query,
            "--max-results",
            str(max_results),
            "--search-depth",
            search_depth,
            "--format",
            output_format,
        ]
        if include_answer:
            command.append("--include-answer")

        env = os.environ.copy()
        settings = get_settings()
        if settings.tavily_api_key and not env.get("TAVILY_API_KEY"):
            env["TAVILY_API_KEY"] = settings.tavily_api_key

        try:
            completed = subprocess.run(
                command,
                cwd=str(script.parent),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=request.timeout_seconds,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            return SkillResult(
                ok=False,
                exit_code=None,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                summary="Tavily search timed out.",
            )

        return SkillResult(
            ok=completed.returncode == 0,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            summary=_summary(completed.returncode, query, completed.stdout),
        )


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _summary(returncode: int, query: str, stdout: str) -> str:
    if returncode == 0:
        urls = _extract_urls(stdout)
        if urls:
            return f"Tavily search completed for: {query}. Source URLs: {', '.join(urls[:3])}"
        return f"Tavily search completed for: {query}"
    return f"Tavily search failed for: {query}"


def _extract_urls(stdout: str) -> list[str]:
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return [
            line.strip()
            for line in stdout.splitlines()
            if line.strip().startswith(("http://", "https://"))
        ]
    results = parsed.get("results") if isinstance(parsed, dict) else None
    if not isinstance(results, list):
        return []
    urls: list[str] = []
    for item in results:
        if isinstance(item, dict) and item.get("url"):
            urls.append(str(item["url"]))
    return urls
