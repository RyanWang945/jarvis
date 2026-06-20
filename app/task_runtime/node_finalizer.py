from __future__ import annotations

import json
import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.llm.client import parse_json_content
from app.llm.model_profiles import LLMNode
from app.llm.model_router import ModelRouter, ResolvedLLM
from app.prompting import PromptRegistry
from app.runtime_usage import usage_record_from_response
from app.task_runtime.node_result import NodeArtifact, NodeError, NodeResult, NodeStatus
from app.task_runtime.planner import PlanNode

logger = logging.getLogger(__name__)


class CodeNodeFinalizerAgent(Protocol):
    def finalize(self, request: "CodeNodeFinalizerRequest") -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class CodeNodeFinalizerRequest:
    node: PlanNode
    user_objective: str
    instruction: str
    provider: str
    provider_ok: bool
    provider_summary: str
    stdout: str
    stderr: str
    exit_code: int | None
    manifest: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMCodeNodeFinalizerAgent:
    model_resolver: Callable[[CodeNodeFinalizerRequest], ResolvedLLM] | None = None
    max_stdout_chars: int = 12000
    max_stderr_chars: int = 4000

    def finalize(self, request: CodeNodeFinalizerRequest) -> dict[str, Any] | None:
        resolved = self._resolve_model(request)
        if not resolved.profile.api_key:
            logger.info("code node llm finalizer skipped node_id=%s reason=missing_api_key", request.node.id)
            return None
        bundle = PromptRegistry().load("coder_node_finalize")
        payload = {
            "user_objective": request.user_objective,
            "node": request.node.model_dump(mode="json"),
            "instruction": request.instruction,
            "provider": request.provider,
            "provider_ok": request.provider_ok,
            "provider_summary": request.provider_summary,
            "exit_code": request.exit_code,
            "stdout": _truncate(request.stdout, limit=self.max_stdout_chars),
            "stderr": _truncate(request.stderr, limit=self.max_stderr_chars),
            "manifest": request.manifest or {},
            "metadata": _jsonable(request.metadata),
        }
        response = resolved.client.chat_normalized(
            bundle.render({"input_json": json.dumps(payload, ensure_ascii=False, default=str)}),
            response_format=bundle.response_format or ({"type": "json_object"} if resolved.profile.supports_json_object else None),
        )
        result = parse_json_content({"content": response.content})
        usage_record = usage_record_from_response(response, stage="coder_node_finalizer")
        if usage_record is not None:
            records = result.get("usage_records")
            if not isinstance(records, list):
                records = []
            records.append(usage_record)
            result["usage_records"] = records
        return result

    def _resolve_model(self, request: CodeNodeFinalizerRequest) -> ResolvedLLM:
        if self.model_resolver is not None:
            return self.model_resolver(request)
        return ModelRouter().resolve(LLMNode.SUMMARY, request.metadata)


