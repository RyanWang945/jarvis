import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.agent.events import build_user_event
from app.agent.dispatcher import DispatcherService
from app.agent.runner import ThreadManager
from app.config import get_settings
from app.llm.deepseek import DeepSeekClient
from app.llm.jarvis import get_jarvis_llm
from app.main import create_app
from app.skills.bootstrap import bootstrap_registries, reset_registries_for_tests
from app.skills import get_default_skill_registry
from app.tools import get_default_tool_registry
from app.tools.specs import ToolCallPlan, ToolSpec
from app.workers import InlineWorkerClient, ThreadWorkerClient, WorkOrder, WorkResult, WorkerEventBus
from app.workers.executor import execute_work_order


class _completed:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


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


def test_blocked_run_returns_worker_diagnostics(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()
    runner = ThreadManager(tmp_path)
    event = build_user_event(
        instruction="Run failing command",
        command="python -c \"import sys; print('out'); sys.stderr.write('err'); sys.exit(2)\"",
    )

    result = runner.run_event(event)

    assert result.status == "blocked"
    assert result.diagnostics is not None
    assert result.diagnostics["exit_code"] == 2
    assert "out" in result.diagnostics["stdout_tail"]
    assert "err" in result.diagnostics["stderr_tail"]


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
    get_jarvis_llm.cache_clear()

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

    monkeypatch.setattr("app.llm.jarvis.JarvisLLM.plan_tasks", fake_plan_tasks)
    runner = ThreadManager(tmp_path)
    event = build_user_event(instruction="Plan this with DeepSeek")

    result = runner.run_event(event)

    assert result.status == "completed"
    assert result.tasks[0]["tool_name"] == "echo"
    assert result.tasks[0]["result_summary"] == "planned by llm"


def test_llm_planned_coder_task_waits_for_approval_then_runs(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "llm")
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()
    get_jarvis_llm.cache_clear()

    def fake_plan_tasks(self, *, instruction, tools):
        return [
            ToolCallPlan(
                tool_name="delegate_to_claude_code",
                tool_args={
                    "instruction": "Modify project code safely.",
                    "workdir": str(tmp_path),
                },
                title="Modify code",
                description="Modify code through coder worker",
                dod="Code change completed",
            )
        ]

    class Completed:
        returncode = 0
        stdout = "code changed"
        stderr = ""

    monkeypatch.setattr("app.llm.jarvis.JarvisLLM.plan_tasks", fake_plan_tasks)
    monkeypatch.setattr(
        "app.llm.jarvis.JarvisLLM.assess_completion",
        lambda self, *, task, result, can_retry: {
            "decision": "success",
            "summary": result["summary"],
        },
    )
    monkeypatch.setattr("app.skills.coder.which", lambda provider: provider)
    monkeypatch.setattr("app.skills.coder.subprocess.run", lambda *args, **kwargs: Completed())

    runner = ThreadManager(tmp_path)
    result = runner.run_event(build_user_event(instruction="Change GitHub code"))

    assert result.status == "waiting_approval"
    assert result.pending_approval_id is not None
    assert result.tasks[0]["worker_type"] == "coder"

    approved = runner.resume(result.thread_id, {"approved": True})

    assert approved.status == "completed"
    assert approved.tasks[0]["status"] == "success"
    assert approved.tasks[0]["result_summary"] == "claude CLI exited with code 0."


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

    monkeypatch.setattr("app.llm.client.httpx.post", fake_post)

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

    monkeypatch.setattr("app.llm.client.httpx.post", fake_post)

    client = DeepSeekClient(api_key="test-key")
    assessment = client.assess_completion(
        task={"title": "Assess output", "dod": "Covers tradeoffs"},
        result={"ok": True, "summary": "Short answer"},
        can_retry=True,
    )

    assert captured_payload["response_format"] == {"type": "json_object"}
    assert assessment == {"decision": "failed", "summary": "DoD was not satisfied."}


def test_deepseek_completion_assessment_allows_replan(monkeypatch) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"decision": "replan", "summary": "Need a different tool."}'
                        }
                    }
                ]
            }

    monkeypatch.setattr("app.llm.client.httpx.post", lambda *args, **kwargs: FakeResponse())

    client = DeepSeekClient(api_key="test-key")
    assessment = client.assess_completion(
        task={"title": "Assess output", "dod": "Covers tradeoffs"},
        result={"ok": True, "summary": "Short answer"},
        can_retry=False,
    )

    assert assessment == {"decision": "replan", "summary": "Need a different tool."}


