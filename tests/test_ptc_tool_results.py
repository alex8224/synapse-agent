"""PTC (programmatic tool calling) artifact contract tests.

These pin the canonical ``{"ptc": {"data": ..., "truncated": ...}}`` envelope
that the filesystem search tools and the MCP adapter publish, while asserting
the historical model-facing text output is unchanged and that a direct
``tool.invoke(args)`` still returns that text (LangChain only attaches the
artifact to a real tool call's ``ToolMessage``).

They also pin:

* the precise JSON Schema each structured-artifact tool advertises for
  ``artifact["ptc"]["data"]`` under ``metadata["ptc_output_schema"]`` (the key
  the PTC SDK's ``ToolSpec`` reads), validated against real artifacts,
* the bounded backend acquisition request (``offset`` is range-limited and the
  fetch budget is clamped, so a large offset cannot request unbounded rows),
* the MCP adapter's split public API: ``call_tool`` stays content-only while
  ``call_tool_result`` carries the canonical payload, and a server-declared
  ``outputSchema`` is passed through verbatim (never fabricated),
* an end-to-end run of a genuine ``create_deep_agent`` graph with the PTC,
  intent, exclusion and path-normalize middleware: model-authored code calling
  ``tools.find_files()`` through the real process sandbox receives the canonical
  artifact (not just a direct ``tool.invoke``).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from synapse.integrations.mcp_client import (
    McpServerConfig,
    McpSessionPool,
    _call_outcome,
    _LiveServer,
    _make_tool,
    _McpCallOutcome,
)
from synapse.runtime.backends import CodingLocalShellBackend
from synapse.runtime.middleware import (
    build_intent_schema_middleware,
    build_path_normalize_middleware,
    build_tool_exclusion_middleware,
)
from synapse.runtime.ptc.middleware import build_ptc_middleware
from synapse.runtime.ptc.protocol import PtcLimits
from synapse.tools.filesystem_search import build_filesystem_search_tools


@pytest.fixture(autouse=True)
def _isolate_home_and_network(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Any:
    """Keep every test off the real HOME/config and off the network."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _assert_valid(instance: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate *instance* against the JSON-Schema subset our schemas use.

    Deliberately dependency-free: the output schemas only use ``type`` (incl.
    ``["integer", "null"]``), ``properties``, ``required``, ``items``,
    ``additionalProperties``, ``enum`` and ``minimum``.
    """
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]

        def _matches(kind: str) -> bool:
            if kind == "object":
                return isinstance(instance, dict)
            if kind == "array":
                return isinstance(instance, list)
            if kind == "string":
                return isinstance(instance, str)
            if kind == "integer":
                return isinstance(instance, int) and not isinstance(instance, bool)
            if kind == "boolean":
                return isinstance(instance, bool)
            if kind == "null":
                return instance is None
            return False

        assert any(_matches(kind) for kind in types), (
            f"{path}: {instance!r} does not match type {types}"
        )

    if "enum" in schema:
        assert instance in schema["enum"], f"{path}: {instance!r} not in {schema['enum']}"
    if "minimum" in schema and isinstance(instance, int | float) and not isinstance(instance, bool):
        assert instance >= schema["minimum"], f"{path}: {instance} < minimum"

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            assert key in instance, f"{path}: missing required {key!r}"
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in properties:
                _assert_valid(value, properties[key], f"{path}.{key}")
            elif additional is False:
                raise AssertionError(f"{path}: unexpected property {key!r}")
            elif isinstance(additional, dict):
                _assert_valid(value, additional, f"{path}.{key}")

    if isinstance(instance, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(instance):
            _assert_valid(item, schema["items"], f"{path}[{index}]")


def _tool_call_message(tool: Any, args: dict[str, Any]) -> Any:
    """Invoke *tool* as a real tool call so LangChain returns a ToolMessage."""
    return tool.invoke(
        {"type": "tool_call", "name": tool.name, "id": "call-1", "args": args}
    )


class _SearchBackend:
    """Deterministic glob/grep backend that honors ``max_results``."""

    def __init__(
        self,
        *,
        glob_matches: list[dict[str, Any]] | None = None,
        grep_matches: list[dict[str, Any]] | None = None,
        error: str | None = None,
    ) -> None:
        self.glob_matches = glob_matches or []
        self.grep_matches = grep_matches or []
        self.error = error
        self.glob_calls: list[int] = []
        self.grep_calls: list[int] = []

    def glob(
        self,
        pattern: str,
        path: str | None = None,
        max_results: int = 1000,
    ) -> Any:
        self.glob_calls.append(max_results)
        if self.error:
            return SimpleNamespace(error=self.error, matches=[])
        return SimpleNamespace(matches=list(self.glob_matches[:max_results]))

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        max_results: int = 1000,
        context_lines: int = 0,
        case_insensitive: bool = False,
    ) -> Any:
        self.grep_calls.append(max_results)
        if self.error:
            return SimpleNamespace(error=self.error, matches=[])
        return SimpleNamespace(matches=list(self.grep_matches[:max_results]))


# --------------------------------------------------------------------------- #
# filesystem search tools
# --------------------------------------------------------------------------- #


def test_find_files_direct_invoke_keeps_text_output() -> None:
    backend = _SearchBackend(
        glob_matches=[
            {"path": "/a.py", "is_dir": False},
            {"path": "/pkg", "is_dir": True},
        ]
    )
    find_files, _ = build_filesystem_search_tools(backend)
    assert find_files.invoke({"pattern": "**/*.py"}) == "/a.py\n/pkg/"


def test_search_tools_declare_content_and_artifact_contract() -> None:
    find_files, search_files = build_filesystem_search_tools(_SearchBackend())
    for tool in (find_files, search_files):
        assert tool.response_format == "content_and_artifact"
        assert tool.handle_tool_error is True


def test_find_files_toolcall_exposes_ptc_artifact() -> None:
    backend = _SearchBackend(
        glob_matches=[
            {"path": "/a.py", "is_dir": False},
            {"path": "/pkg", "is_dir": True},
        ]
    )
    find_files, _ = build_filesystem_search_tools(backend)
    message = _tool_call_message(find_files, {"pattern": "**/*.py"})

    assert message.status == "success"
    assert message.content == "/a.py\n/pkg/"
    assert message.artifact == {
        "ptc": {
            "data": {
                "matches": [
                    {"path": "/a.py", "is_dir": False},
                    {"path": "/pkg", "is_dir": True},
                ],
                "offset": 0,
                "next_offset": None,
            },
            "truncated": False,
        }
    }


def test_find_files_pagination_has_no_gap_or_overlap() -> None:
    matches = [{"path": f"/f{i}.py", "is_dir": False} for i in range(5)]
    backend = _SearchBackend(glob_matches=matches)
    find_files, _ = build_filesystem_search_tools(backend)

    seen: list[str] = []
    page0 = _tool_call_message(
        find_files, {"pattern": "**/*.py", "max_results": 2, "offset": 0}
    ).artifact["ptc"]["data"]
    assert [m["path"] for m in page0["matches"]] == ["/f0.py", "/f1.py"]
    assert page0["offset"] == 0
    assert page0["next_offset"] == 2
    # Budget is offset + max_results + one sentinel row.
    assert backend.glob_calls[-1] == 3
    seen.extend(m["path"] for m in page0["matches"])

    page1 = _tool_call_message(
        find_files, {"pattern": "**/*.py", "max_results": 2, "offset": page0["next_offset"]}
    ).artifact["ptc"]["data"]
    assert [m["path"] for m in page1["matches"]] == ["/f2.py", "/f3.py"]
    assert page1["next_offset"] == 4
    assert backend.glob_calls[-1] == 2 + 2 + 1
    seen.extend(m["path"] for m in page1["matches"])

    page2 = _tool_call_message(
        find_files, {"pattern": "**/*.py", "max_results": 2, "offset": page1["next_offset"]}
    ).artifact["ptc"]["data"]
    assert [m["path"] for m in page2["matches"]] == ["/f4.py"]
    assert page2["next_offset"] is None
    seen.extend(m["path"] for m in page2["matches"])

    assert seen == [m["path"] for m in matches]


def test_find_files_head_limit_still_fetches_the_offset_window() -> None:
    matches = [{"path": f"/f{i}.py", "is_dir": False} for i in range(6)]
    backend = _SearchBackend(glob_matches=matches)
    find_files, _ = build_filesystem_search_tools(backend)

    data = _tool_call_message(
        find_files,
        {"pattern": "**/*.py", "max_results": 4, "head_limit": 1, "offset": 3},
    ).artifact["ptc"]["data"]

    assert [m["path"] for m in data["matches"]] == ["/f3.py"]
    assert data["offset"] == 3
    assert data["next_offset"] == 4
    # offset + max(max_results, head_limit) + sentinel
    assert backend.glob_calls[-1] == 3 + max(4, 1) + 1


def test_search_files_artifact_includes_output_mode() -> None:
    backend = _SearchBackend(
        grep_matches=[
            {"path": "/a.py", "line": 1, "text": "hit"},
            {"path": "/b.py", "line": 2, "text": "hit"},
        ]
    )
    _, search_files = build_filesystem_search_tools(backend)
    message = _tool_call_message(
        search_files, {"pattern": "hit", "output_mode": "content"}
    )

    assert message.content == "/a.py:1: hit\n/b.py:2: hit"
    assert message.artifact == {
        "ptc": {
            "data": {
                "matches": [
                    {"path": "/a.py", "line": 1, "text": "hit"},
                    {"path": "/b.py", "line": 2, "text": "hit"},
                ],
                "offset": 0,
                "next_offset": None,
                "output_mode": "content",
            },
            "truncated": False,
        }
    }


def test_search_files_count_returns_current_page_only() -> None:
    backend = _SearchBackend(
        grep_matches=[{"path": "/a.py", "line": i, "text": "hit"} for i in range(5)]
    )
    _, search_files = build_filesystem_search_tools(backend)
    message = _tool_call_message(
        search_files, {"pattern": "hit", "output_mode": "count", "max_results": 2}
    )

    data = message.artifact["ptc"]["data"]
    assert data["output_mode"] == "count"
    # The count is page-local, not a whole-repo tally.
    assert len(data["matches"]) == 2
    assert data["next_offset"] == 2
    assert message.artifact["ptc"]["truncated"] is True
    assert message.content == "/a.py: 2\n[Results truncated]"


def test_search_files_direct_invoke_keeps_text_output() -> None:
    backend = _SearchBackend(
        grep_matches=[{"path": "/a.py", "line": 3, "text": "needle"}]
    )
    _, search_files = build_filesystem_search_tools(backend)
    assert search_files.invoke({"pattern": "needle"}) == "/a.py"


def test_find_files_backend_error_is_error_status() -> None:
    backend = _SearchBackend(error="Error globbing path '.': boom")
    find_files, _ = build_filesystem_search_tools(backend)

    # Direct invoke keeps returning the error text (no success artifact).
    assert find_files.invoke({"pattern": "**/*.py"}) == "Error globbing path '.': boom"

    message = _tool_call_message(find_files, {"pattern": "**/*.py"})
    assert message.status == "error"
    assert message.artifact is None
    assert "boom" in message.content


def test_search_files_backend_error_is_error_status() -> None:
    backend = _SearchBackend(error="Error searching path '.': boom")
    _, search_files = build_filesystem_search_tools(backend)
    message = _tool_call_message(search_files, {"pattern": "x"})
    assert message.status == "error"
    assert message.artifact is None
    assert "boom" in message.content


def test_no_matches_reports_empty_artifact() -> None:
    backend = _SearchBackend()
    find_files, search_files = build_filesystem_search_tools(backend)

    find_message = _tool_call_message(find_files, {"pattern": "**/*.nope"})
    assert find_message.content == "No paths matched."
    assert find_message.artifact["ptc"] == {
        "data": {"matches": [], "offset": 0, "next_offset": None},
        "truncated": False,
    }

    search_message = _tool_call_message(search_files, {"pattern": "nope"})
    assert search_message.content == "No matches found."
    assert search_message.artifact["ptc"]["data"]["matches"] == []


# --------------------------------------------------------------------------- #
# canonical output schema metadata (describes artifact.ptc.data)
# --------------------------------------------------------------------------- #


def test_find_files_publishes_precise_output_schema() -> None:
    find_files, _ = build_filesystem_search_tools(_SearchBackend())
    schema = find_files.metadata["ptc_output_schema"]

    assert schema["type"] == "object"
    assert set(schema["required"]) == {"matches", "offset", "next_offset"}
    assert schema["additionalProperties"] is False
    item = schema["properties"]["matches"]["items"]
    assert set(item["properties"]) == {"path", "is_dir"}
    assert set(item["required"]) == {"path", "is_dir"}
    assert item["additionalProperties"] is False
    assert schema["properties"]["offset"]["type"] == "integer"
    assert schema["properties"]["next_offset"]["type"] == ["integer", "null"]


def test_search_files_publishes_precise_output_schema() -> None:
    _, search_files = build_filesystem_search_tools(_SearchBackend())
    schema = search_files.metadata["ptc_output_schema"]

    assert set(schema["required"]) == {"matches", "offset", "next_offset", "output_mode"}
    assert schema["additionalProperties"] is False
    item = schema["properties"]["matches"]["items"]
    assert set(item["required"]) == {"path", "line", "text"}
    assert item["properties"]["path"]["type"] == "string"
    assert item["properties"]["line"]["type"] == "integer"
    assert item["properties"]["text"]["type"] == "string"
    # Context extras (e.g. surrounding lines) are permitted on a match.
    assert item["additionalProperties"] is True
    assert schema["properties"]["output_mode"]["enum"] == [
        "files_with_matches",
        "content",
        "count",
    ]


def test_search_output_schema_documents_page_local_count() -> None:
    _, search_files = build_filesystem_search_tools(_SearchBackend())
    schema = search_files.metadata["ptc_output_schema"]
    assert "page-local" in schema["description"]

    input_desc = search_files.tool_call_schema.model_json_schema()["properties"][
        "output_mode"
    ]["description"]
    assert "paths only" in input_desc
    assert "page-local" in input_desc


def test_find_files_artifact_validates_against_output_schema() -> None:
    backend = _SearchBackend(
        glob_matches=[
            {"path": "/a.py", "is_dir": False},
            {"path": "/pkg", "is_dir": True},
        ]
    )
    find_files, _ = build_filesystem_search_tools(backend)
    data = _tool_call_message(find_files, {"pattern": "**/*.py"}).artifact["ptc"]["data"]
    _assert_valid(data, find_files.metadata["ptc_output_schema"])


def test_search_files_artifact_validates_against_output_schema() -> None:
    backend = _SearchBackend(
        grep_matches=[
            {"path": "/a.py", "line": 3, "text": "needle", "context_after": ["next"]}
        ]
    )
    _, search_files = build_filesystem_search_tools(backend)
    data = _tool_call_message(
        search_files, {"pattern": "needle", "output_mode": "content"}
    ).artifact["ptc"]["data"]
    # The schema accepts the context extra and still types path/line/text.
    _assert_valid(data, search_files.metadata["ptc_output_schema"])
    assert data["matches"][0]["context_after"] == ["next"]


def test_empty_artifacts_validate_against_output_schema() -> None:
    find_files, search_files = build_filesystem_search_tools(_SearchBackend())
    cases = (
        (find_files, {"pattern": "**/*.nope"}),
        (search_files, {"pattern": "nope"}),
    )
    for tool, args in cases:
        data = _tool_call_message(tool, args).artifact["ptc"]["data"]
        _assert_valid(data, tool.metadata["ptc_output_schema"])


def test_ptc_sdk_toolspec_reads_output_schema_metadata() -> None:
    """The PTC SDK's ``ToolSpec`` consumes ``metadata['ptc_output_schema']``."""
    from synapse.runtime.ptc import sdk

    find_files, search_files = build_filesystem_search_tools(_SearchBackend())
    specs = {spec.name: spec for spec in sdk.collect_tool_specs([find_files, search_files])}

    assert specs["find_files"].output_schema == find_files.metadata["ptc_output_schema"]
    assert specs["search_files"].output_schema == search_files.metadata["ptc_output_schema"]
    # The rendered SDK document publishes the canonical data schema, so the model
    # can read the output shape instead of guessing it.
    prompt = sdk.build_sdk_prompt(list(specs.values()))
    assert "next_offset" in prompt
    assert "output_mode" in prompt


