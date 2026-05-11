from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.llm.client import ChatClient, LLMMessage, parse_json_content


DEFAULT_MODEL = "deepseek-v4-flash"


@dataclass(frozen=True)
class RefineSummary:
    db_path: str
    total_candidates: int
    refined: int
    already_refined: int
    needs_review: int
    skipped_no_span_v1: int
    dry_run: bool
    backup_path: str | None
    output_jsonl: str | None


@dataclass(frozen=True)
class RefineResult:
    status: str
    payload: dict[str, Any]
    answer_text: str | None = None
    reason: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use an LLM to refine eval gold evidence from chunk-level spans to answer-level spans."
    )
    parser.add_argument("--db-path", default="data/knowledge.db")
    parser.add_argument("--dataset-id", default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--max-workers", type=int, default=1)
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--backup-dir", default=None)
    parser.add_argument("--output-jsonl", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = refine_eval_gold_spans(
        Path(args.db_path),
        dataset_id=args.dataset_id,
        model=args.model,
        limit=args.limit,
        offset=args.offset,
        max_workers=args.max_workers,
        sleep_seconds=args.sleep_seconds,
        force=args.force,
        dry_run=args.dry_run,
        create_backup=not args.no_backup,
        backup_dir=Path(args.backup_dir) if args.backup_dir else None,
        output_jsonl=Path(args.output_jsonl) if args.output_jsonl else None,
    )
    print(json.dumps(asdict(summary), ensure_ascii=False, indent=2))
    return 0


def refine_eval_gold_spans(
    db_path: Path,
    *,
    dataset_id: str | None = None,
    model: str = DEFAULT_MODEL,
    limit: int | None = None,
    offset: int = 0,
    max_workers: int = 1,
    sleep_seconds: float = 0.0,
    force: bool = False,
    dry_run: bool = False,
    create_backup: bool = True,
    backup_dir: Path | None = None,
    output_jsonl: Path | None = None,
) -> RefineSummary:
    resolved_db_path = db_path.resolve()
    if not resolved_db_path.exists():
        raise FileNotFoundError(resolved_db_path)

    backup_path: Path | None = None
    if create_backup and not dry_run:
        backup_path = _backup_database(resolved_db_path, backup_dir=backup_dir)

    output_handle = None
    if output_jsonl is not None:
        output_jsonl.parent.mkdir(parents=True, exist_ok=True)
        output_handle = output_jsonl.open("a", encoding="utf-8")

    conn = sqlite3.connect(resolved_db_path, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000")

    refined = 0
    already_refined = 0
    needs_review = 0
    skipped_no_span_v1 = 0
    total_candidates = 0
    try:
        rows = _load_queries(conn, dataset_id=dataset_id, limit=limit, offset=offset)
        tasks: list[dict[str, Any]] = []
        for row in rows:
            payload = _load_json(row["gold_evidence_json"])
            if not _is_span_v1(payload):
                skipped_no_span_v1 += 1
                continue
            if _has_answer_evidence(payload) and not force:
                already_refined += 1
                continue
            tasks.append({"row": dict(row), "payload": payload})

        total_candidates = len(tasks)
        if not tasks:
            return RefineSummary(
                db_path=str(resolved_db_path),
                total_candidates=0,
                refined=refined,
                already_refined=already_refined,
                needs_review=needs_review,
                skipped_no_span_v1=skipped_no_span_v1,
                dry_run=dry_run,
                backup_path=str(backup_path) if backup_path else None,
                output_jsonl=str(output_jsonl) if output_jsonl else None,
            )

        completed = 0
        max_workers = max(1, min(max_workers, len(tasks)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            active: dict[Future[RefineResult], dict[str, Any]] = {}
            next_index = 0
            while next_index < len(tasks) and len(active) < max_workers:
                task = tasks[next_index]
                next_index += 1
                active[executor.submit(_refine_task, model, task)] = task

            while active:
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    task = active.pop(future)
                    row = task["row"]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = _needs_review_payload(
                            payload=task["payload"],
                            model=model,
                            reason=f"llm_error:{type(exc).__name__}",
                            llm_answer={"error": str(exc)},
                        )
                    completed += 1

                    if result.status == "refined":
                        refined += 1
                    else:
                        needs_review += 1

                    if output_handle is not None:
                        output_handle.write(
                            json.dumps(
                                {
                                    "query_id": row["query_id"],
                                    "dataset_id": row["dataset_id"],
                                    "status": result.status,
                                    "answer_text": result.answer_text,
                                    "reason": result.reason,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        output_handle.flush()

                    if not dry_run:
                        conn.execute(
                            "UPDATE kb_eval_queries SET gold_evidence_json = ? WHERE query_id = ?",
                            (json.dumps(result.payload, ensure_ascii=False, sort_keys=True), row["query_id"]),
                        )
                        conn.commit()

                    print(
                        json.dumps(
                            {
                                "event": "refine_progress",
                                "completed": completed,
                                "total": total_candidates,
                                "query_id": row["query_id"],
                                "status": result.status,
                                "reason": result.reason,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    if sleep_seconds > 0:
                        time.sleep(sleep_seconds)

                    while next_index < len(tasks) and len(active) < max_workers:
                        next_task = tasks[next_index]
                        next_index += 1
                        active[executor.submit(_refine_task, model, next_task)] = next_task
    finally:
        if output_handle is not None:
            output_handle.close()
        conn.close()

    return RefineSummary(
        db_path=str(resolved_db_path),
        total_candidates=total_candidates,
        refined=refined,
        already_refined=already_refined,
        needs_review=needs_review,
        skipped_no_span_v1=skipped_no_span_v1,
        dry_run=dry_run,
        backup_path=str(backup_path) if backup_path else None,
        output_jsonl=str(output_jsonl) if output_jsonl else None,
    )


def _refine_task(model: str, task: dict[str, Any]) -> RefineResult:
    client = _deepseek_client(model=model)
    row = task["row"]
    payload = task["payload"]
    return refine_payload_with_answer_text(
        payload=payload,
        query_text=row["query_text"],
        gold_answer=row["gold_answer"],
        model=model,
        llm_answer=_ask_llm_for_answer_span(client=client, row=row, payload=payload),
    )


def refine_payload_with_answer_text(
    *,
    payload: dict[str, Any],
    query_text: str,
    gold_answer: str | None,
    model: str,
    llm_answer: dict[str, Any],
) -> RefineResult:
    evidence = _evidence_list(payload)
    legacy_evidence = _legacy_evidence(evidence)
    base = _base_legacy_evidence(legacy_evidence)
    if base is None:
        return _needs_review_payload(
            payload=payload,
            model=model,
            reason="missing_legacy_chunk_evidence",
        )

    answer_text = str(llm_answer.get("answer_text") or "").strip()
    if not answer_text:
        return _needs_review_payload(
            payload=payload,
            model=model,
            reason="empty_answer_text",
            llm_answer=llm_answer,
        )

    base_text = str(base.get("evidence_text") or "")
    offset = _find_text_offset(base_text, answer_text)
    if offset is None:
        return _needs_review_payload(
            payload=payload,
            model=model,
            reason="answer_text_not_found",
            answer_text=answer_text,
            llm_answer=llm_answer,
        )

    answer_start = int(base["char_start"]) + offset
    answer_end = answer_start + len(answer_text)
    answer_evidence = {
        "evidence_id": f"{base.get('evidence_id', 'evidence')}:answer",
        "type": "span",
        "role": "answer",
        "doc_id": base["doc_id"],
        "char_start": answer_start,
        "char_end": answer_end,
        "source_chunk_id": base.get("source_chunk_id"),
        "source_chunk_profile_id": base.get("source_chunk_profile_id"),
        "source_chunk_index": base.get("source_chunk_index"),
        "evidence_text": answer_text,
        "generated_by": f"llm:{model}",
        "query_text": query_text,
        "gold_answer": gold_answer,
        "confidence": llm_answer.get("confidence"),
    }

    next_payload = dict(payload)
    next_payload["version"] = "span_v1"
    next_payload["refinement"] = {
        "status": "refined",
        "model": model,
        "refined_at": _utc_now(),
        "method": "llm_answer_span_v1",
    }
    next_payload["evidence"] = [answer_evidence] + [_with_legacy_role(item) for item in legacy_evidence]
    return RefineResult(status="refined", payload=next_payload, answer_text=answer_text)


def _needs_review_payload(
    *,
    payload: dict[str, Any],
    model: str,
    reason: str,
    answer_text: str | None = None,
    llm_answer: dict[str, Any] | None = None,
) -> RefineResult:
    next_payload = dict(payload)
    next_payload["evidence"] = [_with_legacy_role(item) for item in _evidence_list(payload)]
    next_payload["refinement"] = {
        "status": "needs_review",
        "model": model,
        "refined_at": _utc_now(),
        "method": "llm_answer_span_v1",
        "reason": reason,
        "llm_answer": llm_answer,
    }
    return RefineResult(status="needs_review", payload=next_payload, answer_text=answer_text, reason=reason)


def _deepseek_client(*, model: str) -> ChatClient:
    settings = get_settings()
    if not settings.deepseek_api_key:
        raise ValueError("JARVIS_DEEPSEEK_API_KEY is required for gold span refinement")
    return ChatClient(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=model,
        timeout_seconds=settings.deepseek_timeout_seconds or settings.llm_timeout_seconds,
        provider="deepseek",
        supports_reasoning_content=True,
    )


def _ask_llm_for_answer_span(*, client: ChatClient, row: sqlite3.Row, payload: dict[str, Any]) -> dict[str, Any]:
    base = _base_legacy_evidence(_legacy_evidence(_evidence_list(payload)))
    if base is None:
        return {}
    message = client.chat(
        [
            LLMMessage(
                role="system",
                content=(
                    "你是检索评测数据标注助手。你的任务是在给定证据文本中找出能回答 query 的最小充分原文片段。"
                    "必须只返回严格 JSON。answer_text 必须是 evidence_text 中逐字连续出现的原文子串，不要改写、总结或翻译。"
                ),
            ),
            LLMMessage(
                role="user",
                content=json.dumps(
                    {
                        "query": row["query_text"],
                        "gold_answer": row["gold_answer"],
                        "instructions": [
                            "选择最小但足够回答 query 的连续原文片段。",
                            "优先包含答案实体及必要限定词，不要包含整段冗余背景。",
                            "answer_text 必须能在 evidence_text 中精确匹配。",
                            "如果 evidence_text 不能回答 query，answer_text 返回空字符串。",
                            "返回 JSON: {\"answer_text\": \"...\", \"confidence\": 0.0到1.0, \"reason\": \"...\"}",
                        ],
                        "evidence_text": str(base.get("evidence_text") or "")[:6000],
                    },
                    ensure_ascii=False,
                ),
            ),
        ],
        response_format={"type": "json_object"},
    )
    return parse_json_content(message)


def _load_queries(
    conn: sqlite3.Connection,
    *,
    dataset_id: str | None,
    limit: int | None,
    offset: int,
) -> list[sqlite3.Row]:
    sql = """
        SELECT query_id, dataset_id, doc_id, target_chunk_id, query_text, gold_answer, gold_evidence_json
        FROM kb_eval_queries
    """
    params: list[Any] = []
    if dataset_id is not None:
        sql += " WHERE dataset_id = ?"
        params.append(dataset_id)
    sql += " ORDER BY created_at, query_id"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    elif offset:
        sql += " LIMIT -1 OFFSET ?"
        params.append(offset)
    return conn.execute(sql, params).fetchall()


def _load_json(raw: Any) -> Any:
    if raw is None:
        return None
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _is_span_v1(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("version") == "span_v1"


def _has_answer_evidence(payload: dict[str, Any]) -> bool:
    return any(item.get("role") == "answer" for item in _evidence_list(payload) if isinstance(item, dict))


def _evidence_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        return []
    return [item for item in evidence if isinstance(item, dict)]


def _legacy_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    legacy = [
        item
        for item in evidence
        if item.get("role") in {None, "legacy_chunk"} and item.get("type", "span") == "span"
    ]
    if legacy:
        return legacy
    return [item for item in evidence if item.get("type", "span") == "span"]


def _base_legacy_evidence(evidence: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in evidence:
        if item.get("source_chunk_id") and item.get("evidence_text"):
            return item
    return evidence[0] if evidence else None


def _with_legacy_role(evidence: dict[str, Any]) -> dict[str, Any]:
    item = dict(evidence)
    if item.get("role") != "answer":
        item["role"] = "legacy_chunk"
    return item


def _find_text_offset(text: str, needle: str) -> int | None:
    offset = text.find(needle)
    if offset >= 0:
        return offset
    compact_needle = " ".join(needle.split())
    if compact_needle != needle:
        offset = text.find(compact_needle)
        if offset >= 0:
            return offset
    return None


def _backup_database(db_path: Path, *, backup_dir: Path | None) -> Path:
    target_dir = backup_dir.resolve() if backup_dir else db_path.parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = target_dir / f"{db_path.stem}_before_gold_refine_{timestamp}{db_path.suffix}"
    shutil.copy2(db_path, backup_path)
    return backup_path


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
