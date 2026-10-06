"""Tests for the PTC bridge: policy, envelopes, scheduling and dispatch.

The bridge is the trust boundary between model-authored code and the real tools.
These tests pin:

* the child ``ToolEnvelope`` contract (including the "unknown" completeness
  signal when a tool has no canonical artifact),
* bounded, secret-free arg summaries for UI streaming,
* the refusal matrix (recursion / stateful / Command tools / excluded /
  unregistered / approval-gated / unknown-contract) and the approval-vs-write
  policy (writes are foldable only when approval is off),
* fair scheduling: parallel reads, an exclusive write that waits for earlier
  reads, a read that cannot jump a queued write, and cancellation that never
  starves the queue,
* the runtime ``tool_call_id`` rewrite that gives a child call its nested id,
* that a graph ``Command`` is a hard error whose state is not merged,
* the sync offload boundary: a blocking handler runs on a daemon thread, a cancel
  does not release its write ticket, and a still-running handler is reported --
  and the async path's blocking workers (sync-only tools, deepagents filesystem
  wrappers) use the same daemon pool rather than a shielded origin-loop task.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command

from synapse.runtime.ptc import bridge as bridge_module
from synapse.runtime.ptc.bridge import (
    DENIED_TOOLS,
    CallContext,
    PtcBridge,
    ToolCallError,
    _DispatchQueueFull,
    envelope_from_message,
    nested_call_id,
    sanitize_args,
)
from synapse.runtime.ptc.protocol import PtcLimits
from synapse.runtime.ptc.scheduler import READ, WRITE, FairReadWriteScheduler


@dataclass
class _Tool:
    name: str


class _AsyncTool:
    """A registered tool with a real ``coroutine`` (a genuinely async tool).

    The bridge only inspects ``coroutine is not None`` to decide a tool is not a
    blocking worker; the handler is what actually runs, so this coroutine is
    never called.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    async def coroutine(self, request: Any) -> Any:  # pragma: no cover - never invoked
        raise AssertionError("the bridge runs the handler, not the tool coroutine")


@dataclass
class _Runtime:
    tool_call_id: str = "parent"
    tools: list[Any] = field(default_factory=list)
    stream_writer: Any = None
    config: dict[str, Any] = field(default_factory=dict)
    context: Any = None


@dataclass
class _Request:
    tool_call: dict[str, Any]
    tool: Any = None
    state: Any = None
    runtime: Any = None


def _bridge(**overrides: Any) -> PtcBridge:
    params: dict[str, Any] = {
        "project_root": Path("."),
        "excluded_tools": [],
        "require_approval": False,
        "readonly": False,
        "limits": PtcLimits(),
    }
    params.update(overrides)
    return PtcBridge(**params)


def _context(
    bridge: PtcBridge,
    handler: Any,
    *,
    tools: list[str] | None = None,
    writer: Any = None,
    offload: bool = False,
) -> CallContext:
    # A genuinely async stand-in keeps the handler on the origin loop; the tests
    # that need the blocking-worker path override the tool explicitly.
    tool_objs = {name: _AsyncTool(name) for name in (tools or ["read_file", "write_file"])}
    runtime = _Runtime(tools=list(tool_objs.values()), stream_writer=writer)
    request = _Request(
        tool_call={"name": "run_code", "args": {}, "id": "parent", "type": "tool_call"},
        runtime=runtime,
    )
    return CallContext(
        parent_call_id="parent",
        handler=handler,
        request=request,
        runtime=runtime,
        tools=tool_objs,
        stream_writer=writer,
        offload=offload,
        tool_names=tuple(bridge.registered_names(tool_objs)),
    )


async def _ok_handler(req: _Request) -> ToolMessage:
    call = req.tool_call
    return ToolMessage(content="ok", tool_call_id=call["id"], name=call["name"])


# --------------------------------------------------------------------------- #
# Envelope + arg sanitisation
# --------------------------------------------------------------------------- #
def test_envelope_without_artifact_is_unknown_completeness() -> None:
    message = ToolMessage(content="hello", tool_call_id="c", name="t")
    assert envelope_from_message(message) == {
        "content": "hello",
        "data": None,
        "truncated": None,
    }


