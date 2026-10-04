"""Isolated one-shot PTC worker entry point.

Started as ``python -I -u -X utf8 <worker.py>`` (source checkout) or through the
frozen ``--synapse-ptc-worker`` entry route.  It reads one NDJSON ``init`` frame
from stdin, executes the model-authored ``async`` function body, and streams
``log`` / ``tool_call`` / ``result`` frames on stdout.  stdout carries the
protocol, so ``print`` and ``logging`` are redirected into ``log`` frames.

This is **not** an OS permission sandbox: model code runs with the same trust as
the user's shell.  The process boundary only isolates crashes and provides a
bounded, deterministic protocol.  stdout is reserved for the protocol; code
that writes raw bytes to fd 1 can still corrupt it (and the host will report a
protocol error rather than crash).

When the model code returns while a tool call is still in flight (for example an
``asyncio.create_task(tools.x())`` that was never awaited) the worker cancels
those calls and reports an explicit ``unfinished_calls`` error instead of a
false success.  Cancellation is cooperative: a synchronous host tool that
already started may still finish and is not rolled back.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import itertools
import json
import logging
import os
import sys
import textwrap
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

_MAX_LOG_LINE_CHARS = 8000
_MAX_ERROR_CHARS = 4000
#: Bound on how long the worker waits for cancelled in-flight tool tasks to
#: settle before reporting ``unfinished_calls`` and exiting.
_UNFINISHED_CLEANUP_SECONDS = 2.0

_DEFAULT_LIMITS: dict[str, Any] = {
    "timeout_seconds": 120.0,
    "max_calls": 100,
    "max_parallel": 8,
    "max_output_bytes": 64000,
    "max_result_bytes": 4000000,
    "max_code_bytes": 64000,
}


def _load_protocol() -> Any:
    """Import the shared protocol module in every deployment layout.

    * Packaged/frozen builds (PyInstaller) ship a compiled archive with no
      ``.py`` files on disk, so the module can only be imported through its
      package.  A normal package import (``synapse.runtime.ptc.worker``) takes
      the same route.
    * The source worker is started as a bare ``python -I worker.py`` script:
      isolated mode drops the script directory and ``PYTHONPATH`` from
      ``sys.path``, so the package cannot be imported and the sibling file is
      loaded by path instead -- which also avoids importing the package
      ``__init__`` (and the heavier ``process`` module it pulls in).
    """
    if getattr(sys, "frozen", False) or __package__:
        from synapse.runtime.ptc import protocol as protocol_module

        return protocol_module
    return _load_sibling_protocol()


def _sibling_protocol_path() -> Path:
    """Path of the sibling ``protocol.py`` used by the isolated source worker."""
    return Path(__file__).resolve().with_name("protocol.py")


def _load_sibling_protocol() -> Any:
    """Load the sibling ``protocol.py`` by path under a private module name.

    ``sys.path`` is not touched and the private name keeps the module from
    shadowing (or being shadowed by) an unrelated ``protocol`` module.
    """
    import importlib.util

    path = _sibling_protocol_path()
    spec = importlib.util.spec_from_file_location("_synapse_ptc_protocol", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load PTC protocol module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_protocol = _load_protocol()


def _short(text: str, limit: int = _MAX_ERROR_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


class ToolCallError(Exception):
    """Raised inside model code when a tool call fails.

    Mirrors the host bridge contract: ``.kind`` (``denied`` / ``unknown`` /
    ``tool_error`` / ``command`` / ``limit`` / ...), ``.name`` (and the
    ``.tool_name`` alias) and ``.message``.  Model code can branch on ``.kind``
    instead of parsing a flattened string.
    """

    def __init__(self, tool_name: str, message: str, *, kind: str = "tool_error") -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.name = tool_name
        self.kind = kind
        self.message = message


class _ProtocolWriter:
    """Serialises frames to the real stdout (the protocol channel)."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def send(self, frame: dict[str, Any]) -> None:
        try:
            data = _protocol.encode_frame(frame)
        except (TypeError, ValueError):
            return
        with self._lock:
            try:
                self._stream.write(data)
                self._stream.flush()
            except (BrokenPipeError, ValueError, OSError):
                return


