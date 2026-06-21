from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.agent_react.artifacts import artifact_from_payload
from app.runtime_types import ConversationStore
from app.task_runtime.node_result import ExecutionReport, NodeArtifact, NodeResult
from app.task_runtime.session_workspace import SessionWorkspaceRef
from app.tools.common import ToolArtifact

logger = logging.getLogger(__name__)


class ArtifactPublisher:
    """Collects, promotes, and persists artifacts from an ExecutionReport."""

    def __init__(self, store: ConversationStore, workspace: SessionWorkspaceRef) -> None:
        self._store = store
        self._workspace = workspace

    def publish(self, report: ExecutionReport, *, turn_id: int, conversation_id: int) -> list[ToolArtifact]:
        records = self._collect_from_report(report, turn_id)
        promoted = self._promote_to_session(records)
        self._persist_to_store(promoted, conversation_id)
        return promoted

    # ── collect ─────────────────────────────────────────────────────

    def _collect_from_report(self, report: ExecutionReport, turn_id: int) -> list[ToolArtifact]:
        result: list[ToolArtifact] = []
        seen: set[str] = set()
        for node_result in report.node_results:
            for record in self._collect_from_node_result(node_result, turn_id):
                if record.artifact_id not in seen:
                    seen.add(record.artifact_id)
                    result.append(record)
        return result

    def _collect_from_node_result(self, node_result: NodeResult, turn_id: int) -> list[ToolArtifact]:
        records: list[ToolArtifact] = []

        for node_artifact in node_result.artifacts:
            record = self._from_node_artifact(node_artifact, node_result, turn_id)
            if record is not None:
                records.append(record)

        # Top-level tool_artifacts (populated by _react_result_from_response
        # and coder finalizer for the normal code paths).
        for raw in node_result.tool_artifacts:
            record = artifact_from_payload(raw)
            if record is None:
                continue
            record = self._normalize_tool_artifact(record, node_result, turn_id)
            if record is not None:
                records.append(record)

        # Fallback: tool_calls may carry their own tool_artifacts that were
        # not lifted to the top-level list (e.g. by third-party runtimes or
        # test stubs).  The normal path already covers this via
        # _tool_artifacts_from_tool_calls inside _react_result_from_response,
        # but we keep the fallback for safety.
        for call in node_result.tool_calls:
            call_artifacts = call.get("tool_artifacts")
            if not isinstance(call_artifacts, list):
                continue
            for raw in call_artifacts:
                if not isinstance(raw, dict):
                    continue
                record = artifact_from_payload(raw)
                if record is None:
                    continue
                record = self._normalize_tool_artifact(record, node_result, turn_id)
                if record is not None:
                    records.append(record)

        return records

    def _from_node_artifact(
        self, node_artifact: NodeArtifact, result: NodeResult, turn_id: int
    ) -> ToolArtifact | None:
        if not node_artifact.publish:
            return None

        path_info = _resolve_path(
            node_artifact.session_relative_path or node_artifact.path,
            self._workspace,
            allow_absolute=False,
        )
        if path_info is None and node_artifact.kind in {"file", "image", "log", "directory"}:
            logger.warning(
                "node artifact skipped node_id=%s ref=%s reason=invalid_session_relative_path path=%s",
                result.node_id,
                node_artifact.ref,
                node_artifact.path,
            )
            return None

        absolute_path, relative_path = path_info if path_info is not None else (None, None)
        stat = _stat_path(absolute_path)
        metadata = dict(node_artifact.metadata)
        if relative_path:
            metadata.setdefault("session_relative_path", relative_path)
        metadata.setdefault("node_artifact_ref", node_artifact.ref)

        return ToolArtifact(
            artifact_id=node_artifact.artifact_id
            or _stable_id(self._workspace.session_id, result.node_id, node_artifact.ref, relative_path),
            kind=_artifact_kind(node_artifact.kind, absolute_path),
            turn_id=turn_id,
            tool_call_id=f"node:{result.node_id}",
            path=str(absolute_path) if absolute_path is not None else node_artifact.path,
            session_relative_path=relative_path,
            mime_type=node_artifact.mime_type or (_guess_mime(absolute_path) if absolute_path is not None else None),
            filename=node_artifact.filename or node_artifact.name or (absolute_path.name if absolute_path is not None else None),
            size_bytes=node_artifact.size_bytes or (stat.st_size if stat is not None else None),
            source_tool=node_artifact.source_tool or result.runtime,
            node_id=result.node_id,
            publish=node_artifact.publish,
            metadata=metadata,
        )

    def _normalize_tool_artifact(
        self, artifact: ToolArtifact, result: NodeResult, turn_id: int
    ) -> ToolArtifact | None:
        updates: dict[str, Any] = {}
        if artifact.turn_id is None:
            updates["turn_id"] = turn_id
        if not artifact.tool_call_id:
            updates["tool_call_id"] = f"node:{result.node_id}"
        if not artifact.source_tool:
            provider = str(result.debug.get("provider") or "")
            updates["source_tool"] = "coder" if result.runtime == "coder" and provider in {"", "codex"} else result.runtime
        if artifact.node_id is None:
            updates["node_id"] = result.node_id

        path_info = _resolve_path(
            artifact.session_relative_path or artifact.path,
            self._workspace,
            allow_absolute=True,
        )
        if path_info is not None:
            absolute_path, relative_path = path_info
            updates["path"] = str(absolute_path)
            updates["session_relative_path"] = relative_path
            metadata = dict(artifact.metadata)
            metadata.setdefault("session_relative_path", relative_path)
            updates["metadata"] = metadata
            stat = _stat_path(absolute_path)
            if artifact.size_bytes is None and stat is not None:
                updates["size_bytes"] = stat.st_size
            if not artifact.filename:
                updates["filename"] = absolute_path.name
            if not artifact.mime_type:
                updates["mime_type"] = _guess_mime(absolute_path)

        return replace(artifact, **updates) if updates else artifact

    # ── promote ─────────────────────────────────────────────────────

    def _promote_to_session(self, artifacts: list[ToolArtifact]) -> list[ToolArtifact]:
        return [self._promote_one(artifact) for artifact in artifacts]

    def _promote_one(self, artifact: ToolArtifact) -> ToolArtifact:
        if not artifact.publish:
            return artifact
        if artifact.kind not in {"image", "file"} or not artifact.path:
            return artifact
        source = _resolve_source(artifact.path)
        if source is None:
            return artifact

        artifacts_dir = self._workspace.artifacts_dir.resolve()

        # Already inside session
        if _is_inside(source, artifacts_dir):
            return _patch_relative_path(artifact, source, self._workspace.root_path)

        # Copy into session
        target = self._target_path(artifact, source, artifacts_dir)
        if target is None:
            return artifact

        try:
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            stat = target.stat()
        except OSError:
            logger.warning(
                "session artifact promotion failed artifact_id=%s source=%s",
                artifact.artifact_id, source, exc_info=True,
            )
            return artifact

        metadata = dict(artifact.metadata)
        metadata.update({
            "session_id": self._workspace.session_id,
            "session_artifacts_dir": str(artifacts_dir),
            "source_path": str(source),
            "source_session_relative_path": artifact.session_relative_path,
            "promoted_to_session_artifacts": True,
        })
        return replace(
            artifact,
            path=str(target),
            session_relative_path=_relative_to(target, self._workspace.root_path),
            filename=target.name,
            size_bytes=stat.st_size,
            metadata=metadata,
        )

    def _target_path(self, artifact: ToolArtifact, source: Path, artifacts_dir: Path) -> Path | None:
        target = (artifacts_dir / _promotion_filename(artifact, source)).resolve()
        try:
            target.relative_to(artifacts_dir)
        except ValueError:
            logger.warning(
                "session artifact promotion target escaped artifacts dir artifact_id=%s",
                artifact.artifact_id,
            )
            return None
        return target

    # ── persist ─────────────────────────────────────────────────────

    def _persist_to_store(self, artifacts: list[ToolArtifact], conversation_id: int) -> None:
        upsert = getattr(self._store, "upsert_artifact", None)
        if not callable(upsert):
            return
        for artifact in artifacts:
            try:
                upsert(artifact, conversation_id=conversation_id)
            except Exception:
                logger.exception(
                    "task runtime artifact persistence failed conversation_id=%s artifact_id=%s",
                    conversation_id,
                    getattr(artifact, "artifact_id", ""),
                )


