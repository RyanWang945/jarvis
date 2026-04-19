import json
import time

from fastapi.testclient import TestClient

from app.agent.events import build_user_event
from app.agent.dispatcher import DispatcherService
from app.agent.runner import ThreadManager
from app.config import get_settings
from app.llm.deepseek import DeepSeekClient
from app.main import create_app
from app.tools.specs import ToolCallPlan, ToolSpec
from app.workers import InlineWorkerClient, ThreadWorkerClient, WorkOrder, WorkResult, WorkerEventBus


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


def test_deepseek_completion_assessment_parses_json(monkeypatch) -> None:
    captured_payload = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"decision": "failed", "summary": "DoD was not satisfied."}'
                        }
                    }
                ]
            }

    def fake_post(*args, **kwargs):
        captured_payload.update(kwargs["json"])
        return FakeResponse()

    monkeypatch.setattr("app.llm.deepseek.httpx.post", fake_post)

    client = DeepSeekClient(api_key="test-key")
    assessment = client.assess_completion(
        task={"title": "Assess output", "dod": "Covers tradeoffs"},
        result={"ok": True, "summary": "Short answer"},
        can_retry=True,
    )

    assert captured_payload["response_format"] == {"type": "json_object"}
    assert assessment == {"decision": "failed", "summary": "DoD was not satisfied."}


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


def test_aggregate_retries_failed_task_until_max_retries() -> None:
    from app.agent.nodes import aggregate
    from app.agent.state import initial_state

    event = build_user_event(instruction="Retry failed worker")
    state = initial_state(event, thread_id="thread-retry-1")
    order = WorkOrder(
        order_id="order-1",
        task_id="task-1",
        ca_thread_id="thread-retry-1",
        worker_type="echo",
        action="echo",
        args={"text": "retry me"},
        reason="Retry failed worker",
    )
    state["task_list"] = [
        {
            "id": "task-1",
            "title": "Retry failed worker",
            "description": "Retry failed worker",
            "status": "running",
            "resource_key": None,
            "dod": "Task execution completed successfully.",
            "verification_cmd": None,
            "tool_name": "echo",
            "tool_args": {"text": "retry me"},
            "worker_type": "echo",
            "order_id": "order-1",
            "retry_count": 0,
            "max_retries": 1,
            "result_summary": None,
        }
    ]
    state["work_orders"] = {"order-1": order.model_dump()}
    state["worker_results"] = {
        "order-1": WorkResult(
            order_id="order-1",
            task_id="task-1",
            ca_thread_id="thread-retry-1",
            worker_type="echo",
            ok=False,
            summary="first attempt failed",
        ).model_dump()
    }

    update = aggregate(state)

    assert update["next_node"] == "dispatch"
    assert update["task_list"][0]["status"] == "pending"
    assert update["task_list"][0]["retry_count"] == 1
    assert update["task_list"][0]["order_id"] != "order-1"
    assert update["dispatch_queue"][0]["order_id"] == update["task_list"][0]["order_id"]
    assert update["dispatch_queue"][0]["args"] == {"text": "retry me"}


def test_aggregate_blocks_failed_task_after_retry_budget_exhausted() -> None:
    from app.agent.nodes import aggregate
    from app.agent.state import initial_state

    event = build_user_event(instruction="Do not retry failed worker")
    state = initial_state(event, thread_id="thread-retry-2")
    state["task_list"] = [
        {
            "id": "task-1",
            "title": "Do not retry failed worker",
            "description": "Do not retry failed worker",
            "status": "running",
            "resource_key": None,
            "dod": "Task execution completed successfully.",
            "verification_cmd": None,
            "tool_name": "echo",
            "tool_args": {"text": "no retry"},
            "worker_type": "echo",
            "order_id": "order-1",
            "retry_count": 1,
            "max_retries": 1,
            "result_summary": None,
        }
    ]
    state["worker_results"] = {
        "order-1": WorkResult(
            order_id="order-1",
            task_id="task-1",
            ca_thread_id="thread-retry-2",
            worker_type="echo",
            ok=False,
            summary="second attempt failed",
        ).model_dump()
    }

    update = aggregate(state)

    assert update["next_node"] == "blocked"
    assert update["task_list"][0]["status"] == "failed"
    assert update["task_list"][0]["result_summary"] == "second attempt failed"


