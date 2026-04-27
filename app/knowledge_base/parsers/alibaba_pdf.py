from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class AlibabaDocumentAnalyzeTask:
    task_id: str
    request_id: str | None
    latency_ms: float | int | None
    raw_response: dict[str, Any]


@dataclass(frozen=True)
class AlibabaDocumentAnalyzeResult:
    task_id: str
    status: str
    content: str | None
    content_type: str | None
    page_num: int | None
    error: str | None
    usage: dict[str, Any]
    request_id: str | None
    latency_ms: float | int | None
    raw_response: dict[str, Any]


class AlibabaDocumentAnalyzeClient:
    _max_request_body_bytes = 8 * 1024 * 1024

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        workspace: str = "default",
        service_id: str = "ops-document-analyze-002",
        image_storage: str = "base64",
        enable_semantic: bool = True,
        timeout_seconds: float = 120.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint.rstrip("/")
        self._workspace = workspace
        self._service_id = service_id
        self._image_storage = image_storage
        self._enable_semantic = enable_semantic
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client

    def create_async_task_from_file(
        self,
        file_path: str | Path,
        *,
        file_name: str | None = None,
        file_type: str | None = None,
        enable_semantic: bool | None = None,
    ) -> AlibabaDocumentAnalyzeTask:
        path = Path(file_path)
        content = base64.b64encode(path.read_bytes()).decode("ascii")
        resolved_file_name = file_name or path.name
        resolved_file_type = file_type or path.suffix.lstrip(".").lower() or None
        self._validate_request_size(content)
        return self.create_async_task(
            file_content_base64=content,
            file_name=resolved_file_name,
            file_type=resolved_file_type,
            enable_semantic=enable_semantic,
        )

    def create_async_task(
        self,
        *,
        document_url: str | None = None,
        file_content_base64: str | None = None,
        file_name: str | None = None,
        file_type: str | None = None,
        enable_semantic: bool | None = None,
    ) -> AlibabaDocumentAnalyzeTask:
        payload = self._build_payload(
            document_url=document_url,
            file_content_base64=file_content_base64,
            file_name=file_name,
            file_type=file_type,
            enable_semantic=enable_semantic,
        )
        response = self._client.post(
            self._async_url,
            headers=self._headers,
            json=payload,
        )
        response.raise_for_status()
        body = response.json()
        result = body.get("result") or {}
        task_id = result.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise RuntimeError(f"Alibaba document analyze create task response missing task_id: {body}")
        return AlibabaDocumentAnalyzeTask(
            task_id=task_id,
            request_id=_optional_str(body.get("request_id")),
            latency_ms=body.get("latency"),
            raw_response=body if isinstance(body, dict) else {},
        )

    def get_async_task(self, task_id: str) -> AlibabaDocumentAnalyzeResult:
        response = self._client.get(
            f"{self._async_url}/task-status",
            headers=self._headers,
            params={"task_id": task_id},
        )
        response.raise_for_status()
        body = response.json()
        result = body.get("result") or {}
        data = result.get("data") or {}
        usage = body.get("usage") or {}
        return AlibabaDocumentAnalyzeResult(
            task_id=_optional_str(result.get("task_id")) or task_id,
            status=_optional_str(result.get("status")) or "UNKNOWN",
            content=_optional_str(data.get("content")),
            content_type=_optional_str(data.get("content_type")),
            page_num=_optional_int(data.get("page_num")),
            error=_optional_str(result.get("error")),
            usage=usage if isinstance(usage, dict) else {},
            request_id=_optional_str(body.get("request_id")),
            latency_ms=body.get("latency"),
            raw_response=body if isinstance(body, dict) else {},
        )

    def _build_payload(
        self,
        *,
        document_url: str | None,
        file_content_base64: str | None,
        file_name: str | None,
        file_type: str | None,
        enable_semantic: bool | None,
    ) -> dict[str, Any]:
        if bool(document_url) == bool(file_content_base64):
            raise ValueError("Exactly one of document_url or file_content_base64 must be provided")
        document: dict[str, Any] = {}
        if document_url:
            document["url"] = document_url
        if file_content_base64:
            document["content"] = file_content_base64
        if file_name:
            document["file_name"] = file_name
        if file_type:
            document["file_type"] = file_type
        if "content" in document and "file_name" not in document:
            raise ValueError("file_name is required when uploading base64 document content")
        return {
            "service_id": self._service_id,
            "document": document,
            "output": {
                "image_storage": self._image_storage,
            },
            "strategy": {
                "enable_semantic": self._enable_semantic if enable_semantic is None else enable_semantic,
            },
        }

    def _validate_request_size(self, file_content_base64: str) -> None:
        estimated_body_size = len(file_content_base64.encode("ascii")) + 2048
        if estimated_body_size > self._max_request_body_bytes:
            raise ValueError(
                "Encoded document payload exceeds Alibaba document analyze request body limit of 8MB"
            )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    @property
    def _async_url(self) -> str:
        return (
            f"{self._endpoint}/v3/openapi/workspaces/{self._workspace}"
            f"/document-analyze/{self._service_id}/async"
        )

    @property
    def _client(self) -> httpx.Client:
        if self._http_client is not None:
            return self._http_client
        return httpx.Client(timeout=self._timeout_seconds, trust_env=False)


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None