def test_envelope_reads_canonical_ptc_artifact() -> None:
    message = ToolMessage(
        content="summary",
        tool_call_id="c",
        name="t",
        artifact={"ptc": {"data": {"size": 3}, "truncated": True}},
    )
    envelope = envelope_from_message(message)
    assert envelope["content"] == "summary"
    assert envelope["data"] == {"size": 3}
    assert envelope["truncated"] is True


def test_envelope_preserves_block_content() -> None:
    blocks = [{"type": "text", "text": "hi"}]
    message = ToolMessage(content=blocks, tool_call_id="c", name="t")
    assert envelope_from_message(message)["content"] == blocks


def test_sanitize_args_drops_secrets_and_summarizes_content() -> None:
    summary = sanitize_args(
        {
            "path": "a.txt",
            "api_key": "SECRET",
            "token": "SECRET",
            "content": "x" * 5000,
            "count": 3,
        }
    )
    assert "api_key" not in summary
    assert "token" not in summary
    assert summary["path"] == "a.txt"
    assert summary["content"] == {"bytes": 5000, "sha256": summary["content"]["sha256"]}
    assert summary["count"] == 3


def test_nested_ids_are_unique_per_parent() -> None:
    assert nested_call_id("p", 0) != nested_call_id("p", 1)
    assert nested_call_id("p", 0).startswith("p")
    assert nested_call_id("", 0).startswith("ptc")


def test_tool_call_error_exposes_the_unified_surface() -> None:
    error = ToolCallError("nope", kind="denied", name="write_file")
    assert error.kind == "denied"
    assert error.name == "write_file"
    assert error.tool_name == "write_file"
    assert error.message == "nope"
    assert error.to_payload() == {
        "kind": "denied",
        "name": "write_file",
        "tool_name": "write_file",
        "message": "nope",
    }


# --------------------------------------------------------------------------- #
# Policy predicates
# --------------------------------------------------------------------------- #
def test_orchestratable_matrix() -> None:
    bridge = _bridge()
    assert bridge.is_orchestratable("read_file") is True
    # Writes are foldable when approval is off: they would run without a prompt
    # natively too.
    assert bridge.is_orchestratable("write_file") is True
    assert bridge.is_orchestratable("execute") is True
    assert bridge.is_orchestratable("write_todos") is False  # session state
    assert bridge.is_orchestratable("run_code") is False
    assert bridge.is_orchestratable("task") is False
    assert bridge.is_orchestratable("mcp_tool") is True  # unknown autopasses


def test_orchestratable_rejects_async_state_tools() -> None:
    bridge = _bridge()
    for name in (
        "start_async_task",
        "check_async_task",
        "update_async_task",
        "cancel_async_task",
        "list_async_tasks",
    ):
        assert name in DENIED_TOOLS
        assert bridge.is_orchestratable(name) is False


def test_orchestratable_under_approval_keeps_writes_native() -> None:
    bridge = _bridge(require_approval=True)
    assert bridge.is_orchestratable("read_file") is True
    assert bridge.is_orchestratable("write_file") is False  # needs approval
    assert bridge.is_orchestratable("execute") is False
    assert bridge.is_orchestratable("mcp_tool") is False  # unknown contract


def test_readonly_is_never_orchestratable() -> None:
    bridge = _bridge(readonly=True)
    assert bridge.is_orchestratable("read_file") is False
    assert bridge.is_orchestratable("write_file") is False


def test_denial_reason_matrix() -> None:
    tools = {"read_file": _Tool("read_file"), "write_file": _Tool("write_file")}
    bridge = _bridge()
    assert bridge.denial_reason("read_file", tools) is None
    assert bridge.denial_reason("write_file", tools) is None  # allowed without approval
    assert bridge.denial_reason("run_code", tools) is not None
    assert bridge.denial_reason("task", tools) is not None
    assert bridge.denial_reason("write_todos", tools) is not None
    assert bridge.denial_reason("create_goal", tools) is not None
    assert bridge.denial_reason("update_goal", tools) is not None
    assert bridge.denial_reason("mystery", tools) is not None  # unregistered
    assert bridge.denial_reason("mcp_tool", {**tools, "mcp_tool": _Tool("mcp_tool")}) is None