class ManifestArtifact(BaseModel):
    model_config = ConfigDict(extra="allow")

    ref: str | None = None
    id: str | None = None
    artifact_id: str | None = None
    kind: str = "file"
    type: str | None = None
    path: str | None = None
    session_relative_path: str | None = None
    filename: str | None = None
    name: str | None = None
    description: str = ""
    mime_type: str | None = None
    size_bytes: int | None = None
    source_tool: str | None = None
    publish: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class CodeNodeManifest(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: NodeStatus | None = None
    summary: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ManifestArtifact] = Field(default_factory=list)
    artifact_candidates: list[ManifestArtifact] = Field(default_factory=list)
    missing_expected_artifacts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class CodeNodeFinalizer:
    llm_agent: CodeNodeFinalizerAgent | None = None

    def finalize(
        self,
        *,
        node: PlanNode,
        user_objective: str,
        instruction: str,
        provider: str,
        provider_ok: bool,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        provider_summary: str,
        legacy_artifacts: list[str],
        metadata: dict[str, Any],
        approval_required: bool = False,
        approval_data: dict[str, Any] | None = None,
        approval_requests: list[dict[str, Any]] | None = None,
        session_root: Path | None = None,
        node_workspace: Path | None = None,
        manifest_path: Path | None = None,
    ) -> NodeResult:
        manifest_payload, manifest_warnings = _load_manifest(manifest_path)
        llm_payload: dict[str, Any] | None = None
        llm_warnings: list[str] = []
        if self.llm_agent is not None:
            try:
                llm_payload = self.llm_agent.finalize(
                    CodeNodeFinalizerRequest(
                        node=node,
                        user_objective=user_objective,
                        instruction=instruction,
                        provider=provider,
                        provider_ok=provider_ok,
                        provider_summary=provider_summary,
                        stdout=stdout,
                        stderr=stderr,
                        exit_code=exit_code,
                        manifest=manifest_payload,
                        metadata=metadata,
                    )
                )
            except Exception:
                logger.warning("code node llm finalizer failed node_id=%s", node.id, exc_info=True)
                llm_warnings.append("llm_finalizer_failed")

        manifest = _coerce_manifest(manifest_payload, source="manifest", warnings=manifest_warnings)
        llm_manifest = _coerce_manifest(llm_payload, source="llm_finalizer", warnings=llm_warnings)
        status = _final_status(
            provider_ok=provider_ok,
            approval_required=approval_required,
            manifest_status=manifest.status or llm_manifest.status,
        )
        summary = _final_summary(
            manifest.summary,
            llm_manifest.summary,
            stdout,
            provider_summary,
            stderr,
            provider=provider,
        )
        warnings = [*manifest_warnings, *llm_warnings, *manifest.warnings, *llm_manifest.warnings]
        artifacts = [
            *_validated_manifest_artifacts(
                [*manifest.artifacts, *manifest.artifact_candidates, *llm_manifest.artifact_candidates],
                session_root=session_root,
                node_workspace=node_workspace,
                provider=provider,
                node_id=node.id,
                warnings=warnings,
            ),
            *[_artifact_from_legacy_string(item) for item in legacy_artifacts],
        ]
        debug = {
            "provider": provider,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
        }
        data: dict[str, Any] = {}
        if approval_data:
            data.update(approval_data)
        metadata_payload = dict(metadata)
        git = {
            key: metadata_payload.pop(key)
            for key in ("repo_workspace", "node_commit", "node_merge")
            if key in metadata_payload
        }
        metadata_usage_records = metadata_payload.pop("usage_records", None)
        data.update(metadata_payload)
        usage_records = []
        if isinstance(metadata_usage_records, list):
            usage_records.extend(item for item in metadata_usage_records if isinstance(item, dict))
        for payload in (manifest_payload, llm_payload):
            if isinstance(payload, dict) and isinstance(payload.get("usage_records"), list):
                usage_records.extend(item for item in payload["usage_records"] if isinstance(item, dict))
        finalizer_data = {
            "manifest_path": str(manifest_path) if manifest_path is not None else None,
            "manifest_loaded": manifest_payload is not None,
            "llm_finalizer_used": self.llm_agent is not None,
            "warnings": warnings,
        }
        if manifest.missing_expected_artifacts or llm_manifest.missing_expected_artifacts:
            finalizer_data["missing_expected_artifacts"] = [
                *manifest.missing_expected_artifacts,
                *llm_manifest.missing_expected_artifacts,
            ]
        debug["finalizer"] = finalizer_data

        return NodeResult(
            node_id=node.id,
            runtime="coder",
            status=status,
            summary=summary,
            artifacts=artifacts,
            approval_requests=approval_requests or [],
            usage_records=usage_records,
            git=git,
            debug=debug,
            data=data,
            error=_final_error(
                status=status,
                provider_ok=provider_ok,
                approval_required=approval_required,
                summary=summary,
            ),
        )


def _load_manifest(path: Path | None) -> tuple[dict[str, Any] | None, list[str]]:
    if path is None or not path.exists():
        return None, []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"node_manifest_unreadable:{exc}"]
    if not isinstance(payload, dict):
        return None, ["node_manifest_not_object"]
    return payload, []


def _coerce_manifest(payload: dict[str, Any] | None, *, source: str, warnings: list[str]) -> CodeNodeManifest:
    if payload is None:
        return CodeNodeManifest()
    try:
        return CodeNodeManifest.model_validate(payload)
    except ValidationError as exc:
        warnings.append(f"{source}_invalid:{exc.errors()[0].get('msg', 'validation_error')}")
        return CodeNodeManifest()


def _final_status(
    *,
    provider_ok: bool,
    approval_required: bool,
    manifest_status: NodeStatus | None,
) -> NodeStatus:
    if approval_required:
        return "blocked"
    if not provider_ok:
        return "failed"
    return manifest_status or "completed"


def _final_summary(
    *values: str | None,
    provider: str,
) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return f"Coder provider {provider} finished."