# ── helpers ─────────────────────────────────────────────────────────

def _resolve_path(
    path_text: str | None,
    workspace: SessionWorkspaceRef,
    *,
    allow_absolute: bool,
) -> tuple[Path, str] | None:
    text = str(path_text or "").strip()
    if not text:
        return None
    path = Path(text)
    root = workspace.root_path.resolve()
    try:
        if path.is_absolute():
            if not allow_absolute:
                return None
            resolved = path.expanduser().resolve(strict=True)
        else:
            if any(part == ".." for part in path.parts):
                return None
            resolved = (root / path).resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved, relative.as_posix()


def _resolve_source(path_text: str) -> Path | None:
    try:
        source = Path(path_text).expanduser().resolve(strict=True)
    except OSError:
        return None
    return source if source.is_file() else None


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _patch_relative_path(artifact: ToolArtifact, path: Path, session_root: Path) -> ToolArtifact:
    relative = _relative_to(path, session_root)
    if artifact.session_relative_path == relative:
        return artifact
    metadata = dict(artifact.metadata)
    metadata.setdefault("session_relative_path", relative)
    return replace(artifact, session_relative_path=relative, metadata=metadata)


def _relative_to(path: Path, root: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _promotion_filename(artifact: ToolArtifact, source: Path) -> str:
    raw_name = artifact.filename or source.name or "artifact"
    raw_path = Path(raw_name)
    suffix = raw_path.suffix or source.suffix
    suffix = re.sub(r"[^A-Za-z0-9.]+", "", suffix)[:16]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_path.stem).strip("._-")[:80] or "artifact"
    digest = hashlib.sha256(
        f"{artifact.artifact_id}|{source}".encode("utf-8", errors="replace")
    ).hexdigest()[:12]
    return f"{stem}-{digest}{suffix}"


def _artifact_kind(kind: str, path: Path | None) -> str:
    normalized = str(kind or "").strip().lower()
    if normalized in {"image", "file", "directory", "log", "git_ref"}:
        return normalized
    if path is not None and path.is_dir():
        return "directory"
    if path is not None and _guess_mime(path) in {
        "image/png", "image/jpeg", "image/webp", "image/gif", "image/svg+xml",
    }:
        return "image"
    return "file" if path is not None else "git_ref"


def _stable_id(session_id: str, node_id: str, ref: str, relative_path: str | None) -> str:
    identity = relative_path or ref
    digest = hashlib.sha256(
        f"{session_id}|{node_id}|{identity}".encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    safe_ref = re.sub(r"[^A-Za-z0-9._:-]+", "_", ref).strip("._:-")[:48] or "artifact"
    return f"{session_id}:{node_id}:{safe_ref}:{digest}"


def _stat_path(path: Path | None):
    if path is None:
        return None
    try:
        return path.stat() if path.is_file() else None
    except OSError:
        return None


def _guess_mime(path: Path | None) -> str | None:
    if path is None:
        return None
    return mimetypes.guess_type(path.name)[0]
