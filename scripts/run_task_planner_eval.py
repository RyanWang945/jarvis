from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import Any

from app.agent_react.session_state import ConversationSessionState
from app.task_runtime.planning_router import PlanningRouter
from app.task_runtime.planner import ExecutionPlan, TurnPlanner
from app.task_runtime.runtime_context import RuntimeContext

DEFAULT_DATASET = Path("tests/fixtures/task_planner_eval/planner_cases.jsonl")
DEFAULT_OUTPUT_ROOT = Path("data/eval_runs")
DEFAULT_RUNTIME_CONTEXT = {
    "active_repo": "jarvis",
    "available_runtimes": ["llm", "react", "coder"],
}


@dataclass(frozen=True)
class PlannerEvalCase:
    id: str
    category: str
    message: str
    artifacts: list[dict[str, Any]]
    previous_node_results: list[dict[str, Any]]
    runtime_context: dict[str, Any]
    instructions: list[str]
    expected_node_count_min: int
    expected_node_count_max: int
    required_runtimes: list[str]
    forbidden_runtimes: list[str]
    required_tool_names: list[str]
    required_input_refs: list[str]
    objective_contains: list[str]
    required_node_objective_contains: list[list[str]]
    max_latency_ms: int
    raw: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run opt-in Jarvis vNext planner eval cases.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--mode", choices=["planner", "router", "coordinator", "both"], default="planner")
    parser.add_argument("--planner-prompt-version", action="append", default=[])
    parser.add_argument("--fast-intent-prompt-version", action="append", default=[])
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
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

    if os.environ.get("JARVIS_RUN_TASK_PLANNER_EVAL") != "1":
        raise SystemExit("Set JARVIS_RUN_TASK_PLANNER_EVAL=1 to run real planner evals.")

    planner_versions = args.planner_prompt_version or [None]
    fast_intent_versions = args.fast_intent_prompt_version or [None]
    run_dir = _create_run_dir(args.output_root, mode=args.mode)
    results = run_prompt_matrix(
        cases,
        mode=args.mode,
        planner_prompt_versions=planner_versions,
        fast_intent_prompt_versions=fast_intent_versions,
        parallel=args.parallel,
    )
    for result in results:
        console_payload = {
            "case_id": result["case_id"],
            "mode": result["mode"],
            "passed": result["passed"],
            "elapsed_ms": result["metrics"]["elapsed_ms"],
            "node_count": result["metrics"]["node_count"],
            "runtimes": result["runtimes"],
            "tool_names": result["tool_names"],
        }
        if "route" in result:
            console_payload["route"] = result["route"]
        if "planner_elapsed_ms" in result["metrics"]:
            console_payload["planner_elapsed_ms"] = result["metrics"]["planner_elapsed_ms"]
        print(json.dumps(console_payload, ensure_ascii=False), flush=True)

    (run_dir / "run.json").write_text(json.dumps({"results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "report.md").write_text(build_report(results), encoding="utf-8")
    print(str(run_dir))
    return 0 if all(result["passed"] for result in results) else 1


def run_cases(
    cases: list[PlannerEvalCase],
    *,
    mode: str,
    planner: TurnPlanner | None = None,
    router: PlanningRouter | None = None,
    coordinator: PlanningRouter | None = None,
    planner_prompt_version: str | None = None,
    fast_intent_prompt_version: str | None = None,
) -> list[dict[str, Any]]:
    mode = _normalize_mode(mode)
    results: list[dict[str, Any]] = []
    if mode in {"planner", "both"}:
        planner = planner or TurnPlanner(prompt_version=planner_prompt_version)
        for case in cases:
            results.append(run_case(planner, case))
    if mode in {"router", "both"}:
        router = router or coordinator or PlanningRouter(
            fast_intent_prompt_version=fast_intent_prompt_version,
            planner_prompt_version=planner_prompt_version,
        )
        for case in cases:
            results.append(run_router_case(router, case))
    return results


def run_prompt_matrix(
    cases: list[PlannerEvalCase],
    *,
    mode: str,
    planner_prompt_versions: list[str | None],
    fast_intent_prompt_versions: list[str | None],
    parallel: int = 1,
) -> list[dict[str, Any]]:
    mode = _normalize_mode(mode)
    runs = _prompt_matrix_runs(
        mode=mode,
        planner_prompt_versions=planner_prompt_versions,
        fast_intent_prompt_versions=fast_intent_prompt_versions,
    )
    if len(runs) <= 1 or parallel <= 1:
        results: list[dict[str, Any]] = []
        for run in runs:
            run_mode = str(run["mode"])
            run_kwargs = {key: value for key, value in run.items() if key != "mode"}
            results.extend(run_cases(cases, mode=run_mode, **run_kwargs))
        return results

    with ThreadPoolExecutor(max_workers=max(1, parallel), thread_name_prefix="jarvis-prompt-eval") as executor:
        futures = [
            executor.submit(run_cases, cases, mode=str(run["mode"]), **{key: value for key, value in run.items() if key != "mode"})
            for run in runs
        ]
        results = []
        for future in futures:
            results.extend(future.result())
        return results


def run_case(planner: TurnPlanner, case: PlannerEvalCase) -> dict[str, Any]:
    started = time.perf_counter()
    runtime_context = RuntimeContext.from_hints(case.runtime_context)
    plan = planner.plan(
        content=case.message,
        session_state=ConversationSessionState(session_mode="coding", active_repo_id=case.runtime_context.get("active_repo")),
        recent_artifacts=case.artifacts,
        previous_node_results=case.previous_node_results,
        runtime_context=runtime_context,
        instructions=case.instructions,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    result = score_case(case, plan, elapsed_ms=elapsed_ms)
    result["mode"] = "planner"
    if hasattr(planner, "prompt_metadata"):
        result["prompt"] = planner.prompt_metadata()
    return result


def run_router_case(router: PlanningRouter, case: PlannerEvalCase) -> dict[str, Any]:
    runtime_context = RuntimeContext.from_hints(case.runtime_context)
    result = router.plan(
        content=case.message,
        session_state=ConversationSessionState(session_mode="coding", active_repo_id=case.runtime_context.get("active_repo")),
        recent_artifacts=case.artifacts,
        previous_node_results=case.previous_node_results,
        runtime_context=runtime_context,
        instructions=case.instructions,
    )
    scored = score_case(case, result.plan, elapsed_ms=result.elapsed_ms)
    scored["mode"] = "router"
    scored["route"] = result.route
    scored["fast_intent"] = result.fast_intent.model_dump(mode="json")
    scored["metrics"]["planner_elapsed_ms"] = result.planner_elapsed_ms
    if hasattr(router, "prompt_metadata"):
        scored["prompt"] = router.prompt_metadata()
    return scored


def load_cases(path: Path | str) -> list[PlannerEvalCase]:
    path = Path(path)
    cases: list[PlannerEvalCase] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_number, raw_line in enumerate(fh, start=1):
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            cases.append(_case_from_payload(payload, path=path, line_number=line_number))
    _validate_unique_ids(cases)
    return cases


def dataset_summary(cases: list[PlannerEvalCase]) -> dict[str, Any]:
    return {
        "case_count": len(cases),
        "categories": dict(Counter(case.category for case in cases)),
        "cases": [
            {
                "id": case.id,
                "category": case.category,
                "required_runtimes": case.required_runtimes,
                "required_tool_names": case.required_tool_names,
                "required_input_refs": case.required_input_refs,
                "required_node_objective_contains": case.required_node_objective_contains,
                "max_latency_ms": case.max_latency_ms,
            }
            for case in cases
        ],
    }


def score_case(case: PlannerEvalCase, plan: ExecutionPlan, *, elapsed_ms: int) -> dict[str, Any]:
    runtimes = [node.runtime for node in plan.nodes]
    tool_names = []
    input_refs = [ref for node in plan.nodes for ref in node.input_refs]
    checks: list[dict[str, Any]] = []

    checks.append(_check("latency", elapsed_ms <= case.max_latency_ms, f"<= {case.max_latency_ms} ms", f"{elapsed_ms} ms"))
    checks.append(_check("node_count_min", len(plan.nodes) >= case.expected_node_count_min, f">= {case.expected_node_count_min}", len(plan.nodes)))
    checks.append(_check("node_count_max", len(plan.nodes) <= case.expected_node_count_max, f"<= {case.expected_node_count_max}", len(plan.nodes)))
    for runtime in case.required_runtimes:
        checks.append(_check(f"required_runtime:{runtime}", runtime in runtimes, "present", runtimes))
    for runtime in case.forbidden_runtimes:
        checks.append(_check(f"forbidden_runtime:{runtime}", runtime not in runtimes, "absent", runtimes))
    for tool_name in case.required_tool_names:
        checks.append(_check(f"required_tool_name:{tool_name}", tool_name in tool_names, "present", tool_names))
    for input_ref in case.required_input_refs:
        checks.append(_check(f"required_input_ref:{input_ref}", input_ref in input_refs, "present", input_refs))

    objective_text = " ".join([plan.user_objective, *[node.objective for node in plan.nodes], *[node.output_hint for node in plan.nodes]])
    for fragment in case.objective_contains:
        checks.append(_check(f"objective_contains:{fragment}", _contains_fragment(objective_text, fragment), fragment, objective_text))

    node_texts = {
        node.id: " ".join([node.objective, node.output_hint])
        for node in plan.nodes
    }
    for fragments in case.required_node_objective_contains:
        group_name = "+".join(fragments)
        matching_node_ids = [
            node_id
            for node_id, node_text in node_texts.items()
            if all(_contains_fragment(node_text, fragment) for fragment in fragments)
        ]
        checks.append(
            _check(
                f"required_node_objective_contains:{group_name}",
                bool(matching_node_ids),
                fragments,
                matching_node_ids or node_texts,
            )
        )

    passed = all(check["passed"] for check in checks)
    return {
        "case_id": case.id,
        "category": case.category,
        "message": case.message,
        "passed": passed,
        "checks": checks,
        "plan": plan.model_dump(mode="json"),
        "runtimes": runtimes,
        "tool_names": tool_names,
        "input_refs": input_refs,
        "metrics": {
            "elapsed_ms": elapsed_ms,
            "node_count": len(plan.nodes),
        },
    }


def build_report(results: list[dict[str, Any]]) -> str:
    passed = sum(1 for result in results if result["passed"])
    latencies = [int(result["metrics"]["elapsed_ms"]) for result in results]
    lines = [
        "# Task Planner Eval Report",
        "",
        f"- Cases: `{len(results)}`",
        f"- Passed: `{passed}`",
        f"- Failed: `{len(results) - passed}`",
    ]
    if latencies:
        lines.extend([f"- Avg latency: `{int(sum(latencies) / len(latencies))} ms`", f"- Max latency: `{max(latencies)} ms`"])
    comparison = _latency_comparison(results)
    if comparison:
        lines.extend(["", "## Latency Comparison", ""])
        for row in comparison:
            speedup = row["planner_avg_ms"] - row["router_avg_ms"]
            lines.append(f"- {row['category']}: planner `{row['planner_avg_ms']} ms`, router `{row['router_avg_ms']} ms`, delta `{speedup} ms`")
    lines.append("")
    for result in results:
        marker = "PASS" if result["passed"] else "FAIL"
        lines.append(f"## {marker} [{result.get('mode', 'planner')}] {result['case_id']} ({result['metrics']['elapsed_ms']} ms, {result['metrics']['node_count']} nodes)")
        lines.append("")
        if "route" in result:
            lines.append(f"- Route: `{result['route']}`")
            planner_elapsed = result["metrics"].get("planner_elapsed_ms")
            lines.append(f"- Planner elapsed: `{planner_elapsed if planner_elapsed is not None else '-'} ms`")
        if "prompt" in result:
            lines.extend(_prompt_report_lines(result["prompt"]))
        lines.append(f"- Runtimes: `{', '.join(result['runtimes'])}`")
        if result["tool_names"]:
            lines.append(f"- Tool names: `{', '.join(result['tool_names'])}`")
        if result["input_refs"]:
            lines.append(f"- Input refs: `{', '.join(result['input_refs'])}`")
        failed_checks = [check for check in result["checks"] if not check["passed"]]
        if failed_checks:
            lines.append("- Failed checks:")
            for check in failed_checks:
                lines.append(f"  - {check['name']}: expected `{check['expected']}`, actual `{check['actual']}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _latency_comparison(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    categories = sorted({result["category"] for result in results})
    rows: list[dict[str, Any]] = []
    for category in categories:
        planner_latencies = [int(result["metrics"]["elapsed_ms"]) for result in results if result.get("mode") == "planner" and result["category"] == category]
        router_latencies = [int(result["metrics"]["elapsed_ms"]) for result in results if result.get("mode") == "router" and result["category"] == category]
        if not planner_latencies or not router_latencies:
            continue
        rows.append({"category": category, "planner_avg_ms": int(sum(planner_latencies) / len(planner_latencies)), "router_avg_ms": int(sum(router_latencies) / len(router_latencies))})
    return rows


def _prompt_matrix_runs(
    *,
    mode: str,
    planner_prompt_versions: list[str | None],
    fast_intent_prompt_versions: list[str | None],
) -> list[dict[str, str | None]]:
    mode = _normalize_mode(mode)
    planner_versions = planner_prompt_versions or [None]
    fast_versions = fast_intent_prompt_versions or [None]
    if mode == "planner":
        return [{"mode": "planner", "planner_prompt_version": version, "fast_intent_prompt_version": None} for version in planner_versions]
    if mode == "router":
        return [
            {"mode": "router", "planner_prompt_version": planner_version, "fast_intent_prompt_version": fast_version}
            for planner_version, fast_version in product(planner_versions, fast_versions)
        ]
    planner_runs = [{"mode": "planner", "planner_prompt_version": planner_version, "fast_intent_prompt_version": None} for planner_version in planner_versions]
    router_runs = [
        {"mode": "router", "planner_prompt_version": planner_version, "fast_intent_prompt_version": fast_version}
        for planner_version, fast_version in product(planner_versions, fast_versions)
    ]
    return [*planner_runs, *router_runs]


def _normalize_mode(mode: str) -> str:
    return "router" if mode == "coordinator" else mode


def _prompt_report_lines(prompt: dict[str, Any]) -> list[str]:
    if "prompt_id" in prompt:
        return [f"- Prompt: `{prompt['prompt_id']}` `{str(prompt.get('prompt_sha256', ''))[:12]}`"]
    lines: list[str] = []
    fast = prompt.get("fast_intent")
    planner = prompt.get("planner")
    if isinstance(fast, dict):
        lines.append(f"- FastIntent prompt: `{fast.get('prompt_id')}` `{str(fast.get('prompt_sha256', ''))[:12]}`")
    if isinstance(planner, dict):
        lines.append(f"- Planner prompt: `{planner.get('prompt_id')}` `{str(planner.get('prompt_sha256', ''))[:12]}`")
    return lines


def _case_from_payload(payload: dict[str, Any], *, path: Path, line_number: int) -> PlannerEvalCase:
    try:
        return PlannerEvalCase(
            id=str(payload["id"]),
            category=str(payload["category"]),
            message=str(payload["message"]),
            artifacts=list(payload.get("artifacts", [])),
            previous_node_results=list(payload.get("previous_node_results", [])),
            runtime_context=dict(payload.get("runtime_context", payload.get("runtime_hints", DEFAULT_RUNTIME_CONTEXT))),
            instructions=list(payload.get("instructions", [])),
            expected_node_count_min=int(payload.get("expected_node_count_min", 1)),
            expected_node_count_max=int(payload.get("expected_node_count_max", 6)),
            required_runtimes=list(payload.get("required_runtimes", payload.get("required_execution_types", []))),
            forbidden_runtimes=list(payload.get("forbidden_runtimes", payload.get("forbidden_execution_types", []))),
            required_tool_names=list(payload.get("required_tool_names", [])),
            required_input_refs=list(payload.get("required_input_refs", [])),
            objective_contains=list(payload.get("objective_contains", [])),
            required_node_objective_contains=_normalize_fragment_groups(payload.get("required_node_objective_contains", [])),
            max_latency_ms=int(payload.get("max_latency_ms", 15000)),
            raw=payload,
        )
    except KeyError as exc:
        raise ValueError(f"Missing required field {exc} in {path}:{line_number}") from exc


def _validate_unique_ids(cases: list[PlannerEvalCase]) -> None:
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"Duplicate planner eval case id: {case.id}")
        seen.add(case.id)


def _check(name: str, passed: bool, expected: Any, actual: Any) -> dict[str, Any]:
    return {
        "name": name,
        "passed": passed,
        "expected": expected,
        "actual": actual,
    }


def _contains_fragment(text: str, fragment: str) -> bool:
    normalized_text = str(text or "").lower()
    normalized_fragment = str(fragment or "").lower()
    if normalized_fragment in normalized_text:
        return True
    aliases = {
        "报告": ("report", "markdown report"),
        "提醒": ("reminder", "remind", "notify"),
        "调研": ("research", "investigation"),
        "研究": ("research",),
        "实现": ("implement", "implementation", "build", "code changes"),
        "合并": ("merge", "integrate", "integration", "整合", "集成"),
        "订单": ("order",),
        "支付": ("payment", "refund"),
        "订单业务": ("订单模块", "订单能力", "order"),
        "支付/退款业务": ("支付退款业务", "支付模块", "退款模块", "payment", "refund"),
        "review": ("评审", "审查", "代码审查", "code review"),
    }
    return any(alias.lower() in normalized_text for alias in aliases.get(str(fragment), ()))


def _normalize_fragment_groups(value: Any) -> list[list[str]]:
    if not isinstance(value, list):
        return []
    groups: list[list[str]] = []
    for item in value:
        raw_group = item if isinstance(item, list) else [item]
        group = [str(fragment).strip() for fragment in raw_group if str(fragment).strip()]
        if group:
            groups.append(group)
    return groups


def _create_run_dir(output_root: Path, *, mode: str = "planner") -> Path:
    run_dir = output_root / f"{_timestamp()}_task_planner_eval_{mode}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
