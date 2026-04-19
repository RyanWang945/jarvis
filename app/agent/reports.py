from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def write_run_report(data_dir: Path, inspection: dict[str, Any]) -> dict[str, str]:
    run = inspection["run"]
    thread_id = run["thread_id"]
    report_dir = data_dir / "reports"
    notes_dir = data_dir / "notes"
    report_dir.mkdir(parents=True, exist_ok=True)
    notes_dir.mkdir(parents=True, exist_ok=True)

    generated_at = datetime.now(UTC).isoformat()
    report = {
        "generated_at": generated_at,
        "run": run,
        "tasks": inspection["tasks"],
        "work_orders": inspection["work_orders"],
        "work_results": inspection["work_results"],
        "approvals": inspection["approvals"],
        "audit_logs": inspection["audit_logs"],
    }

    json_path = report_dir / f"{thread_id}.json"
    markdown_path = notes_dir / f"{thread_id}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")
    return {
        "json": str(json_path),
        "markdown": str(markdown_path),
    }


def _render_markdown(report: dict[str, Any]) -> str:
    run = report["run"]
    lines = [
        f"# Jarvis Run {run['thread_id']}",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Status: {run.get('status')}",
        f"- Instruction: {run.get('instruction') or ''}",
        f"- Summary: {run.get('summary') or ''}",
        "",
        "## Tasks",
    ]
    for task in report["tasks"]:
        lines.extend(
            [
                "",
                f"### {task.get('title') or task.get('task_id')}",
                "",
                f"- Status: {task.get('status')}",
                f"- Worker: {task.get('worker_type') or ''}",
                f"- Tool: {task.get('tool_name') or ''}",
                f"- DoD: {task.get('dod') or ''}",
                f"- Result: {task.get('result_summary') or ''}",
            ]
        )

    lines.extend(["", "## Work Results"])
    for result in report["work_results"]:
        lines.extend(
            [
                "",
                f"### {result.get('order_id')}",
                "",
                f"- OK: {bool(result.get('ok'))}",
                f"- Exit Code: {result.get('exit_code')}",
                f"- Summary: {result.get('summary') or ''}",
            ]
        )
        stdout = (result.get("stdout") or "").strip()
        stderr = (result.get("stderr") or "").strip()
        if stdout:
            lines.extend(["", "```text", stdout[:4000], "```"])
        if stderr:
            lines.extend(["", "```text", stderr[:4000], "```"])

    if report["approvals"]:
        lines.extend(["", "## Approvals"])
        for approval in report["approvals"]:
            lines.append(
                f"- {approval.get('approval_id')}: {approval.get('status')} "
                f"risk={approval.get('risk_level') or ''}"
            )

    return "\n".join(lines).rstrip() + "\n"
