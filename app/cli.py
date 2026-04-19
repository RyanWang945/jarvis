from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from app.agent.events import build_user_event
from app.agent.runner import AgentRunResult, ThreadManager
from app.config import get_settings


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    manager = ThreadManager(get_settings().data_dir)
    output = args.func(manager, args)
    _print_json(output)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis-cli")
    subcommands = parser.add_subparsers(dest="command", required=True)

    run = subcommands.add_parser("run", help="Submit a local Jarvis task.")
    run.add_argument("instruction")
    run.add_argument("--command")
    run.add_argument("--verification-cmd")
    run.add_argument("--workdir")
    run.add_argument("--resource-key")
    run.add_argument("--thread-id")
    run.add_argument("--user-id")
    run.set_defaults(func=_run)

    status = subcommands.add_parser("status", help="Inspect one run.")
    status.add_argument("thread_id")
    status.set_defaults(func=_status)

    list_runs = subcommands.add_parser("list", help="List unfinished runs.")
    list_runs.set_defaults(func=_list)

    approve = subcommands.add_parser("approve", help="Approve a pending action.")
    approve.add_argument("thread_id")
    approve.add_argument("--approval-id")
    approve.set_defaults(func=_approve)

    reject = subcommands.add_parser("reject", help="Reject a pending action.")
    reject.add_argument("thread_id")
    reject.add_argument("--approval-id")
    reject.set_defaults(func=_reject)

    recover = subcommands.add_parser("recover", help="Replay recoverable worker results.")
    recover.set_defaults(func=_recover)

    report = subcommands.add_parser("report", help="Write JSON and Markdown reports for a run.")
    report.add_argument("thread_id")
    report.set_defaults(func=_report)

    return parser


def _run(manager: ThreadManager, args: argparse.Namespace) -> dict[str, Any]:
    event = build_user_event(
        instruction=args.instruction,
        command=args.command,
        verification_cmd=args.verification_cmd,
        workdir=args.workdir,
        resource_key=args.resource_key,
        thread_id=args.thread_id,
        user_id=args.user_id,
    )
    return _result_payload(manager.run_event(event))


def _status(manager: ThreadManager, args: argparse.Namespace) -> dict[str, Any]:
    run = manager.inspect_run(args.thread_id)
    if not run:
        raise SystemExit(f"Run not found: {args.thread_id}")
    return run


def _list(manager: ThreadManager, args: argparse.Namespace) -> dict[str, Any]:
    return {"runs": manager.db.runs.list_unfinished()}


def _approve(manager: ThreadManager, args: argparse.Namespace) -> dict[str, Any]:
    _ensure_pending_approval(manager, args.thread_id, args.approval_id)
    return _result_payload(
        manager.resume(
            args.thread_id,
            {"approved": True, "approval_id": args.approval_id},
        )
    )


def _reject(manager: ThreadManager, args: argparse.Namespace) -> dict[str, Any]:
    _ensure_pending_approval(manager, args.thread_id, args.approval_id)
    return _result_payload(
        manager.resume(
            args.thread_id,
            {"approved": False, "approval_id": args.approval_id},
        )
    )


def _recover(manager: ThreadManager, args: argparse.Namespace) -> dict[str, Any]:
    return manager.recover_unfinished()


def _report(manager: ThreadManager, args: argparse.Namespace) -> dict[str, Any]:
    return {"paths": manager.export_run_report(args.thread_id)}


def _ensure_pending_approval(manager: ThreadManager, thread_id: str, approval_id: str | None) -> None:
    pending = manager.db.approvals.get_pending_by_thread(thread_id)
    if not pending:
        raise SystemExit(f"No pending approval for thread: {thread_id}")
    if approval_id and all(approval["approval_id"] != approval_id for approval in pending):
        raise SystemExit(f"Approval not found or not pending: {approval_id}")


def _result_payload(result: AgentRunResult) -> dict[str, Any]:
    return {
        "thread_id": result.thread_id,
        "status": result.status,
        "summary": result.summary,
        "tasks": result.tasks,
        "pending_approval_id": result.pending_approval_id,
    }


def _print_json(output: Any) -> None:
    text = json.dumps(output, ensure_ascii=False, indent=2, default=str)
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    main()
