from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_DATASET_DIR = Path("tests/agent_system/fixtures/eval")


@dataclass(frozen=True)
class AgentSystemEvalCase:
    id: str
    layer: str
    category: str
    messages: list[dict[str, Any]]
    expected_classification: dict[str, Any]
    expected_task_plan: dict[str, Any]
    expected_tools: list[str]
    forbidden_tools: list[str]
    max_tool_calls: dict[str, int]
    requires: list[str]
    success_criteria: list[str]
    raw: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run or summarize the independent Jarvis agent system eval suite.")
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET_DIR)
    parser.add_argument("--layer", action="append", default=[])
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--allow-requires", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cases = load_cases(args.dataset_dir)
    if args.layer:
        allowed_layers = set(args.layer)
        cases = [case for case in cases if case.layer in allowed_layers]
    if args.case_id:
        selected = set(args.case_id)
        cases = [case for case in cases if case.id in selected]

    allowed_requires = set(args.allow_requires)
    runnable = [
        case
        for case in cases
        if not case.requires or all(requirement in allowed_requires for requirement in case.requires)
    ]
    skipped = [case for case in cases if case not in runnable]

    summary = summarize_cases(cases, runnable, skipped)
    if args.dry_run:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if os.environ.get("JARVIS_RUN_AGENT_SYSTEM_EVAL") != "1":
        raise SystemExit("Set JARVIS_RUN_AGENT_SYSTEM_EVAL=1 to run real agent system evals.")

    raise SystemExit(
        "Real execution for agent_system eval is intentionally not wired yet. "
        "Use --dry-run to validate datasets while runner execution is implemented by layer."
    )


def load_cases(dataset_dir: Path) -> list[AgentSystemEvalCase]:
    cases: list[AgentSystemEvalCase] = []
    for path in sorted(dataset_dir.glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as fh:
            for line_number, raw_line in enumerate(fh, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                cases.append(_case_from_payload(payload, path=path, line_number=line_number))
    _validate_unique_ids(cases)
    return cases


def summarize_cases(
    cases: list[AgentSystemEvalCase],
    runnable: list[AgentSystemEvalCase] | None = None,
    skipped: list[AgentSystemEvalCase] | None = None,
) -> dict[str, Any]:
    runnable = cases if runnable is None else runnable
    skipped = [] if skipped is None else skipped
    return {
        "case_count": len(cases),
        "runnable_count": len(runnable),
        "skipped_count": len(skipped),
        "layers": dict(Counter(case.layer for case in cases)),
        "categories": dict(Counter(case.category for case in cases)),
        "requires": dict(Counter(requirement for case in cases for requirement in case.requires)),
        "cases": [
            {
                "id": case.id,
                "layer": case.layer,
                "category": case.category,
                "requires": case.requires,
                "expected_tools": case.expected_tools,
                "forbidden_tools": case.forbidden_tools,
            }
            for case in cases
        ],
    }


def _case_from_payload(payload: dict[str, Any], *, path: Path, line_number: int) -> AgentSystemEvalCase:
    try:
        return AgentSystemEvalCase(
            id=str(payload["id"]),
            layer=str(payload["layer"]),
            category=str(payload["category"]),
            messages=list(payload["messages"]),
            expected_classification=dict(payload.get("expected_classification", {})),
            expected_task_plan=dict(payload.get("expected_task_plan", {})),
            expected_tools=list(payload.get("expected_tools", [])),
            forbidden_tools=list(payload.get("forbidden_tools", [])),
            max_tool_calls=dict(payload.get("max_tool_calls", {})),
            requires=list(payload.get("requires", [])),
            success_criteria=list(payload.get("success_criteria", [])),
            raw=payload,
        )
    except KeyError as exc:
        raise ValueError(f"Missing required field {exc} in {path}:{line_number}") from exc


def _validate_unique_ids(cases: list[AgentSystemEvalCase]) -> None:
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"Duplicate eval case id: {case.id}")
        seen.add(case.id)


if __name__ == "__main__":
    raise SystemExit(main())
