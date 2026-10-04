"""Tests for the PTC SDK document builder.

The SDK is what tells the model how to orchestrate tools from inside
``run_code``. These tests pin the contract that matters:

* runtime-injected parameters never leak into the model's SDK,
* each tool gets an exact JSON schema plus a typed ``async`` Python stub that is
  keyword-only and always compiles,
* exotic names / argument names fall back to ``**kwargs`` or ``tools.call``,
* the document teaches the worker's real surface (body of an async function,
  ``await``, ``tools.call``) and its call/concurrency/truncation policy,
* the total budget fails loudly instead of silently truncating a schema, and the
  compact fallback is itself bounded and points at the native definitions,
* ``data`` shapes come only from ``metadata['ptc_output_schema']`` (never guessed),
* output types are never guessed (every stub returns ``ToolEnvelope``).
"""

from __future__ import annotations

import re
from typing import Annotated

import pytest
from langchain_core.tools import StructuredTool, tool
from langgraph.prebuilt import InjectedState, ToolRuntime

from synapse.runtime.ptc import sdk


@tool
def read_file(path: str, limit: int = 10) -> str:
    """Read a file from the workspace."""
    return path


@tool
def search_files(query: str, state: Annotated[dict, InjectedState], runtime: ToolRuntime) -> str:
    """Search the workspace."""
    return query


def _code_block(prompt: str) -> str:
    """Extract the fenced ```python`` block from the SDK document."""
    match = re.search(r"```python\n(.*?)\n```", prompt, re.DOTALL)
    assert match is not None, "the SDK must contain a python code block"
    return match.group(1)


def test_collect_specs_extracts_exact_schema() -> None:
    specs = sdk.collect_tool_specs([read_file])
    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "read_file"
    schema = spec.input_schema
    assert schema["properties"]["path"]["type"] == "string"
    assert schema["properties"]["limit"]["type"] == "integer"
    assert set(schema["required"]) == {"path"}


def test_injected_args_are_stripped_from_schema() -> None:
    spec = sdk.extract_tool_spec(search_files)
    assert spec is not None
    assert set(spec.input_schema["properties"]) == {"query"}
    assert "state" in spec.injected_keys
    assert "runtime" in spec.injected_keys


def test_build_sdk_prompt_has_async_keyword_only_stub() -> None:
    prompt = sdk.build_sdk_prompt(sdk.collect_tool_specs([read_file]))
    assert "async def read_file(*, path: str, limit: int = 10) -> ToolEnvelope:" in prompt
    assert '"type":"object"' in prompt.replace(" ", "")
    assert "ToolEnvelope" in prompt
    assert "ToolCallError" in prompt


def test_build_sdk_prompt_teaches_the_worker_surface() -> None:
    prompt = sdk.build_sdk_prompt(sdk.collect_tool_specs([read_file]))
    # The code is the body of an async function with await/return and injected names.
    assert "body of an `async` function" in prompt
    assert "`asyncio`" in prompt
    assert "`json`" in prompt
    assert "must be awaited" in prompt
    assert "tools.call(" in prompt
    # Only print/return reach the model; everything else is discarded.
    assert "`print(...)`" in prompt
    assert "the value you `return`" in prompt
    # Truncation is explicit, never silent.
    assert "truncated" in prompt
    # The subprocess is not a security sandbox.
    assert "not" in prompt and "security sandbox" in prompt


def test_build_sdk_prompt_reports_call_and_concurrency_budget() -> None:
    prompt = sdk.build_sdk_prompt(sdk.collect_tool_specs([read_file]), max_calls=7, max_parallel=3)
    assert "at most 7 calls" in prompt
    assert "at most 3 running at once" in prompt


def test_build_sdk_prompt_never_guesses_output_types() -> None:
    prompt = sdk.build_sdk_prompt(sdk.collect_tool_specs([read_file]))
    # The return annotation is always the shared envelope, never a guessed type.
    assert "-> ToolEnvelope:" in prompt
    assert "-> str:" not in prompt


def test_sdk_code_block_compiles() -> None:
    prompt = sdk.build_sdk_prompt(sdk.collect_tool_specs([read_file, search_files]))
    compile(_code_block(prompt), "<sdk>", "exec")


def test_required_after_optional_is_keyword_only_and_compiles() -> None:
    spec = sdk.ToolSpec(
        name="odd",
        description="d",
        input_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}, "path": {"type": "string"}},
            "required": ["path"],
        },
    )
    prompt = sdk.build_sdk_prompt([spec])
    assert "async def odd(*, limit: int = 10, path: str) -> ToolEnvelope:" in prompt
    compile(_code_block(prompt), "<sdk>", "exec")