def test_denial_reason_excluded_and_approval() -> None:
    tools = {"read_file": _Tool("read_file"), "write_file": _Tool("write_file")}
    assert _bridge(excluded_tools=["read_file"]).denial_reason("read_file", tools) is not None
    approval = _bridge(require_approval=True)
    assert approval.denial_reason("write_file", tools) is not None
    assert approval.denial_reason("mcp_tool", {**tools, "mcp_tool": _Tool("mcp_tool")}) is not None


def test_registered_names_returns_every_registered_tool() -> None:
    tools = {
        "read_file": _Tool("read_file"),
        "write_file": _Tool("write_file"),
        "write_todos": _Tool("write_todos"),
    }
    bridge = _bridge()
    # Every registered name is handed to the sandbox, so denial is not masked as
    # "unknown" for a registered-but-denied tool.
    assert bridge.registered_names(tools) == ["read_file", "write_file", "write_todos"]


def test_classify_reads_and_writes() -> None:
    bridge = _bridge()
    assert bridge.classify("read_file") == READ
    assert bridge.classify("write_file") == WRITE
    assert bridge.classify("mcp_tool") == WRITE  # unknown -> exclusive


# --------------------------------------------------------------------------- #
# Dispatch behaviour
# --------------------------------------------------------------------------- #
def test_dispatch_returns_envelope_and_rewrites_runtime_id() -> None:
    seen: list[tuple[str, str, str | None]] = []

    async def handler(req: _Request) -> ToolMessage:
        seen.append((req.tool_call["name"], req.tool_call["id"], req.runtime.tool_call_id))
        return ToolMessage(
            content="body",
            tool_call_id=req.tool_call["id"],
            name=req.tool_call["name"],
            artifact={"ptc": {"data": {"n": 1}, "truncated": False}},
        )

    bridge = _bridge()
    ctx = _context(bridge, handler)
    envelope = asyncio.run(bridge.dispatch(ctx, "read_file", {"path": "a"}))
    assert envelope == {"content": "body", "data": {"n": 1}, "truncated": False}
    assert seen == [("read_file", "parent:ptc:0", "parent:ptc:0")]
    assert bridge.total_calls == 1


def test_dispatch_inherits_parent_config_and_context() -> None:
    captured: dict[str, Any] = {}

    async def handler(req: _Request) -> ToolMessage:
        captured["config"] = dict(req.runtime.config)
        captured["context"] = req.runtime.context
        captured["tool_call_id"] = req.runtime.tool_call_id
        return ToolMessage(
            content="ok", tool_call_id=req.tool_call["id"], name=req.tool_call["name"]
        )

    bridge = _bridge()
    tool_objs = {"read_file": _Tool("read_file")}
    runtime = _Runtime(
        tool_call_id="parent",
        tools=list(tool_objs.values()),
        config={"configurable": {"thread_id": "t1"}},
        context={"user": "u"},
    )
    request = _Request(
        tool_call={"name": "run_code", "args": {}, "id": "parent", "type": "tool_call"},
        runtime=runtime,
    )
    ctx = CallContext(
        parent_call_id="parent",
        handler=handler,
        request=request,
        runtime=runtime,
        tools=tool_objs,
    )
    asyncio.run(bridge.dispatch(ctx, "read_file", {"path": "a"}))
    assert captured["config"] == {"configurable": {"thread_id": "t1"}}
    assert captured["context"] == {"user": "u"}
    assert captured["tool_call_id"] == "parent:ptc:0"


def test_dispatch_rejects_recursion_and_stateful_tools() -> None:
    bridge = _bridge()

    async def handler(req: _Request) -> ToolMessage:  # pragma: no cover - never called
        raise AssertionError("handler must not run for denied tools")

    ctx = _context(bridge, handler)
    for name in ("run_code", "task", "write_todos", "create_goal", "update_goal"):
        with pytest.raises(ToolCallError) as info:
            asyncio.run(bridge.dispatch(ctx, name, {}))
        assert info.value.kind == "denied"


def test_dispatch_rejects_unregistered_tool() -> None:
    bridge = _bridge()

    async def handler(req: _Request) -> ToolMessage:  # pragma: no cover
        raise AssertionError

    ctx = _context(bridge, handler)
    with pytest.raises(ToolCallError) as info:
        asyncio.run(bridge.dispatch(ctx, "mystery", {}))
    assert info.value.kind == "unknown"