def test_aggregate_uses_semantic_assessment_only_for_non_objective_success(monkeypatch) -> None:
    from app.agent.nodes import CompletionAssessment, aggregate
    from app.agent.state import initial_state

    calls = []

    def fake_semantic_assessment(task, result):
        calls.append((task, result))
        return CompletionAssessment("success", "semantic success")

    monkeypatch.setattr(
        "app.agent.nodes._assess_task_completion_semantically",
        fake_semantic_assessment,
    )

    event = build_user_event(instruction="Assess narrative result")
    state = initial_state(event, thread_id="thread-assess-1")
    state["task_list"] = [
        {
            "id": "task-1",
            "title": "Assess narrative result",
            "description": "Assess narrative result",
            "status": "running",
            "resource_key": None,
            "dod": "Answer covers the important tradeoffs.",
            "verification_cmd": None,
            "tool_name": "delegate_to_claude_code",
            "tool_args": {"instruction": "write assessment"},
            "worker_type": "coder",
            "order_id": "order-1",
            "retry_count": 0,
            "max_retries": 0,
            "result_summary": None,
        }
    ]
    state["worker_results"] = {
        "order-1": WorkResult(
            order_id="order-1",
            task_id="task-1",
            ca_thread_id="thread-assess-1",
            worker_type="coder",
            ok=True,
            summary="worker succeeded",
        ).model_dump()
    }

    update = aggregate(state)

    assert len(calls) == 1
    assert update["next_node"] == "summarize"
    assert update["task_list"][0]["status"] == "success"
    assert update["task_list"][0]["result_summary"] == "semantic success"


def test_aggregate_llm_assessment_can_fail_non_objective_success(monkeypatch) -> None:
    from app.agent.nodes import aggregate
    from app.agent.state import initial_state

    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "llm")
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()

    def fake_assess_completion(self, *, task, result, can_retry):
        return {"decision": "failed", "summary": "LLM says DoD was not met."}

    monkeypatch.setattr("app.agent.nodes.DeepSeekClient.assess_completion", fake_assess_completion)

    event = build_user_event(instruction="Assess narrative result")
    state = initial_state(event, thread_id="thread-assess-2")
    state["task_list"] = [
        {
            "id": "task-1",
            "title": "Assess narrative result",
            "description": "Assess narrative result",
            "status": "running",
            "resource_key": None,
            "dod": "Answer covers the important tradeoffs.",
            "verification_cmd": None,
            "tool_name": "delegate_to_claude_code",
            "tool_args": {"instruction": "write assessment"},
            "worker_type": "coder",
            "order_id": "order-1",
            "retry_count": 0,
            "max_retries": 0,
            "result_summary": None,
        }
    ]
    state["worker_results"] = {
        "order-1": WorkResult(
            order_id="order-1",
            task_id="task-1",
            ca_thread_id="thread-assess-2",
            worker_type="coder",
            ok=True,
            summary="worker succeeded",
        ).model_dump()
    }

    update = aggregate(state)

    assert update["next_node"] == "blocked"
    assert update["task_list"][0]["status"] == "failed"
    assert update["task_list"][0]["result_summary"] == "LLM says DoD was not met."


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


