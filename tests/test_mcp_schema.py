"""MCP JSON-schema → pydantic conversion (avoid root anyOf collapse)."""

from __future__ import annotations

from synapse.mcp_client import _make_tool, json_schema_to_pydantic_model
from synapse.middleware import TOOL_INTENT_KEY, add_intent_to_tool


class _Server:
    name = "anysearch"
    tool_prefix = "anysearch__"


def test_json_schema_to_pydantic_exposes_real_properties():
    model = json_schema_to_pydantic_model(
        "batch_search",
        {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                }
            },
            "required": ["queries"],
        },
    )
    fields = model.model_fields
    assert "queries" in fields
    assert "root" not in fields
    schema = model.model_json_schema()
    assert "properties" in schema
    assert "queries" in schema["properties"]
    assert "anyOf" not in schema or "queries" in schema.get("properties", {})


def test_make_tool_schema_not_root_anyof():
    calls: list[tuple[str, dict]] = []

    def call_fn(name: str, arguments: dict) -> str:
        calls.append((name, arguments))
        return "ok"

    tool = _make_tool(
        server=_Server(),  # type: ignore[arg-type]
        tool_name="batch_search",
        description="batch",
        input_schema={
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                }
            },
            "required": ["queries"],
        },
        call_fn=call_fn,
    )
    schema = tool.get_input_schema().model_json_schema()
    props = schema.get("properties") or {}
    assert "queries" in props
    assert "root" not in props
    # invoke with real args
    out = tool.invoke({"queries": [{"query": "今日新闻"}]})
    assert out == "ok"
    assert calls and calls[0][0] == "batch_search"
    assert calls[0][1] == {"queries": [{"query": "今日新闻"}]}


def test_intent_wrap_keeps_mcp_properties():
    tool = _make_tool(
        server=_Server(),  # type: ignore[arg-type]
        tool_name="search",
        description="search",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "q"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        },
        call_fn=lambda n, a: "ok",
    )
    wrapped = add_intent_to_tool(tool)
    props = (wrapped.tool_call_schema.model_json_schema().get("properties") or {})
    assert TOOL_INTENT_KEY in props
    assert "query" in props
    assert "root" not in props
