"""Cross-loop lifecycle tests for PTC blocking workers.

The PTC bridge holds a scheduler ticket until a blocking handler *really*
finishes. The async path used to await such a handler on the origin loop behind
``asyncio.shield``: when ``asyncio.run`` tore that loop down it cancelled the
shielded task while the underlying ``to_thread`` worker kept running, releasing
the write ticket early so the next turn's write could overlap.

These tests pin the fix. A *blocking worker* -- a sync-only ``BaseTool``
(``coroutine is None``) or a deepagents filesystem wrapper whose async body is
``asyncio.to_thread`` over a sync backend -- is dispatched to the daemon pool and
driven by an independent ``asyncio.run`` loop, so the ticket outlives the origin
loop. A genuinely async tool stays on the origin loop and is never forced onto
another one (that would break a real async client's thread/loop affinity).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from deepagents.backends.protocol import (
    BackendProtocol,
    ExecuteResponse,
    SandboxBackendProtocol,
    WriteResult,
)
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

from synapse.runtime.ptc.bridge import (
    CallContext,
    PtcBridge,
    _is_blocking_worker_tool,
    _is_deepagents_blocking_wrapper,
)
from synapse.runtime.ptc.middleware import build_ptc_middleware
from synapse.runtime.ptc.protocol import PtcLimits


# --------------------------------------------------------------------------- #
# Controlled backends + tools
# --------------------------------------------------------------------------- #
class _BlockingWriteBackend(BackendProtocol):
    """A backend whose sync ``write`` blocks until released.

    ``awrite`` is inherited from ``BackendProtocol`` and is ``asyncio.to_thread``
    over this sync ``write`` -- exactly the shape the deepagents filesystem
    ``write_file`` wrapper awaits, so the worker cannot be cancelled.
    """

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def write(self, file_path: str, content: str) -> WriteResult:  # noqa: ARG002
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=10)
        return WriteResult(path=file_path)


class _BlockingExecuteBackend(SandboxBackendProtocol):
    """A sandbox backend whose sync ``execute`` blocks until released."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    @property
    def id(self) -> str:
        return "blocking-sandbox"

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:  # noqa: ARG002
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=10)
        return ExecuteResponse(output="done", exit_code=0)


def _filesystem_tool(backend: Any, name: str) -> Any:
    """Return a real deepagents filesystem tool by name."""
    return next(tool for tool in FilesystemMiddleware(backend=backend).tools if tool.name == name)


def _async_tool(name: str) -> Any:
    """A genuinely async tool defined outside the deepagents filesystem module."""

    async def _run(path: str = "a") -> str:  # noqa: ARG001
        return "ok"

    return StructuredTool.from_function(
        func=lambda path="a": "ok",  # noqa: ARG005
        coroutine=_run,
        name=name,
        description="a genuinely async tool",
    )


async def _tool_node_handler(request: Any) -> Any:
    """Stand in for the graph tool node: inject the runtime the tool declares."""
    args = dict(request.tool_call["args"])
    return await request.tool.coroutine(runtime=request.runtime, **args)


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
    tool: Any,
    writer: Any = None,
    offload: bool = False,
    parent_call_id: str = "parent",
) -> CallContext:
    runtime = SimpleNamespace(
        tool_call_id=parent_call_id,
        tools=[tool],
        stream_writer=writer,
        config={},
        context=None,
    )
    request = SimpleNamespace(
        tool_call={"name": "run_code", "args": {}, "id": parent_call_id, "type": "tool_call"},
        tool=tool,
        runtime=runtime,
    )
    return CallContext(
        parent_call_id=parent_call_id,
        handler=handler,
        request=request,
        runtime=runtime,
        tools={tool.name: tool},
        stream_writer=writer,
        offload=offload,
        tool_names=tuple(bridge.registered_names({tool.name: tool})),
    )


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def test_deepagents_filesystem_wrappers_are_blocking_workers() -> None:
    tools = {
        tool.name: tool for tool in FilesystemMiddleware(backend=_BlockingWriteBackend()).tools
    }
    for name in ("execute", "read_file", "write_file", "edit_file"):
        assert _is_deepagents_blocking_wrapper(tools[name]) is True, name
        assert _is_blocking_worker_tool(tools[name]) is True, name
    # The read-only wrappers share the same body shape but stay on the origin loop.
    for name in ("ls", "glob", "grep"):
        assert _is_deepagents_blocking_wrapper(tools[name]) is False, name
        assert _is_blocking_worker_tool(tools[name]) is False, name


def test_genuinely_async_tool_is_not_a_blocking_worker() -> None:
    assert _is_deepagents_blocking_wrapper(_async_tool("read_file")) is False
    assert _is_blocking_worker_tool(_async_tool("read_file")) is False


def test_sync_only_tool_is_a_blocking_worker() -> None:
    tool = StructuredTool.from_function(
        func=lambda path="a": "ok", name="read_file", description="x"
    )
    assert _is_blocking_worker_tool(tool) is True


