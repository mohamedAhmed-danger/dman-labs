from langgraph.graph import END, START, StateGraph

from graph.nodes.booking_node import booking_node
from graph.nodes.complaint_node import complaint_node
from graph.nodes.direct_node import direct_node
from graph.nodes.inquiry_node import inquiry_node
from graph.nodes.intent_node import intent_node
from graph.nodes.rag_node import rag_node
from graph.nodes.result_node import result_node
from graph.nodes.region_guard_node import region_guard_node
from graph.state import AgentState

import utils.trace_logger as trace_logger


__agent_graph = None


def route_intent(state: AgentState) -> str:
    """Return the current intent string used for graph routing."""

    intent = state.get("intent") or "direct"

    # تحويل الـ Enum لـ string صريح لو كان Enum object
    if hasattr(intent, "value"):
        intent_str = str(intent.value)
    else:
        intent_str = str(intent)

    trace_logger.step(
        "ROUTING",
        f"GRAPH CONDITIONAL ROUTING -> {intent_str.upper()}",
        input_data={"intent": intent_str},
        output_data={"target_branch": intent_str},
    )

    return intent_str


def route_after_region_guard(state: AgentState) -> str:
    return state.get("region_guard_action") or "stop"


def route_agent_entry(state: AgentState) -> str:
    return "intent" if state.get("city_id") else "region_guard"


def build_graph():
    graph = StateGraph(AgentState)

    # Nodes
    graph.add_node("intent", intent_node)
    graph.add_node("rag", rag_node)
    graph.add_node("booking", booking_node)
    graph.add_node("complaint", complaint_node)
    graph.add_node("inquiry", inquiry_node)
    graph.add_node("direct", direct_node)
    graph.add_node("labresults", result_node)
    graph.add_node("region_guard", region_guard_node)

    graph.add_conditional_edges(
        START,
        route_agent_entry,
        {
            "intent": "intent",
            "region_guard": "region_guard",
        },
    )

    graph.add_conditional_edges(
        "region_guard",
        route_after_region_guard,
        {
            "stop": END,
            "classify": "intent",
        },
    )

    graph.add_conditional_edges(
        "intent",
        route_intent,
        {
            "visit": "rag",
            "inquiry": "rag",
            "complaint": "complaint",
            "direct": "direct",
            "labresults": "labresults",
        },
    )

    graph.add_conditional_edges(
        "rag",
        route_intent,
        {
            "visit": "booking",
            "inquiry": "inquiry",
        },
    )

    # Workflows → End
    graph.add_edge("booking", END)
    graph.add_edge("complaint", END)
    graph.add_edge("inquiry", END)
    graph.add_edge("direct", END)
    graph.add_edge("labresults", END)

    return graph.compile()


def get_agent_graph():
    global __agent_graph

    if __agent_graph is None:
        __agent_graph = build_graph()

    return __agent_graph