def test_jarvis_llm_factory_supports_kimi_provider(monkeypatch) -> None:
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
                                        "arguments": '{"text": "planned by kimi"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }

    def fake_post(*args, **kwargs):
        captured_payload["url"] = args[0]
        captured_payload["headers"] = kwargs["headers"]
        captured_payload["json"] = kwargs["json"]
        return FakeResponse()

    monkeypatch.setenv("JARVIS_LLM_PROVIDER", "kimi")
    monkeypatch.setenv("JARVIS_KIMI_API_KEY", "kimi-key")
    monkeypatch.setenv("JARVIS_KIMI_MODEL", "moonshot-test")
    get_settings.cache_clear()
    get_jarvis_llm.cache_clear()
    monkeypatch.setattr("app.llm.client.httpx.post", fake_post)

    plans = get_jarvis_llm().plan_tasks(
        instruction="Plan with Kimi",
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

    assert captured_payload["url"] == "https://api.moonshot.cn/v1/chat/completions"
    assert captured_payload["headers"]["Authorization"] == "Bearer kimi-key"
    assert captured_payload["json"]["model"] == "moonshot-test"
    assert plans == [ToolCallPlan(tool_name="echo", tool_args={"text": "planned by kimi"})]


def test_jarvis_llm_factory_supports_gemini_provider(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("JARVIS_GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("JARVIS_GEMINI_MODEL", "gemini-test")
    get_settings.cache_clear()
    get_jarvis_llm.cache_clear()

    llm = get_jarvis_llm()

    assert llm is get_jarvis_llm()


def test_jarvis_llm_factory_keeps_legacy_deepseek_timeout(monkeypatch) -> None:
    captured_payload = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": '{"tasks": []}'}}]}

    def fake_post(*args, **kwargs):
        captured_payload["timeout"] = kwargs["timeout"]
        return FakeResponse()

    monkeypatch.setenv("JARVIS_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "deepseek-key")
    monkeypatch.setenv("JARVIS_DEEPSEEK_TIMEOUT_SECONDS", "123")
    get_settings.cache_clear()
    get_jarvis_llm.cache_clear()
    monkeypatch.setattr("app.llm.client.httpx.post", fake_post)

    plans = get_jarvis_llm().plan_tasks(instruction="No-op", tools=[])

    assert plans == []
    assert captured_payload["timeout"] == 123


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


def test_skill_registry_exposes_default_skills() -> None:
    registry = get_default_skill_registry()

    assert registry.get("echo").name == "echo"
    assert registry.get("shell").name == "shell"
    assert registry.get("coder").name == "coder"
    try:
        registry.get("web_search")
    except ValueError as exc:
        assert "unknown skill" in str(exc)
    else:
        raise AssertionError("web_search should not be registered as a built-in skill")


def test_coder_worker_invokes_claude_with_publish_workflow_guidance(tmp_path, monkeypatch) -> None:
    get_settings.cache_clear()
    captured = {}
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    stale_lock = git_dir / "index.lock"
    stale_lock.write_text("", encoding="utf-8")

    def fake_run(command, **kwargs):
        if command[0] != "git":
            captured["command"] = command
            captured["kwargs"] = kwargs
            return _completed(stdout="committed and pushed")
        if command[-1] == "--branch":
            return _completed(stdout="## main...origin/main\n")
        if command[-1] == "--show-current":
            return _completed(stdout="main\n")
        if command[-2:] == ["--short", "HEAD"]:
            return _completed(stdout="abc1234\n")
        if command[-1] == "--pretty=%s":
            return _completed(stdout="docs: add readme\n")
        if command[-2:] == ["get-url", "origin"]:
            return _completed(stdout="git@github.com:RyanWang945/nltk.git\n")
        return _completed()

    monkeypatch.setattr("app.skills.coder.which", lambda provider: f"C:/bin/{provider}.ps1")
    monkeypatch.setattr("app.skills.coder.subprocess.run", fake_run)

    result = execute_work_order(
        WorkOrder(
            order_id="coder-order-2",
            task_id="coder-task-2",
            ca_thread_id="thread-coder-2",
            worker_type="coder",
            action="run",
            args={
                "instruction": "Update README.md, commit with message docs: add readme, and push origin HEAD.",
                "verification_cmd": "git status --short",
            },
            workdir=str(tmp_path),
            reason="Code publish",
        )
    )

    prompt = captured["kwargs"]["input"]
    assert result.ok is True
    assert captured["command"][:5] == [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
    ]
    assert captured["command"][5] == "&"
    assert "--allowedTools" in captured["command"]
    assert "bypassPermissions" in captured["command"]
    assert "If the task explicitly asks to commit" in prompt
    assert "If the task explicitly asks to push" in prompt
    assert "Run this verification command before finishing: git status --short" in prompt
    assert "Update README.md" in prompt
    assert "JARVIS_POSTFLIGHT" in result.stdout
    assert "Removed stale .git/index.lock." in result.stdout
    assert not stale_lock.exists()
    assert "git_commit:abc1234" in result.artifacts
    assert "git_upstream:synced" in result.artifacts


def test_tool_registry_prefers_coder_for_development_publish_workflows() -> None:
    registry = get_default_tool_registry()

    coder_tool = registry.get("delegate_to_claude_code")
    shell_tool = registry.get("run_shell_command")

    assert "git commit" in coder_tool.description
    assert "git push" in coder_tool.description
    assert "Do not use this for multi-step code editing" in shell_tool.description


def test_builtin_web_search_tool_is_not_registered() -> None:
    registry = get_default_tool_registry()

    try:
        registry.get("web_search")
    except ValueError as exc:
        assert "unknown tool" in str(exc)
    else:
        raise AssertionError("web_search should not be registered as a built-in tool")


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
    get_jarvis_llm.cache_clear()

    def fake_assess_completion(self, *, task, result, can_retry):
        return {"decision": "failed", "summary": "LLM says DoD was not met."}

    monkeypatch.setattr("app.llm.jarvis.JarvisLLM.assess_completion", fake_assess_completion)

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


def test_aggregate_does_not_use_completion_assessor_for_external_skill(monkeypatch) -> None:
    from app.agent.nodes import aggregate
    from app.agent.state import initial_state

    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "llm")
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()
    get_jarvis_llm.cache_clear()

    def fail_if_called(*args, **kwargs):
        raise AssertionError("external skill success should be handled by summarize")

    monkeypatch.setattr("app.agent.nodes._assess_task_completion_semantically", fail_if_called)

    state = initial_state(build_user_event(instruction="Search and cite URLs"), thread_id="thread-search-assess")
    state["task_list"] = [
        {
            "id": "task-search",
            "title": "Search and cite URLs",
            "description": "Search and cite URLs",
            "status": "running",
            "resource_key": None,
            "dod": "Return 3 source URLs.",
            "verification_cmd": None,
            "tool_name": "tavily_search",
            "tool_args": {"query": "Tavily API docs"},
            "worker_type": "tavily-search",
            "order_id": "order-search",
            "retry_count": 0,
            "max_retries": 0,
            "result_summary": None,
        }
    ]
    state["worker_results"] = {
        "order-search": WorkResult(
            order_id="order-search",
            task_id="task-search",
            ca_thread_id="thread-search-assess",
            worker_type="tavily-search",
            ok=True,
            stdout='{"results":[{"url":"https://docs.tavily.com"}]}',
            summary="Tavily search completed.",
        ).model_dump()
    }

    update = aggregate(state)

    assert update["next_node"] == "summarize"
    assert update["task_list"][0]["status"] == "success"
    assert update["task_list"][0]["result_summary"] == "Tavily search completed."


def test_summarize_llm_synthesizes_user_facing_answer(monkeypatch) -> None:
    from app.agent.nodes import summarize
    from app.agent.state import initial_state

    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "llm")
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()
    get_jarvis_llm.cache_clear()

    def fake_synthesize(self, *, instruction, tasks, worker_results):
        assert instruction == "Return 3 URLs and one sentence."
        assert tasks[0]["tool_name"] == "tavily_search"
        assert "https://docs.tavily.com" in worker_results[0]["stdout"]
        return "Tavily /search returns ranked web results.\n\n1. https://docs.tavily.com"

    monkeypatch.setattr("app.llm.jarvis.JarvisLLM.synthesize_final_answer", fake_synthesize)

    state = initial_state(build_user_event(instruction="Return 3 URLs and one sentence."), thread_id="thread-summary")
    state["task_list"] = [
        {
            "id": "task-search",
            "title": "Return 3 URLs and one sentence.",
            "description": "Return 3 URLs and one sentence.",
            "status": "success",
            "resource_key": None,
            "dod": "Return 3 URLs and one sentence.",
            "verification_cmd": None,
            "tool_name": "tavily_search",
            "tool_args": {"query": "Tavily API docs"},
            "worker_type": "tavily-search",
            "order_id": "order-search",
            "retry_count": 0,
            "max_retries": 0,
            "result_summary": "Tavily search completed.",
        }
    ]
    state["worker_results"] = {
        "order-search": WorkResult(
            order_id="order-search",
            task_id="task-search",
            ca_thread_id="thread-summary",
            worker_type="tavily-search",
            ok=True,
            stdout='{"results":[{"url":"https://docs.tavily.com"}]}',
            summary="Tavily search completed.",
        ).model_dump()
    }

    update = summarize(state)

    assert update["status"] == "completed"
    assert update["final_summary"].startswith("Tavily /search")
    assert "https://docs.tavily.com" in update["final_summary"]


def test_summarize_falls_back_to_search_results_when_llm_rejects(monkeypatch) -> None:
    from app.agent.nodes import summarize
    from app.agent.state import initial_state

    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "llm")
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()
    get_jarvis_llm.cache_clear()

    def fake_synthesize(self, *, instruction, tasks, worker_results):
        raise RuntimeError("Content Exists Risk")

    monkeypatch.setattr("app.llm.jarvis.JarvisLLM.synthesize_final_answer", fake_synthesize)

    state = initial_state(build_user_event(instruction="查查某人的简介"), thread_id="thread-summary-fallback")
    state["task_list"] = [
        {
            "id": "task-search",
            "title": "查查某人的简介",
            "description": "查查某人的简介",
            "status": "success",
            "resource_key": None,
            "dod": "Return a profile.",
            "verification_cmd": None,
            "tool_name": "tavily_search",
            "tool_args": {"query": "某人 简介"},
            "worker_type": "tavily-search",
            "order_id": "order-search",
            "retry_count": 0,
            "max_retries": 0,
            "result_summary": "Tavily search completed.",
        }
    ]
    state["worker_results"] = {
        "order-search": WorkResult(
            order_id="order-search",
            task_id="task-search",
            ca_thread_id="thread-summary-fallback",
            worker_type="tavily-search",
            ok=True,
            stdout=json.dumps(
                {
                    "query": "某人 简介",
                    "results": [
                        {
                            "title": "Profile Source",
                            "url": "https://example.com/profile",
                            "snippet": "Profile snippet",
                        }
                    ],
                }
            ),
            summary="Tavily search completed.",
        ).model_dump()
    }

    update = summarize(state)

    assert update["status"] == "completed"
    assert "https://example.com/profile" in update["final_summary"]
    assert "Profile snippet" in update["final_summary"]
    assert "摘要：" in update["final_summary"]
    assert update["final_summary"].index("摘要：") < update["final_summary"].index("来源：")


def test_summarize_falls_back_to_urls_from_text_when_llm_rejects(monkeypatch) -> None:
    from app.agent.nodes import summarize
    from app.agent.state import initial_state

    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "llm")
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()
    get_jarvis_llm.cache_clear()

    def fake_synthesize(self, *, instruction, tasks, worker_results):
        raise RuntimeError("Content Exists Risk")

    monkeypatch.setattr("app.llm.jarvis.JarvisLLM.synthesize_final_answer", fake_synthesize)

    state = initial_state(build_user_event(instruction="查查某人的简介"), thread_id="thread-summary-text-fallback")
    state["task_list"] = [
        {
            "id": "task-search",
            "title": "查查某人的简介",
            "description": "查查某人的简介",
            "status": "success",
            "resource_key": None,
            "dod": "Return a profile.",
            "verification_cmd": None,
            "tool_name": "tavily_search",
            "tool_args": {"query": "某人 简介", "format": "md"},
            "worker_type": "tavily-search",
            "order_id": "order-search",
            "retry_count": 0,
            "max_retries": 0,
            "result_summary": "Tavily search completed.",
        }
    ]
    state["worker_results"] = {
        "order-search": WorkResult(
            order_id="order-search",
            task_id="task-search",
            ca_thread_id="thread-summary-text-fallback",
            worker_type="tavily-search",
            ok=True,
            stdout="1. Profile Source\n   https://example.com/profile\n   - Profile text snippet",
            summary="Tavily search completed.",
        ).model_dump()
    }

    update = summarize(state)

    assert update["status"] == "completed"
    assert "https://example.com/profile" in update["final_summary"]
    assert "Profile text snippet" in update["final_summary"]
    assert "摘要：" in update["final_summary"]
    assert update["final_summary"].index("摘要：") < update["final_summary"].index("来源：")


