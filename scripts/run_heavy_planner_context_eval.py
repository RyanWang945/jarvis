from __future__ import annotations

import argparse
from itertools import product
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agent_react.context_manager import ContextMessage, ConversationContext
from app.agent_react.session_state import ConversationSessionState
from app.skills.bootstrap import reset_registries_for_tests
from app.task_runtime.planner import ExecutionPlan, TurnPlanner, build_plan_input
from app.task_runtime.runtime_context import RuntimeContext

DEFAULT_DATASET = Path("tests/fixtures/heavy_planner_eval/context_cases.jsonl")
DEFAULT_OUTPUT_ROOT = Path("data/eval_runs/heavy_planner_context")


@dataclass(frozen=True)
class HeavyPlannerContextCase:
    id: str
    category: str
    current_user_input: str
    conversation_context: ConversationContext
    runtime_context: dict[str, Any]
    session_state: dict[str, Any]
    artifacts: list[dict[str, Any]]
    previous_node_results: list[dict[str, Any]]
    instructions: list[str]
    conversation_metadata: dict[str, Any]
    expected: dict[str, Any]
    raw: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run real-LLM heavy planner context regression cases.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument(
        "--planner-prompt-version",
        action="append",
        default=[],
        help="Prompt version to test. Repeat to compare versions. Omit to test the configured default.",
    )
    parser.add_argument(
        "--skill-version",
        action="append",
        default=[],
        help="Skill version override as skill_id=version. Repeat with the same skill_id to compare versions.",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--repeat", type=int, default=1, help="Run each case N times; case passes only if every attempt passes.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dump-input", action="store_true")
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
        print(json.dumps(dataset_summary(cases), ensure_ascii=False, indent=2))
        return 0

    if os.environ.get("JARVIS_RUN_HEAVY_PLANNER_EVAL") != "1":
        raise SystemExit("Set JARVIS_RUN_HEAVY_PLANNER_EVAL=1 to run real heavy planner evals.")

    run_dir = _create_run_dir(args.output_root)
    results: list[dict[str, Any]] = []
    original_skill_versions = os.environ.get("JARVIS_SKILL_VERSIONS")
    try:
        for planner_prompt_version, skill_versions in product(
            _planner_prompt_versions(args.planner_prompt_version),
            _skill_version_sets(args.skill_version),
        ):
            _apply_skill_versions(skill_versions)
            reset_registries_for_tests()
            planner = TurnPlanner(prompt_version=planner_prompt_version)
            for case in cases:
                results.append(
                    run_case(
                        planner,
                        case,
                        planner_prompt_version=planner_prompt_version,
                        skill_versions=skill_versions,
                        repeat=max(1, args.repeat),
                        dump_input=args.dump_input,
                    )
                )
    finally:
        if original_skill_versions is None:
            os.environ.pop("JARVIS_SKILL_VERSIONS", None)
        else:
            os.environ["JARVIS_SKILL_VERSIONS"] = original_skill_versions
        reset_registries_for_tests()

    for result in results:
        print(
            json.dumps(
                {
                    "case_id": result["case_id"],
                    "planner_prompt_version": result["planner_prompt_version"],
                    "skill_versions": result["skill_versions"],
                    "passed": result["passed"],
                    "attempt_count": result["attempt_count"],
                    "failed_attempts": [
                        attempt["attempt"]
                        for attempt in result["attempts"]
                        if not attempt["passed"]
                    ],
                    "attempts": [
                        {
                            "attempt": attempt["attempt"],
                            "elapsed_ms": attempt["elapsed_ms"],
                            "runtimes": attempt["runtimes"],
                            "modes": attempt["modes"],
                            "repo_ids": attempt["repo_ids"],
                            "failed_checks": [
                                check["name"]
                                for check in attempt["checks"]
                                if not check["passed"]
                            ],
                        }
                        for attempt in result["attempts"]
                    ],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    (run_dir / "run.json").write_text(json.dumps({"results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "report.md").write_text(build_report(results), encoding="utf-8")
    print(str(run_dir))
    return 0 if all(result["passed"] for result in results) else 1


def load_cases(path: Path | str) -> list[HeavyPlannerContextCase]:
    path = Path(path)
    cases: list[HeavyPlannerContextCase] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_number, raw_line in enumerate(fh, start=1):
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            cases.append(_case_from_payload(payload, path=path, line_number=line_number))
    _validate_unique_case_ids(cases)
    return cases


def run_case(
    planner: TurnPlanner,
    case: HeavyPlannerContextCase,
    *,
    planner_prompt_version: str | None,
    skill_versions: dict[str, str] | None,
    repeat: int = 1,
    dump_input: bool = False,
) -> dict[str, Any]:
    attempts = [
        run_case_attempt(planner, case, attempt=attempt, dump_input=dump_input)
        for attempt in range(1, repeat + 1)
    ]
    return {
        "case_id": case.id,
        "category": case.category,
        "planner_prompt_version": planner_prompt_version,
        "skill_versions": skill_versions,
        "prompt": planner.prompt_metadata(),
        "passed": all(attempt["passed"] for attempt in attempts),
        "attempt_count": len(attempts),
        "attempts": attempts,
    }


def run_case_attempt(
    planner: TurnPlanner,
    case: HeavyPlannerContextCase,
    *,
    attempt: int,
    dump_input: bool = False,
) -> dict[str, Any]:
    runtime_context = RuntimeContext.from_hints(case.runtime_context)
    session_state = _session_state_from_payload(case.session_state)
    plan_input = build_plan_input(
        current_user_input=case.current_user_input,
        conversation_context=case.conversation_context,
        artifacts=case.artifacts,
        previous_node_results=case.previous_node_results,
        runtime_context=runtime_context,
        session_state=session_state,
        instructions=case.instructions,
    )
    started = time.perf_counter()
    result = planner.plan_with_usage(
        content=case.current_user_input,
        session_state=session_state,
        conversation_metadata=case.conversation_metadata,
        conversation_context=case.conversation_context,
        runtime_context=runtime_context,
        recent_artifacts=case.artifacts,
        previous_node_results=case.previous_node_results,
        instructions=case.instructions,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    scored = score_case(case, result.plan, elapsed_ms=elapsed_ms)
    scored["attempt"] = attempt
    scored["usage_records"] = result.usage_records
    scored["prompt"] = planner.prompt_metadata()
    if dump_input:
        scored["planner_input"] = plan_input.model_dump(mode="json")
    return scored


def score_case(case: HeavyPlannerContextCase, plan: ExecutionPlan, *, elapsed_ms: int) -> dict[str, Any]:
    runtimes = [node.runtime for node in plan.nodes]
    modes = [node.mode for node in plan.nodes]
    repo_ids = [node.repo_id for node in plan.nodes if node.repo_id]
    objective_text = " ".join([plan.user_objective, *[node.objective for node in plan.nodes], *[node.output_hint for node in plan.nodes]])
    expected = case.expected
    checks: list[dict[str, Any]] = []

    checks.append(_check("node_count_min", len(plan.nodes) >= int(expected.get("node_count_min", 1)), expected.get("node_count_min"), len(plan.nodes)))
    checks.append(_check("node_count_max", len(plan.nodes) <= int(expected.get("node_count_max", 999)), expected.get("node_count_max"), len(plan.nodes)))
    for runtime in expected.get("required_runtimes", []):
        checks.append(_check(f"required_runtime:{runtime}", runtime in runtimes, "present", runtimes))
    for runtime in expected.get("forbidden_runtimes", []):
        checks.append(_check(f"forbidden_runtime:{runtime}", runtime not in runtimes, "absent", runtimes))
    for mode in expected.get("required_modes", []):
        checks.append(_check(f"required_mode:{mode}", mode in modes, "present", modes))
    for repo_id in expected.get("required_repo_ids", []):
        checks.append(_check(f"required_repo_id:{repo_id}", repo_id in repo_ids, "present", repo_ids))
    for fragment in expected.get("objective_contains", []):
        checks.append(_check(f"objective_contains:{fragment}", str(fragment).lower() in objective_text.lower(), "present", objective_text[:500]))

    return {
        "case_id": case.id,
        "category": case.category,
        "passed": all(check["passed"] for check in checks),
        "elapsed_ms": elapsed_ms,
        "runtimes": runtimes,
        "modes": modes,
        "repo_ids": repo_ids,
        "checks": checks,
        "plan": plan.model_dump(mode="json"),
    }


def build_report(results: list[dict[str, Any]]) -> str:
    passed = sum(1 for result in results if result["passed"])
    lines = [
        "# Heavy Planner Context Eval",
        "",
        f"Cases: `{len(results)}`",
        f"Passed: `{passed}`",
        f"Failed: `{len(results) - passed}`",
        "",
    ]
    for result in results:
        status = "PASS" if result["passed"] else "FAIL"
        lines.extend(
            [
                f"## {status} {result['case_id']} / {result['prompt']['prompt_id']}",
                "",
                f"- planner_prompt_version: `{result['planner_prompt_version'] or 'default'}`",
                f"- skill_versions: `{_display_skill_versions(result['skill_versions'])}`",
                f"- prompt_sha256: `{result['prompt']['prompt_sha256']}`",
                f"- attempt_count: `{result['attempt_count']}`",
                "",
            ]
        )
        for attempt in result["attempts"]:
            attempt_status = "PASS" if attempt["passed"] else "FAIL"
            lines.extend(
                [
                    f"### {attempt_status} attempt {attempt['attempt']}",
                    "",
                    f"- elapsed_ms: `{attempt['elapsed_ms']}`",
                    f"- runtimes: `{attempt['runtimes']}`",
                    f"- modes: `{attempt['modes']}`",
                    f"- repo_ids: `{attempt['repo_ids']}`",
                    "",
                ]
            )
            for check in attempt["checks"]:
                mark = "PASS" if check["passed"] else "FAIL"
                lines.append(f"- {mark} `{check['name']}` expected `{check['expected']}` actual `{check['actual']}`")
            lines.extend(["", "```json", json.dumps(attempt["plan"], ensure_ascii=False, indent=2), "```", ""])
    return "\n".join(lines)


def dataset_summary(cases: list[HeavyPlannerContextCase]) -> dict[str, Any]:
    return {
        "case_count": len(cases),
        "cases": [
            {
                "id": case.id,
                "category": case.category,
                "current_user_input": case.current_user_input,
                "message_count": len(case.conversation_context.messages),
                "context_reference_detected": case.conversation_context.context_reference_detected,
                "artifact_count": len(case.artifacts),
                "previous_node_result_count": len(case.previous_node_results),
                "instruction_count": len(case.instructions),
                "expected": case.expected,
            }
            for case in cases
        ],
    }


def _case_from_payload(payload: dict[str, Any], *, path: Path, line_number: int) -> HeavyPlannerContextCase:
    case_id = str(payload.get("id") or "").strip()
    if not case_id:
        raise ValueError(f"{path}:{line_number}: id is required")
    context_payload = payload.get("conversation_context")
    if not isinstance(context_payload, dict):
        raise ValueError(f"{path}:{line_number}: conversation_context must be an object")
    messages = context_payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError(f"{path}:{line_number}: conversation_context.messages must be a list")
    context = ConversationContext(
        messages=tuple(
            ContextMessage(role=str(message.get("role") or ""), content=str(message.get("content") or ""))
            for message in messages
            if isinstance(message, dict)
        ),
        context_reference_detected=bool(context_payload.get("context_reference_detected", False)),
        older_summary=str(context_payload.get("older_summary") or ""),
    )
    return HeavyPlannerContextCase(
        id=case_id,
        category=str(payload.get("category") or "uncategorized"),
        current_user_input=str(payload.get("current_user_input") or "").strip(),
        conversation_context=context,
        runtime_context=dict(payload.get("runtime_context") or {}),
        session_state=dict(payload.get("session_state") or {}),
        artifacts=_dict_list(payload.get("artifacts")),
        previous_node_results=_dict_list(payload.get("previous_node_results")),
        instructions=[
            str(item).strip()
            for item in (payload.get("instructions") or [])
            if str(item).strip()
        ],
        conversation_metadata=dict(payload.get("conversation_metadata") or {}),
        expected=dict(payload.get("expected") or {}),
        raw=payload,
    )


def _check(name: str, passed: bool, expected: Any, actual: Any) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "expected": expected, "actual": actual}


def _session_state_from_payload(payload: dict[str, Any]) -> ConversationSessionState:
    allowed = set(ConversationSessionState.__dataclass_fields__)
    return ConversationSessionState(**{key: value for key, value in payload.items() if key in allowed})


def _validate_unique_case_ids(cases: list[HeavyPlannerContextCase]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for case in cases:
        if case.id in seen:
            duplicates.add(case.id)
        seen.add(case.id)
    if duplicates:
        raise ValueError(f"duplicate case ids: {', '.join(sorted(duplicates))}")


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _planner_prompt_versions(values: list[str]) -> list[str | None]:
    if not values:
        return [None]
    result: list[str | None] = []
    for value in values:
        text = str(value or "").strip()
        normalized = None if text in {"", "default", "current"} else text
        if normalized not in result:
            result.append(normalized)
    return result or [None]


def _skill_version_sets(values: list[str]) -> list[dict[str, str] | None]:
    if not values:
        return [None]
    choices: dict[str, list[str]] = {}
    for value in values:
        skill_id, separator, version = str(value or "").partition("=")
        if not separator:
            skill_id, separator, version = str(value or "").partition(":")
        skill_id = skill_id.strip()
        version = version.strip()
        if not skill_id or not version:
            raise ValueError(f"invalid --skill-version value: {value!r}; expected skill_id=version")
        versions = choices.setdefault(skill_id, [])
        if version not in versions:
            versions.append(version)
    skill_ids = list(choices)
    return [
        dict(zip(skill_ids, versions, strict=True))
        for versions in product(*(choices[skill_id] for skill_id in skill_ids))
    ]


def _apply_skill_versions(skill_versions: dict[str, str] | None) -> None:
    if skill_versions is None:
        return
    os.environ["JARVIS_SKILL_VERSIONS"] = json.dumps(skill_versions, ensure_ascii=False)


def _display_skill_versions(skill_versions: dict[str, str] | None) -> str:
    if not skill_versions:
        return "default"
    return ",".join(f"{skill_id}={version}" for skill_id, version in sorted(skill_versions.items()))


def _create_run_dir(output_root: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = output_root / stamp
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = output_root / f"{stamp}-{suffix}"
    run_dir.mkdir(parents=True)
    return run_dir


if __name__ == "__main__":
    raise SystemExit(main())