# --------------------------------------------------------------------------- #
# bounded backend acquisition
# --------------------------------------------------------------------------- #


def test_offset_is_bounded_in_both_input_schemas() -> None:
    from pydantic import ValidationError

    find_files, search_files = build_filesystem_search_tools(_SearchBackend())
    for tool in (find_files, search_files):
        offset = tool.args_schema.model_json_schema()["properties"]["offset"]
        assert offset["minimum"] == 0
        assert offset["maximum"] == 1000
    with pytest.raises(ValidationError):
        find_files.invoke({"pattern": "**/*.py", "offset": 1001})
    with pytest.raises(ValidationError):
        search_files.invoke({"pattern": "x", "offset": 1001})


def test_backend_fetch_budget_is_bounded() -> None:
    from synapse.tools.filesystem_search import _MAX_BACKEND_ROWS, _fetch_limit

    assert _MAX_BACKEND_ROWS == 2001
    assert _fetch_limit(2, 0, 0) == 3
    # A forged oversized offset cannot request an unbounded number of rows.
    assert _fetch_limit(1000, 1000, 10**9) == _MAX_BACKEND_ROWS


# --------------------------------------------------------------------------- #
# MCP adapter
# --------------------------------------------------------------------------- #


def _mcp_tool(call_fn: Any, *, output_schema: Any = None) -> Any:
    return _make_tool(
        server=SimpleNamespace(name="demo", tool_prefix=None),
        tool_name="do",
        description="do",
        input_schema={"type": "object", "properties": {}},
        call_fn=call_fn,
        output_schema=output_schema,
    )


