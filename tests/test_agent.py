from fastapi.testclient import TestClient

from app.agent.events import build_user_event
from app.agent.runner import GraphRunner
from app.config import get_settings
from app.llm.deepseek import DeepSeekClient
from app.main import create_app
from app.tools.specs import ToolCallPlan, ToolSpec
from app.workers import InlineWorkerClient, WorkOrder


def test_graph_runner_completes_echo_task(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()
    runner = GraphRunner(tmp_path)
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
    runner = GraphRunner(tmp_path)
    event = build_user_event(instruction="Run a harmless command", command="python --version")

    result = runner.run_event(event)

    assert result.status == "completed"
    assert result.tasks[0]["status"] == "success"
    assert result.tasks[0]["worker_type"] == "shell"
    assert result.tasks[0]["result_summary"] == "Command exited with code 0."


def test_graph_runner_blocks_high_risk_shell_task_for_approval(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()
    runner = GraphRunner(tmp_path)
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
    runner = GraphRunner(tmp_path)
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
