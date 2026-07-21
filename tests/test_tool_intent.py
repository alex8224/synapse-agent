"""Tests for required tool intent schema middleware + timeline labels."""

from __future__ import annotations

import asyncio

from langchain_core.tools import tool

from synapse.middleware import (
    TOOL_INTENT_KEY,
    add_intent_to_tool,
    build_intent_schema_middleware,
    build_tool_error_recovery_middleware,
)
from synapse.ui.timeline import extract_intent, item_label


@tool
def _demo_read(file_path: str, offset: int = 0) -> str:
    """demo read"""
    return f"{file_path}:{offset}"


def test_add_intent_requires_intent_field():
    wrapped = add_intent_to_tool(_demo_read)
    schema = wrapped.get_input_schema().model_json_schema()
    assert TOOL_INTENT_KEY in schema.get("properties", {})
    assert TOOL_INTENT_KEY in (schema.get("required") or [])
    # Original required args remain required.
    assert "file_path" in (schema.get("required") or [])


def test_wrapped_tool_strips_intent_on_invoke():
    wrapped = add_intent_to_tool(_demo_read)
    out = wrapped.invoke(
        {
            "intent": "查看配置",
            "file_path": "/pyproject.toml",
            "offset": 0,
        }
    )
    assert out == "/pyproject.toml:0"


def test_item_label_prefers_intent():
    assert (
        item_label(
            "read_file",
            {"intent": "查看 pytest 配置", "file_path": "/pyproject.toml"},
        )
        == "查看 pytest 配置"
    )
    assert extract_intent({"intent": "  定位失败  "}) == "定位失败"


def test_item_label_fallback_without_intent():
    assert item_label("read_file", {"file_path": "/docs/README.md"}) == "Read README.md"
    assert item_label("execute", {"command": "pytest -q"}).startswith("Run ")


def test_intent_middleware_factory_returns_hooks():
    hooks = build_intent_schema_middleware()
    assert isinstance(hooks, list)
    assert len(hooks) == 2


def test_add_intent_handles_toolruntime_injected_field():
    """compact_conversation-style tools inject ToolRuntime (has BaseStore).

    Wrapping must not raise PydanticSchemaGenerationError.
    """
    from langchain_core.tools import StructuredTool
    from langgraph.prebuilt.tool_node import ToolRuntime
    from pydantic import BaseModel, ConfigDict, Field

    class CompactSchema(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        runtime: ToolRuntime = Field(description="runtime")

    def _compact(runtime: ToolRuntime) -> str:  # noqa: ARG001
        return "ok"

    tool = StructuredTool.from_function(
        func=_compact,
        name="compact_conversation",
        description="compact",
        args_schema=CompactSchema,
    )
    wrapped = add_intent_to_tool(tool)
    # Model-facing schema exposes intent, hides injected runtime.
    tcs = wrapped.tool_call_schema
    props = tcs.model_json_schema().get("properties", {})
    assert TOOL_INTENT_KEY in props
    assert "runtime" not in props


def test_tool_error_recovery_returns_error_message_and_keeps_graph_running():
    middleware = build_tool_error_recovery_middleware()

    class Request:
        tool_call = {"name": "edit", "id": "call-path"}

    def handler(request):  # noqa: ANN001, ARG001
        raise ValueError("Path outside root directory")

    result = middleware.wrap_tool_call(Request(), handler)
    assert result.status == "error"
    assert result.name == "edit"
    assert result.tool_call_id == "call-path"
    assert "outside root directory" in result.content


def test_tool_error_recovery_handles_async_tool_calls():
    middleware = build_tool_error_recovery_middleware()

    class Request:
        tool_call = {"name": "write", "id": "call-async-path"}

    async def handler(request):  # noqa: ANN001, ARG001
        raise ValueError("Path traversal not allowed")

    result = asyncio.run(middleware.awrap_tool_call(Request(), handler))
    assert result.status == "error"
    assert result.tool_call_id == "call-async-path"
    assert "Path traversal not allowed" in result.content