def test_mcp_structured_content_is_canonical_artifact_data() -> None:
    payload = {"snapshot_id": "abc", "count": 3}
    tool = _mcp_tool(lambda name, args: _McpCallOutcome(content="ok", data=payload))

    assert tool.response_format == "content_and_artifact"
    assert tool.handle_tool_error is True
    assert tool.invoke({}) == "ok"
    message = _tool_call_message(tool, {})
    assert message.status == "success"
    assert message.content == "ok"
    assert message.artifact == {"ptc": {"data": payload, "truncated": None}}


def test_mcp_without_structured_content_has_null_data() -> None:
    tool = _mcp_tool(lambda name, args: _McpCallOutcome(content="plain text"))

    assert tool.invoke({}) == "plain text"
    assert _tool_call_message(tool, {}).artifact == {
        "ptc": {"data": None, "truncated": None}
    }


def test_mcp_declared_output_schema_is_passed_through_verbatim() -> None:
    output_schema = {
        "type": "object",
        "properties": {"snapshot_id": {"type": "string"}},
        "required": ["snapshot_id"],
    }
    tool = _mcp_tool(
        lambda name, args: _McpCallOutcome(content="ok", data={"snapshot_id": "s1"}),
        output_schema=output_schema,
    )
    assert tool.metadata == {"ptc_output_schema": output_schema}

    from synapse.runtime.ptc import sdk

    spec = sdk.extract_tool_spec(tool)
    assert spec is not None
    assert spec.output_schema == output_schema