# --------------------------------------------------------------------------- #
# The repro: a thread-backed async write keeps the barrier across loops
# --------------------------------------------------------------------------- #
def test_async_thread_backed_write_keeps_barrier_after_origin_loop_closes() -> None:
    backend = _BlockingWriteBackend()
    tool = _filesystem_tool(backend, "write_file")
    assert _is_blocking_worker_tool(tool) is True

    bridge = _bridge()
    ctx = _context(bridge, _tool_node_handler, tool=tool)

    async def run_one() -> None:
        task = asyncio.ensure_future(
            bridge.dispatch(ctx, "write_file", {"file_path": "/a.txt", "content": "x"})
        )
        while not backend.started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The to_thread worker cannot be cancelled, so the write ticket is still
        # held -- and stays held when this loop closes below.
        assert bridge.sync_inflight == 1

    asyncio.run(run_one())
    assert backend.started.is_set()
    assert bridge.sync_inflight == 1

    async def run_two() -> None:
        task = asyncio.ensure_future(
            bridge.dispatch(ctx, "write_file", {"file_path": "/b.txt", "content": "y"})
        )
        await asyncio.sleep(0.2)
        # A fresh loop's write must not barge past the still-running one.
        assert backend.calls == 1
        backend.release.set()
        await asyncio.wait_for(task, timeout=5)
        assert backend.calls == 2

    asyncio.run(run_two())
    assert bridge.sync_inflight == 0


def test_genuinely_async_tool_runs_on_the_origin_loop() -> None:
    seen: dict[str, Any] = {}

    async def handler(request: Any) -> ToolMessage:
        seen["loop"] = asyncio.get_running_loop()
        return ToolMessage(
            content="ok", tool_call_id=request.tool_call["id"], name=request.tool_call["name"]
        )

    tool = _async_tool("read_file")
    bridge = _bridge()
    ctx = _context(bridge, handler, tool=tool)

    async def run() -> None:
        seen["origin"] = asyncio.get_running_loop()
        await bridge.dispatch(ctx, "read_file", {})

    asyncio.run(run())
    # A genuinely async tool is never forced onto another loop: its thread/loop
    # affinity (e.g. an MCP session) is preserved.
    assert seen["loop"] is seen["origin"]
    assert bridge.sync_inflight == 0


# --------------------------------------------------------------------------- #
# The run returns within budget while the execute worker keeps running
# --------------------------------------------------------------------------- #
@dataclass
class _RunCodeRequest:
    tool_call: dict[str, Any]
    tool: Any = None
    state: Any = None
    runtime: Any = None


@dataclass
class _Runtime:
    tools: list[Any] = field(default_factory=list)
    stream_writer: Any = None


def test_execute_worker_outlives_the_timeout_and_is_counted() -> None:
    backend = _BlockingExecuteBackend()
    tool = _filesystem_tool(backend, "execute")
    holder: dict[str, Any] = {}

    async def runner(*, code, tool_names, dispatch, cwd, limits, **kwargs):  # noqa: ARG001
        # Fire the blocking execute and return as a timed-out run would, without
        # waiting for the worker that cannot be cancelled.
        asyncio.ensure_future(dispatch("execute", {"command": "sleep 30"}))
        for _ in range(1_000):
            if backend.started.is_set():
                break
            await asyncio.sleep(0.005)
        return {
            "logs": [],
            "value": None,
            "error": {"kind": "timeout", "message": "run_code timed out"},
        }

    middleware = build_ptc_middleware(
        mode="both",
        project_root=Path("."),
        excluded_tools=[],
        require_approval=False,
        readonly=False,
        limits=PtcLimits(timeout_seconds=30.0),
        run_code=runner,
    )
    holder["bridge"] = middleware._bridge  # type: ignore[attr-defined]

    runtime = _Runtime(tools=[tool])
    request = _RunCodeRequest(
        tool_call={
            "name": "run_code",
            "args": {"code": "return 1", "intent": "go"},
            "id": "call-1",
            "type": "tool_call",
        },
        runtime=runtime,
    )

    budget = 5.0
    start = time.monotonic()
    message = asyncio.run(middleware.awrap_tool_call(request, _tool_node_handler))
    elapsed = time.monotonic() - start

    assert elapsed < budget, f"run blocked for {elapsed:.1f}s on a blocking execute"
    assert backend.started.is_set()
    # The count includes the async-path blocking worker, not only sync handlers.
    assert holder["bridge"].sync_inflight == 1
    payload = json.loads(message.content)
    assert "1 blocking tool call(s) may still be running" in payload.get("warning", "")

    backend.release.set()
    for _ in range(1_000):
        if holder["bridge"].sync_inflight == 0:
            break
        time.sleep(0.005)
    assert holder["bridge"].sync_inflight == 0
