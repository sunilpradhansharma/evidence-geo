"""Compile the copilot LangGraph.

    START -> router -> {orchestrator <-> tool_executor | analyst} -> validator -> END

Nodes are a mix of sync (router/tool_executor/validator) and async
(orchestrator/analyst); LangGraph runs both under ``ainvoke``/``astream``.
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.config.settings import get_settings
from app.copilot.nodes.analyst import make_analyst_node
from app.copilot.nodes.orchestrator import (
    make_orchestrator_node,
    route_after_orchestrator,
)
from app.copilot.nodes.router import route_after_router, router_node
from app.copilot.nodes.tool_executor import (
    route_after_tool_executor,
    tool_executor_node,
)
from app.copilot.nodes.validator import validator_node
from app.copilot.providers.base import LLMProvider
from app.copilot.state import AgentState


def build_graph(provider: LLMProvider):
    cap = int(get_settings().copilot_react_iter_cap)

    g = StateGraph(AgentState)
    g.add_node("router", router_node)
    g.add_node("orchestrator", make_orchestrator_node(provider))
    g.add_node("tool_executor", tool_executor_node)
    g.add_node("analyst", make_analyst_node(provider))
    g.add_node("validator", validator_node)

    g.add_edge(START, "router")
    g.add_conditional_edges(
        "router",
        route_after_router,
        {"orchestrator": "orchestrator", "analyst": "analyst"},
    )
    g.add_conditional_edges(
        "orchestrator",
        lambda s: route_after_orchestrator(s, react_iter_cap=cap),
        {"tool_executor": "tool_executor", "validator": "validator"},
    )
    g.add_conditional_edges(
        "tool_executor",
        lambda s: route_after_tool_executor(s, react_iter_cap=cap),
        {"orchestrator": "orchestrator", "validator": "validator"},
    )
    g.add_edge("analyst", "validator")
    g.add_edge("validator", END)
    return g.compile()