def test_mcp_without_declared_output_schema_has_no_metadata() -> None:
    # Never fabricate an output schema the server did not declare.
    tool = _mcp_tool(lambda name, args: _McpCallOutcome(content="ok"))
    assert tool.metadata is None


def test_mcp_text_blocks_are_not_json_parsed() -> None:
    result = SimpleNamespace(
        content=[SimpleNamespace(text='{"count": 5}')],
        isError=False,
    )
    outcome = _call_outcome(result)

    assert outcome.content == '{"count": 5}'
    assert outcome.data is None
    assert outcome.truncated is None
    assert outcome.is_error is False


def test_mcp_images_are_preserved_and_data_stays_null() -> None:
    result = SimpleNamespace(
        content=[
            SimpleNamespace(text="see image"),
            SimpleNamespace(type="image", mimeType="image/png", data="QUJD"),
        ],
        isError=False,
    )
    outcome = _call_outcome(result)
    assert isinstance(outcome.content, list)
    assert [block["type"] for block in outcome.content] == ["text", "image_url"]
    assert outcome.content[1]["image_url"]["url"] == "data:image/png;base64,QUJD"
    assert outcome.data is None

    tool = _mcp_tool(lambda name, args: outcome)
    message = _tool_call_message(tool, {})
    assert isinstance(message.content, list)
    assert message.content[1]["type"] == "image_url"
    assert message.artifact == {"ptc": {"data": None, "truncated": None}}


