"""Host side of the one-shot PTC (programmatic tool calling) runtime.

``run_code`` starts a *fresh* Python subprocess per invocation, speaks a bounded
newline-delimited JSON (NDJSON) protocol with it, and dispatches the model's
tool calls back into the host process.  The process boundary isolates crashes
and provides a deterministic, bounded protocol -- it is **not** an OS permission
sandbox: model code runs with the same trust as the user's shell.

Cancellation is cooperative only for host callbacks.  If a dispatch callback
ignores ``asyncio.CancelledError`` the run stops waiting for it after a short
deadline (the process tree is still killed), so cancelling a run is *not* a
rollback of a side effect a synchronous tool already performed.

``run_code`` owns neither the tool concurrency barrier nor tool permissions: it
only bounds how many dispatches are in flight.  Whatever approval, ordering or
rate limiting the tools require belongs in the middleware-supplied
``dispatch``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from .protocol import (
    FRAME_INIT,
    FRAME_LOG,
    FRAME_RESULT,
    FRAME_TOOL_CALL,
    FRAME_TOOL_RESULT,
    PtcLimits,
    decode_frame,
    encode_frame,
    encode_json,
)

_CLEANUP_TIMEOUT_SECONDS = 5.0
_STDERR_TAIL_BYTES = 8192
_FRAME_SLACK_BYTES = 65536
#: Appended to the log prefix when the combined output cap forces truncation.
_OUTPUT_LIMIT_MARKER = "[logs truncated: output limit reached]"
#: Headroom kept when bounding an error message against the output cap so the
#: surrounding ``{"error": {...}}`` envelope always fits.
_ERROR_ENVELOPE_HEADROOM = 64

# Minimal child environment: only what the OS needs to run Python plus locale
# hints.  API keys, tokens, PYTHONPATH and similar host secrets are never
# inherited.  This is not a security boundary (the worker has shell trust); it
# keeps runs deterministic and avoids leaking credentials into model output.
_ENV_ALLOWLIST_POSIX = frozenset(
    {
        "PATH",
        "HOME",
        "SHELL",
        "TMPDIR",
        "TZ",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "TERM",
    }
)
_ENV_ALLOWLIST_WINDOWS = frozenset(
    {
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "OS",
    }
)


def _worker_path() -> Path:
    """Absolute path to the sibling worker entry point."""
    return Path(__file__).resolve().with_name("worker.py")


def _worker_command() -> list[str]:
    """Return the argv used to start a worker in the current deployment."""
    if getattr(sys, "frozen", False):
        # The frozen entry point routes ``--synapse-ptc-worker`` to
        # ``synapse.runtime.ptc.worker:main``.
        return [sys.executable, "--synapse-ptc-worker"]
    # ``-I`` isolates the child (no PYTHONPATH, no user site, no cwd on
    # sys.path); ``-X utf8`` keeps text deterministic regardless of locale.
    return [sys.executable, "-I", "-u", "-X", "utf8", str(_worker_path())]


def _build_env() -> dict[str, str]:
    """Build a minimal child environment without inherited secrets."""
    if os.name == "nt":
        # CPython uppercases Windows environment keys, but match defensively so
        # an unusual casing still keeps ``SYSTEMROOT`` (required by Winsock).
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in _ENV_ALLOWLIST_WINDOWS
        }
    else:
        env = {
            key: value
            for key, value in os.environ.items()
            if key in _ENV_ALLOWLIST_POSIX
        }
    # ``-I`` ignores PYTHON* variables; set them for non-isolated deployments
    # (for example the frozen route) as a best effort.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _short(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _error(logs: list[str], kind: str, message: str) -> dict[str, Any]:
    return {
        "logs": list(logs),
        "value": None,
        "error": {"kind": kind, "message": _short(message)},
    }


def _exception_fields(exc: BaseException) -> dict[str, str]:
    """Extract the tool-error contract from a dispatch exception.

    A tool (or the bridge) may raise an exception exposing ``kind`` / ``name`` /
    ``tool_name`` / ``message``; those are forwarded verbatim so the sandbox can
    raise a ``ToolCallError`` that keeps its ``.kind`` and ``.name`` instead of a
    single flattened string.  Anything else degrades to a ``tool_error``.
    """
    kind = "tool_error"
    raw_kind = getattr(exc, "kind", None)
    if isinstance(raw_kind, str) and raw_kind:
        kind = raw_kind
    name = ""
    for attr in ("name", "tool_name"):
        raw_name = getattr(exc, attr, None)
        if isinstance(raw_name, str) and raw_name:
            name = raw_name
            break
    raw_message = getattr(exc, "message", None)
    if isinstance(raw_message, str) and raw_message:
        message = raw_message
    else:
        message = f"{type(exc).__name__}: {exc}"
    return {"kind": kind, "name": name, "message": message}


def _bound_message(message: str, cap: int) -> str:
    """Truncate ``message`` so its canonical JSON encoding fits ``cap`` bytes.

    Truncation is measured on the *encoded* form: a message full of control
    characters escapes to up to six bytes each, so a plain character count would
    let the envelope overflow the cap.
    """
    budget = max(0, cap - _ERROR_ENVELOPE_HEADROOM)
    if len(encode_json(message)) <= budget:
        return message
    text = message[:budget]
    while text and len(encode_json(text)) > budget:
        text = text[: len(text) // 2]
    return text


def _fit_output(
    logs: list[str],
    kind: str,
    message: str,
    cap: int,
) -> dict[str, Any]:
    """Build a bounded ``{logs, value: None, error}`` result that fits ``cap``.

    Keeps the longest log *prefix* that still fits alongside the error and a
    truncation marker, so a caller sees what happened before the cap was hit.
    """
    message = _bound_message(message, cap)
    error: dict[str, str] = {"kind": kind, "message": message}
    empty: dict[str, Any] = {"logs": [], "value": None, "error": error}
    if len(encode_json(empty)) > cap:
        error = {"kind": kind, "message": ""}
        empty = {"logs": [], "value": None, "error": error}
    if len(encode_json(empty)) > cap:
        # Even a bare envelope does not fit: emit the smallest explicit error.
        return {
            "logs": [],
            "value": None,
            "error": {"kind": "output_limit", "message": ""},
        }
    if not logs:
        return empty

    base = len(encode_json(empty))
    sizes = [len(encode_json(line)) for line in logs]
    prefix = [0]
    for size in sizes:
        prefix.append(prefix[-1] + size)
    marker_size = len(encode_json(_OUTPUT_LIMIT_MARKER))

    def whole(count: int, *, marker: bool) -> int:
        total = prefix[count]
        lines = count
        if marker:
            total += marker_size
            lines += 1
        if lines == 0:
            return base
        return base + total + (lines - 1)

    # The value was dropped; if every log line now fits, keep them all.
    if whole(len(logs), marker=False) <= cap:
        return {"logs": list(logs), "value": None, "error": error}
    if whole(0, marker=True) > cap:
        return empty
    low, high, best = 0, len(logs), 0
    while low <= high:
        mid = (low + high) // 2
        if whole(mid, marker=True) <= cap:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    candidate = {
        "logs": list(logs[:best]) + [_OUTPUT_LIMIT_MARKER],
        "value": None,
        "error": error,
    }
    if len(encode_json(candidate)) <= cap:
        return candidate
    return empty


def _finalize_output(result: dict[str, Any], cap: int) -> dict[str, Any]:
    """Enforce the combined ``max_output_bytes`` cap on a final result.

    ``max_output_bytes`` bounds the serialised ``{logs, value, error?}`` mapping
    (not just the logs), so a large ``value`` can never be smuggled into the
    model context through the per-result ``max_result_bytes`` budget.  When the
    normal encoding exceeds the cap the result becomes an explicit
    ``output_limit`` error -- never a silent success -- while preserving as much
    of the log prefix as fits.
    """
    if len(encode_json(result)) <= cap:
        return result
    raw_logs = result.get("logs")
    if isinstance(raw_logs, list):
        logs = [line for line in raw_logs if isinstance(line, str)]
    else:
        logs = []
    error = result.get("error")
    if isinstance(error, dict) and isinstance(error.get("kind"), str):
        kind = error["kind"]
        message = error.get("message") if isinstance(error.get("message"), str) else ""
    else:
        kind = "output_limit"
        message = (
            f"run_code output exceeded max_output_bytes ({cap}); "
            "value dropped and logs truncated"
        )
    return _fit_output(logs, kind, message, cap)


async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the worker and its descendants, then let the caller reap it."""
    pid = proc.pid
    if pid is None:
        return
    if os.name == "nt":
        killer: asyncio.subprocess.Process | None = None
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/T",
                "/F",
                "/PID",
                str(pid),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            killer = None
        if killer is not None:
            # Bound the wait and kill the killer itself on timeout: a wedged
            # ``taskkill`` must not outlive the run as a leaked process we no
            # longer track.
            try:
                await asyncio.wait_for(killer.wait(), timeout=_CLEANUP_TIMEOUT_SECONDS)
            except TimeoutError:
                with contextlib.suppress(Exception):
                    killer.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        killer.wait(), timeout=_CLEANUP_TIMEOUT_SECONDS
                    )
            except Exception:  # noqa: BLE001 - cleanup must not mask the real error
                pass
    else:
        # ``start_new_session=True`` made the worker a session and process-group
        # leader, so its pid *is* the group id.  Signal the group directly: the
        # leader may already have exited (a zombie awaiting ``proc.wait``) while
        # grandchildren remain, and ``getpgid(pid)`` would then fail.  Only the
        # group we created is ever signalled -- never an arbitrary pid.
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.kill()


