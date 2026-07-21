"""Tests for cancel repair: keep checkpoint continuous after ESC."""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from synapse.cancel_repair import repair_thread_after_cancel


class _S(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


@tool
def echo(x: str) -> str:
    """echo"""
    return f"ok:{x}"


def _build_app():
    def agent_node(state: _S):
        msgs = state["messages"]
        if msgs and isinstance(msgs[-1], ToolMessage):
            return {"messages": [AIMessage(content="done")]}
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "c1",
                            "name": "echo",
                            "args": {"x": "1"},
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        }

    def route(state: _S):
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        return END

    g = StateGraph(_S)
    g.add_node("model", agent_node)
    g.add_node("tools", ToolNode([echo]))
    g.add_edge(START, "model")
    g.add_conditional_edges("model", route, {"tools": "tools", END: END})
    g.add_edge("tools", "model")
    return g.compile(checkpointer=MemorySaver())


def test_repair_seals_open_tool_calls_and_keeps_prior_context():
    app = _build_app()
    cfg = {"configurable": {"thread_id": "repair-1"}}

    # Prior completed turn
    app.update_state(
        cfg,
        {
            "messages": [
                HumanMessage(content="old question"),
                AIMessage(content="old answer"),
            ]
        },
        as_node="model",
    )
    # Cancelled mid-tool turn
    app.update_state(
        cfg,
        {
            "messages": [
                HumanMessage(content="new question"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "c1",
                            "name": "echo",
                            "args": {"x": "1"},
                            "type": "tool_call",
                        }
                    ],
                ),
            ]
        },
        as_node="model",
    )
    st = app.get_state(cfg)
    assert st.next == ("tools",)

    notes = repair_thread_after_cancel(app, cfg)
    assert notes
    st2 = app.get_state(cfg)
    msgs = st2.values["messages"]
    types = [type(m).__name__ for m in msgs]
    assert "HumanMessage" in types
    assert any(isinstance(m, ToolMessage) for m in msgs)
    # prior context still present
    assert any(getattr(m, "content", None) == "old question" for m in msgs)
    assert any(getattr(m, "content", None) == "old answer" for m in msgs)
    # graph not stuck on tools
    assert st2.next == ()

    # Next turn must work on same thread
    out = app.invoke({"messages": [HumanMessage(content="continue please")]}, cfg)
    assert out["messages"]
    assert any(getattr(m, "content", None) == "continue please" for m in out["messages"])
    assert any(getattr(m, "content", None) == "old answer" for m in out["messages"])