class _LogStream(io.TextIOBase):
    """Text stream that turns writes into bounded ``log`` frames."""

    def __init__(self, send: Callable[[str], None]) -> None:
        self._send = send
        self._buffer = ""

    def writable(self) -> bool:
        return True

    def write(self, text: Any) -> int:  # type: ignore[override]
        if not isinstance(text, str):
            text = str(text)
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line)
        return len(text)

    def flush(self) -> None:
        if self._buffer:
            line, self._buffer = self._buffer, ""
            self._emit(line)

    def _emit(self, line: str) -> None:
        if not line:
            self._send("")
            return
        while line:
            self._send(line[:_MAX_LOG_LINE_CHARS])
            line = line[_MAX_LOG_LINE_CHARS:]


class _LogHandler(logging.Handler):
    """Routes stdlib ``logging`` records into ``log`` frames."""

    def __init__(self, send: Callable[[str], None]) -> None:
        super().__init__()
        self._send = send

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # noqa: BLE001 - a logging failure must not kill the run
            return
        self._send(message)


class _ToolBridge:
    """Correlates tool calls with the host over the NDJSON protocol."""

    def __init__(self, writer: _ProtocolWriter, max_parallel: int) -> None:
        self._writer = writer
        self._loop = asyncio.get_running_loop()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._counter = itertools.count(1)
        self._semaphore = asyncio.Semaphore(max(1, max_parallel))

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        async with self._semaphore:
            call_id = f"c{next(self._counter)}"
            future: asyncio.Future[dict[str, Any]] = self._loop.create_future()
            self._pending[call_id] = future
            try:
                self._writer.send(
                    {
                        "type": _protocol.FRAME_TOOL_CALL,
                        "id": call_id,
                        "name": name,
                        "arguments": arguments,
                    }
                )
                response = await future
            finally:
                self._pending.pop(call_id, None)
        if response.get("ok") is True:
            return response.get("value")
        message = "tool call failed"
        kind = "tool_error"
        error = response.get("error")
        if isinstance(error, Mapping):
            raw_kind = error.get("kind")
            if isinstance(raw_kind, str) and raw_kind:
                kind = raw_kind
            raw_name = error.get("name")
            if isinstance(raw_name, str) and raw_name:
                name = raw_name
            raw = error.get("message")
            if isinstance(raw, str) and raw:
                message = raw
        raise ToolCallError(name, message, kind=kind)

    def deliver(self, frame: dict[str, Any]) -> None:
        call_id = frame.get("id")
        if not isinstance(call_id, str):
            return
        future = self._pending.get(call_id)
        if future is None or future.done():
            return
        future.set_result(frame)

    def pending(self) -> list[asyncio.Future[dict[str, Any]]]:
        """The tool calls still awaiting a host response."""
        return [future for future in self._pending.values() if not future.done()]

    def cancel_pending(self) -> None:
        """Cancel every in-flight call so the run can settle deterministically."""
        for future in list(self._pending.values()):
            if not future.done():
                future.cancel()

    def fail_all(self, message: str) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(ToolCallError("", message, kind="host_error"))


class _Tools:
    """The ``tools`` object injected into model code."""

    def __init__(self, bridge: _ToolBridge) -> None:
        self._bridge = bridge

    async def call(self, name: str, arguments: Any = None) -> Any:
        if not isinstance(name, str) or not name:
            raise ToolCallError(
                str(name),
                "tool name must be a non-empty string",
                kind="invalid_arguments",
            )
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            raise ToolCallError(
                name,
                "tool arguments must be a mapping",
                kind="invalid_arguments",
            )
        return await self._bridge.call(name, dict(arguments))

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        async def _invoke(**kwargs: Any) -> Any:
            return await self._bridge.call(name, dict(kwargs))

        _invoke.__name__ = name
        return _invoke


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def _coerce_positive_float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def _normalise_limits(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "timeout_seconds": _coerce_positive_float(
            raw.get("timeout_seconds"), _DEFAULT_LIMITS["timeout_seconds"]
        ),
        "max_calls": _coerce_positive_int(
            raw.get("max_calls"), _DEFAULT_LIMITS["max_calls"]
        ),
        "max_parallel": _coerce_positive_int(
            raw.get("max_parallel"), _DEFAULT_LIMITS["max_parallel"]
        ),
        "max_output_bytes": _coerce_positive_int(
            raw.get("max_output_bytes"), _DEFAULT_LIMITS["max_output_bytes"]
        ),
        "max_result_bytes": _coerce_positive_int(
            raw.get("max_result_bytes"), _DEFAULT_LIMITS["max_result_bytes"]
        ),
        "max_code_bytes": _coerce_positive_int(
            raw.get("max_code_bytes"), _DEFAULT_LIMITS["max_code_bytes"]
        ),
    }


