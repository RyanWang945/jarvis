from app.tools.runtime import build_llm_tools


def test_x_search_description_marks_x_search_as_specialized_without_blocking_tavily() -> None:
    tools = {
        tool["function"]["name"]: tool["function"]["description"]
        for tool in build_llm_tools(allowed_tools={"tavily_search", "x_search"})
    }

    assert "最新 twitter" in tools["x_search"]
    assert "马斯克的最新twitter" in tools["x_search"]
    assert "该专用工具" in tools["x_search"]
    assert "已索引的 X/Twitter 页面" in tools["tavily_search"]
    assert "优先使用专用 x_search 工具" in tools["tavily_search"]
