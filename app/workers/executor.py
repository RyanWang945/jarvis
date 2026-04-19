from app.skills import EchoSkill, ShellSkill
from app.skills.base import SkillRequest, SkillResult
from app.workers.base import WorkOrder, WorkResult


def execute_work_order(order: WorkOrder) -> WorkResult:
    if order.worker_type == "shell":
        result = ShellSkill().run(
            SkillRequest(
                skill="shell",
                action=order.action,
                workdir=order.workdir,
                args=order.args,
                risk_level=order.risk_level,
                timeout_seconds=order.timeout_seconds,
            )
        )
    elif order.worker_type == "echo":
        result = EchoSkill().run(
            SkillRequest(
                skill="echo",
                action=order.action,
                workdir=order.workdir,
                args=order.args,
                risk_level=order.risk_level,
                timeout_seconds=order.timeout_seconds,
            )
        )
    else:
        result = SkillResult(
            ok=False,
            exit_code=None,
            stderr=f"{order.worker_type} worker is not implemented.",
            summary=f"{order.worker_type} worker is not implemented.",
        )

    if result.ok and order.verification_cmd:
        result = ShellSkill().run(
            SkillRequest(
                skill="shell",
                action="verify",
                workdir=order.workdir,
                args={"command": order.verification_cmd},
                risk_level="low",
                timeout_seconds=order.timeout_seconds,
            )
        )

    return WorkResult(
        order_id=order.order_id,
        task_id=order.task_id,
        ca_thread_id=order.ca_thread_id,
        worker_type=order.worker_type,
        ok=result.ok,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        artifacts=result.artifacts,
        summary=result.summary,
    )