def test_dispatch_rejects_excluded_tool() -> None:
    bridge = _bridge(excluded_tools=["read_file"])

    async def handler(req: _Request) -> ToolMessage:  # pragma: no cover
        raise AssertionError

    ctx = _context(bridge, handler)
    with pytest.raises(ToolCallError) as info:
        asyncio.run(bridge.dispatch(ctx, "read_file", {}))
    assert info.value.kind == "denied"


def test_dispatch_error_message_becomes_tool_call_error() -> None:
    async def handler(req: _Request) -> ToolMessage:
        return ToolMessage(
            content="boom",
            tool_call_id=req.tool_call["id"],
            name=req.tool_call["name"],
            status="error",
        )

    bridge = _bridge()
    ctx = _context(bridge, handler)
    with pytest.raises(ToolCallError) as info:
        asyncio.run(bridge.dispatch(ctx, "read_file", {}))
    assert info.value.kind == "tool_error"
    assert info.value.tool_name == "read_file"
    assert "boom" in info.value.message


def test_dispatch_command_is_hard_error() -> None:
    async def handler(req: _Request) -> Command:
        return Command(update={"messages": []})

    bridge = _bridge()
    ctx = _context(bridge, handler)
    with pytest.raises(ToolCallError) as info:
        asyncio.run(bridge.dispatch(ctx, "read_file", {}))
    assert info.value.kind == "command"


def test_dispatch_emits_bounded_stream_events() -> None:
    events: list[dict[str, Any]] = []

    async def handler(req: _Request) -> ToolMessage:
        return ToolMessage(
            content="ok", tool_call_id=req.tool_call["id"], name=req.tool_call["name"]
        )

    bridge = _bridge()
    ctx = _context(bridge, handler, writer=events.append)
    asyncio.run(
        bridge.dispatch(
            ctx,
            "read_file",
            {"path": "a.txt", "api_key": "SECRET", "content": "x" * 100},
        )
    )
    assert [event["event"] for event in events] == ["started", "finished"]
    started, finished = events
    assert started["type"] == "ptc_tool"
    assert started["parent_call_id"] == "parent"
    assert started["call_id"] == "parent:ptc:0"
    assert started["name"] == "read_file"
    assert "api_key" not in started["args"]
    assert started["args"]["content"]["bytes"] == 100
    assert finished["status"] == "success"
    assert finished["preview"] == "ok"


def test_dispatch_emit_on_error() -> None:
    events: list[dict[str, Any]] = []

    async def handler(req: _Request) -> ToolMessage:
        return ToolMessage(
            content="nope",
            tool_call_id=req.tool_call["id"],
            name=req.tool_call["name"],
            status="error",
        )

    bridge = _bridge()
    ctx = _context(bridge, handler, writer=events.append)
    with pytest.raises(ToolCallError):
        asyncio.run(bridge.dispatch(ctx, "read_file", {}))
    assert events[-1]["event"] == "finished"
    assert events[-1]["status"] == "error"


def test_offload_runs_sync_handler_in_thread() -> None:
    calls: list[str] = []

    def handler(req: _Request) -> ToolMessage:
        calls.append(req.tool_call["name"])
        return ToolMessage(
            content="ok", tool_call_id=req.tool_call["id"], name=req.tool_call["name"]
        )

    bridge = _bridge()
    ctx = _context(bridge, handler, offload=True)
    envelope = asyncio.run(bridge.dispatch(ctx, "read_file", {"path": "a"}))
    assert envelope["content"] == "ok"
    assert calls == ["read_file"]
    assert bridge.sync_inflight == 0


# --------------------------------------------------------------------------- #
# Sync offload boundary
# --------------------------------------------------------------------------- #
def test_offload_cancel_keeps_write_lock_until_thread_finishes() -> None:
    started = threading.Event()
    release = threading.Event()

    def write_handler(req: _Request) -> ToolMessage:
        started.set()
        release.wait(timeout=10)
        return ToolMessage(
            content="wrote", tool_call_id=req.tool_call["id"], name=req.tool_call["name"]
        )

    read_started = asyncio.Event()

    async def read_handler(req: _Request) -> ToolMessage:
        read_started.set()
        return ToolMessage(
            content="read", tool_call_id=req.tool_call["id"], name=req.tool_call["name"]
        )

    bridge = _bridge()
    write_ctx = _context(bridge, write_handler, tools=["write_file"], offload=True)
    read_ctx = _context(bridge, read_handler, tools=["read_file"], offload=False)

    async def scenario() -> None:
        write_task = asyncio.ensure_future(bridge.dispatch(write_ctx, "write_file", {}))
        while not started.is_set():
            await asyncio.sleep(0.01)
        write_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await write_task
        # The handler cannot be killed: the bridge still reports it as running.
        assert bridge.sync_inflight == 1
        # A later read must not barge past the still-running write.
        read_task = asyncio.ensure_future(bridge.dispatch(read_ctx, "read_file", {}))
        await asyncio.sleep(0.1)
        assert not read_started.is_set()
        # Once the real handler ends, the write ticket is released and the read runs.
        release.set()
        await asyncio.wait_for(read_task, timeout=5)
        assert read_started.is_set()
        assert bridge.sync_inflight == 0

    asyncio.run(scenario())


