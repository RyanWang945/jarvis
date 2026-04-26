from langgraph.graph import END, START, StateGraph

from app.agent.nodes import (
    aggregate,
    blocked,
    classify_intent,
    clarify,
    contextualize,
    dispatch,
    ingest_event,
    monitor,
    risk_gate,
    strategize,
    summarize,
    verify,
    wait_approval,
)
from app.agent.state import AgentState


def build_agent_graph(checkpointer=None):
    graph = StateGraph(AgentState)
    graph.add_node("ingest_event", ingest_event)
    graph.add_node("contextualize", contextualize)
    graph.add_node("classify_intent", classify_intent)
    graph.add_node("strategize", strategize, destinations=("risk_gate", "clarify", "blocked"))
    graph.add_node("clarify", clarify, destinations=("contextualize", "blocked"))
    graph.add_node("risk_gate", risk_gate, destinations=("dispatch", "wait_approval", "blocked"))
    graph.add_node("dispatch", dispatch, destinations=("monitor", "blocked"))
    graph.add_node("monitor", monitor, destinations=("aggregate", "monitor"))
    graph.add_node("aggregate", aggregate, destinations=("verify", "blocked", "risk_gate"))
    graph.add_node("verify", verify, destinations=("summarize", "blocked", "strategize", "risk_gate"))
    graph.add_node("wait_approval", wait_approval, destinations=("risk_gate", "blocked", "summarize"))
    graph.add_node("summarize", summarize)
    graph.add_node("blocked", blocked)

    graph.add_edge(START, "ingest_event")
    graph.add_edge("ingest_event", "contextualize")
    graph.add_edge("contextualize", "classify_intent")
    graph.add_edge("classify_intent", "strategize")
    graph.add_edge("summarize", END)
    graph.add_edge("blocked", END)
    return graph.compile(checkpointer=checkpointer)