def test_unrepresentable_arg_name_falls_back_to_kwargs() -> None:
    spec = sdk.ToolSpec(
        name="weird_args",
        description="d",
        input_schema={
            "type": "object",
            "properties": {"max-results": {"type": "integer"}},
            "required": ["max-results"],
        },
    )
    prompt = sdk.build_sdk_prompt([spec])
    assert "async def weird_args(**kwargs) -> ToolEnvelope:" in prompt
    assert "max-results" in prompt
    compile(_code_block(prompt), "<sdk>", "exec")


def test_exotic_name_uses_generic_call() -> None:
    spec = sdk.ToolSpec(name="weird-name", description="odd", input_schema={"type": "object"})
    prompt = sdk.build_sdk_prompt([spec])
    assert "def weird-name" not in prompt
    assert "call('weird-name'" in prompt


def test_keyword_tool_name_is_not_a_function() -> None:
    spec = sdk.ToolSpec(name="import", description="odd", input_schema={"type": "object"})
    prompt = sdk.build_sdk_prompt([spec])
    assert "async def import" not in prompt
    assert "call('import'" in prompt


def test_dict_style_tool_is_supported() -> None:
    spec = sdk.extract_tool_spec(
        {
            "type": "function",
            "function": {
                "name": "mcp_thing",
                "description": "an mcp tool",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }
    )
    assert spec is not None
    assert spec.name == "mcp_thing"
    assert spec.input_schema["properties"]["q"]["type"] == "string"


def test_output_schema_is_read_from_tool_metadata() -> None:
    schema = {"type": "object", "properties": {"matches": {"type": "array"}}}
    tool_obj = StructuredTool.from_function(
        func=lambda path: path,
        name="finder",
        description="find things",
        metadata={"ptc_output_schema": schema},
    )
    spec = sdk.extract_tool_spec(tool_obj)
    assert spec is not None
    assert spec.output_schema == schema
    prompt = sdk.build_sdk_prompt([spec])
    assert "data JSON Schema" in prompt
    assert '"matches"' in prompt


def test_unknown_output_schema_is_honest() -> None:
    spec = sdk.extract_tool_spec(read_file)
    assert spec is not None
    assert spec.output_schema is None
    prompt = sdk.build_sdk_prompt([spec])
    assert "data shape is undeclared" in prompt
    # Never tells the model to assume a concrete shape such as ``data["matches"]``.
    assert 'data["matches"]' in prompt  # only as the "do not assume" example
    assert "do not assume" in prompt


def test_budget_exceeded_raises_instead_of_truncating() -> None:
    specs = sdk.collect_tool_specs([read_file])
    with pytest.raises(sdk.SdkBudgetExceeded):
        sdk.build_sdk_prompt(specs, max_bytes=10)


def test_compact_prompt_is_bounded_and_points_to_native_definitions() -> None:
    prompt = sdk.build_compact_sdk_prompt(sdk.collect_tool_specs([read_file]))
    assert "read_file" in prompt
    assert "ToolEnvelope" in prompt
    # Compact form is explicit about the omission, and carries no raw schema.
    assert "exceeded the SDK size budget" in prompt
    assert "native tool definition" in prompt
    assert '"properties"' not in prompt
    assert len(prompt.encode("utf-8")) <= sdk.COMPACT_MAX_BYTES


def test_compact_prompt_bounds_the_number_of_names() -> None:
    specs = [
        sdk.ToolSpec(name=f"tool_{index:03d}", description="x" * 400, input_schema={})
        for index in range(500)
    ]
    prompt = sdk.build_compact_sdk_prompt(specs)
    assert len(prompt.encode("utf-8")) <= sdk.COMPACT_MAX_BYTES
    assert "only the first" in prompt
    assert "of 500 tools are listed" in prompt
    # The last tool is not listed once the prefix is bounded.
    assert "tool_499" not in prompt


def test_anyof_schema_maps_to_union_hint() -> None:
    spec = sdk.ToolSpec(
        name="maybe",
        description="d",
        input_schema={
            "type": "object",
            "properties": {"x": {"anyOf": [{"type": "string"}, {"type": "integer"}]}},
            "required": ["x"],
        },
    )
    prompt = sdk.build_sdk_prompt([spec])
    assert "x: str | int" in prompt


def test_deterministic_ordering() -> None:
    a = sdk.ToolSpec(name="zeta", description="z", input_schema={})
    b = sdk.ToolSpec(name="alpha", description="a", input_schema={})
    prompt = sdk.build_sdk_prompt([a, b])
    assert prompt.index("def alpha") < prompt.index("def zeta")


def test_description_with_triple_quote_still_compiles() -> None:
    spec = sdk.ToolSpec(
        name="tricky",
        description='has a """ triple quote and a \\ backslash',
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )
    prompt = sdk.build_sdk_prompt([spec])
    compile(_code_block(prompt), "<sdk>", "exec")


# --------------------------------------------------------------------------- #
# Schema fidelity: dict schemas, args_schema fallback, and honest unknowns
# --------------------------------------------------------------------------- #
class _DictCallSchemaTool:
    """A tool whose ``tool_call_schema`` is already a plain dict."""

    name = "dicty"
    description = "d"
    args = {"a": {"type": "string"}}
    args_schema = None
    tool_call_schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        "required": ["a"],
        "additionalProperties": False,
    }


