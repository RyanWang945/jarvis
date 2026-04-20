from app.skills import ShellSkill, get_default_skill_registry
from app.skills.base import SkillRequest
from app.skills.registry import SkillRegistry
from app.workers.base import WorkOrder, WorkResult


def execute_work_order(order: WorkOrder, skill_registry: SkillRegistry | None = None) -> WorkResult:
    registry = skill_registry or get_default_skill_registry()
    try:
        skill = registry.get(order.worker_type)
    except ValueError:
        result = WorkResult(
            order_id=order.order_id,
            task_id=order.task_id,
            ca_thread_id=order.ca_thread_id,
            worker_type=order.worker_type,
            ok=False,
            stderr=f"{order.worker_type} worker is not implemented.",
            summary=f"{order.worker_type} worker is not implemented.",
        )
    else:
        skill_result = skill.run(
            SkillRequest(
                skill=skill.name,
                action=order.action,
                workdir=order.workdir,
                args=order.args,
                risk_level=order.risk_level,
                timeout_seconds=order.timeout_seconds,
            )
        )
        if skill_result.ok and order.verification_cmd:
            skill_result = ShellSkill().run(
                SkillRequest(
                    skill="shell",
                    action="verify",
                    workdir=order.workdir,
                    args={"command": order.verification_cmd},
                    risk_level=order.risk_level,
                    timeout_seconds=order.timeout_seconds,
                )
            )
        result = WorkResult(
            order_id=order.order_id,
            task_id=order.task_id,
            ca_thread_id=order.ca_thread_id,
            worker_type=order.worker_type,
            ok=skill_result.ok,
            exit_code=skill_result.exit_code,
            stdout=skill_result.stdout,
            stderr=skill_result.stderr,
            artifacts=skill_result.artifacts,
            summary=skill_result.summary,
        )
    return result
