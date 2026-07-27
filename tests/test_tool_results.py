"""Tests for durable tool-result journals and context reference middleware."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from synapse.execute_capture import capture_execute_output
from synapse.middleware import build_tool_result_offload_middleware
from synapse.tool_results import ToolResultStore
from synapse.tools.session_tools import build_tool_result_reader_tool


def _request(*, thread_id: str = "thread-a", namespace: str = "", name: str = "execute"):
    return SimpleNamespace(
        tool_call={"id": "call-1", "name": name, "args": {}},
        runtime=SimpleNamespace(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": namespace,
                }
            }
        ),
    )


def test_store_round_trip_and_rejects_wrong_thread(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    record = store.append(
        thread_id="thread-a",
        checkpoint_ns="task_call:c1",
        tool_call_id="call-1",
        tool_name="execute",
        status="success",
        content="first\nsecond\nthird",
    )

    loaded = store.get(record.ref, expected_thread_id="thread-a")
    assert loaded is not None
    assert loaded.content == "first\nsecond\nthird"
    assert loaded.checkpoint_ns == "task_call:c1"
    assert store.get(record.ref, expected_thread_id="thread-b") is None
    assert (tmp_path / "thread-a" / "tool-results.jsonl").is_file()


def test_store_detects_tampered_record(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    record = store.append(
        thread_id="thread-a",
        tool_call_id="call-1",
        tool_name="read_file",
        status="success",
        content="original",
    )
    journal = tmp_path / "thread-a" / "tool-results.jsonl"
    tampered = journal.read_text(encoding="utf-8").replace("original", "tampered")
    journal.write_text(tampered, encoding="utf-8")

    assert store.get(record.ref) is None


def test_large_result_is_archived_and_replaced_with_preview(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    middleware = build_tool_result_offload_middleware(
        store,
        threshold_bytes=20,
        preview_head_chars=8,
        preview_tail_chars=4,
    )
    request = _request(namespace="task_call:parent")
    original = "0123456789" * 10

    result = middleware.wrap_tool_call(
        request,
        lambda _request: ToolMessage(
            content=original,
            tool_call_id="call-1",
            name="execute",
        ),
    )

    assert isinstance(result, ToolMessage)
    assert "[tool result archived]" in str(result.content)
    artifact = result.artifact
    assert isinstance(artifact, dict)
    ref = artifact["tool_result_ref"]
    assert str(ref).startswith("tool-result://thread-a/")
    loaded = store.get(ref, expected_thread_id="thread-a")
    assert loaded is not None
    assert loaded.content == original
    assert loaded.checkpoint_ns == "task_call:parent"


def test_execute_archives_full_output_captured_before_backend_truncation(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    middleware = build_tool_result_offload_middleware(
        store,
        threshold_bytes=10_000,
        preview_head_chars=8,
        preview_tail_chars=4,
    )
    full_output = "0123456789" * 20
    visible_output = "0123456789\n\n... Output truncated at 10 bytes."

    def handler(_request):
        capture_execute_output(
            full_output=full_output,
            displayed_output=visible_output,
            truncated=True,
        )
        return ToolMessage(content=visible_output, tool_call_id="call-1", name="execute")

    result = middleware.wrap_tool_call(_request(), handler)

    assert "[tool result archived]" in result.content
    assert isinstance(result.artifact, dict)
    assert result.artifact["tool_result_contains_untruncated_execute_output"] is True
    record = store.get(result.artifact["tool_result_ref"], expected_thread_id="thread-a")
    assert record is not None
    assert record.content == full_output


def test_small_and_error_result_keep_model_visible_content(tmp_path: Path) -> None:
    middleware = build_tool_result_offload_middleware(ToolResultStore(tmp_path), threshold_bytes=20)

    small = middleware.wrap_tool_call(
        _request(),
        lambda _request: ToolMessage(content="small", tool_call_id="call-1", name="read_file"),
    )
    error = middleware.wrap_tool_call(
        _request(),
        lambda _request: ToolMessage(
            content="Error: detailed failure " * 10,
            tool_call_id="call-1",
            name="read_file",
            status="error",
        ),
    )

    assert small.content == "small"
    assert isinstance(small.artifact, dict)
    assert str(error.content).startswith("Error: detailed failure")
    assert isinstance(error.artifact, dict)


def test_reader_and_compact_result_are_excluded_from_recursive_offload(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path)
    record = store.append(
        thread_id="thread-a",
        tool_call_id="prior",
        tool_name="execute",
        status="success",
        content="line-0\nline-1\nline-2",
    )
    reader = build_tool_result_reader_tool(tmp_path)
    # The runtime argument is injected by ToolNode and therefore absent from the
    # model-visible tool schema.
    assert set(reader.tool_call_schema.model_fields) == {"ref", "offset", "limit"}
    tool_runtime = ToolRuntime(
        state={},
        context=None,
        config={"configurable": {"thread_id": "thread-a"}},
        stream_writer=lambda _value: None,
        tool_call_id="read-1",
        store=None,
    )
    output = reader.invoke(
        {"runtime": tool_runtime, "ref": record.ref, "offset": 1, "limit": 1}
    )
    assert "line-1" in output
    denied_runtime = ToolRuntime(
        state={},
        context=None,
        config={"configurable": {"thread_id": "thread-b"}},
        stream_writer=lambda _value: None,
        tool_call_id="read-2",
        store=None,
    )
    denied = reader.invoke({"runtime": denied_runtime, "ref": record.ref})
    assert "无权读取" in denied

    middleware = build_tool_result_offload_middleware(store, threshold_bytes=1)
    direct = middleware.wrap_tool_call(
        _request(name="read_tool_result"),
        lambda _request: ToolMessage(
            content="a" * 100,
            tool_call_id="call-1",
            name="read_tool_result",
        ),
    )
    compact = middleware.wrap_tool_call(
        _request(name="compact_conversation"),
        lambda _request: Command(
            update={
                "messages": [
                    ToolMessage(
                        content="b" * 100,
                        tool_call_id="call-1",
                        name="compact_conversation",
                    )
                ]
            }
        ),
    )

    assert direct.content == "a" * 100
    assert isinstance(compact, Command)
    assert compact.update["messages"][0].content == "b" * 100


def test_command_and_async_results_are_rewritten(tmp_path: Path) -> None:
    middleware = build_tool_result_offload_middleware(
        ToolResultStore(tmp_path),
        threshold_bytes=4,
    )
    request = _request()
    command = middleware.wrap_tool_call(
        request,
        lambda _request: Command(
            update={"messages": [ToolMessage(content="large-result", tool_call_id="call-1")]}
        ),
    )

    async def run():
        return await middleware.awrap_tool_call(
            request,
            lambda _request: _async_message(),
        )

    async def _async_message():
        return ToolMessage(content="async-large", tool_call_id="call-1", name="execute")

    async_result = asyncio.run(run())
    assert isinstance(command, Command)
    assert "[tool result archived]" in command.update["messages"][0].content
    assert isinstance(async_result, ToolMessage)
    assert "[tool result archived]" in async_result.content
