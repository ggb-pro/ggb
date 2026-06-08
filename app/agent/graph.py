"""LangGraph state graph: wire nodes into an executable graph."""

import logging

from langgraph.graph import StateGraph, END

from app.agent.state import AgentState
from app.agent.nodes import (
    intent_classify,
    generate_plan,
    execute_tools,
    generate_answer,
    reflect,
    adjust_params,
)
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _route_after_classify(state: AgentState) -> str:
    """Route based on intent: simple → end, complex → plan."""
    if state.get("intent") == "simple":
        return "end"
    return "complex"


def _route_after_reflect(state: AgentState) -> str:
    """Route based on reflection: pass → end, fail & retries left → retry.

    agent_max_retries=2 means 3 retry attempts after first try (4 total).
    +1 allows the level-3 strategy (original query full search) to execute.
    """
    max_retries = getattr(settings, 'agent_max_retries', 2)
    if state.get("should_retry") and state.get("retry_count", 0) <= max_retries + 1:
        return "retry"
    return "end"


def build_agent_graph():
    """Build the LangGraph agent state graph."""
    builder = StateGraph(AgentState)

    # Add nodes
    builder.add_node("intent_classify", intent_classify)
    builder.add_node("generate_plan", generate_plan)
    builder.add_node("execute_tools", execute_tools)
    builder.add_node("generate_answer", generate_answer)
    builder.add_node("reflect", reflect)
    builder.add_node("adjust_params", adjust_params)

    # Set entry point
    builder.set_entry_point("intent_classify")

    # Conditional: after classify, simple → END, complex → plan
    builder.add_conditional_edges(
        "intent_classify",
        _route_after_classify,
        {"end": END, "complex": "generate_plan"},
    )

    # Linear: plan → execute → generate → reflect
    builder.add_edge("generate_plan", "execute_tools")
    builder.add_edge("execute_tools", "generate_answer")
    builder.add_edge("generate_answer", "reflect")

    # Conditional: after reflect, pass → END, fail → adjust → execute (retry loop)
    builder.add_conditional_edges(
        "reflect",
        _route_after_reflect,
        {"retry": "adjust_params", "end": END},
    )
    builder.add_edge("adjust_params", "execute_tools")

    return builder.compile()


# Lazy singleton — graph is built once on first access
_graph = None


def get_agent_graph():
    global _graph
    if _graph is None:
        _graph = build_agent_graph()
        logger.info("Agent graph built successfully")
    return _graph
