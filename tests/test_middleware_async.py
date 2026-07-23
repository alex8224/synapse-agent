"""Custom middleware must expose both sync and async wrap hooks for astream."""

from __future__ import annotations

from pathlib import Path

from langchain.agents.middleware.types import AgentMiddleware

from synapse.middleware import (
    build_intent_schema_middleware,
    build_path_normalize_middleware,
    build_task_namespace_middleware,
    build_tool_exclusion_middleware,
)
from synapse.steer import SteerQueue, build_steer_middleware


def _assert_dual_model(m: AgentMiddleware) -> None:
    cls = type(m)
    assert cls.wrap_model_call is not AgentMiddleware.wrap_model_call
    assert cls.awrap_model_call is not AgentMiddleware.awrap_model_call


def _assert_dual_tool(m: AgentMiddleware) -> None:
    cls = type(m)
    assert cls.wrap_tool_call is not AgentMiddleware.wrap_tool_call
    assert cls.awrap_tool_call is not AgentMiddleware.awrap_tool_call


def test_exclusion_middleware_has_async_wrap():
    m = build_tool_exclusion_middleware(["execute"])
    _assert_dual_model(m)

    class T:
        name = "execute"

    class T2:
        name = "read_file"

    seen: dict[str, list[str]] = {}

    def handler(r):  # noqa: ANN001
        seen["tools"] = [t.name for t in r.tools]
        return "ok"

    class Req:
        def __init__(self, tools):
            self.tools = tools

        def override(self, **kwargs):
            return Req(kwargs.get("tools", self.tools))

    out = m.wrap_model_call(Req([T(), T2()]), handler)
    assert out == "ok"
    assert seen["tools"] == ["read_file"]


def test_intent_and_path_middleware_have_async_wrap():
    inject, strip = build_intent_schema_middleware()
    _assert_dual_model(inject)
    _assert_dual_tool(strip)
    _assert_dual_tool(build_path_normalize_middleware(Path(".")))


def test_task_namespace_middleware_scopes_sync_handler():
    from types import SimpleNamespace

    from langchain_core.runnables.config import var_child_runnable_config

    middleware = build_task_namespace_middleware()
    _assert_dual_tool(middleware)
    request = SimpleNamespace(
        tool_call={"name": "task", "id": "call-a", "args": {}},
        runtime=SimpleNamespace(config={"configurable": {"checkpoint_ns": "tools:root"}}),
    )

    def handler(_request):  # noqa: ANN001
        return var_child_runnable_config.get()["configurable"]["checkpoint_ns"]

    assert middleware.wrap_tool_call(request, handler) == "tools:root|task_call:call-a"


def test_steer_middleware_has_async_before_model():
    mw = build_steer_middleware(SteerQueue())
    cls = type(mw)
    assert cls.before_model is not AgentMiddleware.before_model
    assert cls.abefore_model is not AgentMiddleware.abefore_model
