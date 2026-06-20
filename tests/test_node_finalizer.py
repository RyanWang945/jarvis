from __future__ import annotations

import json
from pathlib import Path

from app.task_runtime.node_finalizer import CodeNodeFinalizer
from app.task_runtime.planner import PlanNode


def test_code_node_finalizer_loads_manifest_artifact(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions" / "sess_1"
    node_dir = session_root / "nodes" / "write_report"
    node_dir.mkdir(parents=True)
    report = node_dir / "report.md"
    report.write_text("# Report\n", encoding="utf-8")
    manifest = node_dir / "node_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "summary": "report generated",
                "artifacts": [
                    {
                        "ref": "report",
                        "kind": "file",
                        "path": "nodes/write_report/report.md",
                        "description": "Generated report",
                        "publish": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = CodeNodeFinalizer().finalize(
        node=PlanNode(id="write_report", runtime="coder", objective="Write report"),
        user_objective="write report",
        instruction="write report",
        provider="fake",
        provider_ok=True,
        exit_code=0,
        stdout="provider stdout",
        stderr="",
        provider_summary="provider summary",
        legacy_artifacts=[],
        metadata={},
        session_root=session_root,
        node_workspace=node_dir,
        manifest_path=manifest,
    )

    assert result.status == "completed"
    assert result.summary == "report generated"
    assert len(result.artifacts) == 1
    assert result.artifacts[0].ref == "report"
    assert result.artifacts[0].session_relative_path == "nodes/write_report/report.md"
    assert result.artifacts[0].filename == "report.md"
    assert result.artifacts[0].publish is True


def test_code_node_finalizer_rejects_absolute_manifest_artifact(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions" / "sess_1"
    node_dir = session_root / "nodes" / "write_report"
    node_dir.mkdir(parents=True)
    external = tmp_path / "external.md"
    external.write_text("external", encoding="utf-8")
    manifest = node_dir / "node_manifest.json"
    manifest.write_text(
        json.dumps({"artifacts": [{"ref": "external", "kind": "file", "path": str(external)}]}),
        encoding="utf-8",
    )

    result = CodeNodeFinalizer().finalize(
        node=PlanNode(id="write_report", runtime="coder", objective="Write report"),
        user_objective="write report",
        instruction="write report",
        provider="fake",
        provider_ok=True,
        exit_code=0,
        stdout="provider stdout",
        stderr="",
        provider_summary="provider summary",
        legacy_artifacts=[],
        metadata={},
        session_root=session_root,
        node_workspace=node_dir,
        manifest_path=manifest,
    )

    assert result.artifacts == []
    assert "artifact_candidate_rejected:external:invalid_session_relative_path" in result.data["finalizer"]["warnings"]


def test_code_node_finalizer_validates_llm_artifact_candidates(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions" / "sess_1"
    node_dir = session_root / "nodes" / "write_report"
    node_dir.mkdir(parents=True)
    output = node_dir / "output.md"
    output.write_text("body", encoding="utf-8")

    class FakeAgent:
        def finalize(self, request):
            return {
                "summary": "llm summary",
                "artifact_candidates": [
                    {"ref": "output", "kind": "file", "path": "nodes/write_report/output.md"},
                    {"ref": "missing", "kind": "file", "path": "nodes/write_report/missing.md"},
                ],
            }

    result = CodeNodeFinalizer(llm_agent=FakeAgent()).finalize(
        node=PlanNode(id="write_report", runtime="coder", objective="Write report"),
        user_objective="write report",
        instruction="write report",
        provider="fake",
        provider_ok=True,
        exit_code=0,
        stdout="provider stdout",
        stderr="",
        provider_summary="provider summary",
        legacy_artifacts=[],
        metadata={},
        session_root=session_root,
        node_workspace=node_dir,
    )

    assert result.summary == "llm summary"
    assert [artifact.ref for artifact in result.artifacts] == ["output"]
    assert "artifact_candidate_rejected:missing:missing_file" in result.data["finalizer"]["warnings"]