def test_mcp_is_error_becomes_error_status() -> None:
    result = SimpleNamespace(
        content=[SimpleNamespace(text="kaboom")],
        isError=True,
    )
    outcome = _call_outcome(result)
    assert outcome.is_error is True

    tool = _mcp_tool(lambda name, args: outcome)
    assert tool.invoke({}) == "MCP error: kaboom"
    message = _tool_call_message(tool, {})
    assert message.status == "error"
    assert message.artifact is None
    assert "kaboom" in message.content


def test_mcp_raw_call_projects_structured_content() -> None:
    class FakeSession:
        async def call_tool(self, name: str, arguments: Any = None) -> Any:
            return SimpleNamespace(
                content=[SimpleNamespace(text="ok")],
                structuredContent={"snapshot_id": "s1"},
                isError=False,
            )

    pool = McpSessionPool()
    live = _LiveServer(
        config=McpServerConfig(name="demo", command="x"),
        session=FakeSession(),
        transport_cm=SimpleNamespace(__aexit__=lambda *a, **k: None),
        session_cm=SimpleNamespace(__aexit__=lambda *a, **k: None),
    )
    pool._servers["demo"] = live  # noqa: SLF001 - exercise the raw call path
    try:
        outcome = asyncio.run(pool._call("demo", "shot", {}))  # noqa: SLF001
    finally:
        pool.close()

    assert isinstance(outcome, _McpCallOutcome)
    assert outcome.is_error is False
    # Text stays model-facing and still carries the compact structured summary.
    assert outcome.content.startswith("ok")
    assert "structuredContent:" in outcome.content
    assert outcome.data == {"snapshot_id": "s1"}
    assert outcome.truncated is None


