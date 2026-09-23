"""Subprocess entry point: run one workflow script and talk to the parent.

The worker holds the generated program, its orchestration checkpoint and the workflow
records.  It holds **no model client and no credentials**: every agent call is a message
to the parent, which executes it through the existing Agent Runtime and replies.  That is
what keeps tool policy, approvals and usage in the daemon.

stdout belongs to the protocol.  The real stream is captured before anything else runs and
``sys.stdout`` is pointed at stderr, so a library that prints cannot corrupt a frame.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import traceback
from collections.abc import Mapping
from typing import Any

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from synapse.workflows import protocol
from synapse.workflows.contract import CallRequest, WorkflowStatus
from synapse.workflows.errors import CallResultUncertainError, WorkflowError
from synapse.workflows.runner import build_runner
from synapse.workflows.store import WorkflowStore

__all__ = [
    "EXIT_BAD_CONFIG",
    "EXIT_FAILED",
    "EXIT_OK",
    "JsonChannel",
    "ParentApprovalGate",
    "ParentCallExecutor",
    "main",
]

EXIT_OK = 0
EXIT_FAILED = 2
EXIT_BAD_CONFIG = 3


class JsonChannel:
    """Request/response over line-delimited JSON, with one reader thread.

    The reader runs in a thread because the worker's event loop is busy running the
    script; replies are handed back to the loop as futures.  A closed pipe fails every
    pending request instead of leaving the script waiting forever.
    """

    def __init__(self, reader: Any, writer: Any, *, loop: asyncio.AbstractEventLoop) -> None:
        self._reader = reader
        self._writer = writer
        self._loop = loop
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = threading.Lock()
        self._counter = 0
        self._thread = threading.Thread(
            target=self._pump, name="workflow-channel", daemon=True
        )
        self._thread.start()

    def send(self, message: Mapping[str, Any]) -> None:
        """Write one message; a serialization problem is reported, never silent."""
        line = protocol.encode(message)
        with self._write_lock:
            self._writer.write(line + "\n")
            self._writer.flush()

    async def request(
        self, message: Mapping[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Send a request and await its reply."""
        self._counter += 1
        request_id = f"r{self._counter}"
        payload = dict(message)
        payload["id"] = request_id
        future: asyncio.Future[dict[str, Any]] = self._loop.create_future()
        self._pending[request_id] = future
        try:
            self.send(payload)
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    def _pump(self) -> None:
        try:
            for line in self._reader:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = protocol.decode(line)
                except protocol.ProtocolError:
                    continue
                request_id = message.get("id")
                future = self._pending.pop(str(request_id), None)
                if future is not None:
                    self._loop.call_soon_threadsafe(_resolve, future, message)
        finally:
            for future in list(self._pending.values()):
                self._loop.call_soon_threadsafe(
                    _resolve_error, future, "the host closed the workflow connection"
                )
            self._pending.clear()


def _resolve(future: asyncio.Future[dict[str, Any]], message: dict[str, Any]) -> None:
    if not future.done():
        future.set_result(message)


def _resolve_error(future: asyncio.Future[dict[str, Any]], message: str) -> None:
    if not future.done():
        future.set_exception(WorkflowError(message))


class ParentCallExecutor:
    """Send one agent call to the parent and wait for its result."""

    def __init__(self, channel: JsonChannel) -> None:
        self._channel = channel

    async def execute(self, request: CallRequest, *, correction: bool) -> Any:
        reply = await self._channel.request(
            {"type": protocol.KIND_CALL, "correction": bool(correction), **_call_payload(request)}
        )
        kind = reply.get("type")
        if kind == protocol.KIND_CALL_ERROR:
            # Rebuild the class the host raised: a schema/format failure drives the SDK's
            # single corrective attempt, while an ordinary failure must not be retried.
            raise protocol.workflow_error_from_wire(
                reply.get("error_type"),
                str(reply.get("error") or "the host could not run the call"),
            )
        if kind != protocol.KIND_CALL_RESULT:
            raise WorkflowError(f"unexpected reply to a call: {kind!r}")
        return reply.get("value")