def _make_log_sender(writer: _ProtocolWriter, limits: Mapping[str, Any]) -> Callable[[str], None]:
    max_bytes = int(limits["max_output_bytes"])
    state = {"bytes": 0, "truncated": False}

    def send(message: str) -> None:
        if state["truncated"]:
            return
        size = len(message.encode("utf-8")) + 1
        if state["bytes"] + size > max_bytes:
            state["truncated"] = True
            writer.send(
                {
                    "type": _protocol.FRAME_LOG,
                    "stream": "stdout",
                    "message": "[output truncated: limit reached]",
                }
            )
            return
        state["bytes"] += size
        writer.send(
            {"type": _protocol.FRAME_LOG, "stream": "stdout", "message": message}
        )

    return send


def _compile_entry(code: str) -> tuple[Any, dict[str, str] | None]:
    """Wrap model code as an async function body and compile it."""
    body = textwrap.indent(code, "    ") if code.strip() else "    return None"
    source = (
        "async def __synapse_ptc_entry__(tools, asyncio, json, ToolCallError):\n"
        f"{body}\n"
    )
    try:
        compiled = compile(source, "<ptc>", "exec")
    except SyntaxError as exc:
        return None, {
            "kind": "syntax_error",
            "message": _short(f"{exc.msg} (line {exc.lineno})"),
        }
    namespace: dict[str, Any] = {}
    try:
        exec(compiled, namespace)
    except Exception as exc:  # noqa: BLE001 - surfaced to the host as a script error
        return None, {
            "kind": "syntax_error",
            "message": _short(f"{type(exc).__name__}: {exc}"),
        }
    return namespace.get("__synapse_ptc_entry__"), None


async def _execute(entry: Any, tools: _Tools) -> tuple[Any, dict[str, str] | None]:
    try:
        value = await entry(tools, asyncio, json, ToolCallError)
    except asyncio.CancelledError:
        raise
    except ToolCallError as exc:
        label = f"{exc.tool_name}: {exc.message}" if exc.tool_name else exc.message
        return None, {"kind": "tool_error", "message": _short(label)}
    except BaseException as exc:  # noqa: BLE001 - surfaced as a script error to the host
        return None, {
            "kind": "exception",
            "message": _short(f"{type(exc).__name__}: {exc}"),
        }
    return value, None


def _serialize_result(value: Any, max_result_bytes: int) -> tuple[Any, dict[str, str] | None]:
    try:
        encoded = _protocol.encode_json(value)
    except (TypeError, ValueError) as exc:
        return None, {
            "kind": "invalid_result",
            "message": _short(f"result is not JSON-serializable: {exc}"),
        }
    if len(encoded) > max_result_bytes:
        return None, {
            "kind": "result_too_large",
            "message": f"result exceeds max_result_bytes ({max_result_bytes})",
        }
    return value, None


async def _settle_unfinished(bridge: _ToolBridge) -> int:
    """Cancel tool work the script left in flight and count it.

    A script can return while a tool call is still awaiting the host -- most
    often ``asyncio.create_task(tools.x())`` without awaiting it.  Reporting a
    plain success there would be false, so the caller turns a non-zero count into
    an explicit ``unfinished_calls`` error.

    Detection is best effort: ``bridge.pending()`` catches calls that already
    reached the host, while ``asyncio.all_tasks()`` also catches a task that has
    not yet started.  Cancellation is cooperative -- a synchronous host tool
    that already began running may still complete and is *not* rolled back.
    """
    pending_calls = bridge.pending()
    current = asyncio.current_task()
    loop_tasks = [
        task for task in asyncio.all_tasks() if task is not current and not task.done()
    ]
    if not pending_calls and not loop_tasks:
        return 0
    count = max(len(pending_calls), len(loop_tasks))
    for task in loop_tasks:
        task.cancel()
    bridge.cancel_pending()
    if loop_tasks:
        with contextlib.suppress(Exception):
            await asyncio.wait(loop_tasks, timeout=_UNFINISHED_CLEANUP_SECONDS)
    return count


def _stdin_loop(
    stream: Any,
    loop: asyncio.AbstractEventLoop,
    deliver: Callable[[bytes], None],
    on_eof: Callable[[], None],
) -> None:
    """Daemon thread: forward stdin lines to the loop thread."""
    try:
        while True:
            line = stream.readline()
            if not line:
                break
            loop.call_soon_threadsafe(deliver, line)
    except Exception:  # noqa: BLE001 - the thread must never crash the process
        pass
    finally:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(on_eof)