def test_mcp_raw_call_not_connected_is_error_outcome() -> None:
    pool = McpSessionPool()
    try:
        outcome = asyncio.run(pool._call("missing", "tool", {}))  # noqa: SLF001
    finally:
        pool.close()

    assert isinstance(outcome, _McpCallOutcome)
    assert outcome.is_error is True
    assert "not connected" in outcome.content


def _live_pool(result: Any) -> McpSessionPool:
    """A pool with one fake live session returning *result* for every call."""

    class FakeSession:
        async def call_tool(self, name: str, arguments: Any = None) -> Any:
            return result

    pool = McpSessionPool()
    live = _LiveServer(
        config=McpServerConfig(name="demo", command="x"),
        session=FakeSession(),
        transport_cm=SimpleNamespace(__aexit__=lambda *a, **k: None),
        session_cm=SimpleNamespace(__aexit__=lambda *a, **k: None),
    )
    pool._servers["demo"] = live  # noqa: SLF001 - exercise the public call path
    return pool


def test_mcp_call_tool_public_api_returns_content_only() -> None:
    pool = _live_pool(
        SimpleNamespace(content=[SimpleNamespace(text="hello")], isError=False)
    )
    try:
        content = pool.call_tool("demo", "shot", {})
    finally:
        pool.close()

    # The long-standing public method stays content-only.
    assert content == "hello"


def test_mcp_call_tool_result_rich_api_returns_payload() -> None:
    result = SimpleNamespace(
        content=[SimpleNamespace(text="hello")],
        structuredContent={"snapshot_id": "s1"},
        isError=False,
    )
    pool = _live_pool(result)
    try:
        outcome = pool.call_tool_result("demo", "shot", {})
    finally:
        pool.close()

    assert isinstance(outcome, _McpCallOutcome)
    assert outcome.content.startswith("hello")
    assert outcome.data == {"snapshot_id": "s1"}
    assert outcome.is_error is False


def test_mcp_content_and_rich_public_api_surface() -> None:
    from synapse.integrations.mcp_client import _normalize_call_outcome

    # Both surfaces exist: content-only ``call_tool`` (compat) and rich
    # ``call_tool_result`` (bound internally by ``_make_tool``).
    assert hasattr(McpSessionPool, "call_tool")
    assert hasattr(McpSessionPool, "call_tool_result")
    # A legacy content-only value (the old ``call_tool`` return) normalizes to
    # ``data=None``; the artifact carries no fabricated payload.
    content, data, truncated, is_error = _normalize_call_outcome("legacy-content")
    assert (content, data, truncated, is_error) == ("legacy-content", None, None, False)
    tool = _mcp_tool(lambda name, args: "legacy-content")
    assert _tool_call_message(tool, {}).artifact == {
        "ptc": {"data": None, "truncated": None}
    }