def _validated_manifest_artifacts(
    artifacts: list[ManifestArtifact],
    *,
    session_root: Path | None,
    node_workspace: Path | None,
    provider: str,
    node_id: str,
    warnings: list[str],
) -> list[NodeArtifact]:
    result: list[NodeArtifact] = []
    seen: set[str] = set()
    for item in artifacts:
        raw_path = _optional_text(item.session_relative_path or item.path)
        ref = _optional_text(item.ref or item.id or item.artifact_id or _ref_from_path(raw_path))
        if not ref:
            warnings.append("artifact_candidate_rejected:missing_ref")
            continue
        resolved_path, relative_path = _resolve_session_relative_path(
            raw_path,
            session_root=session_root,
            node_workspace=node_workspace,
        )
        if raw_path and relative_path is None:
            warnings.append(f"artifact_candidate_rejected:{ref}:invalid_session_relative_path")
            continue
        kind = str(item.kind or item.type or "artifact")
        if kind in {"file", "image", "log", "directory"}:
            if resolved_path is None or not resolved_path.exists():
                warnings.append(f"artifact_candidate_rejected:{ref}:missing_file")
                continue
        dedupe_key = f"{ref}:{relative_path or ''}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        stat = _stat_path(resolved_path)
        metadata = dict(item.metadata)
        metadata.setdefault("source_node", node_id)
        if relative_path:
            metadata.setdefault("session_relative_path", relative_path)
        result.append(
            NodeArtifact(
                ref=ref,
                artifact_id=_optional_text(item.artifact_id or item.id),
                kind=kind,
                name=_optional_text(item.name or item.filename),
                description=str(item.description or ""),
                path=relative_path or raw_path,
                session_relative_path=relative_path,
                mime_type=_optional_text(item.mime_type) or (_guess_mime(resolved_path) if resolved_path is not None else None),
                filename=_optional_text(item.filename or item.name) or (resolved_path.name if resolved_path is not None else None),
                size_bytes=(
                    item.size_bytes
                    if item.size_bytes is not None
                    else (stat.st_size if stat is not None and resolved_path is not None and resolved_path.is_file() else None)
                ),
                source_tool=_optional_text(item.source_tool) or provider,
                publish=bool(item.publish),
                metadata=metadata,
            )
        )
    return result


def _resolve_session_relative_path(
    raw_path: str | None,
    *,
    session_root: Path | None,
    node_workspace: Path | None,
) -> tuple[Path | None, str | None]:
    if not raw_path:
        return None, None
    if session_root is None:
        return None, None
    text = raw_path.replace("\\", "/").strip()
    path = Path(text)
    if path.is_absolute():
        return None, None
    root = session_root.resolve()
    candidate = (root / path).resolve()
    try:
        relative = candidate.relative_to(root).as_posix()
    except ValueError:
        return None, None
    if node_workspace is not None:
        try:
            candidate.relative_to(node_workspace.resolve())
        except ValueError:
            if not relative.startswith("artifacts/"):
                return None, None
    return candidate, relative


def _artifact_from_legacy_string(value: str) -> NodeArtifact:
    text = str(value)
    kind, _, ref = text.partition(":")
    if not ref:
        kind = "artifact"
        ref = text
    return NodeArtifact(ref=ref, kind=kind or "artifact", name=ref, publish=False, metadata={"legacy": text})


def _final_error(
    *,
    status: NodeStatus,
    provider_ok: bool,
    approval_required: bool,
    summary: str,
) -> NodeError | None:
    if approval_required:
        return NodeError(code="coder_approval_required", message=summary, retryable=False)
    if status == "failed" and not provider_ok:
        return NodeError(code="coder_provider_failed", message=summary, retryable=False)
    if status == "failed":
        return NodeError(code="coder_finalizer_failed", message=summary, retryable=False)
    if status == "blocked":
        return NodeError(code="coder_finalizer_blocked", message=summary, retryable=False)
    return None


def _ref_from_path(path: str | None) -> str | None:
    if not path:
        return None
    name = Path(path).name
    return name or None


def _stat_path(path: Path | None):
    if path is None:
        return None
    try:
        return path.stat()
    except OSError:
        return None


def _guess_mime(path: Path | None) -> str | None:
    if path is None:
        return None
    return mimetypes.guess_type(path.name)[0]


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _truncate(value: Any, *, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, default=str)
        return value
    except TypeError:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