def test_aggregate_llm_assessment_can_trigger_replan(monkeypatch) -> None:
    from app.agent.nodes import aggregate
    from app.agent.state import initial_state

    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "llm")
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()
    get_jarvis_llm.cache_clear()

    def fake_assess_completion(self, *, task, result, can_retry):
        return {"decision": "replan", "summary": "Need a different approach."}

    monkeypatch.setattr("app.llm.jarvis.JarvisLLM.assess_completion", fake_assess_completion)

    event = build_user_event(instruction="Assess narrative result")
    state = initial_state(event, thread_id="thread-replan-1")
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
            ca_thread_id="thread-replan-1",
            worker_type="coder",
            ok=True,
            summary="worker succeeded",
        ).model_dump()
    }

    update = aggregate(state)

    assert update["next_node"] == "strategize"
    assert update["task_list"][0]["status"] == "cancelled"
    assert update["task_list"][0]["result_summary"] == "Replanning: Need a different approach."


def test_strategize_appends_replanned_tasks_without_overwriting_history(monkeypatch) -> None:
    from app.agent.nodes import strategize
    from app.agent.state import initial_state

    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()

    event = build_user_event(instruction="Create a new plan")
    state = initial_state(event, thread_id="thread-replan-2")
    state["task_list"] = [
        {
            "id": "old-task",
            "title": "Old task",
            "description": "Old task",
            "status": "cancelled",
            "resource_key": None,
            "dod": "Old DoD",
            "verification_cmd": None,
            "tool_name": "delegate_to_claude_code",
            "tool_args": {"instruction": "old"},
            "worker_type": "coder",
            "order_id": "old-order",
            "retry_count": 0,
            "max_retries": 0,
            "result_summary": "Replanning: Need a different approach.",
        }
    ]
    state["work_orders"] = {
        "old-order": WorkOrder(
            order_id="old-order",
            task_id="old-task",
            ca_thread_id="thread-replan-2",
            worker_type="echo",
            action="echo",
            args={"text": "old"},
            reason="old",
        ).model_dump()
    }

    update = strategize(state)

    assert update["next_node"] == "dispatch"
    assert len(update["task_list"]) == 2
    assert update["task_list"][0]["id"] == "old-task"
    assert update["task_list"][1]["id"] != "old-task"
    assert "old-order" in update["work_orders"]


