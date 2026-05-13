from __future__ import annotations

import json
from pathlib import Path

from app.tools.common import ToolArtifact, ToolExecutionRequest, ToolExecutionResult


def run_deliver_file(request: ToolExecutionRequest) -> ToolExecutionResult:
    from app.agent_react.artifacts import resolve_channel_attachments
    from app.agent_react.delivery import get_delivery_manager

    artifact_id = str(request.args.get("artifact_id") or "").strip()
    raw_path = str(request.args.get("path") or request.args.get("file_path") or "").strip()
    platform = str(request.args.get("platform") or "").strip()
    external_chat_id = str(request.args.get("external_chat_id") or "").strip()
    conversation_id = _optional_int(request.args.get("conversation_id"))
    turn_id = _optional_int(request.args.get("turn_id"))
    purpose = str(request.args.get("_delivery_purpose") or "explicit").strip() or "explicit"

    if not platform or not external_chat_id:
        return ToolExecutionResult(ok=False, exit_code=None, stderr="delivery channel context is missing", summary="Missing delivery context.")

    store = _conversation_store()
    artifact: ToolArtifact | None = None
    if artifact_id:
        record = getattr(store, "get_artifact", lambda _artifact_id: None)(artifact_id)
        if record is None:
            return ToolExecutionResult(ok=False, exit_code=None, stderr=f"artifact not found: {artifact_id}", summary="Artifact not found.")
        artifact = ToolArtifact(
            artifact_id=record.artifact_id,
            kind=record.kind,  # type: ignore[arg-type]
            turn_id=record.turn_id,
            tool_call_id=record.tool_call_id,
            path=record.path,
            mime_type=record.mime_type,
            filename=record.filename,
            size_bytes=record.size_bytes,
            source_tool=record.source_tool,
            metadata=dict(record.metadata),
        )
    elif raw_path:
        path = Path(raw_path)
        if not path.is_absolute() and request.workdir:
            path = Path(request.workdir) / path
        artifact = ToolArtifact(
            artifact_id=f"manual:{conversation_id or 0}:{_path_digest(str(path))}",
            kind="file",
            turn_id=turn_id,
            path=str(path),
            filename=str(request.args.get("filename") or path.name),
            source_tool="deliver_file",
            metadata={"manual_path": True},
        )
        if conversation_id is not None:
            upsert = getattr(store, "upsert_artifact", None)
            if upsert is not None:
                upsert(artifact, conversation_id=conversation_id)
    else:
        return ToolExecutionResult(ok=False, exit_code=None, stderr="artifact_id or path is required", summary="Missing file target.")

    resolution = resolve_channel_attachments([artifact], turn_id=turn_id if artifact_id else None)
    if not resolution.attachments:
        reason = resolution.rejected[0].reason if resolution.rejected else "no_deliverable_attachment"
        update_status = getattr(store, "update_artifact_status", None)
        if update_status is not None:
            update_status(artifact.artifact_id, status="rejected", metadata_patch={"delivery_rejection": reason})
        return ToolExecutionResult(ok=False, exit_code=None, stderr=reason, summary=f"File not delivered: {reason}.")

    manager = get_delivery_manager(store, platform)
    if manager is None:
        return ToolExecutionResult(ok=False, exit_code=None, stderr=f"delivery handler unavailable: {platform}", summary="Delivery handler unavailable.")

    results = manager.deliver_attachments(
        external_chat_id=external_chat_id,
        attachments=resolution.attachments,
        conversation_id=conversation_id,
        turn_id=turn_id,
        purpose=purpose,
    )
    ok = all(item.status in {"sent", "already_sent"} for item in results)
    payload = {
        "status": "sent" if ok else "failed",
        "results": [item.__dict__ for item in results],
    }
    return ToolExecutionResult(
        ok=ok,
        exit_code=0 if ok else None,
        stdout=json.dumps(payload, ensure_ascii=False),
        stderr="" if ok else json.dumps(payload, ensure_ascii=False),
        summary="File delivered." if ok else "File delivery failed.",
    )


def _conversation_store():
    from app.api.agent import get_conversation_store

    return get_conversation_store()


def _optional_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _path_digest(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]