def test_inspect_run_returns_orders_results_and_approval_history(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()
    runner = ThreadManager(tmp_path)
    event = build_user_event(instruction="Push code", command="git push origin main")

    result = runner.run_event(event)
    assert result.status == "waiting_approval"

    restarted = ThreadManager(tmp_path)
    inspection = restarted.inspect_run(result.thread_id)

    assert inspection is not None
    assert inspection["run"]["status"] == "waiting_approval"
    assert len(inspection["tasks"]) == 1
    assert len(inspection["work_orders"]) == 1
    assert inspection["work_orders"][0]["risk_level"] == "high"
    assert inspection["work_results"] == []
    assert len(inspection["approvals"]) == 1
    assert inspection["approvals"][0]["status"] == "waiting"


def test_recover_unfinished_replays_persisted_worker_result(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()

    class PendingWorkerClient:
        def dispatch(self, order):
            self.order = order
            return order.order_id

        def poll(self, order_id):
            return None

    pending_client = PendingWorkerClient()
    monkeypatch.setattr("app.agent.nodes.get_worker_client", lambda: pending_client)

    runner = ThreadManager(tmp_path)
    event = build_user_event(instruction="Recover worker result")
    result = runner.run_event(event)

    assert result.status == "monitoring"
    order = runner.db.work_orders.get_by_thread(result.thread_id)[0]
    assert order["status"] == "dispatched"

    runner.db.work_results.save(
        WorkResult(
            order_id=order["order_id"],
            task_id=order["task_id"],
            ca_thread_id=result.thread_id,
            worker_type=order["worker_type"],
            ok=True,
            summary="Recovered worker result.",
        )
    )

    restarted = ThreadManager(tmp_path)
    recovery = restarted.recover_unfinished()

    assert recovery["failed"] == []
    assert recovery["recovered"] == [
        {
            "thread_id": result.thread_id,
            "order_id": order["order_id"],
            "status": "completed",
        }
    ]
    run = restarted.db.runs.get_by_thread(result.thread_id)
    assert run is not None
    assert run["status"] == "completed"
    tasks = restarted.db.tasks.get_by_run(run["run_id"])
    assert tasks[0]["status"] == "success"


def test_agent_run_detail_api_returns_recovery_fields(tmp_path, monkeypatch) -> None:
    from app.api.agent import get_thread_manager

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()
    get_thread_manager.cache_clear()

    client = TestClient(create_app())
    response = client.post(
        "/agent/run",
        json={"instruction": "Push code", "command": "git push origin main"},
    )
    assert response.status_code == 200
    thread_id = response.json()["thread_id"]

    detail = client.get(f"/agent/runs/{thread_id}")

    assert detail.status_code == 200
    body = detail.json()
    assert body["run"]["status"] == "waiting_approval"
    assert len(body["work_orders"]) == 1
    assert body["work_results"] == []
    assert len(body["approvals"]) == 1

    get_thread_manager.cache_clear()


def test_thread_worker_client_runs_work_order_asynchronously(monkeypatch) -> None:
    def fake_execute(order):
        time.sleep(0.05)
        return WorkResult(
            order_id=order.order_id,
            task_id=order.task_id,
            ca_thread_id=order.ca_thread_id,
            worker_type=order.worker_type,
            ok=True,
            summary="threaded ok",
        )

    monkeypatch.setattr("app.workers.threaded.execute_work_order", fake_execute)

    client = ThreadWorkerClient(max_workers=1)
    order = WorkOrder(
        order_id="order-thread-1",
        task_id="task-thread-1",
        ca_thread_id="thread-1",
        worker_type="echo",
        action="echo",
        args={"text": "hello"},
        reason="Thread worker test",
    )

    assert client.dispatch(order) == "order-thread-1"
    assert client.poll(order.order_id) is None

    deadline = time.monotonic() + 1
    result = None
    while time.monotonic() < deadline:
        result = client.poll(order.order_id)
        if result is not None:
            break
        time.sleep(0.01)

    client.shutdown()

    assert result is not None
    assert result.ok is True
    assert result.summary == "threaded ok"


def test_dispatcher_resumes_thread_after_worker_completion(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()

    def fake_execute(order):
        time.sleep(0.05)
        return WorkResult(
            order_id=order.order_id,
            task_id=order.task_id,
            ca_thread_id=order.ca_thread_id,
            worker_type=order.worker_type,
            ok=True,
            summary="dispatcher completed worker",
        )

    monkeypatch.setattr("app.workers.threaded.execute_work_order", fake_execute)

    event_bus = WorkerEventBus()
    worker_client = ThreadWorkerClient(max_workers=1, event_bus=event_bus)
    monkeypatch.setattr("app.agent.nodes.get_worker_client", lambda: worker_client)

    manager = ThreadManager(tmp_path)
    dispatcher = DispatcherService(manager, event_bus=event_bus)
    result = manager.run_event(build_user_event(instruction="Run through dispatcher"))

    assert result.status == "monitoring"

    deadline = time.monotonic() + 1
    processed = 0
    while time.monotonic() < deadline:
        processed += dispatcher.drain_once()
        run = manager.db.runs.get_by_thread(result.thread_id)
        if run and run["status"] == "completed":
            break
        time.sleep(0.01)

    worker_client.shutdown()

    assert processed == 1
    run = manager.db.runs.get_by_thread(result.thread_id)
    assert run is not None
    assert run["status"] == "completed"
    tasks = manager.db.tasks.get_by_run(run["run_id"])
    assert tasks[0]["status"] == "success"