class _DictArgsSchemaTool:
    """A tool that only exposes a plain-dict ``args_schema``."""

    name = "argsy"
    description = "d"
    args = {"a": {"type": "string"}}
    args_schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a"],
        "additionalProperties": False,
    }


class _ArgsOnlyTool:
    """A tool that only exposes the property-name map (no required info)."""

    name = "argsonly"
    description = "d"
    args = {"a": {"type": "string"}, "b": {"type": "integer"}}


def test_dict_tool_call_schema_is_respected_exactly() -> None:
    spec = sdk.extract_tool_spec(_DictCallSchemaTool())
    assert spec is not None
    # The dict is kept verbatim: ``required`` is not dropped for a missing model.
    assert spec.schema_fidelity is True
    assert spec.input_schema["required"] == ["a"]
    assert spec.input_schema["additionalProperties"] is False
    prompt = sdk.build_sdk_prompt([spec])
    assert "async def dicty(*, a: str, b: int | None = None) -> ToolEnvelope:" in prompt


def test_dict_args_schema_fallback_keeps_required() -> None:
    spec = sdk.extract_tool_spec(_DictArgsSchemaTool())
    assert spec is not None
    assert spec.schema_fidelity is True
    assert spec.input_schema["required"] == ["a"]
    assert spec.input_schema["additionalProperties"] is False


def test_pydantic_schema_keeps_defs_and_required() -> None:
    from pydantic import BaseModel

    class Inner(BaseModel):
        value: str

    class Args(BaseModel):
        inner: Inner
        count: int = 1

    tool_obj = StructuredTool.from_function(
        func=lambda inner, count=1: "ok",  # noqa: ARG005
        name="nested",
        description="d",
        args_schema=Args,
    )
    spec = sdk.extract_tool_spec(tool_obj)
    assert spec is not None
    assert spec.schema_fidelity is True
    # A normal Pydantic schema keeps its ``$defs`` and ``required`` untouched.
    assert "Inner" in spec.input_schema["$defs"]
    assert spec.input_schema["required"] == ["inner"]


def test_args_only_schema_is_not_claimed_exact() -> None:
    spec = sdk.extract_tool_spec(_ArgsOnlyTool())
    assert spec is not None
    # Only the property names are known, so the SDK must not pretend they are
    # required *or* optional.
    assert spec.schema_fidelity is False
    prompt = sdk.build_sdk_prompt([spec])
    assert "async def argsonly(**kwargs) -> ToolEnvelope:" in prompt
    # It never fabricates an all-optional typed signature.
    assert "| None = None" not in prompt
    assert "exact input schema is unknown" in prompt
    compile(_code_block(prompt), "<sdk>", "exec")


def test_default_budget_is_finite_and_folds_a_large_but_sane_toolset() -> None:
    # Raised from 24 KB to fold the default coding toolset (~25 KB) without
    # becoming unbounded.
    assert sdk.DEFAULT_MAX_SDK_BYTES == 32_000
    spec = sdk.ToolSpec(
        name="big",
        description="d",
        input_schema={
            "type": "object",
            "properties": {f"field_{index:03d}": {"type": "string"} for index in range(450)},
            "required": ["field_000"],
        },
    )
    # Fits the raised default budget...
    document = sdk.build_sdk_prompt([spec])
    assert 24_000 < len(document.encode("utf-8")) <= sdk.DEFAULT_MAX_SDK_BYTES
    # ...but the old 24 KB budget would still have rejected it loudly.
    with pytest.raises(sdk.SdkBudgetExceeded):
        sdk.build_sdk_prompt([spec], max_bytes=24_000)