def test_invoke_warns_when_sync_handler_outlives_the_run() -> None:
    started = threading.Event()
    release = threading.Event()
    holder: dict[str, Any] = {}

    def handler(req: _Request) -> ToolMessage:
        started.set()
        release.wait(timeout=10)
        return ToolMessage(
            content="late", tool_call_id=req.tool_call["id"], name=req.tool_call["name"]
        )

    async def runner(*, code, tool_names, dispatch, cwd, limits, **kwargs):  # noqa: ARG001
        asyncio.ensure_future(dispatch("write_file", {}))
        bridge = holder["bridge"]
        for _ in range(500):
            if bridge.sync_inflight:
                break
            await asyncio.sleep(0.01)
        # Simulate the run ending (e.g. timeout) while the thread keeps running.
        return {"logs": [], "value": None}

    bridge = _bridge(run_code=runner)
    holder["bridge"] = bridge
    ctx = _context(bridge, handler, tools=["write_file"], offload=True)

    async def scenario() -> dict[str, Any]:
        result = await bridge.invoke(ctx, code="")
        assert "warning" in result
        assert "may still be running" in result["warning"]
        assert any("may still be running" in line for line in result["logs"])
        return result

    asyncio.run(scenario())
    release.set()


# --------------------------------------------------------------------------- #
# Scheduling through the bridge
# --------------------------------------------------------------------------- #
def _recording_handler(order: list[str], delay: float = 0.05) -> Any:
    async def handler(req: _Request) -> ToolMessage:
        tag = req.tool_call["args"]["tag"]
        order.append(f"{tag}-start")
        await asyncio.sleep(delay)
        order.append(f"{tag}-end")
        return ToolMessage(
            content="ok", tool_call_id=req.tool_call["id"], name=req.tool_call["name"]
        )

    return handler


def test_bridge_parallel_reads_overlap() -> None:
    order: list[str] = []
    bridge = _bridge()
    ctx = _context(bridge, _recording_handler(order))

    async def run() -> None:
        await asyncio.gather(
            bridge.dispatch(ctx, "read_file", {"tag": "a"}),
            bridge.dispatch(ctx, "read_file", {"tag": "b"}),
        )

    asyncio.run(run())
    assert order == ["a-start", "b-start", "a-end", "b-end"]


def test_bridge_write_waits_for_earlier_reads_and_blocks_later_read() -> None:
    order: list[str] = []
    bridge = _bridge()
    ctx = _context(bridge, _recording_handler(order))

    async def run() -> None:
        await asyncio.gather(
            bridge.dispatch(ctx, "read_file", {"tag": "r1"}),
            bridge.dispatch(ctx, "write_file", {"tag": "w"}),
            bridge.dispatch(ctx, "read_file", {"tag": "r2"}),
        )

    asyncio.run(run())
    # r2 was submitted after w and must not jump ahead of it.
    assert order == ["r1-start", "r1-end", "w-start", "w-end", "r2-start", "r2-end"]


def test_bridge_respects_max_parallel_one() -> None:
    order: list[str] = []
    bridge = _bridge(limits=PtcLimits(max_parallel=1))
    ctx = _context(bridge, _recording_handler(order))

    async def run() -> None:
        await asyncio.gather(
            bridge.dispatch(ctx, "read_file", {"tag": "a"}),
            bridge.dispatch(ctx, "read_file", {"tag": "b"}),
        )

    asyncio.run(run())
    assert order == ["a-start", "a-end", "b-start", "b-end"]


