from langgraph.graph import END, START, StateGraph

from app.agent.nodes import (
    aggregate,
    blocked,
    contextualize,
    dispatch,
    ingest_event,
    monitor,
    route_after_aggregate,
    route_after_dispatch,
    route_after_monitor,
    route_after_strategize,
    route_after_wait_approval,
    strategize,
    summarize,
    wait_approval,
)
from app.agent.state import AgentState


def build_agent_graph(checkpointer=None):
    graph = StateGraph(AgentState)
    graph.add_node("ingest_event", ingest_event)
    graph.add_node("contextualize", contextualize)
    graph.add_node("strategize", strategize)
    graph.add_node("dispatch", dispatch)
    graph.add_node("monitor", monitor)
    graph.add_node("aggregate", aggregate)
    graph.add_node("wait_approval", wait_approval)
    graph.add_node("summarize", summarize)
    graph.add_node("blocked", blocked)

    graph.add_edge(START, "ingest_event")
    graph.add_edge("ingest_event", "contextualize")
    graph.add_edge("contextualize", "strategize")
    graph.add_conditional_edges(
        "strategize",
        route_after_strategize,
        {"dispatch": "dispatch", "blocked": "blocked"},
    )
    graph.add_conditional_edges(
        "dispatch",
        route_after_dispatch,
        {"monitor": "monitor", "wait_approval": "wait_approval", "blocked": "blocked"},
    )
    graph.add_conditional_edges(
        "monitor",
        route_after_monitor,
        {"aggregate": "aggregate", "monitor": "monitor", "blocked": "blocked"},
    )
    graph.add_conditional_edges(
        "aggregate",
        route_after_aggregate,
        {
            "summarize": "summarize",
            "blocked": "blocked",
            "strategize": "strategize",
            "dispatch": "dispatch",
        },
    )
    graph.add_conditional_edges(
        "wait_approval",
        route_after_wait_approval,
        {"dispatch": "dispatch", "blocked": "blocked", "summarize": "summarize"},
    )
    graph.add_edge("summarize", END)
    graph.add_edge("blocked", END)
    return graph.compile(checkpointer=checkpointer)
