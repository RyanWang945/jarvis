from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.tools.common import ToolExecutionRequest, ToolExecutionResult

MAX_READ_BYTES = 64 * 1024
DEFAULT_MAX_LINES = 200
MAX_SEARCH_RESULTS = 50
MAX_CONTENT_FILE_BYTES = 256 * 1024
SKIPPED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "htmlcov",
}


def run_read_file(request: ToolExecutionRequest) -> ToolExecutionResult:
    workspace_root = get_settings().workspace_root.resolve()
    raw_path = str(request.args.get("path") or request.args.get("relative_path") or "").strip()
    if not raw_path:
        return _json_result(
            ok=False,
            payload={"error": "Missing required argument: path."},
            summary="File path is required.",
        )

    resolved = _resolve_workspace_path(raw_path, workspace_root)
    if resolved is None:
        return _json_result(
            ok=False,
            payload={"path": raw_path, "error": "Path must stay inside the Jarvis workspace."},
            summary="read_file path must stay inside the workspace.",
        )

    payload: dict[str, Any] = _path_metadata(raw_path, resolved, workspace_root)
    if not resolved.exists() or not resolved.is_file():
        return _json_result(ok=True, payload=payload, summary="Path is not an existing file.")

    if _looks_binary(resolved):
        payload.update({"binary": True, "content": None, "truncated": False})
        return _json_result(ok=True, payload=payload, summary="File is binary; content was not returned.")

    start_line = _coerce_positive_int(request.args.get("start_line"), default=1)
    max_lines = _coerce_positive_int(request.args.get("max_lines"), default=DEFAULT_MAX_LINES)
    max_lines = min(max_lines, DEFAULT_MAX_LINES)

    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _json_result(
            ok=False,
            payload={**payload, "error": str(exc)},
            summary=f"Failed to read file: {exc}",
        )

    encoded = text.encode("utf-8", errors="replace")
    byte_truncated = len(encoded) > MAX_READ_BYTES
    if byte_truncated:
        text = encoded[:MAX_READ_BYTES].decode("utf-8", errors="replace")

    lines = text.splitlines()
    start_index = max(start_line - 1, 0)
    selected = lines[start_index : start_index + max_lines]
    line_truncated = start_index + max_lines < len(lines)
    payload.update(
        {
            "binary": False,
            "start_line": start_line,
            "end_line": start_line + len(selected) - 1 if selected else start_line,
            "returned_lines": len(selected),
            "truncated": byte_truncated or line_truncated,
            "content": "\n".join(selected),
        }
    )
    return _json_result(ok=True, payload=payload, summary=f"Read file: {payload['relative_path']}")


def run_search_files(request: ToolExecutionRequest) -> ToolExecutionResult:
    workspace_root = get_settings().workspace_root.resolve()
    query = str(request.args.get("query") or "").strip()
    if not query:
        return _json_result(
            ok=False,
            payload={"error": "Missing required argument: query."},
            summary="Search query is required.",
        )

    mode = str(request.args.get("mode") or "path").strip().lower()
    if mode not in {"path", "content"}:
        return _json_result(
            ok=False,
            payload={"query": query, "error": "mode must be path or content."},
            summary="Invalid search mode.",
        )

    max_results = min(_coerce_positive_int(request.args.get("max_results"), default=20), MAX_SEARCH_RESULTS)
    exact = _resolve_workspace_path(query, workspace_root)
    exact_payload = _path_metadata(query, exact, workspace_root) if exact is not None else None
    results = _search_paths(workspace_root, query, max_results) if mode == "path" else _search_content(workspace_root, query, max_results)
    payload = {
        "query": query,
        "mode": mode,
        "exact_path": exact_payload,
        "results": results,
        "result_count": len(results),
    }
    return _json_result(ok=True, payload=payload, summary=f"Found {len(results)} matching file result(s).")


def _search_paths(workspace_root: Path, query: str, max_results: int) -> list[dict[str, Any]]:
    lowered = query.casefold()
    matches: list[dict[str, Any]] = []
    for path in _iter_workspace_files(workspace_root):
        rel = path.relative_to(workspace_root).as_posix()
        if lowered in rel.casefold():
            matches.append(_path_metadata(rel, path, workspace_root))
            if len(matches) >= max_results:
                break
    return matches


def _search_content(workspace_root: Path, query: str, max_results: int) -> list[dict[str, Any]]:
    lowered = query.casefold()
    matches: list[dict[str, Any]] = []
    for path in _iter_workspace_files(workspace_root):
        try:
            if path.stat().st_size > MAX_CONTENT_FILE_BYTES or _looks_binary(path):
                continue
            for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                if lowered in line.casefold():
                    item = _path_metadata(path.relative_to(workspace_root).as_posix(), path, workspace_root)
                    item.update({"line": line_number, "preview": line.strip()[:300]})
                    matches.append(item)
                    break
        except OSError:
            continue
        if len(matches) >= max_results:
            break
    return matches


def _iter_workspace_files(workspace_root: Path):
    stack = [workspace_root]
    while stack:
        current = stack.pop()
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name.lower(), reverse=True)
        except OSError:
            continue
        for child in children:
            if child.is_dir():
                if child.name not in SKIPPED_DIRS:
                    stack.append(child)
            elif child.is_file():
                yield child


def _resolve_workspace_path(raw_path: str, workspace_root: Path) -> Path | None:
    candidate = Path(raw_path)
    resolved = candidate.resolve() if candidate.is_absolute() else (workspace_root / candidate).resolve()
    try:
        resolved.relative_to(workspace_root)
    except ValueError:
        return None
    return resolved


def _path_metadata(raw_path: str, path: Path, workspace_root: Path) -> dict[str, Any]:
    exists = path.exists()
    relative_path = path.relative_to(workspace_root).as_posix() if _is_within_workspace(path, workspace_root) else raw_path
    payload: dict[str, Any] = {
        "path": raw_path,
        "relative_path": relative_path,
        "exists": exists,
        "is_file": path.is_file() if exists else False,
        "is_dir": path.is_dir() if exists else False,
    }
    if exists:
        try:
            stat = path.stat()
        except OSError:
            pass
        else:
            payload["size_bytes"] = stat.st_size
    return payload


def _looks_binary(path: Path) -> bool:
    try:
        sample = path.read_bytes()[:4096]
    except OSError:
        return False
    return b"\x00" in sample


def _coerce_positive_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def _is_within_workspace(path: Path, workspace_root: Path) -> bool:
    try:
        path.relative_to(workspace_root)
        return True
    except ValueError:
        return False


def _json_result(*, ok: bool, payload: dict[str, Any], summary: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        ok=ok,
        exit_code=0 if ok else None,
        stdout=json.dumps(payload, ensure_ascii=False),
        summary=summary,
    )