# --------------------------------------------------------------------------- #
# Scheduler unit tests
# --------------------------------------------------------------------------- #
def test_scheduler_write_barrier_is_fair() -> None:
    order: list[str] = []
    scheduler = FairReadWriteScheduler(max_parallel=4)

    async def reader(tag: str) -> None:
        ticket = await scheduler.acquire(READ)
        order.append(f"{tag}-start")
        await asyncio.sleep(0.05)
        order.append(f"{tag}-end")
        await scheduler.release(ticket)

    async def writer(tag: str) -> None:
        ticket = await scheduler.acquire(WRITE)
        order.append(f"{tag}-start")
        await asyncio.sleep(0.01)
        order.append(f"{tag}-end")
        await scheduler.release(ticket)

    async def run() -> None:
        await asyncio.gather(reader("r1"), writer("w"), reader("r2"))

    asyncio.run(run())
    assert order == ["r1-start", "r1-end", "w-start", "w-end", "r2-start", "r2-end"]


def test_scheduler_cancel_releases_queue_without_starvation() -> None:
    scheduler = FairReadWriteScheduler(max_parallel=1)
    started: list[str] = []
    a_got = asyncio.Event()
    release_a = asyncio.Event()

    async def a() -> None:
        ticket = await scheduler.acquire(READ)
        started.append("a")
        a_got.set()
        await release_a.wait()
        await scheduler.release(ticket)

    async def b() -> None:
        ticket = await scheduler.acquire(WRITE)
        started.append("b")
        await scheduler.release(ticket)

    async def c() -> None:
        ticket = await scheduler.acquire(READ)
        started.append("c")
        await scheduler.release(ticket)

    async def run() -> None:
        task_a = asyncio.create_task(a())
        await a_got.wait()
        task_b = asyncio.create_task(b())
        await asyncio.sleep(0)
        task_c = asyncio.create_task(c())
        await asyncio.sleep(0.01)
        task_b.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task_b
        release_a.set()
        await asyncio.gather(task_a, task_c)

    asyncio.run(run())
    assert started == ["a", "c"]


def test_scheduler_rejects_unknown_kind() -> None:
    scheduler = FairReadWriteScheduler(max_parallel=1)

    async def run() -> None:
        with pytest.raises(ValueError):
            await scheduler.acquire("sideways")

    asyncio.run(run())


def test_scheduler_barrier_is_shared_across_loops() -> None:
    # One scheduler per bridge spans every loop: a write held on one loop blocks
    # a write acquired on a different loop (the old per-loop state did not).
    scheduler = FairReadWriteScheduler(max_parallel=1)
    held = threading.Event()
    release = threading.Event()
    second_acquired = threading.Event()

    def holder() -> None:
        async def run() -> None:
            ticket = await scheduler.acquire(WRITE)
            held.set()
            await asyncio.get_running_loop().run_in_executor(None, release.wait, 10)
            scheduler.release_nowait(ticket)

        asyncio.run(run())

    def waiter() -> None:
        async def run() -> None:
            ticket = await scheduler.acquire(WRITE)
            second_acquired.set()
            scheduler.release_nowait(ticket)

        asyncio.run(run())

    first = threading.Thread(target=holder)
    first.start()
    assert held.wait(timeout=5)
    second = threading.Thread(target=waiter)
    second.start()
    time.sleep(0.2)
    assert not second_acquired.is_set()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert second_acquired.is_set()


def test_limits_dataclass_replace_keeps_other_fields() -> None:
    # Guard the dataclasses.replace fallback used for request/runtime rewriting.
    runtime = _Runtime(tool_call_id="parent")
    replaced = dataclasses.replace(runtime, tool_call_id="child")
    assert replaced.tool_call_id == "child"
    assert replaced.config == {}


