from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PlanValidationIssue:
    code: str
    message: str
    path: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "path": self.path}


def validate_plan(
    plan: Any,
    *,
    allowed_runtimes: set[str] | None = None,
    known_artifact_refs: set[str] | None = None,
    registered_repo_ids: set[str] | None = None,
    max_nodes: int = 8,
) -> list[PlanValidationIssue]:
    issues: list[PlanValidationIssue] = []
    if len(plan.nodes) > max_nodes:
        issues.append(
            PlanValidationIssue(
                code="too_many_nodes",
                message=f"plan has {len(plan.nodes)} nodes; maximum is {max_nodes}",
                path="nodes",
            )
        )
    for index, node in enumerate(plan.nodes):
        path = f"nodes[{index}]"
        if allowed_runtimes is not None and node.runtime not in allowed_runtimes:
            issues.append(
                PlanValidationIssue(
                    code="runtime_not_allowed",
                    message=f"runtime {node.runtime!r} is not in available runtimes",
                    path=f"{path}.runtime",
                )
            )
        if node.runtime == "react" and node.repo_id:
            issues.append(
                PlanValidationIssue(
                    code="react_repo_id_not_allowed",
                    message="react nodes must not set repo_id",
                    path=f"{path}.repo_id",
                )
            )
        if node.runtime == "coder" and node.repo_id and registered_repo_ids is not None and node.repo_id not in registered_repo_ids:
            issues.append(
                PlanValidationIssue(
                    code="unknown_repo_id",
                    message=f"repo_id {node.repo_id!r} is not registered",
                    path=f"{path}.repo_id",
                )
            )
        for ref_index, ref in enumerate(node.input_refs):
            ref_path = f"{path}.input_refs[{ref_index}]"
            if ref.startswith("artifact:") and known_artifact_refs is not None and ref not in known_artifact_refs:
                issues.append(
                    PlanValidationIssue(
                        code="unknown_artifact_ref",
                        message=f"artifact ref {ref!r} is not available",
                        path=ref_path,
                    )
                )
    return issues


def issues_payload(issues: list[PlanValidationIssue]) -> list[dict[str, Any]]:
    return [issue.as_dict() for issue in issues]