def test_llm_replan_context_is_sent_to_planner(monkeypatch) -> None:
    from app.agent.nodes import _planned_tool_calls
    from app.agent.state import initial_state

    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "llm")
    monkeypatch.setenv("JARVIS_DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()
    get_jarvis_llm.cache_clear()

    captured = {}

    def fake_plan_tasks(self, *, instruction, tools):
        captured["instruction"] = instruction
        return [ToolCallPlan(tool_name="echo", tool_args={"text": "new plan"})]

    monkeypatch.setattr("app.llm.jarvis.JarvisLLM.plan_tasks", fake_plan_tasks)

    event = build_user_event(instruction="Create a new plan")
    state = initial_state(event, thread_id="thread-replan-context")
    state["task_list"] = [
        {
            "id": "old-task",
            "title": "Old task",
            "description": "Old task",
            "status": "cancelled",
            "resource_key": None,
            "dod": "Answer covers tradeoffs",
            "verification_cmd": None,
            "tool_name": "delegate_to_claude_code",
            "tool_args": {"instruction": "old"},
            "worker_type": "coder",
            "order_id": "old-order",
            "retry_count": 0,
            "max_retries": 0,
            "result_summary": "Replanning: Need a different approach.",
        }
    ]
    state["worker_results"] = {
        "old-order": WorkResult(
            order_id="old-order",
            task_id="old-task",
            ca_thread_id="thread-replan-context",
            worker_type="coder",
            ok=True,
            stdout="short output",
            stderr="",
            summary="worker succeeded",
        ).model_dump()
    }

    plans = _planned_tool_calls(state)

    assert plans == [ToolCallPlan(tool_name="echo", tool_args={"text": "new plan"})]
    assert "Replanning context from previous attempts" in captured["instruction"]
    assert "Need a different approach" in captured["instruction"]
    assert "Answer covers tradeoffs" in captured["instruction"]
    assert "short output" in captured["instruction"]


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
    assert any(a["action"] == "skill_call_recorded" for a in audits)
    assert any(a["action"] == "worker_completed" for a in audits)

    report_path = tmp_path / "reports" / f"{result.thread_id}.json"
    note_path = tmp_path / "notes" / f"{result.thread_id}.md"
    assert report_path.exists()
    assert note_path.exists()
    assert json.loads(report_path.read_text(encoding="utf-8"))["run"]["status"] == "completed"
    assert "Echo test persist" in note_path.read_text(encoding="utf-8")


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


def test_agent_report_api_exports_files(tmp_path, monkeypatch) -> None:
    from app.api.agent import get_thread_manager

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()
    get_thread_manager.cache_clear()

    client = TestClient(create_app())
    response = client.post("/agent/run", json={"instruction": "Create report"})
    assert response.status_code == 200
    thread_id = response.json()["thread_id"]

    report = client.post(f"/agent/runs/{thread_id}/report")

    assert report.status_code == 200
    paths = report.json()["paths"]
    assert Path(paths["json"]).exists()
    assert Path(paths["markdown"]).exists()

    get_thread_manager.cache_clear()


def test_startup_recovery_replays_persisted_worker_result(tmp_path, monkeypatch) -> None:
    from app.api.agent import get_thread_manager

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    monkeypatch.setenv("JARVIS_AUTO_RECOVER_ON_STARTUP", "true")
    get_settings.cache_clear()
    get_thread_manager.cache_clear()

    class PendingWorkerClient:
        def dispatch(self, order):
            self.order = order
            return order.order_id

        def poll(self, order_id):
            return None

    pending_client = PendingWorkerClient()
    monkeypatch.setattr("app.agent.nodes.get_worker_client", lambda: pending_client)

    manager = ThreadManager(tmp_path)
    result = manager.run_event(build_user_event(instruction="Recover on startup"))
    order = manager.db.work_orders.get_by_thread(result.thread_id)[0]
    manager.db.work_results.save(
        WorkResult(
            order_id=order["order_id"],
            task_id=order["task_id"],
            ca_thread_id=result.thread_id,
            worker_type=order["worker_type"],
            ok=True,
            summary="Startup recovered worker result.",
        )
    )

    get_thread_manager.cache_clear()
    with TestClient(create_app()):
        recovered = get_thread_manager().db.runs.get_by_thread(result.thread_id)

    assert recovered is not None
    assert recovered["status"] == "completed"
    get_thread_manager.cache_clear()


def test_cli_run_status_and_report(tmp_path, monkeypatch, capsys) -> None:
    from app.cli import main as cli_main

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_PLANNER_TYPE", "rule_based")
    get_settings.cache_clear()

    cli_main(["run", "CLI smoke test"])
    run_output = json.loads(capsys.readouterr().out)
    thread_id = run_output["thread_id"]
    assert run_output["status"] == "completed"

    cli_main(["status", thread_id])
    status_output = json.loads(capsys.readouterr().out)
    assert status_output["run"]["thread_id"] == thread_id

    cli_main(["report", thread_id])
    report_output = json.loads(capsys.readouterr().out)
    assert Path(report_output["paths"]["json"]).exists()


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


def test_external_manifest_skill_registers_tool_and_executes(tmp_path) -> None:
    skills_root = tmp_path / "skills"
    package = skills_root / "uuid_generator"
    package.mkdir(parents=True)
    (package / "manifest.yaml").write_text(
        """
name: uuid_generator
description: Generate UUID values.
jarvis:
  module: skill
  class_name: UuidSkill
  tools:
    - name: generate_uuid
      description: Generate a UUID.
      args_schema:
        type: object
        properties: {}
      skill: uuid_generator
      worker_type: uuid_generator
      action: generate
      risk_level: low
      exposed_to_llm: true
""",
        encoding="utf-8",
    )
    (package / "skill.py").write_text(
        """
from app.skills.base import SkillResult


class UuidSkill:
    name = "uuid_generator"

    def run(self, request):
        return SkillResult(ok=True, exit_code=0, stdout="fixed-uuid", summary="generated uuid")
""",
        encoding="utf-8",
    )

    reset_registries_for_tests()
    try:
        registries = bootstrap_registries(external_paths=[skills_root], force=True)

        tool = registries.tool_registry.get("generate_uuid")
        assert tool.worker_type == "uuid_generator"
        assert tool.exposed_to_llm is True

        result = execute_work_order(
            WorkOrder(
                order_id="uuid-order-1",
                task_id="uuid-task-1",
                ca_thread_id="uuid-thread-1",
                worker_type="uuid_generator",
                action="generate",
                args={},
                reason="Generate UUID",
            ),
            skill_registry=registries.skill_registry,
        )

        assert result.ok is True
        assert result.stdout == "fixed-uuid"
    finally:
        reset_registries_for_tests()


def test_invalid_external_skill_package_is_skipped(tmp_path, caplog) -> None:
    skills_root = tmp_path / "skills"
    package = skills_root / "broken"
    package.mkdir(parents=True)
    (package / "manifest.yaml").write_text("name: broken\njarvis:\n  class_name: MissingSkill\n", encoding="utf-8")

    reset_registries_for_tests()
    try:
        registries = bootstrap_registries(external_paths=[skills_root], force=True)

        assert registries.tool_registry.get("echo").name == "echo"
        assert "skipping invalid skill package" in caplog.text
    finally:
        reset_registries_for_tests()


def test_external_skill_md_frontmatter_registers_tool(tmp_path) -> None:
    skills_root = tmp_path / "skills"
    package = skills_root / "note_echo"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        """---
name: note_echo
description: Echo notes.
metadata:
  jarvis:
    module: skill
    class_name: NoteEchoSkill
    tools:
      - name: note_echo
        description: Echo a note.
        args_schema:
          type: object
          properties:
            text:
              type: string
          required:
            - text
        action: echo
        exposed_to_llm: true
---

Use this skill to echo note text.
""",
        encoding="utf-8",
    )
    (package / "skill.py").write_text(
        """
from app.skills.base import SkillResult


class NoteEchoSkill:
    name = "note_echo"

    def run(self, request):
        text = str(request.args.get("text", ""))
        return SkillResult(ok=True, exit_code=0, stdout=text, summary=text)
""",
        encoding="utf-8",
    )

    reset_registries_for_tests()
    try:
        registries = bootstrap_registries(external_paths=[skills_root], force=True)

        tool = registries.tool_registry.get("note_echo")

        assert tool.skill == "note_echo"
        assert tool.worker_type == "note_echo"
        assert tool.exposed_to_llm is True
    finally:
        reset_registries_for_tests()
