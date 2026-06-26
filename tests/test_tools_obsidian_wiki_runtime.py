import json
import shutil
from pathlib import Path
from uuid import uuid4

from app.llm.provider_adapters import NormalizedLLMResponse, NormalizedToolCall
from app.task_runtime.node_execute_runtime import NodeExecutionContext, ReactNodeExecuteRuntime
from app.task_runtime.planner import PlanNode


def _unique_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


class FakeProfile:
    api_key = "test-key"
    id = "test"
    supports_json_object = True


class FakeResolvedModel:
    def __init__(self, client) -> None:
        self.client = client
        self.profile = FakeProfile()


class ObsidianWikiChat:
    def __init__(self, *, vault_path: str) -> None:
        self.vault_path = vault_path
        self.calls = []

    def chat_normalized(self, messages, **kwargs):
        self.calls.append({"messages": list(messages), **kwargs})
        tool_messages = [m for m in messages if m.role == "tool"]
        if len(tool_messages) == 0:
            return _response(
                tool_calls=(
                    NormalizedToolCall(
                        id="call_wiki_draft_1",
                        name="obsidian_wiki_draft",
                        args={
                            "vault_path": self.vault_path,
                            "title": "Runtime Draft",
                            "page_type": "design",
                            "content": "This page was created through the DAG React runtime integration test.",
                            "source_ids": [],
                        },
                    ),
                )
            )
        if len(tool_messages) == 1:
            observation = json.loads(str(tool_messages[-1].content))
            payload = json.loads(observation["stdout"])
            return _response(
                tool_calls=(
                    NormalizedToolCall(
                        id="call_wiki_apply_1",
                        name="obsidian_wiki_apply",
                        args={
                            "vault_path": self.vault_path,
                            "draft_id": payload["draft_id"],
                        },
                    ),
                )
            )
        return _response('{"summary":"wiki-applied"}')


def _response(content: str = "", *, tool_calls: tuple[NormalizedToolCall, ...] = ()) -> NormalizedLLMResponse:
    return NormalizedLLMResponse(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=None,
        usage=None,
        model="fake",
        finish_reason=None,
        raw={},
    )


def test_obsidian_wiki_tools_run_inside_react_runtime() -> None:
    vault_root = Path("sandbox") / _unique_id("obsidian-wiki-runtime")
    vault_path = vault_root / "JarvisWiki"
    chat = ObsidianWikiChat(vault_path=str(vault_path))
    runtime = ReactNodeExecuteRuntime(
        model_resolver=lambda context: FakeResolvedModel(chat),
        max_steps=4,
    )

    try:
        result = runtime.run(
            NodeExecutionContext(
                user_objective="Please write this design note into the wiki",
                node=PlanNode(id="wiki", runtime="react", objective="Draft and apply an Obsidian wiki page."),
            )
        )

        assert result.status == "completed"
        assert result.summary == "wiki-applied"

        tool_calls = result.tool_calls
        assert [tool_call["tool_name"] for tool_call in tool_calls] == ["obsidian_wiki_draft", "obsidian_wiki_apply"]
        assert all(tool_call["status"] == "completed" for tool_call in tool_calls)
        assert (vault_path / "vault" / "projects" / "jarvis" / "designs" / "runtime-draft.md").exists()
    finally:
        shutil.rmtree(vault_root, ignore_errors=True)
