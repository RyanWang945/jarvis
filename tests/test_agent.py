import json

from fastapi.testclient import TestClient

from app.agent.events import build_user_event
from app.agent.runner import ThreadManager
from app.config import get_settings
from app.llm.deepseek import DeepSeekClient
from app.main import create_app
from app.tools.specs import ToolCallPlan, ToolSpec
from app.workers import InlineWorkerClient, WorkOrder, WorkResult


def test_graph_runner_completes_echo_task(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()
    runner = ThreadManager(tmp_path)
    event = build_user_event(instruction="Summarize current project shape")

    result = runner.run_event(event)

    assert result.status == "completed"
    assert result.pending_approval_id is None
    assert result.tasks[0]["status"] == "success"
    assert result.tasks[0]["worker_type"] == "echo"
    assert result.tasks[0]["order_id"]
    assert "Completed 1 task" in (result.summary or "")


def test_graph_runner_runs_shell_task(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()
    runner = ThreadManager(tmp_path)
    event = build_user_event(instruction="Run a harmless command", command="python --version")

    result = runner.run_event(event)

    assert result.status == "completed"
    assert result.tasks[0]["status"] == "success"
    assert result.tasks[0]["worker_type"] == "shell"
    assert result.tasks[0]["result_summary"] == "Command exited with code 0."


def test_graph_runner_blocks_high_risk_shell_task_for_approval(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()
    runner = ThreadManager(tmp_path)
    event = build_user_event(instruction="Push code", command="git push origin main")

    result = runner.run_event(event)

    assert result.status == "waiting_approval"
    assert result.pending_approval_id is not None
    assert result.tasks[0]["status"] == "waiting"


def test_agent_run_api_completes_task(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()
    client = TestClient(create_app())

    response = client.post("/agent/run", json={"instruction": "Echo this task"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["thread_id"]
    assert body["tasks"][0]["status"] == "success"


def test_llm_planner_uses_deepseek_response(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "llm")
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()

    def fake_plan_tasks(self, *, instruction, tools):
        return [
            ToolCallPlan(
                tool_name="echo",
                tool_args={"text": "planned by llm"},
                title="Echo planned task",
                description="Use the echo tool",
                dod="Echo result exists",
            )
        ]

    monkeypatch.setattr("app.agent.nodes.DeepSeekClient.plan_tasks", fake_plan_tasks)
    runner = ThreadManager(tmp_path)
    event = build_user_event(instruction="Plan this with DeepSeek")

    result = runner.run_event(event)

    assert result.status == "completed"
    assert result.tasks[0]["tool_name"] == "echo"
    assert result.tasks[0]["result_summary"] == "planned by llm"


def test_deepseek_planner_sends_tools_and_parses_tool_calls(monkeypatch) -> None:
    captured_payload = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "echo",
                                        "arguments": '{"text": "selected by model"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }

    def fake_post(*args, **kwargs):
        captured_payload.update(kwargs["json"])
        return FakeResponse()

    monkeypatch.setattr("app.llm.deepseek.httpx.post", fake_post)

    client = DeepSeekClient(api_key="test-key")
    plans = client.plan_tasks(
        instruction="Choose a tool",
        tools=[
            ToolSpec(
                name="echo",
                description="Echo text",
                args_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                skill="echo",
                action="echo",
                exposed_to_llm=True,
            )
        ],
    )

    assert captured_payload["tool_choice"] == "auto"
    assert captured_payload["tools"][0]["function"]["name"] == "echo"
    assert plans == [ToolCallPlan(tool_name="echo", tool_args={"text": "selected by model"})]


def test_inline_worker_runs_shell_work_order(tmp_path) -> None:
    client = InlineWorkerClient()
    order = WorkOrder(
        order_id="order-1",
        task_id="task-1",
        ca_thread_id="thread-1",
        worker_type="shell",
        action="run",
        args={"command": "python --version"},
        workdir=str(tmp_path),
        risk_level="low",
        reason="Check Python version",
    )

    order_id = client.dispatch(order)
    result = client.poll(order_id)

    assert result is not None
    assert result.ok is True
    assert result.exit_code == 0


def test_dispatch_creates_active_workers_and_monitor_collects_results(monkeypatch) -> None:
    """Verify dispatch only starts workers; monitor collects results."""
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()

    from app.agent.nodes import dispatch, monitor
    from app.agent.state import initial_state

    event = build_user_event(instruction="Echo test")
    state = initial_state(event, thread_id="test-1")
    state["task_list"] = [
        {
            "id": "task-1",
            "title": "Echo",
            "description": "Echo test",
            "status": "pending",
            "resource_key": None,
            "dod": "Echo done",
            "verification_cmd": None,
            "tool_name": "echo",
            "tool_args": {"text": "hello"},
            "worker_type": "echo",
            "order_id": "order-1",
            "retry_count": 0,
            "max_retries": 0,
            "result_summary": None,
        }
    ]
    state["dispatch_queue"] = [
        WorkOrder(
            order_id="order-1",
            task_id="task-1",
            ca_thread_id="test-1",
            worker_type="echo",
            action="echo",
            args={"text": "hello"},
            workdir=None,
            risk_level="low",
            reason="Echo test",
        ).model_dump()
    ]

    # dispatch only starts workers, does not poll results
    dispatch_update = dispatch(state)
    assert dispatch_update["active_workers"] == {"task-1": "order-1"}
    assert "worker_results" not in dispatch_update or not dispatch_update.get("worker_results")

    # merge dispatch results back into state
    state.update(dispatch_update)

    # monitor collects results and drains active_workers
    monitor_update = monitor(state)
    assert "order-1" in monitor_update["worker_results"]
    assert monitor_update["active_workers"] == {}
    assert monitor_update["next_node"] == "aggregate"


def test_monitor_waits_again_when_resume_leaves_active_workers(monkeypatch) -> None:
    from app.agent.nodes import monitor
    from app.agent.state import initial_state

    monkeypatch.setattr(
        "app.agent.nodes.interrupt",
        lambda payload: {
            "event_type": "worker_complete",
            "payload": {
                "order_id": "order-1",
                "task_id": "task-1",
                "ca_thread_id": "test-1",
                "worker_type": "echo",
                "ok": True,
                "summary": "one worker finished",
            },
        },
    )

    event = build_user_event(instruction="Two workers")
    state = initial_state(event, thread_id="test-1")
    state["active_workers"] = {"task-1": "order-1", "task-2": "order-2"}

    monitor_update = monitor(state)

    assert monitor_update["active_workers"] == {"task-2": "order-2"}
    assert monitor_update["next_node"] == "monitor"


def test_wait_approval_interrupt_and_reject(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()
    runner = ThreadManager(tmp_path)
    event = build_user_event(instruction="Push code", command="git push origin main")

    result = runner.run_event(event)
    assert result.status == "waiting_approval"
    assert result.pending_approval_id is not None

    result2 = runner.resume(result.thread_id, {"approved": False})
    assert result2.status == "blocked"
    assert runner.db.approvals.get_pending_by_thread(result.thread_id) == []


def test_business_db_persists_run_and_tasks(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()
    runner = ThreadManager(tmp_path)
    event = build_user_event(instruction="Echo test persist")

    result = runner.run_event(event)
    assert result.status == "completed"

    # Verify business DB persisted the run
    run = runner.db.runs.get_by_thread(result.thread_id)
    assert run is not None
    assert run["instruction"] == "Echo test persist"
    assert run["status"] == "completed"

    # Verify tasks persisted
    tasks = runner.db.tasks.get_by_run(run["run_id"])
    assert len(tasks) == 1
    assert tasks[0]["status"] == "success"
    assert tasks[0]["worker_type"] == "echo"

    # Verify work order and result persistence
    orders = runner.db.work_orders.get_by_thread(result.thread_id)
    assert len(orders) == 1
    assert orders[0]["status"] == "completed"
    assert orders[0]["worker_type"] == "echo"

    work_result = runner.db.work_results.get_by_order(orders[0]["order_id"])
    assert work_result is not None
    assert work_result["ok"] == 1
    assert work_result["summary"] == "Echo test persist"

    # Verify audit logs
    audits = runner.db.audits.get_by_thread(result.thread_id)
    assert any(a["action"] == "persist_state" for a in audits)
    assert any(a["action"] == "worker_result_persisted" for a in audits)


def test_wait_approval_interrupt_and_approve(tmp_path, monkeypatch) -> None:
    from app.skills.base import SkillResult
    from app.skills.shell import ShellSkill

    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()

    def fake_run(self, request):
        return SkillResult(ok=True, exit_code=0, stdout="mocked", summary="Mock push ok.")

    monkeypatch.setattr(ShellSkill, "run", fake_run)

    runner = ThreadManager(tmp_path)
    event = build_user_event(instruction="Push code", command="git push origin main")

    result = runner.run_event(event)
    assert result.status == "waiting_approval"

    result2 = runner.resume(result.thread_id, {"approved": True})
    assert result2.status == "completed"
    assert result2.tasks[0]["status"] == "success"

    assert runner.db.approvals.get_pending_by_thread(result.thread_id) == []


def test_approval_preserves_original_work_order_metadata(tmp_path, monkeypatch) -> None:
    from app.skills.base import SkillResult
    from app.skills.shell import ShellSkill

    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()

    calls = []

    def fake_run(self, request):
        calls.append(request)
        return SkillResult(
            ok=True,
            exit_code=0,
            stdout="mocked",
            summary=f"{request.action} ok.",
        )

    monkeypatch.setattr(ShellSkill, "run", fake_run)

    runner = ThreadManager(tmp_path)
    event = build_user_event(
        instruction="Push code",
        command="git push origin main",
        verification_cmd="python --version",
    )

    result = runner.run_event(event)
    assert result.status == "waiting_approval"

    result2 = runner.resume(result.thread_id, {"approved": True})

    assert result2.status == "completed"
    assert [call.action for call in calls] == ["run", "verify"]
    orders = runner.db.work_orders.get_by_thread(result.thread_id)
    assert len(orders) == 1
    assert orders[0]["risk_level"] == "high"
    assert orders[0]["verification_cmd"] == "python --version"
    assert orders[0]["timeout_seconds"] == 30


def test_run_repository_updates_existing_thread_instead_of_duplicating(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()
    runner = ThreadManager(tmp_path)

    runner.db.runs.save(
        {
            "run_id": "run-1",
            "thread_id": "thread-1",
            "status": "created",
            "instruction": "first instruction",
        }
    )
    runner.db.runs.save(
        {
            "run_id": "run-2",
            "thread_id": "thread-1",
            "status": "completed",
            "instruction": None,
            "summary": "done",
        }
    )

    run = runner.db.runs.get_by_thread("thread-1")
    assert run is not None
    assert run["run_id"] == "run-1"
    assert run["status"] == "completed"
    assert run["instruction"] == "first instruction"
    rows = runner.db.conn.execute("SELECT * FROM runs WHERE thread_id = ?", ("thread-1",)).fetchall()
    assert len(rows) == 1


def test_work_result_repository_persists_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()
    runner = ThreadManager(tmp_path)

    runner.db.work_results.save(
        WorkResult(
            order_id="order-1",
            task_id="task-1",
            ca_thread_id="thread-1",
            worker_type="echo",
            ok=True,
            artifacts=["report.md", "logs/output.txt"],
            summary="done",
        )
    )

    row = runner.db.work_results.get_by_order("order-1")
    assert row is not None
    assert json.loads(row["artifacts"]) == ["report.md", "logs/output.txt"]
