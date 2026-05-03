from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.api.agent import get_conversation_store
from app.main import create_app


DEFAULT_DATASET = Path("tests/fixtures/agent_eval/smoke.jsonl")
DEFAULT_OUTPUT_ROOT = Path("data/eval_runs")


@dataclass(frozen=True)
class EvalCase:
    id: str
    category: str
    description: str
    messages: list[dict[str, Any]]
    expected_tools: list[str]
    forbidden_tools: list[str]
    required_status: str
    max_tool_calls: dict[str, int]
    required_reply_contains: list[str]
    requires: list[str]
    success_criteria: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run opt-in Jarvis agent E2E eval cases.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Validate and summarize cases without running Jarvis.")
    parser.add_argument(
        "--allow-requires",
        action="append",
        default=[],
        help="Run only cases whose requires values are in this allow-list. Repeatable.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cases = load_cases(args.dataset)
    if args.case_id:
        selected = set(args.case_id)
        cases = [case for case in cases if case.id in selected]
    if args.limit is not None:
        cases = cases[: args.limit]

    if args.dry_run:
        print(json.dumps(_dataset_summary(cases), ensure_ascii=False, indent=2))
        return 0

    if os.environ.get("JARVIS_RUN_AGENT_EVAL") != "1":
        raise SystemExit("Set JARVIS_RUN_AGENT_EVAL=1 to run real agent evals.")

    allowed_requires = set(args.allow_requires)
    runnable = [
        case
        for case in cases
        if not case.requires or all(requirement in allowed_requires for requirement in case.requires)
    ]
    skipped = [case for case in cases if case not in runnable]

    run_dir = _create_run_dir(args.output_root)
    client = TestClient(create_app())
    results = []
    for case in runnable:
        result = run_case(client, case, run_dir)
        results.append(result)
        print(
            json.dumps(
                {
                    "case_id": case.id,
                    "passed": result["passed"],
                    "status": result["status"],
                    "tools": result["tool_names"],
                    "elapsed_ms": result["metrics"]["elapsed_ms"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    report = build_report(results, skipped)
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    (run_dir / "run.json").write_text(
        json.dumps({"results": results, "skipped": [case.__dict__ for case in skipped]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(str(run_dir))
    return 0 if all(result["passed"] for result in results) else 1


def load_cases(path: Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_number, raw_line in enumerate(fh, start=1):
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            cases.append(_case_from_payload(payload, path=path, line_number=line_number))
    return cases


def run_case(client: TestClient, case: EvalCase, run_dir: Path) -> dict[str, Any]:
    started = time.perf_counter()
    external_chat_id = f"agent-eval-{_timestamp()}-{case.id}"
    conversation_id = None
    turn_ids: list[int] = []
    run_responses: list[dict[str, Any]] = []

    for index, message in enumerate(case.messages, start=1):
        response = client.post(
            "/messages",
            json={
                "platform": "agent_eval",
                "external_chat_id": external_chat_id,
                "chat_type": message.get("chat_type", "dm"),
                "sender": message.get("sender", {"platform_user_id": "agent_eval", "display_name": "Agent Eval"}),
                "content": message["content"],
                "content_type": message.get("content_type", "text"),
                "external_message_id": message.get("external_message_id", f"{case.id}-{index}"),
                "mentions": message.get("mentions", []),
                "metadata": message.get("metadata", {}),
                "raw_payload": {"source": "agent_eval", "case_id": case.id},
            },
        )
        response.raise_for_status()
        ingested = response.json()
        conversation_id = ingested["conversation_id"]
        if ingested.get("turn_id") is None:
            continue
        turn_ids.append(ingested["turn_id"])
        run_response = client.post(f"/turns/{ingested['turn_id']}/run")
        run_response.raise_for_status()
        run_responses.append(run_response.json())

    if conversation_id is None:
        raise RuntimeError(f"case did not create a conversation: {case.id}")

    store = get_conversation_store()
    messages = [_record_to_dict(record) for record in store.list_messages(conversation_id)]
    turns = [_record_to_dict(store.get_turn(turn_id)) for turn_id in turn_ids if store.get_turn(turn_id) is not None]
    tool_calls = [
        _record_to_dict(tool_call)
        for turn_id in turn_ids
        for tool_call in store.list_tool_calls_by_turn(turn_id)
    ]
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    result = score_case(
        case,
        {
            "case_id": case.id,
            "category": case.category,
            "description": case.description,
            "conversation_id": conversation_id,
            "turn_ids": turn_ids,
            "status": run_responses[-1]["status"] if run_responses else "no_turn",
            "reply": run_responses[-1]["reply"] if run_responses else "",
            "messages": messages,
            "turns": turns,
            "tool_calls": tool_calls,
            "tool_names": [tool_call["tool_name"] for tool_call in tool_calls],
            "run_responses": run_responses,
            "metrics": {
                "elapsed_ms": elapsed_ms,
                "turn_count": len(turn_ids),
                "tool_call_count": len(tool_calls),
            },
            "success_criteria": case.success_criteria,
        },
    )
    trace_path = run_dir / "traces" / f"{case.id}.json"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def score_case(case: EvalCase, trace: dict[str, Any]) -> dict[str, Any]:
    tool_names = trace["tool_names"]
    tool_counts = Counter(tool_names)
    checks: list[dict[str, Any]] = []

    checks.append({
        "name": "required_status",
        "passed": trace["status"] == case.required_status,
        "expected": case.required_status,
        "actual": trace["status"],
    })
    for tool in case.expected_tools:
        checks.append({
            "name": f"expected_tool:{tool}",
            "passed": tool in tool_counts,
            "expected": "called at least once",
            "actual": tool_counts.get(tool, 0),
        })
    for tool in case.forbidden_tools:
        checks.append({
            "name": f"forbidden_tool:{tool}",
            "passed": tool_counts.get(tool, 0) == 0,
            "expected": 0,
            "actual": tool_counts.get(tool, 0),
        })
    for tool, maximum in case.max_tool_calls.items():
        checks.append({
            "name": f"max_tool_calls:{tool}",
            "passed": tool_counts.get(tool, 0) <= maximum,
            "expected": f"<= {maximum}",
            "actual": tool_counts.get(tool, 0),
        })
    for text in case.required_reply_contains:
        checks.append({
            "name": f"reply_contains:{text}",
            "passed": text in trace["reply"],
            "expected": text,
            "actual": trace["reply"][:200],
        })

    return {
        **trace,
        "checks": checks,
        "passed": all(check["passed"] for check in checks),
    }


def build_report(results: list[dict[str, Any]], skipped: list[EvalCase]) -> str:
    passed = sum(1 for result in results if result["passed"])
    lines = [
        "# Jarvis Agent Eval Report",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}",
        f"Cases run: {len(results)}",
        f"Passed: {passed}",
        f"Failed: {len(results) - passed}",
        f"Skipped: {len(skipped)}",
        "",
        "## Results",
        "",
        "| Case | Category | Passed | Status | Tools | Elapsed |",
        "| --- | --- | --- | --- | --- | ---: |",
    ]
    for result in results:
        tools = ", ".join(result["tool_names"]) or "-"
        lines.append(
            f"| {result['case_id']} | {result['category']} | {result['passed']} | "
            f"{result['status']} | {tools} | {result['metrics']['elapsed_ms']}ms |"
        )
    if skipped:
        lines.extend(["", "## Skipped", ""])
        for case in skipped:
            lines.append(f"- `{case.id}` requires: {', '.join(case.requires)}")
    lines.extend(["", "## Failed Checks", ""])
    failed_any = False
    for result in results:
        failed = [check for check in result["checks"] if not check["passed"]]
        if not failed:
            continue
        failed_any = True
        lines.append(f"### {result['case_id']}")
        for check in failed:
            lines.append(f"- {check['name']}: expected `{check['expected']}`, actual `{check['actual']}`")
    if not failed_any:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def _case_from_payload(payload: dict[str, Any], *, path: Path, line_number: int) -> EvalCase:
    try:
        return EvalCase(
            id=str(payload["id"]),
            category=str(payload.get("category", "uncategorized")),
            description=str(payload.get("description", "")),
            messages=list(payload["messages"]),
            expected_tools=list(payload.get("expected_tools", [])),
            forbidden_tools=list(payload.get("forbidden_tools", [])),
            required_status=str(payload.get("required_status", "completed")),
            max_tool_calls=dict(payload.get("max_tool_calls", {})),
            required_reply_contains=list(payload.get("required_reply_contains", [])),
            requires=list(payload.get("requires", [])),
            success_criteria=list(payload.get("success_criteria", [])),
        )
    except KeyError as exc:
        raise ValueError(f"Missing required field {exc} in {path}:{line_number}") from exc


def _record_to_dict(record: Any) -> dict[str, Any]:
    if record is None:
        return {}
    result: dict[str, Any] = {}
    for key, value in vars(record).items():
        if key.startswith("_"):
            continue
        result[key] = value
    return result


def _dataset_summary(cases: list[EvalCase]) -> dict[str, Any]:
    return {
        "case_count": len(cases),
        "categories": dict(Counter(case.category for case in cases)),
        "requires": dict(Counter(requirement for case in cases for requirement in case.requires)),
        "cases": [
            {
                "id": case.id,
                "category": case.category,
                "requires": case.requires,
                "expected_tools": case.expected_tools,
                "forbidden_tools": case.forbidden_tools,
            }
            for case in cases
        ],
    }


def _create_run_dir(output_root: Path) -> Path:
    run_dir = output_root / f"{_timestamp()}_agent_eval"
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "traces").mkdir()
    return run_dir


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")


if __name__ == "__main__":
    raise SystemExit(main())