async def _amain(
    *,
    stdin_stream: Any,
    writer: _ProtocolWriter,
    code: str,
    tool_names: list[str],
    limits: dict[str, Any],
) -> int:
    del tool_names  # the host enforces the tool-name whitelist
    send_log = _make_log_sender(writer, limits)
    log_stdout = _LogStream(send_log)
    log_stderr = _LogStream(send_log)
    original_stdout, original_stderr, original_stdin = sys.stdout, sys.stderr, sys.stdin
    sys.stdout = log_stdout
    sys.stderr = log_stderr
    # Reserve the real stdin for the protocol; model code cannot consume it.
    sys.stdin = open(os.devnull, encoding="utf-8")  # noqa: SIM115

    handler = _LogHandler(send_log)
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    def flush_logs() -> None:
        with contextlib.suppress(Exception):
            log_stdout.flush()
        with contextlib.suppress(Exception):
            log_stderr.flush()

    try:
        entry, error = _compile_entry(code)
        if error is not None:
            flush_logs()
            writer.send({"type": _protocol.FRAME_RESULT, "value": None, "error": error})
            return 0

        bridge = _ToolBridge(writer, int(limits["max_parallel"]))
        loop = asyncio.get_running_loop()

        def deliver(line: bytes) -> None:
            try:
                frame = _protocol.decode_frame(line)
            except Exception as exc:  # noqa: BLE001 - a bad host frame is fatal to this run
                bridge.fail_all(f"host sent a malformed frame: {exc}")
                return
            if frame.get("type") != _protocol.FRAME_TOOL_RESULT:
                bridge.fail_all("host sent an unexpected frame")
                return
            bridge.deliver(frame)

        def on_eof() -> None:
            bridge.fail_all("host closed the protocol channel")

        thread = threading.Thread(
            target=_stdin_loop,
            args=(stdin_stream, loop, deliver, on_eof),
            name="ptc-stdin",
            daemon=True,
        )
        thread.start()

        value, error = await _execute(entry, _Tools(bridge))
        if error is None:
            unfinished = await _settle_unfinished(bridge)
            if unfinished:
                value = None
                error = {
                    "kind": "unfinished_calls",
                    "message": _short(
                        f"script returned with {unfinished} tool call(s) still in "
                        "flight; they were cancelled, but a synchronous tool may "
                        "already be running and is not rolled back -- await every "
                        "call before returning"
                    ),
                }
        if error is None:
            value, error = _serialize_result(value, int(limits["max_result_bytes"]))
        flush_logs()
        writer.send({"type": _protocol.FRAME_RESULT, "value": value, "error": error})
        return 0
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
        with contextlib.suppress(Exception):
            sys.stdin.close()
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        sys.stdin = original_stdin


def _run() -> int:
    stdout_stream = getattr(sys.stdout, "buffer", sys.stdout)
    stdin_stream = getattr(sys.stdin, "buffer", sys.stdin)
    writer = _ProtocolWriter(stdout_stream)
    try:
        raw = stdin_stream.readline()
    except Exception:  # noqa: BLE001 - a closed stdin means nothing to do
        raw = b""
    if not raw:
        return 1
    try:
        init = _protocol.decode_frame(raw)
        code = init["code"]
        tool_names = init["tool_names"]
        raw_limits = init["limits"]
        if (
            not isinstance(code, str)
            or not isinstance(tool_names, list)
            or not isinstance(raw_limits, Mapping)
        ):
            raise ValueError("init frame has the wrong shape")
    except Exception as exc:  # noqa: BLE001 - report instead of crashing silently
        writer.send(
            {
                "type": _protocol.FRAME_RESULT,
                "value": None,
                "error": {
                    "kind": "protocol_error",
                    "message": _short(f"invalid init frame: {exc}"),
                },
            }
        )
        return 1
    limits = _normalise_limits(raw_limits)
    try:
        return asyncio.run(
            _amain(
                stdin_stream=stdin_stream,
                writer=writer,
                code=code,
                tool_names=tool_names,
                limits=limits,
            )
        )
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - report instead of crashing the host
        writer.send(
            {
                "type": _protocol.FRAME_RESULT,
                "value": None,
                "error": {
                    "kind": "internal_error",
                    "message": _short(f"{type(exc).__name__}: {exc}"),
                },
            }
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    """Entry point used by the frozen ``--synapse-ptc-worker`` route."""
    del argv  # the worker takes no command-line arguments
    try:
        return _run()
    except KeyboardInterrupt:
        return 130
    except Exception:  # noqa: BLE001 - never crash the entry point
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
