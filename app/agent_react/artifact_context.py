from __future__ import annotations

from typing import Any


def artifact_record_to_context(record: Any) -> dict[str, Any]:
    return {
        "artifact_id": getattr(record, "artifact_id", None),
        "kind": getattr(record, "kind", None),
        "filename": getattr(record, "filename", None),
        "path": getattr(record, "path", None),
        "mime_type": getattr(record, "mime_type", None),
        "size_bytes": getattr(record, "size_bytes", None),
        "source_tool": getattr(record, "source_tool", None),
        "turn_id": getattr(record, "turn_id", None),
        "status": getattr(record, "status", None),
        "created_at": getattr(record, "created_at", None),
        "updated_at": getattr(record, "updated_at", None),
    }


def artifact_records_to_context(records: list[Any], *, limit: int = 5) -> list[dict[str, Any]]:
    return [artifact_record_to_context(record) for record in records[:limit]]
