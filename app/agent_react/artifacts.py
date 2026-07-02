from __future__ import annotations

import hashlib
import importlib
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
from xml.etree import ElementTree

from app.config import get_settings
from app.tools.common import ToolArtifact

IMAGE_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
SVG_MIME = "image/svg+xml"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_SVG_BYTES = 5 * 1024 * 1024
MAX_FILE_BYTES = 50 * 1024 * 1024
_SENSITIVE_NAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".crt", ".cer", ".p12", ".pfx", ".sqlite", ".db", ".log"}
_SENSITIVE_PARTS = {"logs", ".git", ".venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


@dataclass(frozen=True)
class ChannelAttachment:
    artifact_id: str
    kind: Literal["image", "file"]
    path: str
    mime_type: str
    filename: str
    size_bytes: int
    source_tool: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactRejection:
    artifact_id: str
    path: str | None
    reason: str


@dataclass(frozen=True)
class ArtifactResolution:
    attachments: tuple[ChannelAttachment, ...]
    rejected: tuple[ArtifactRejection, ...]


def artifact_to_payload(artifact: ToolArtifact) -> dict[str, Any]:
    return asdict(artifact)


def artifact_from_payload(payload: dict[str, Any]) -> ToolArtifact | None:
    try:
        artifact_id = str(payload.get("artifact_id") or "").strip()
        kind = str(payload.get("kind") or "file").strip()
        if not artifact_id or kind not in {"image", "file", "directory", "log", "git_ref"}:
            return None
        return ToolArtifact(
            artifact_id=artifact_id,
            kind=kind,  # type: ignore[arg-type]
            turn_id=_optional_int(payload.get("turn_id")),
            tool_call_id=_optional_str(payload.get("tool_call_id")),
            path=_optional_str(payload.get("path")),
            session_relative_path=_optional_str(payload.get("session_relative_path")),
            mime_type=_optional_str(payload.get("mime_type")),
            filename=_optional_str(payload.get("filename")),
            size_bytes=_optional_int(payload.get("size_bytes")),
            source_tool=str(payload.get("source_tool") or ""),
            node_id=_optional_str(payload.get("node_id")),
            publish=_optional_bool(payload.get("publish"), default=True),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )
    except Exception:
        return None


def legacy_artifact_to_tool_artifact(
    artifact: str,
    *,
    turn_id: int,
    tool_call_id: str,
    source_tool: str,
    base_dir: Path | None,
) -> ToolArtifact | None:
    raw = str(artifact or "").strip()
    if not raw:
        return None

    label = ""
    value = raw
    if ":" in raw:
        label, value = raw.split(":", 1)
        label = label.strip()
        value = value.strip()

    if label in {"git_commit", "git_branch", "git_worktree", "git_upstream", "permission_violation"}:
        return ToolArtifact(
            artifact_id=_stable_artifact_id(turn_id, tool_call_id, source_tool, raw),
            kind="git_ref",
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            source_tool=source_tool,
            publish=False,
            metadata={"legacy": raw},
        )

    path = _legacy_artifact_path(label, value, base_dir)
    kind: Literal["file", "directory", "log"] = "file"
    if label in {"codex_run"}:
        kind = "directory"
    elif label in {"codex_events", "jarvis_audit", "codex_stderr", "codex_approval_requests"}:
        kind = "log"

    stat_size: int | None = None
    stat_mtime_ns: int | None = None
    if path is not None:
        try:
            resolved = path.resolve(strict=True)
            if resolved.is_dir():
                kind = "directory"
            elif resolved.is_file():
                stat = resolved.stat()
                stat_size = stat.st_size
                stat_mtime_ns = stat.st_mtime_ns
        except OSError:
            pass

    return ToolArtifact(
        artifact_id=_stable_artifact_id(
            turn_id,
            tool_call_id,
            source_tool,
            str(path or raw),
            size=stat_size,
            mtime_ns=stat_mtime_ns,
        ),
        kind=kind,
        turn_id=turn_id,
        tool_call_id=tool_call_id,
        path=str(path) if path is not None else None,
        session_relative_path=None,
        mime_type=_guess_mime(path) if path is not None else None,
        filename=path.name if path is not None else None,
        size_bytes=stat_size,
        source_tool=source_tool,
        publish=kind in {"file", "log"},
        metadata={"legacy": raw} if label else {},
    )


def resolve_channel_attachments(
    artifacts: list[ToolArtifact],
    *,
    turn_id: int | None = None,
    extra_allowed_roots: list[Path] | tuple[Path, ...] | None = None,
) -> ArtifactResolution:
    settings = get_settings()
    workspace_root = settings.workspace_root.resolve()
    allowed_roots = _dedupe_roots(
        [
            *_allowed_roots(workspace_root, settings.data_dir),
            *_resolve_existing_roots(extra_allowed_roots or ()),
        ]
    )
    attachments: list[ChannelAttachment] = []
    rejected: list[ArtifactRejection] = []
    seen: set[str] = set()

    for artifact in artifacts:
        path_text = artifact.path
        if not path_text:
            continue
        if turn_id is not None and artifact.turn_id is not None and artifact.turn_id != turn_id:
            rejected.append(ArtifactRejection(artifact.artifact_id, path_text, "artifact_turn_mismatch"))
            continue
        if artifact.artifact_id in seen:
            continue
        seen.add(artifact.artifact_id)
        attachment, reason = _resolve_one(artifact, allowed_roots, workspace_root)
        if attachment is not None:
            attachments.append(attachment)
        elif reason is not None:
            rejected.append(ArtifactRejection(artifact.artifact_id, path_text, reason))
    return ArtifactResolution(tuple(attachments), tuple(rejected))


def _resolve_one(
    artifact: ToolArtifact,
    allowed_roots: tuple[Path, ...],
    workspace_root: Path,
) -> tuple[ChannelAttachment | None, str | None]:
    try:
        raw_path = Path(artifact.path or "")
        resolved = raw_path.expanduser().resolve(strict=True)
    except OSError:
        return None, "path_resolve_failed"

    if not resolved.is_file():
        return None, "not_a_file"
    if not _is_allowed_attachment_path(resolved, allowed_roots, workspace_root):
        return None, "path_outside_allowed_roots"
    if _is_sensitive_path(resolved):
        return None, "sensitive_path"

    suffix = resolved.suffix.lower()
    if suffix == ".svg":
        return _resolve_svg_preview(artifact, resolved, allowed_roots)

    expected_mime = IMAGE_MIME_BY_SUFFIX.get(suffix)
    if expected_mime is None:
        return _resolve_file_attachment(artifact, resolved)
    guessed_mime = artifact.mime_type or _guess_mime(resolved)
    if guessed_mime != expected_mime:
        return None, "mime_mismatch"

    size = resolved.stat().st_size
    if size > MAX_IMAGE_BYTES:
        return None, "file_too_large"

    return (
        ChannelAttachment(
            artifact_id=artifact.artifact_id,
            kind="image",
            path=str(resolved),
            mime_type=expected_mime,
            filename=artifact.filename or resolved.name,
            size_bytes=size,
            source_tool=artifact.source_tool,
            metadata=dict(artifact.metadata),
        ),
        None,
    )


def _resolve_file_attachment(artifact: ToolArtifact, resolved: Path) -> tuple[ChannelAttachment | None, str | None]:
    if artifact.kind != "file":
        return None, "unsupported_type"

    size = resolved.stat().st_size
    if size > MAX_FILE_BYTES:
        return None, "file_too_large"

    return (
        ChannelAttachment(
            artifact_id=artifact.artifact_id,
            kind="file",
            path=str(resolved),
            mime_type=artifact.mime_type or _guess_mime(resolved) or "application/octet-stream",
            filename=artifact.filename or resolved.name,
            size_bytes=size,
            source_tool=artifact.source_tool,
            metadata=dict(artifact.metadata),
        ),
        None,
    )


def _resolve_svg_preview(
    artifact: ToolArtifact,
    resolved: Path,
    allowed_roots: tuple[Path, ...],
) -> tuple[ChannelAttachment | None, str | None]:
    guessed_mime = artifact.mime_type or _guess_mime(resolved)
    if guessed_mime != SVG_MIME:
        return None, "mime_mismatch"

    size = resolved.stat().st_size
    if size > MAX_SVG_BYTES:
        return None, "file_too_large"

    preview_path, reason = _render_svg_preview(artifact, resolved)
    if preview_path is None:
        return None, reason or "svg_preview_failed"
    try:
        preview_resolved = preview_path.resolve(strict=True)
    except OSError:
        return None, "svg_preview_resolve_failed"
    if not _is_within_any(preview_resolved, allowed_roots):
        return None, "svg_preview_outside_allowed_roots"
    if not preview_resolved.is_file():
        return None, "svg_preview_not_a_file"
    preview_size = preview_resolved.stat().st_size
    if preview_size > MAX_IMAGE_BYTES:
        return None, "svg_preview_too_large"

    return (
        ChannelAttachment(
            artifact_id=f"{artifact.artifact_id}:preview:png",
            kind="image",
            path=str(preview_resolved),
            mime_type="image/png",
            filename=f"{resolved.stem}.preview.png",
            size_bytes=preview_size,
            source_tool=artifact.source_tool,
            metadata={
                **dict(artifact.metadata),
                "source_path": str(resolved),
                "source_mime_type": SVG_MIME,
                "preview_for": artifact.artifact_id,
            },
        ),
        None,
    )


def _render_svg_preview(artifact: ToolArtifact, svg_path: Path) -> tuple[Path | None, str | None]:
    settings = get_settings()
    data_root = settings.data_dir if settings.data_dir.is_absolute() else settings.workspace_root / settings.data_dir
    preview_dir = data_root / "artifact_previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / f"{_path_safe_digest(artifact.artifact_id)}.png"

    cairo_reason = _render_svg_preview_with_cairosvg(svg_path, preview_path)
    if cairo_reason is None:
        return preview_path, None

    browser_reason = _render_svg_preview_with_browser(svg_path, preview_path, preview_dir)
    if browser_reason is None:
        return preview_path, None

    if cairo_reason == "svg_preview_unavailable" and browser_reason == "svg_preview_unavailable":
        return None, "svg_preview_unavailable"
    return None, browser_reason or cairo_reason


def _render_svg_preview_with_cairosvg(svg_path: Path, preview_path: Path) -> str | None:
    try:
        cairosvg = importlib.import_module("cairosvg")
    except Exception:
        return "svg_preview_unavailable"
    try:
        cairosvg.svg2png(url=str(svg_path), write_to=str(preview_path))
    except Exception:
        return "svg_preview_failed"
    return None


def _render_svg_preview_with_browser(svg_path: Path, preview_path: Path, preview_dir: Path) -> str | None:
    browser = _find_svg_preview_browser()
    if browser is None:
        return "svg_preview_unavailable"

    width, height = _svg_viewport_size(svg_path)
    profile_dir = tempfile.mkdtemp(prefix="svg-preview-browser-", dir=str(preview_dir))
    try:
        command = [
            str(browser),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-crash-reporter",
            "--disable-crashpad",
            f"--user-data-dir={profile_dir}",
            f"--screenshot={preview_path}",
            f"--window-size={width},{height}",
            svg_path.resolve(strict=True).as_uri(),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "svg_preview_failed"
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)
    if completed.returncode != 0:
        return "svg_preview_failed"
    try:
        if not preview_path.is_file() or preview_path.stat().st_size <= 0:
            return "svg_preview_failed"
    except OSError:
        return "svg_preview_failed"
    return None


def _find_svg_preview_browser() -> Path | None:
    env_path = os.environ.get("JARVIS_SVG_RENDERER_BROWSER")
    if env_path:
        path = Path(env_path)
        if path.is_file():
            return path

    for command in ("msedge", "chrome", "chromium", "google-chrome"):
        found = shutil.which(command)
        if found:
            return Path(found)

    for candidate in (
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
    ):
        if candidate.is_file():
            return candidate
    return None


def _svg_viewport_size(svg_path: Path) -> tuple[int, int]:
    default = (1600, 1000)
    try:
        root = ElementTree.parse(svg_path).getroot()
    except ElementTree.ParseError:
        return default

    width = _svg_length_to_px(root.attrib.get("width"))
    height = _svg_length_to_px(root.attrib.get("height"))
    if (width is None or height is None) and root.attrib.get("viewBox"):
        parts = re.split(r"[\s,]+", root.attrib["viewBox"].strip())
        if len(parts) == 4:
            try:
                width = width or float(parts[2])
                height = height or float(parts[3])
            except ValueError:
                pass
    if width is None or height is None:
        return default
    return _clamp_viewport(width), _clamp_viewport(height)


def _svg_length_to_px(value: str | None) -> float | None:
    if not value:
        return None
    match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(px)?\s*$", value)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _clamp_viewport(value: float) -> int:
    return max(100, min(4096, int(round(value))))


def _allowed_roots(workspace_root: Path, data_dir: Path) -> tuple[Path, ...]:
    roots: list[Path] = []
    data_root = data_dir if data_dir.is_absolute() else workspace_root / data_dir
    for candidate in (
        data_root / "artifact_previews",
        data_root / "coder_runs",
        *_session_artifact_roots(workspace_root),
    ):
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        roots.append(resolved)
    return _dedupe_roots(roots)


def _resolve_existing_roots(roots: list[Path] | tuple[Path, ...]) -> tuple[Path, ...]:
    resolved_roots: list[Path] = []
    for root in roots:
        try:
            resolved = Path(root).resolve(strict=True)
        except OSError:
            continue
        if resolved.is_dir():
            resolved_roots.append(resolved)
    return tuple(resolved_roots)


def _dedupe_roots(roots: list[Path] | tuple[Path, ...]) -> tuple[Path, ...]:
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = os.path.normcase(str(root))
        if key not in seen:
            seen.add(key)
            deduped.append(root)
    return tuple(deduped)


def _session_artifact_roots(workspace_root: Path) -> tuple[Path, ...]:
    sessions_root = workspace_root / "sessions"
    try:
        sessions = [item for item in sessions_root.iterdir() if item.is_dir()]
    except OSError:
        return ()
    return tuple(session / "artifacts" for session in sessions)


def _legacy_artifact_path(label: str, value: str, base_dir: Path | None) -> Path | None:
    if label and label not in {"git_file", "codex_events", "codex_run", "jarvis_audit", "codex_stderr", "codex_approval_requests"}:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    if base_dir is not None:
        return base_dir / path
    return get_settings().workspace_root / path


def _is_within_any(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _is_allowed_attachment_path(path: Path, allowed_roots: tuple[Path, ...], workspace_root: Path) -> bool:
    sessions_root = (workspace_root / "sessions").resolve()
    if _is_within(path, sessions_root):
        return _is_session_artifact_path(path, sessions_root)
    return _is_within_any(path, allowed_roots)


def _is_session_artifact_path(path: Path, sessions_root: Path) -> bool:
    try:
        relative = path.relative_to(sessions_root)
    except ValueError:
        return False
    return len(relative.parts) >= 3 and relative.parts[1] == "artifacts"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_sensitive_path(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    if parts.intersection(_SENSITIVE_PARTS):
        return True
    name = path.name.lower()
    if name in _SENSITIVE_NAMES:
        return True
    return path.suffix.lower() in _SENSITIVE_SUFFIXES


def _guess_mime(path: Path) -> str | None:
    if path is None:
        return None
    return mimetypes.guess_type(str(path))[0]


def _path_safe_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:24]


def _stable_artifact_id(
    turn_id: int,
    tool_call_id: str,
    source_tool: str,
    identity: str,
    *,
    size: int | None = None,
    mtime_ns: int | None = None,
) -> str:
    digest_source = f"{identity}|{size or ''}|{mtime_ns or ''}"
    digest = hashlib.sha256(digest_source.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"{turn_id}:{tool_call_id}:{source_tool}:{digest}"


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "external", "publish"}:
        return True
    if text in {"0", "false", "no", "internal", "none"}:
        return False
    return default
