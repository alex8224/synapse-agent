"""Tool-policy guard: same-source request filtering + execution rejection.

``build_tool_exclusion_middleware`` hides denied tools from the model request
*and* rejects a forged/forced call before the real tool can execute. Both
layers share one denial predicate, so a blocked (or non-whitelisted) tool can
never run — even when the model emits a tool call for it anyway.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from synapse.runtime.middleware import build_tool_exclusion_middleware


class _NamedTool:
    """Minimal model-facing tool stand-in (only ``name`` matters here)."""

    def __init__(self, name: str) -> None:
        self.name = name


class _ModelRequest:
    def __init__(self, tools: list[Any]) -> None:
        self.tools = list(tools)

    def override(self, **changes: Any) -> _ModelRequest:
        return _ModelRequest(changes.get("tools", self.tools))


class _ToolRequest:
    def __init__(self, tool_call: dict[str, Any]) -> None:
        self.tool_call = tool_call


def _names(request: _ModelRequest) -> list[str]:
    return [t.name for t in request.tools]


def _call(middleware: Any, name: str, call_id: str = "call-1") -> tuple[Any, list[Any]]:
    """Run a forced sync tool call; return (result, handler-invocations)."""
    request = _ToolRequest({"name": name, "args": {}, "id": call_id, "type": "tool_call"})
    ran: list[Any] = []

    def handler(req: Any) -> ToolMessage:
        ran.append(req)
        return ToolMessage(content="ran", tool_call_id=call_id, name=name)

    return middleware.wrap_tool_call(request, handler), ran


def _acall(middleware: Any, name: str, call_id: str = "call-1") -> tuple[Any, list[Any]]:
    """Async twin of :func:`_call`."""
    request = _ToolRequest({"name": name, "args": {}, "id": call_id, "type": "tool_call"})
    ran: list[Any] = []

    async def handler(req: Any) -> ToolMessage:
        ran.append(req)
        return ToolMessage(content="ran", tool_call_id=call_id, name=name)

    return asyncio.run(middleware.awrap_tool_call(request, handler)), ran


def test_middleware_exposes_all_four_hooks():
    m = build_tool_exclusion_middleware(["execute"])
    assert isinstance(m, AgentMiddleware)
    cls = type(m)
    assert cls.wrap_model_call is not AgentMiddleware.wrap_model_call
    assert cls.awrap_model_call is not AgentMiddleware.awrap_model_call
    assert cls.wrap_tool_call is not AgentMiddleware.wrap_tool_call
    assert cls.awrap_tool_call is not AgentMiddleware.awrap_tool_call


# --------------------------------------------------------------------------- #
# request.tools filtering (sync + async)
# --------------------------------------------------------------------------- #
def test_sync_filter_hides_blocked_tool():
    m = build_tool_exclusion_middleware(["execute"])
    request = _ModelRequest([_NamedTool("execute"), _NamedTool("read_file")])
    out = m.wrap_model_call(request, lambda r: r)
    assert _names(out) == ["read_file"]


def test_dict_tool_formats_share_the_same_name_policy() -> None:
    m = build_tool_exclusion_middleware(["execute"])
    allowed = {"type": "function", "function": {"name": "read_file"}}
    request = _ModelRequest([
        {"name": "execute"},
        {"type": "function", "function": {"name": "execute"}},
        allowed,
    ])
    assert m.wrap_model_call(request, lambda r: r).tools == [allowed]


def test_async_filter_hides_blocked_tool():
    m = build_tool_exclusion_middleware(["execute"])
    request = _ModelRequest([_NamedTool("execute"), _NamedTool("read_file")])

    async def handler(r: Any) -> Any:
        return r

    out = asyncio.run(m.awrap_model_call(request, handler))
    assert _names(out) == ["read_file"]


def test_filter_leaves_request_untouched_when_nothing_denied():
    m = build_tool_exclusion_middleware(["execute"])
    request = _ModelRequest([_NamedTool("read_file"), _NamedTool("write_file")])
    assert m.wrap_model_call(request, lambda r: r) is request


# --------------------------------------------------------------------------- #
# Forced call rejection (handler must never run)
# --------------------------------------------------------------------------- #
def test_sync_forced_blocked_call_never_runs_handler():
    m = build_tool_exclusion_middleware(["execute"])
    result, ran = _call(m, "execute")
    assert ran == []  # handler / real tool never executed
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.tool_call_id == "call-1"
    assert result.name == "execute"
    text = result.content.lower()
    assert "permission" in text
    assert "do not retry" in text


def test_async_forced_blocked_call_never_runs_handler():
    m = build_tool_exclusion_middleware(["execute"])
    result, ran = _acall(m, "execute")
    assert ran == []
    assert result.status == "error"
    assert result.tool_call_id == "call-1"
    assert result.name == "execute"
    assert "permission" in result.content.lower()


# --------------------------------------------------------------------------- #
# allow-list semantics
# --------------------------------------------------------------------------- #
def test_whitelist_filters_and_rejects_non_members():
    m = build_tool_exclusion_middleware([], allowed_tools=["read_file"])
    request = _ModelRequest([_NamedTool("read_file"), _NamedTool("write_file")])
    assert _names(m.wrap_model_call(request, lambda r: r)) == ["read_file"]

    allowed_result, allowed_ran = _call(m, "read_file")
    assert len(allowed_ran) == 1  # allowed tool runs
    assert allowed_result.content == "ran"

    denied_result, denied_ran = _call(m, "write_file")
    assert denied_ran == []
    assert denied_result.status == "error"
    assert "permission" in denied_result.content.lower()


def test_blocked_wins_over_whitelist():
    # "execute" is both blocked and whitelisted -> blocked priority wins.
    m = build_tool_exclusion_middleware(["execute"], allowed_tools=["execute", "read_file"])
    request = _ModelRequest([_NamedTool("execute"), _NamedTool("read_file")])
    assert _names(m.wrap_model_call(request, lambda r: r)) == ["read_file"]

    result, ran = _call(m, "execute")
    assert ran == []
    assert result.status == "error"


def test_unknown_tool_allowed_without_whitelist():
    m = build_tool_exclusion_middleware(["execute"])
    result, ran = _call(m, "mystery")
    assert len(ran) == 1  # no whitelist -> unknown tool is not denied
    assert result.content == "ran"


def test_unknown_tool_denied_with_whitelist():
    m = build_tool_exclusion_middleware([], allowed_tools=["read_file"])
    result, ran = _call(m, "mystery")
    assert ran == []
    assert result.status == "error"
    assert result.name == "mystery"


def test_empty_whitelist_denies_everything():
    m = build_tool_exclusion_middleware([], allowed_tools=[])
    request = _ModelRequest([_NamedTool("read_file"), _NamedTool("execute")])
    assert _names(m.wrap_model_call(request, lambda r: r)) == []
    for name in ("read_file", "execute"):
        result, ran = _call(m, name)
        assert ran == []
        assert result.status == "error"


def test_legacy_single_argument_call_keeps_compat():
    # Old call shape (positional excluded only) still works and keeps its name.
    m = build_tool_exclusion_middleware(frozenset({"execute"}))
    assert type(m).__name__ == "exclude_tools"
    request = _ModelRequest([_NamedTool("execute"), _NamedTool("read_file")])
    assert _names(m.wrap_model_call(request, lambda r: r)) == ["read_file"]


# --------------------------------------------------------------------------- #
# Real graph integration: a forged call to a disabled tool must not reach the
# tool body (spy), and the guard returns a ToolMessage error instead.
# --------------------------------------------------------------------------- #
class _ToolBindableFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> _ToolBindableFakeModel:  # noqa: ARG002
        return self


def _forced_call_model() -> _ToolBindableFakeModel:
    return _ToolBindableFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "execute",
                        "args": {"command": "echo policy-probe"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )


def _build_guarded_agent() -> tuple[Any, dict[str, int]]:
    calls = {"count": 0}

    @tool
    def execute(command: str) -> str:
        """Run a shell command in the sandbox."""
        calls["count"] += 1
        return "ran"

    @tool
    def read_file(path: str) -> str:
        """Read a file from the workspace."""
        return "contents"

    agent = create_agent(
        model=_forced_call_model(),
        tools=[execute, read_file],
        middleware=[build_tool_exclusion_middleware(["execute"])],
    )
    return agent, calls


def test_integration_forced_disabled_tool_never_reaches_spy():
    agent, calls = _build_guarded_agent()
    result = agent.invoke({"messages": [HumanMessage(content="do it")]})
    assert calls["count"] == 0  # spy never executed

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    denied = tool_messages[0]
    assert denied.status == "error"
    assert denied.tool_call_id == "call-1"
    assert denied.name == "execute"
    assert "permission" in denied.content.lower()
    # The graph keeps running after the refusal (final model turn is reached).
    assert result["messages"][-1].content == "done"


def test_integration_async_forced_disabled_tool_never_reaches_spy():
    agent, calls = _build_guarded_agent()
    result = asyncio.run(agent.ainvoke({"messages": [HumanMessage(content="do it")]}))
    assert calls["count"] == 0

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].status == "error"
    assert tool_messages[0].tool_call_id == "call-1"