# --------------------------------------------------------------------------- #
# Cross-run offload: a background write from a closed loop still blocks
# --------------------------------------------------------------------------- #
def test_sync_write_from_closed_loop_blocks_the_next_run() -> None:
    started_one = threading.Event()
    release_one = threading.Event()
    started_two = threading.Event()

    def handler(req: _Request) -> ToolMessage:
        if req.tool_call["args"].get("tag") == "run1":
            started_one.set()
            release_one.wait(timeout=10)
        else:
            started_two.set()
        return ToolMessage(
            content="ok", tool_call_id=req.tool_call["id"], name=req.tool_call["name"]
        )

    bridge = _bridge()
    ctx_one = _context(bridge, handler, tools=["write_file"], offload=True)
    ctx_two = _context(bridge, handler, tools=["write_file"], offload=True)

    async def run_one() -> None:
        task = asyncio.ensure_future(bridge.dispatch(ctx_one, "write_file", {"tag": "run1"}))
        while not started_one.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # run_one's loop closes here; the daemon thread keeps running.

    asyncio.run(run_one())
    assert started_one.is_set()
    assert bridge.sync_inflight == 1

    async def run_two() -> None:
        task = asyncio.ensure_future(bridge.dispatch(ctx_two, "write_file", {"tag": "run2"}))
        await asyncio.sleep(0.2)
        # The write ticket from the closed run-one loop is still held.
        assert not started_two.is_set()
        release_one.set()
        await asyncio.wait_for(task, timeout=5)
        assert started_two.is_set()

    asyncio.run(run_two())
    assert bridge.sync_inflight == 0


def test_async_sync_tool_cancel_keeps_write_lock_until_return() -> None:
    started = threading.Event()
    release = threading.Event()

    def slow(path: str = "a") -> str:  # noqa: ARG001
        started.set()
        release.wait(timeout=10)
        return "done"

    tool = StructuredTool.from_function(func=slow, name="write_file", description="slow write")

    async def handler(req: _Request) -> Any:
        # Mimic the graph tool node: a sync BaseTool runs on a thread and cannot
        # be cancelled, so the ticket must outlive the caller's cancellation.
        return await req.tool.ainvoke(req.tool_call["args"])

    bridge = _bridge()
    write_ctx = _context(bridge, handler, tools=["write_file"], offload=False)
    write_ctx.tools["write_file"] = tool
    write_ctx.tool_names = tuple(bridge.registered_names(write_ctx.tools))

    read_started = asyncio.Event()

    async def read_handler(req: _Request) -> ToolMessage:
        read_started.set()
        return ToolMessage(
            content="read", tool_call_id=req.tool_call["id"], name=req.tool_call["name"]
        )

    read_ctx = _context(bridge, read_handler, tools=["read_file"], offload=False)

    async def scenario() -> None:
        task = asyncio.ensure_future(bridge.dispatch(write_ctx, "write_file", {"path": "a"}))
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        follow_up = asyncio.ensure_future(bridge.dispatch(read_ctx, "read_file", {}))
        await asyncio.sleep(0.2)
        # The blocking tool still holds the write lock; the read must not overlap.
        assert not read_started.is_set()
        release.set()
        await asyncio.wait_for(follow_up, timeout=5)
        assert read_started.is_set()

    asyncio.run(scenario())


def test_offload_propagates_contextvars() -> None:
    var: contextvars.ContextVar[str] = contextvars.ContextVar("ptc_test_var", default="unset")
    seen: dict[str, str] = {}

    def handler(req: _Request) -> ToolMessage:
        seen["value"] = var.get()
        return ToolMessage(
            content="ok", tool_call_id=req.tool_call["id"], name=req.tool_call["name"]
        )

    bridge = _bridge()
    ctx = _context(bridge, handler, tools=["read_file"], offload=True)
    token = var.set("trace-123")
    try:
        asyncio.run(bridge.dispatch(ctx, "read_file", {}))
    finally:
        var.reset(token)
    assert seen["value"] == "trace-123"


class _FullPool:
    """A pool whose bounded queue is always full."""

    def submit(self, fn: Any, *args: Any) -> Any:  # noqa: ARG002
        raise _DispatchQueueFull("queue full")


def test_offload_queue_full_releases_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_module, "_SYNC_DISPATCH_POOL", _FullPool())
    bridge = _bridge()
    write_ctx = _context(bridge, lambda req: None, tools=["write_file"], offload=True)

    async def scenario() -> None:
        with pytest.raises(ToolCallError) as info:
            await bridge.dispatch(write_ctx, "write_file", {})
        assert info.value.kind == "limit"
        # The granted ticket was released, so a later call is not blocked.
        read_ctx = _context(bridge, _ok_handler, tools=["read_file"], offload=False)
        await asyncio.wait_for(bridge.dispatch(read_ctx, "read_file", {}), timeout=2)

    asyncio.run(scenario())