class _Session:
    """State for a single worker process and its bounded protocol stream."""

    def __init__(
        self,
        *,
        code: str,
        tool_names: list[str],
        available_names: Sequence[str] | None = None,
        dispatch: Callable[[str, dict[str, Any]], Awaitable[Any]],
        cwd: Path,
        limits: PtcLimits,
    ) -> None:
        self._code = code
        self._tool_names = list(tool_names)
        self._tool_name_set = set(tool_names)
        # ``tool_names`` stays the registered host whitelist; ``available_names``
        # is the inventory the script self-inspects through ``tools.available``.
        # A ``None`` value keeps older callers working by mirroring ``tool_names``.
        self._available_names = (
            list(available_names) if available_names is not None else list(tool_names)
        )
        self._dispatch = dispatch
        self._cwd = cwd
        self._limits = limits
        self.logs: list[str] = []
        self._log_bytes = 0
        self._logs_truncated = False
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        # Dispatch tasks whose tool result has not yet been handed to the
        # transport, keyed by task, plus the call ids already responded to.  The
        # settlement guard in ``_handle_result`` uses these to tell a call still
        # in flight apart from one whose result is merely being drained.
        self._task_calls: dict[asyncio.Task[None], str] = {}
        self._settled_calls: set[str] = set()
        self._semaphore = asyncio.Semaphore(limits.max_parallel)
        self._stdin_lock = asyncio.Lock()
        self._calls = 0
        self._seen_ids: set[str] = set()
        self._closed = False
        self._dispatch_stopped = False
        self._result_seen = False
        self._value: Any = None
        self._error: dict[str, str] | None = None
        self._failure: tuple[str, str] | None = None
        self._stderr_tail = bytearray()
        self._stderr_task: asyncio.Task[None] | None = None

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> dict[str, Any]:
        try:
            await self._spawn()
        except OSError as exc:
            return _error(self.logs, "spawn_failed", f"failed to start PTC worker: {exc}")
        try:
            await self._send_init()
            await self._read_loop()
        finally:
            await self._stop_dispatch()
        return self._finalize(self._assemble())

    def _assemble(self) -> dict[str, Any]:
        """Build the raw ``{logs, value, error?}`` result before the outer cap."""
        if self._failure is not None:
            kind, message = self._failure
            return _error(self.logs, kind, message)
        if not self._result_seen:
            return _error(self.logs, "worker_crashed", self._crash_message())
        if self._error is not None:
            return {"logs": list(self.logs), "value": None, "error": self._error}
        return {"logs": list(self.logs), "value": self._value}

    def _finalize(self, result: dict[str, Any]) -> dict[str, Any]:
        return _finalize_output(result, self._limits.max_output_bytes)

    async def aclose(self) -> None:
        """Idempotent teardown: stop dispatch, kill the tree, drain stderr."""
        if self._closed:
            return
        self._closed = True
        await self._stop_dispatch()
        await self._terminate_process()
        await self._cancel_stderr()

    # -- spawn / protocol IO ----------------------------------------------

    def _max_frame_bytes(self) -> int:
        return (
            max(
                self._limits.max_result_bytes,
                self._limits.max_output_bytes,
                self._limits.max_code_bytes,
            )
            + _FRAME_SLACK_BYTES
        )

    async def _spawn(self) -> None:
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        self._proc = await asyncio.create_subprocess_exec(
            *_worker_command(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._cwd),
            env=_build_env(),
            limit=self._max_frame_bytes(),
            **kwargs,
        )
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())

    async def _send_init(self) -> None:
        await self._send_frame(
            {
                "type": FRAME_INIT,
                "code": self._code,
                "tool_names": list(self._tool_names),
                "available_names": list(self._available_names),
                "limits": self._limits.as_frame(),
            }
        )

    async def _send_frame(self, frame: dict[str, Any]) -> None:
        if self._closed:
            return
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        try:
            data = encode_frame(frame)
        except (TypeError, ValueError):
            return
        async with self._stdin_lock:
            try:
                proc.stdin.write(data)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, RuntimeError, OSError):
                return

    async def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            self._fail("protocol_error", "worker stdout is unavailable")
            return
        stdout = proc.stdout
        max_frame = self._max_frame_bytes()
        while True:
            try:
                line = await stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                self._fail("protocol_error", f"worker frame exceeded the limit: {exc}")
                return
            if not line:
                return
            if len(line) > max_frame:
                self._fail("protocol_error", "worker frame exceeded the maximum size")
                return
            try:
                frame = decode_frame(line)
            except (ValueError, UnicodeDecodeError) as exc:
                self._fail("protocol_error", f"invalid worker frame: {exc}")
                return
            if await self._handle_frame(frame):
                return

    async def _handle_frame(self, frame: dict[str, Any]) -> bool:
        frame_type = frame.get("type")
        if not isinstance(frame_type, str):
            self._fail("protocol_error", "worker frame is missing a string 'type'")
            return True
        if frame_type == FRAME_LOG:
            return self._handle_log(frame)
        if frame_type == FRAME_TOOL_CALL:
            return await self._handle_tool_call(frame)
        if frame_type == FRAME_RESULT:
            return self._handle_result(frame)
        self._fail("protocol_error", f"unknown worker frame type {frame_type!r}")
        return True

    def _handle_log(self, frame: dict[str, Any]) -> bool:
        message = frame.get("message")
        if not isinstance(message, str):
            self._fail("protocol_error", "log frame 'message' must be a string")
            return True
        self._append_log(message)
        return False

    def _append_log(self, message: str) -> None:
        if self._logs_truncated:
            return
        size = len(message.encode("utf-8")) + 1
        if self._log_bytes + size > self._limits.max_output_bytes:
            self._logs_truncated = True
            self.logs.append("[logs truncated: output limit reached]")
            return
        self._log_bytes += size
        self.logs.append(message)

    async def _handle_tool_call(self, frame: dict[str, Any]) -> bool:
        call_id = frame.get("id")
        name = frame.get("name")
        arguments = frame.get("arguments")
        if not isinstance(call_id, str) or not call_id:
            self._fail("protocol_error", "tool_call 'id' must be a non-empty string")
            return True
        if call_id in self._seen_ids:
            self._fail("protocol_error", f"duplicate tool_call id {call_id!r}")
            return True
        if not isinstance(arguments, dict):
            self._fail(
                "protocol_error",
                f"tool_call {call_id!r} 'arguments' must be an object",
            )
            return True
        # Every ``tool_call`` frame counts against the budget *before* it is
        # recorded, including an unknown or malformed-but-valid name.  Otherwise
        # a script could spam distinct unknown names to grow ``_seen_ids``
        # without bound and never trip ``max_calls``.  Over budget is terminal:
        # it is not a catchable per-call error the script could keep going past.
        if self._calls >= self._limits.max_calls:
            self._fail(
                "max_calls_exceeded",
                f"tool call budget exceeded (max_calls={self._limits.max_calls})",
            )
            return True
        self._calls += 1
        self._seen_ids.add(call_id)
        if not isinstance(name, str) or name not in self._tool_name_set:
            label = name if isinstance(name, str) else ""
            await self._send_tool_error(
                call_id,
                label,
                f"unknown tool {name!r}",
                kind="unknown",
            )
            return False
        # Bound the number of pending dispatch tasks: acquire a slot before
        # creating the task, and let the done callback release it.
        await self._semaphore.acquire()
        task = asyncio.ensure_future(self._dispatch_one(call_id, name, arguments))
        self._tasks.add(task)
        self._task_calls[task] = call_id
        task.add_done_callback(self._on_dispatch_done)
        return False

    def _on_dispatch_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        self._task_calls.pop(task, None)
        self._semaphore.release()

    async def _dispatch_one(
        self,
        call_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> None:
        try:
            value = await self._dispatch(name, dict(arguments))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - tool failure is reported to the script
            self._settle(call_id)
            fields = _exception_fields(exc)
            await self._send_tool_error(
                call_id,
                fields["name"] or name,
                fields["message"],
                kind=fields["kind"],
            )
            return
        # The tool work is done; only the response frame may still be draining.
        # Mark the call settled before that await so the settlement guard never
        # mistakes a completed call for one still in flight.
        self._settle(call_id)
        try:
            encoded = encode_json(value)
        except (TypeError, ValueError) as exc:
            await self._send_tool_error(
                call_id,
                name,
                f"tool result is not JSON-serializable: {exc}",
                kind="tool_error",
            )
            return
        if len(encoded) > self._limits.max_result_bytes:
            await self._send_tool_error(
                call_id,
                name,
                f"tool result exceeds max_result_bytes ({self._limits.max_result_bytes})",
                kind="limit",
            )
            return
        await self._send_frame(
            {"type": FRAME_TOOL_RESULT, "id": call_id, "ok": True, "value": value}
        )

    async def _send_tool_error(
        self,
        call_id: str,
        tool_name: str,
        message: str,
        *,
        kind: str = "tool_error",
    ) -> None:
        await self._send_frame(
            {
                "type": FRAME_TOOL_RESULT,
                "id": call_id,
                "ok": False,
                "error": {
                    "kind": kind,
                    "name": tool_name,
                    "message": _short(message),
                },
            }
        )

    def _settle(self, call_id: str) -> None:
        """Record that a dispatch's tool result has been handed to the transport.

        A dispatch task stays in ``self._tasks`` until its done callback runs, so
        ``_handle_result`` must not count a call whose tool already completed and
        whose response frame is merely draining as still in flight.
        """
        self._settled_calls.add(call_id)

    def _unfinished_calls(self) -> int:
        """Count dispatches that are genuinely still in flight.

        Mirrors the worker's settlement guard (``_settle_unfinished``): a call
        whose tool has not returned yet -- or has not even started -- is
        unfinished, while a completed call whose response is being drained is
        not.
        """
        return sum(
            1
            for task, call_id in self._task_calls.items()
            if not task.done() and call_id not in self._settled_calls
        )

    def _handle_result(self, frame: dict[str, Any]) -> bool:
        self._result_seen = True
        self._value = frame.get("value")
        error = frame.get("error")
        if error is not None:
            if not isinstance(error, dict):
                self._fail("protocol_error", "result 'error' must be an object")
                return True
            kind = error.get("kind")
            message = error.get("message")
            if not isinstance(kind, str) or not isinstance(message, str):
                self._fail(
                    "protocol_error",
                    "result error needs string 'kind' and 'message'",
                )
                return True
            self._error = {"kind": kind, "message": _short(message)}
            return True
        # Success frame.  Model code can bypass the worker's own bookkeeping by
        # writing a raw ``result`` frame to fd 1 while a host dispatch is still
        # in flight.  Accepting it would report a false success for a tool that
        # is then cancelled, so mirror the worker's settlement guard: a success
        # frame with unsettled dispatches becomes a terminal ``unfinished_calls``.
        unfinished = self._unfinished_calls()
        if unfinished:
            self._fail(
                "unfinished_calls",
                f"script returned with {unfinished} tool call(s) still in flight; "
                "they were cancelled, but a synchronous tool may already be "
                "running and is not rolled back -- await every call before "
                "returning",
            )
        return True

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        stderr = proc.stderr
        try:
            while True:
                chunk = await stderr.read(4096)
                if not chunk:
                    return
                room = _STDERR_TAIL_BYTES - len(self._stderr_tail)
                if room > 0:
                    self._stderr_tail.extend(chunk[:room])
        except asyncio.CancelledError:
            return
        except (ValueError, OSError):
            return

    # -- teardown ----------------------------------------------------------

    def _fail(self, kind: str, message: str) -> None:
        if self._failure is None:
            self._failure = (kind, message)

    async def _stop_dispatch(self) -> None:
        if self._dispatch_stopped:
            return
        self._dispatch_stopped = True
        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            with contextlib.suppress(Exception):
                await asyncio.wait(pending, timeout=_CLEANUP_TIMEOUT_SECONDS)
        for task in pending:
            if task.done() and not task.cancelled():
                with contextlib.suppress(Exception):
                    task.exception()

    async def _terminate_process(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        if os.name == "nt":
            # ``taskkill /T`` walks the *live* parent/child tree, so it can only
            # reach descendants while the worker itself is still running; skip it
            # once the worker is reaped so we never signal a recycled pid.
            if proc.returncode is None:
                await _kill_process_tree(proc)
        else:
            # The worker is its own process-group leader (``start_new_session``),
            # so kill the group even after the leader exits -- grandchildren that
            # outlived it (for example after ``os._exit``) must not leak.
            await _kill_process_tree(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=_CLEANUP_TIMEOUT_SECONDS)

    async def _cancel_stderr(self) -> None:
        task = self._stderr_task
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait({task}, timeout=_CLEANUP_TIMEOUT_SECONDS)

    def _crash_message(self) -> str:
        code = self._proc.returncode if self._proc is not None else None
        tail = bytes(self._stderr_tail).decode("utf-8", "replace").strip()
        message = f"PTC worker exited without a result frame (exit code {code})"
        if tail:
            message += f": {tail}"
        return message


async def run_code(
    *,
    code: str,
    tool_names: list[str],
    available_names: Sequence[str] | None = None,
    dispatch: Callable[[str, dict[str, Any]], Awaitable[Any]],
    cwd: Path,
    limits: PtcLimits,
) -> dict[str, Any]:
    """Run ``code`` as the body of an async function in a fresh subprocess.

    Returns ``{"logs": list[str], "value": JSON | None, "error"?: {...}}``.
    ``code`` is the body of an ``async`` function: top-level ``await`` and
    ``return`` are valid, and the injected names are ``tools``, ``asyncio``,
    ``json`` and ``ToolCallError``.  ``tool_names`` stays the registered host
    whitelist, while ``available_names`` is the inventory the script
    self-inspects through ``tools.available``; a ``None`` value (the default)
    mirrors ``tool_names`` so existing callers are unchanged.  A user cancellation
    (``asyncio.CancelledError``) always propagates; it is never converted into
    an error dict.

    The returned mapping is bounded by ``limits.max_output_bytes`` as a whole:
    if ``{logs, value, error?}`` would exceed it, the run fails with an explicit
    ``output_limit`` error (dropping the value and keeping the log prefix that
    fits) rather than smuggling a large ``value`` past the per-result
    ``max_result_bytes`` budget into the model context.  A script that returns
    while a tool call is still in flight fails with ``unfinished_calls`` instead
    of a false success.
    """
    if not isinstance(code, str):
        raise TypeError("code must be a str")
    if not isinstance(limits, PtcLimits):
        raise TypeError("limits must be a PtcLimits instance")
    code_size = len(code.encode("utf-8"))
    if code_size > limits.max_code_bytes:
        return _finalize_output(
            _error(
                [],
                "code_too_large",
                f"code is {code_size} bytes, limit is {limits.max_code_bytes}",
            ),
            limits.max_output_bytes,
        )
    session = _Session(
        code=code,
        tool_names=list(tool_names),
        available_names=available_names,
        dispatch=dispatch,
        cwd=Path(cwd),
        limits=limits,
    )
    try:
        return await asyncio.wait_for(session.run(), timeout=limits.timeout_seconds)
    except TimeoutError:
        return _finalize_output(
            _error(
                session.logs,
                "timeout",
                f"PTC run exceeded {limits.timeout_seconds:g}s",
            ),
            limits.max_output_bytes,
        )
    except Exception as exc:  # noqa: BLE001 - boundary: report instead of crashing the host
        return _finalize_output(
            _error(session.logs, "internal_error", f"{type(exc).__name__}: {exc}"),
            limits.max_output_bytes,
        )
    finally:
        await session.aclose()
