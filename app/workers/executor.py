from app.skills import ShellSkill, get_default_skill_registry
from app.skills.base import SkillRequest, SkillResult
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
            verification_result = ShellSkill().run(
                SkillRequest(
                    skill="shell",
                    action="verify",
                    workdir=order.workdir,
                    args={"command": order.verification_cmd},
                    risk_level=order.risk_level,
                    timeout_seconds=order.timeout_seconds,
                )
            )
            skill_result = _merge_verification_result(skill_result, verification_result)
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


def _combine_output(primary: str, verification: str, *, label: str) -> str:
    if not verification:
        return primary
    if not primary:
        return f"[{label}]\n{verification}"
    return f"{primary}\n\n[{label}]\n{verification}"


def _merge_verification_result(primary: SkillResult, verification: SkillResult) -> SkillResult:
    summary = (
        f"{primary.summary} Verification: {verification.summary}"
        if verification.ok
        else verification.summary
    )
    return SkillResult(
        ok=verification.ok,
        exit_code=verification.exit_code,
        stdout=_combine_output(
            primary.stdout,
            verification.stdout,
            label="VERIFICATION_STDOUT",
        ),
        stderr=_combine_output(
            primary.stderr,
            verification.stderr,
            label="VERIFICATION_STDERR",
        ),
        artifacts=[*primary.artifacts, *verification.artifacts],
        summary=summary,
    )
