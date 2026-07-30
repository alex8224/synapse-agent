"""Unit tests for build_compact_tool_descriptions middleware."""

from __future__ import annotations

from unittest.mock import MagicMock

from langchain_core.tools import StructuredTool

from synapse.runtime.middleware import (
    _compact_base_tool,
    _compact_dict_tool,
    _is_tool_dict,
    build_compact_tool_descriptions,
)


def _make_tool(name: str, desc: str) -> StructuredTool:
    return StructuredTool.from_function(name=name, description=desc, func=lambda x: x)


# ── _is_tool_dict ────────────────────────────────────────────


def test_is_tool_dict_function_format() -> None:
    assert _is_tool_dict({"type": "function", "function": {"name": "x"}})


def test_is_tool_dict_flat_name() -> None:
    assert _is_tool_dict({"name": "x"})


def test_is_tool_dict_rejects_plain_str() -> None:
    assert not _is_tool_dict("not a tool")


def test_is_tool_dict_rejects_empty() -> None:
    assert not _is_tool_dict({})


# ── _compact_base_tool ───────────────────────────────────────


def test_compact_base_tool_replaces_description() -> None:
    tool = _make_tool("write_todos", "A" * 1000)
    name, new, saved = _compact_base_tool(tool, {"write_todos": "B" * 10})
    assert name == "write_todos"
    assert new is not None
    assert new.description == "B" * 10
    assert saved > 0
    assert new is not tool  # returns a new instance


def test_compact_base_tool_skips_unknown() -> None:
    tool = _make_tool("unknown", "Keep me")
    name, new, saved = _compact_base_tool(tool, {"write_todos": "Short"})
    assert name == "unknown"
    assert new is None
    assert saved == 0


def test_compact_base_tool_skips_already_short() -> None:
    tool = _make_tool("write_todos", "Already short")
    name, new, saved = _compact_base_tool(tool, {"write_todos": "Already short"})
    assert name == "write_todos"
    assert new is None
    assert saved == 0


# ── _compact_dict_tool ───────────────────────────────────────


def test_compact_dict_tool_flat() -> None:
    d = {"name": "write_todos", "description": "X" * 500}
    name, new, saved = _compact_dict_tool(d, {"write_todos": "Y" * 5})
    assert name == "write_todos"
    assert new is not None
    assert new["description"] == "Y" * 5
    assert saved > 0


def test_compact_dict_tool_openai_format() -> None:
    d = {"type": "function", "function": {"name": "write_todos", "description": "L" * 333}}
    name, new, saved = _compact_dict_tool(d, {"write_todos": "S" * 5})
    assert name == "write_todos"
    assert new is not None
    assert new["function"]["description"] == "S" * 5
    assert saved > 0
    # type key preserved
    assert new["type"] == "function"


def test_compact_dict_tool_skips_unknown() -> None:
    d = {"name": "other", "description": "X" * 100}
    name, new, saved = _compact_dict_tool(d, {"write_todos": "Short"})
    assert name == "other"
    assert new is None


# ── build_compact_tool_descriptions (integration) ────────────


def _make_request(tools: list) -> MagicMock:
    req = MagicMock()
    req.tools = tools
    # request.override(tools=new_tools) must return a mock whose .tools
    # yields the *actual* new_tools list, because the middleware reads
    # result.tools after calling override.
    def _override(**overrides):
        new_req = MagicMock()
        # Copy attributes from original, then apply overrides
        for k, v in overrides.items():
            setattr(new_req, k, v)
        return new_req

    req.override = _override
    return req


def test_middleware_no_tools_passthrough() -> None:
    mw = build_compact_tool_descriptions()
    req = _make_request([])
    result = mw.wrap_model_call(req, lambda r: r)
    assert result is req


def test_middleware_unknown_tool_passthrough() -> None:
    mw = build_compact_tool_descriptions()
    tool = _make_tool("other_tool", "Some description")
    req = _make_request([tool])
    result = mw.wrap_model_call(req, lambda r: r)
    assert result.tools[0] is tool  # same object, no copy


