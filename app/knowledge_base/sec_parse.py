from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.knowledge_base.parsers.alibaba_pdf import (
    AlibabaDocumentAnalyzeClient,
    AlibabaDocumentAnalyzeResult,
)


@dataclass(frozen=True)
class SecParseItemResult:
    source_file: str
    output_file: str
    task_id: str | None
    status: str
    page_num: int | None
    skipped: bool


@dataclass(frozen=True)
class SecParseBatchResult:
    input_dir: str
    output_dir: str
    files_total: int
    parsed: int
    skipped: int
    failed: int
    items: list[SecParseItemResult]


class SecFilingParseService:
    _success_statuses = {"SUCCESS"}
    _failure_statuses = {"FAILED", "FAIL", "ERROR", "CANCELED", "CANCELLED"}
    _pending_statuses = {"PENDING", "RUNNING", "PROCESSING", "QUEUED", "UNKNOWN"}

    def __init__(
        self,
        *,
        client: AlibabaDocumentAnalyzeClient,
        input_dir: Path,
        output_dir: Path,
    ) -> None:
        self._client = client
        self._input_dir = input_dir
        self._output_dir = output_dir

    def parse_directory(
        self,
        *,
        force: bool = False,
        poll_interval_seconds: float = 3.0,
        timeout_seconds: float = 600.0,
        limit: int | None = None,
        file_names: list[str] | None = None,
    ) -> SecParseBatchResult:
        files = self._list_pdf_files(file_names=file_names)
        if limit is not None:
            files = files[:limit]
        items: list[SecParseItemResult] = []
        parsed = 0
        skipped = 0
        failed = 0
        self._output_dir.mkdir(parents=True, exist_ok=True)

        for file_path in files:
            output_path = self.output_path_for(file_path)
            if output_path.exists() and not force:
                skipped += 1
                items.append(
                    SecParseItemResult(
                        source_file=str(file_path),
                        output_file=str(output_path),
                        task_id=None,
                        status="SKIPPED",
                        page_num=None,
                        skipped=True,
                    )
                )
                continue
            result = self.parse_file(
                file_path=file_path,
                poll_interval_seconds=poll_interval_seconds,
                timeout_seconds=timeout_seconds,
            )
            if result.status in self._success_statuses:
                parsed += 1
            else:
                failed += 1
            items.append(result)

        return SecParseBatchResult(
            input_dir=str(self._input_dir),
            output_dir=str(self._output_dir),
            files_total=len(files),
            parsed=parsed,
            skipped=skipped,
            failed=failed,
            items=items,
        )

    def parse_file(
        self,
        *,
        file_path: str | Path,
        poll_interval_seconds: float = 3.0,
        timeout_seconds: float = 600.0,
    ) -> SecParseItemResult:
        resolved_file = Path(file_path).resolve()
        task = self._client.create_async_task_from_file(resolved_file)
        started_at = time.monotonic()
        final = self._client.get_async_task(task.task_id)
        while final.status in self._pending_statuses:
            if time.monotonic() - started_at > timeout_seconds:
                raise TimeoutError(f"Timed out waiting for Alibaba parse task {task.task_id}")
            time.sleep(poll_interval_seconds)
            final = self._client.get_async_task(task.task_id)

        output_path = self.output_path_for(resolved_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(
                {
                    "source_file": str(resolved_file),
                    "task_id": task.task_id,
                    "saved_at": _utc_now(),
                    "create_task_response": task.raw_response,
                    "final_task_response": final.raw_response,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return SecParseItemResult(
            source_file=str(resolved_file),
            output_file=str(output_path),
            task_id=task.task_id,
            status=final.status,
            page_num=final.page_num,
            skipped=False,
        )

    def output_path_for(self, file_path: str | Path) -> Path:
        path = Path(file_path)
        return self._output_dir / f"{path.stem}.aliyun.json"

    def _list_pdf_files(self, *, file_names: list[str] | None = None) -> list[Path]:
        if file_names:
            files = [(self._input_dir / file_name).resolve() for file_name in file_names]
        else:
            files = sorted(self._input_dir.glob("*.pdf"))
        return [file_path for file_path in files if file_path.is_file()]


def is_parse_success(result: AlibabaDocumentAnalyzeResult) -> bool:
    return result.status in SecFilingParseService._success_statuses


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