# --------------------------------------------------------------------------- #
# real create_deep_agent + PTC + intent/exclusion/path middleware integration
# --------------------------------------------------------------------------- #


class _FakeToolModel(FakeMessagesListChatModel):
    """A fake chat model that accepts ``bind_tools`` without any network."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> _FakeToolModel:  # noqa: ARG002
        return self


def _run_code_ai(code: str, call_id: str = "call-1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "run_code",
                "args": {"code": code, "intent": "orchestrate"},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def test_real_deep_agent_ptc_call_carries_canonical_artifact(tmp_path: Path) -> None:
    """Model code calling ``tools.find_files()`` must receive the artifact.

    A genuine ``create_deep_agent`` graph runs the PTC middleware alongside the
    intent, exclusion and path-normalize middleware, with an offline fake model
    and a real filesystem backend rooted in ``tmp_path``. ``run_code`` calls
    ``tools.find_files(...)`` through the real process sandbox; the envelope's
    ``data`` must equal the tool's canonical artifact.

    The intent middleware only rewrites the *model-facing* tool schema; the
    ToolNode registry (``runtime.tools``) keeps the artifact-bearing original,
    so the child call cannot lose ``artifact['ptc']['data']``. This is the
    evidence for that claim -- a failure here means the artifact was dropped
    somewhere in the middleware stack, not merely in a direct ``invoke``.
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "a.py").write_text("print('a')\n", encoding="utf-8")
    (tmp_path / "pkg" / "b.py").write_text("print('b')\n", encoding="utf-8")

    backend = CodingLocalShellBackend(
        root_dir=tmp_path,
        virtual_mode=True,
        timeout=30,
        inherit_env=True,
        shell_executable="pwsh" if sys.platform == "win32" else "bash",
    )
    find_files, search_files = build_filesystem_search_tools(backend)
    middleware = [
        build_ptc_middleware(
            mode="code",
            project_root=tmp_path,
            excluded_tools=[],
            require_approval=False,
            readonly=False,
            limits=PtcLimits(),
        ),
        build_tool_exclusion_middleware([]),
        build_path_normalize_middleware(tmp_path),
        *build_intent_schema_middleware(),
    ]
    code = (
        "files = await tools.find_files(pattern='**/*.py')\n"
        "hits = await tools.search_files(pattern='print', output_mode='content')\n"
        "return {'files': files, 'hits': hits}"
    )
    agent = create_deep_agent(
        model=_FakeToolModel(responses=[_run_code_ai(code), AIMessage(content="done")]),
        tools=[find_files, search_files],
        backend=backend,
        middleware=middleware,
    )
    result = agent.invoke({"messages": [HumanMessage(content="go")]})

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    run_code_messages = [m for m in tool_messages if m.name == "run_code"]
    assert len(run_code_messages) == 1
    message = run_code_messages[0]
    assert message.status == "success", message.content

    value = json.loads(message.content)["value"]
    files_envelope = value["files"]
    assert files_envelope["data"] == {
        "matches": [
            {"path": "/a.py", "is_dir": False},
            {"path": "/pkg/b.py", "is_dir": False},
        ],
        "offset": 0,
        "next_offset": None,
    }
    assert files_envelope["truncated"] is False
    _assert_valid(files_envelope["data"], find_files.metadata["ptc_output_schema"])

    hits_envelope = value["hits"]
    assert hits_envelope["data"]["output_mode"] == "content"
    assert hits_envelope["data"]["matches"]
    assert all(
        {"path", "line", "text"} <= set(match)
        for match in hits_envelope["data"]["matches"]
    )
    _assert_valid(hits_envelope["data"], search_files.metadata["ptc_output_schema"])
    # The child call never leaked a nested ToolMessage into graph state.
    assert message.tool_call_id == "call-1"
    assert all(":ptc:" not in (m.tool_call_id or "") for m in tool_messages)
