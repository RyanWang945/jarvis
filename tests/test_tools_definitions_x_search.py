from app.tools.runtime import build_llm_tools


def test_x_search_description_marks_x_search_as_specialized_without_blocking_tavily() -> None:
    tools = {
        tool["function"]["name"]: tool["function"]["description"]
        for tool in build_llm_tools(allowed_tools={"tavily_search", "x_search"})
    }

    assert "latest twitter" in tools["x_search"]
    assert "马斯克的最新twitter" in tools["x_search"]
    assert "specialized tool for direct X/Twitter search" in tools["x_search"]
    assert "It can also find indexed X/Twitter pages" in tools["tavily_search"]
    assert "prefer the specialized x_search tool" in tools["tavily_search"]