class ParentApprovalGate:
    """Ask the host for a business approval and return its decision."""

    def __init__(self, channel: JsonChannel) -> None:
        self._channel = channel

    async def __call__(self, key: str, description: str) -> bool:
        reply = await self._channel.request(
            {"type": protocol.KIND_APPROVAL, "key": key, "description": description}
        )
        if reply.get("type") != protocol.KIND_APPROVAL_RESULT:
            raise WorkflowError("unexpected reply to an approval request")
        return bool(reply.get("granted"))


def _call_payload(request: CallRequest) -> dict[str, Any]:
    return protocol.call_request_payload(request)


def _run_is_uncertain(config: protocol.WorkerConfig) -> bool:
    """Whether the run's own record says an outcome is unknown.

    Read from the store rather than inferred from the exception type: a resume that refuses
    to continue because a call has no established outcome is the same situation as one that
    reaches that call while running, and the host must be told the same thing.
    """
    try:
        store = WorkflowStore(config.db_path)
    except Exception:  # noqa: BLE001 - an unreadable store cannot upgrade the report
        return False
    try:
        run = store.get_run(config.run_id)
        return run is not None and run.status is WorkflowStatus.UNCERTAIN
    except Exception:  # noqa: BLE001 - never fail the report itself
        return False
    finally:
        store.close()


async def run_worker(config: protocol.WorkerConfig, channel: JsonChannel) -> Any:
    """Open the store and the orchestration checkpoint, then run the script once.

    The checkpoint lives in its own file (see
    :func:`~synapse.workflows.protocol.checkpoint_path_for`): the checkpointer holds a
    write transaction while a task runs, so sharing the store's file would make the two
    writers block each other.
    """
    store = WorkflowStore(config.db_path)
    try:
        conn = await aiosqlite.connect(protocol.checkpoint_path_for(config.db_path))
        try:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA busy_timeout=5000")
            saver = AsyncSqliteSaver(conn)
            await saver.setup()
            runner = build_runner(
                run_id=config.run_id,
                thread_id=config.thread_id,
                script=config.script,
                store=store,
                limits=config.limits,
                checkpointer=saver,
                executor=ParentCallExecutor(channel),
                approval_gate=ParentApprovalGate(channel),
                # An empty registry is *deny-all*, not unrestricted: passing it through as
                # ``None`` would let a script name any role the parent never enabled.
                known_roles=config.known_roles,
            )
            return await runner.ainvoke(config.inputs)
        finally:
            await conn.close()
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    """Read the start message, run the script, report the outcome; exit code says which."""
    channel_out = sys.stdout
    # stdout is the protocol's: anything that prints must land on stderr instead.
    sys.stdout = sys.stderr
    first = sys.stdin.readline()
    try:
        config = protocol.WorkerConfig.from_message(protocol.decode(first))
    except protocol.ProtocolError as exc:
        _emit(channel_out, {"type": protocol.KIND_ERROR, "error": str(exc)})
        return EXIT_BAD_CONFIG

    async def scenario() -> Any:
        loop = asyncio.get_running_loop()
        channel = JsonChannel(sys.stdin, channel_out, loop=loop)
        return await run_worker(config, channel)

    try:
        value = asyncio.run(scenario())
    except CallResultUncertainError as exc:
        _emit(
            channel_out,
            {"type": protocol.KIND_ERROR, "error": str(exc), "uncertain": True},
        )
        return EXIT_FAILED
    except BaseException as exc:
        # stderr is the diagnostic channel (stdout is the protocol), and the parent keeps
        # only a bounded tail of it; the message itself stays free of payloads.
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        _emit(
            channel_out,
            {
                "type": protocol.KIND_ERROR,
                "error": f"{type(exc).__name__}: {exc}"[:2000],
                "uncertain": _run_is_uncertain(config),
            },
        )
        return EXIT_FAILED
    _emit(channel_out, {"type": protocol.KIND_RESULT, "value": value})
    return EXIT_OK


def _emit(stream: Any, message: Mapping[str, Any]) -> None:
    """Write one protocol line, falling back to stderr if it cannot be encoded."""
    try:
        stream.write(protocol.encode(message) + "\n")
        stream.flush()
    except protocol.ProtocolError as exc:
        sys.stderr.write(f"workflow worker could not report its outcome: {exc}\n")
        sys.stderr.flush()


if __name__ == "__main__":  # pragma: no cover - exercised through a subprocess
    raise SystemExit(main())
