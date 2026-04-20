from app.tools.specs import ToolSpec


class ToolRegistry:
    def __init__(self, tools: list[ToolSpec]) -> None:
        self._tools: dict[str, ToolSpec] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool: {tool.name}")
            self._tools[tool.name] = tool

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ValueError(f"unknown tool: {name}") from exc

    def list(self, *, exposed_to_llm: bool | None = None) -> list[ToolSpec]:
        tools = list(self._tools.values())
        if exposed_to_llm is None:
            return tools
        return [tool for tool in tools if tool.exposed_to_llm is exposed_to_llm]


def get_default_tool_registry() -> ToolRegistry:
    from app.skills.bootstrap import get_tool_registry

    return get_tool_registry()