def test_middleware_replaces_write_todos() -> None:
    mw = build_compact_tool_descriptions()
    tool = _make_tool("write_todos", "VERY LONG " * 200)
    req = _make_request([tool])
    result = mw.wrap_model_call(req, lambda r: r)

    new_tool = result.tools[0]
    assert new_tool is not tool
    assert len(new_tool.description) < 1000
    assert "pending" in new_tool.description.lower()
    assert "in_progress" in new_tool.description.lower()


def test_middleware_compacts_native_grep_description() -> None:
    mw = build_compact_tool_descriptions()
    tool = _make_tool("grep", "Searches for literal text, not regex." * 100)
    req = _make_request([tool])
    result = mw.wrap_model_call(req, lambda r: r)

    new_tool = result.tools[0]
    assert new_tool is not tool
    assert "regular expression" in new_tool.description
    assert "TODO|FIXME" in new_tool.description
    assert "glob='**/*.py'" in new_tool.description


def test_middleware_compacts_native_glob_description() -> None:
    mw = build_compact_tool_descriptions()
    tool = _make_tool("glob", "Find files using glob patterns." * 100)
    req = _make_request([tool])
    result = mw.wrap_model_call(req, lambda r: r)

    new_tool = result.tools[0]
    assert new_tool is not tool
    assert "**/*.py" in new_tool.description
    assert "src/**/*.ts" in new_tool.description


def test_middleware_dict_tools() -> None:
    mw = build_compact_tool_descriptions()
    req = _make_request([
        {"name": "write_todos", "description": "X" * 2000, "parameters": {}},
        {"name": "grep", "description": "Searches for literal text, not regex."},
        {"name": "glob", "description": "Find files using glob patterns."},
    ])
    result = mw.wrap_model_call(req, lambda r: r)

    assert len(result.tools[0]["description"]) < 500
    assert "TODO|FIXME" in result.tools[1]["description"]
    assert "glob='**/*.py'" in result.tools[1]["description"]
    assert "src/**/*.ts" in result.tools[2]["description"]


def test_middleware_custom_overrides() -> None:
    mw = build_compact_tool_descriptions(overrides={
        "write_todos": "Custom short desc",
        "my_tool": "Also custom",
    })
    tool = _make_tool("write_todos", "LONG " * 200)
    req = _make_request([tool])
    result = mw.wrap_model_call(req, lambda r: r)
    assert result.tools[0].description == "Custom short desc"


def test_middleware_empty_overrides_noop() -> None:
    mw = build_compact_tool_descriptions(overrides={
        "write_todos": "Short",
    })
    # Remove the built-in one implicitly? Actually built-ins + overrides merge.
    # So write_todos should still be covered.
    tool = _make_tool("write_todos", "LONG " * 200)
    req = _make_request([tool])
    result = mw.wrap_model_call(req, lambda r: r)
    # Custom overrides take precedence
    assert result.tools[0].description == "Short"


def test_middleware_compacts_execute() -> None:
    mw = build_compact_tool_descriptions()
    tool = _make_tool("execute", "EXECUTE SHELL COMMANDS " * 100)
    req = _make_request([tool])
    result = mw.wrap_model_call(req, lambda r: r)
    new_tool = result.tools[0]
    assert new_tool is not tool
    assert "sandbox" in new_tool.description.lower()
    assert "stdout" in new_tool.description.lower()
    assert len(new_tool.description) < 500


def test_middleware_compacts_read_file() -> None:
    mw = build_compact_tool_descriptions()
    tool = _make_tool("read_file", "READ A FILE " * 100)
    req = _make_request([tool])
    result = mw.wrap_model_call(req, lambda r: r)
    new_tool = result.tools[0]
    assert new_tool is not tool
    assert "pagination" in new_tool.description.lower()
    assert len(new_tool.description) < 500
