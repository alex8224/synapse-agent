"""Bridge between the PTC Python subprocess runtime and the host's tool runtime.

``process.run_code`` executes model-authored Python in a worker. That code calls
tools through an async ``dispatch(name, args)`` callable this module provides.
The bridge is the policy + plumbing layer between those subprocess calls and the
real ``BaseTool`` implementations the agent registered.

Responsibilities:

* resolve each child call against the *registered* tools on the live runtime,
* enforce the PTC policy (recursion/stateful/Command tools denied, ``excluded``
  re-validated, approval-gated tools refused in approval mode),
* schedule child calls fairly (parallel reads, exclusive writes),
* re-enter the LangGraph tool node through the captured handler -- never
  ``BaseTool.ainvoke`` directly, so argument injection and validation still run,
* rewrite the runtime's ``tool_call_id`` to the nested id so a tool that reads
  ``runtime.tool_call_id`` sees the child, not the parent,
* convert a child ``ToolMessage`` into the shared ``ToolEnvelope`` contract and
  emit bounded ``ptc_tool`` stream events for the UI.

Child results never enter graph state: only the parent ``run_code`` ``ToolMessage``
does. A tool that returns a graph ``Command`` is a hard error -- its state update
is intentionally *not* merged (documented on :class:`ToolCallError`).

Offload boundary
----------------

The synchronous ``run_code`` path runs the blocking tool node on a *daemon*
thread from a bounded, shared pool (``_SYNC_DISPATCH_POOL``) rather than
``asyncio.to_thread``. ``asyncio.to_thread`` submits to the running loop's
default executor, which ``asyncio.run`` joins in ``shutdown_default_executor``,
so a handler that ignores cancellation (``time.sleep``) would stall the run past
its timeout. These daemon threads are never joined: a timed-out run returns
immediately, the run result carries a warning that a sync handler may still be
running, and the scheduler ticket is held until the real handler finishes so a
write lock is not released while a background write is still in flight.

Because the scheduler is shared across loops, that background write still blocks
a later ``run_code`` even though the first run's loop has closed: the ticket is
released from the daemon future's completion callback with
``FairReadWriteScheduler.release_nowait`` -- never by scheduling on the (possibly
dead) origin loop. The daemon pool queue is bounded so a stream of cancelled runs
whose handlers cannot be killed cannot pile up unbounded queued work. The
caller's ``contextvars`` are captured before submission and re-entered on the
worker thread, so an injected tool runtime and trace context survive the hop.

The async path re-enters the graph's own handler too, but a tool whose work runs
on a thread must *not* be awaited on the origin loop: ``asyncio.run`` teardown
cancels a shielded child task while its ``to_thread`` worker keeps running, so
the task's completion callback would release the write ticket early and let the
next turn's write overlap. Instead, a *blocking worker* tool -- a sync-only
``BaseTool`` (``coroutine is None``) or a deepagents filesystem wrapper whose
body is ``asyncio.to_thread`` over a sync backend -- is dispatched to the same
daemon pool, where the handler is driven by an independent ``asyncio.run`` loop.
That loop is torn down with its own default-executor join, so the daemon future
only completes after the real call (and its worker thread) finished, and the
origin loop's teardown can no longer cancel it. The ticket is released from the
daemon future's callback with ``release_nowait``, exactly like the sync path.

A genuinely async tool is awaited on the origin loop and cancelled/cleaned up
normally; it is never forced onto another loop, which would break the thread or
loop affinity of a real async client (e.g. an MCP session). An *unknown* async
tool that internally spawns its own threads offers no such guarantee -- the
bridge cannot see inside it -- so its ticket may still be released while that
private work runs; this is documented rather than guessed at.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import contextvars
import copy
import dataclasses
import hashlib
import inspect
import json
import re
import threading
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langgraph.types import Command

from synapse.runtime.ptc.scheduler import READ, WRITE, FairReadWriteScheduler
from synapse.runtime.tool_contract import SESSION_SCOPE, tool_contract

if TYPE_CHECKING:
    from synapse.runtime.ptc.protocol import PtcLimits

RUN_CODE = "run_code"

#: Tools that must never be orchestrated from generated code. ``run_code`` is
#: recursion; the rest mutate session state or return graph ``Command`` objects
#: that cannot be safely merged from a child call. The ``*_async_task`` names are
#: the deepagents async-subagent tools (``deepagents.middleware.async_subagents``):
#: each writes ``async_tasks`` in its ``Command``. Tools that are registered but
#: carry no contract (e.g. MCP) are *not* denied -- they "autopass" unless approval
#: mode forbids an unknown contract.
DENIED_TOOLS: frozenset[str] = frozenset(
    {
        RUN_CODE,
        "task",
        "write_todos",
        "create_goal",
        "update_goal",
        "start_async_task",
        "check_async_task",
        "update_async_task",
        "cancel_async_task",
        "list_async_tasks",
    }
)

#: Upper bound on the shared daemon-thread pool used for blocking dispatch. The
#: pool is shared and never joined, so this is the hard ceiling on threads a
#: runaway handler (sync tool or thread-backed async wrapper) can hold -- never
#: an unbounded fan-out.
_MAX_SYNC_DISPATCH_WORKERS = 32

#: Upper bound on *queued* (not yet running) blocking dispatches. A blocking handler
#: cannot be killed, so without a cap repeated cancelled runs would accumulate
#: queued work forever; an over-limit submit fails fast instead.
_MAX_SYNC_DISPATCH_PENDING = _MAX_SYNC_DISPATCH_WORKERS * 2

_SECRET_MARKERS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "api-key",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "authorization",
    "auth_header",
    "cookie",
    "private_key",
    "session_key",
)
_CONTENT_MARKERS: tuple[str, ...] = (
    "content",
    "new_string",
    "old_string",
    "text",
    "body",
    "payload",
    "prompt",
    "code",
    "script",
)

_MAX_EVENT_ARG_KEYS = 12
_MAX_EVENT_VALUE_CHARS = 200
_MAX_EVENT_ARGS_CHARS = 1_200
_MAX_PREVIEW_CHARS = 400

#: Upper bound on the unavailable tool names a pre-flight scan reports. The
#: caller surfaces the first few; the cap keeps the scan's result bounded even
#: for pathological code.
_MAX_PREFLIGHT_NAMES = 20

#: Total-character budget for :meth:`PtcBridge.denial_message`. The available-tool
#: listing is truncated (with an explicit "and N more") to stay within it.
_MAX_DENIAL_MESSAGE_CHARS = 400

#: ``tools.<identifier>`` attribute access in generated code.
_TOOLS_ATTR_RE = re.compile(r"\btools\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)")

#: The generic escape hatch ``tools.call(`` -- its first argument may be a literal.
_TOOLS_CALL_OPEN_RE = re.compile(r"\btools\s*\.\s*call\s*\(")

#: ``getattr(tools, ...)``: the tool name is computed, so the scan declines to guess.
_GETATTR_TOOLS_RE = re.compile(r"\bgetattr\s*\(\s*tools\b")

#: Attributes on the ``tools`` object that are *not* tool names.
_TOOLS_NON_TOOL_ATTRS: frozenset[str] = frozenset({"call", "available"})


class ToolCallError(Exception):
    """A child tool call that model code should observe as a failure.

    ``kind`` is one of ``denied`` (policy), ``unknown`` (not registered),
    ``tool_error`` (the tool returned an error ``ToolMessage``), ``command``
    (the tool returned a graph ``Command``) or ``limit``.

    The public error surface is ``.kind``, ``.name``, ``.tool_name`` (an alias of
    ``.name``) and ``.message``; the worker mirrors this so model code can catch
    one shape regardless of where the failure originated.

    When ``kind == "command"`` the tool's state update is deliberately **not**
    merged into the graph: a child call may not mutate agent state, so the
    bridge fails the call instead of applying a partial command.
    """

    def __init__(self, message: str, *, kind: str = "tool_error", name: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.name = name
        self.message = message

    @property
    def tool_name(self) -> str:
        """Alias of :attr:`name` for callers that read ``tool_name``."""
        return self.name

    def to_payload(self) -> dict[str, Any]:
        """A JSON-safe ``{kind, name, tool_name, message}`` representation."""
        return {
            "kind": self.kind,
            "name": self.name,
            "tool_name": self.name,
            "message": self.message,
        }


class _DispatchQueueFull(RuntimeError):
    """Raised when the bounded sync dispatch queue is saturated.

    A blocking handler cannot be killed, so a worker may never free up. Bounding
    the queue turns a runaway stream of cancelled runs into an explicit refusal
    for new sync calls instead of unbounded queued work.
    """


class _DaemonDispatchPool:
    """A bounded pool of daemon threads for blocking tool handlers.

    ``asyncio.to_thread`` would use the running loop's default executor, which
    ``asyncio.run`` joins on shutdown; these threads are daemon and the pool is
    shared and never joined, so a timed-out run returns immediately. Threads are
    created lazily up to ``max_workers`` and then reused, so a runaway handler
    can never spawn an unbounded number of threads. The same pool serves both the
    sync ``run_code`` path and the async path's blocking workers, so the ceiling
    is shared.

    The work queue is *bounded*: repeated cancelled runs whose handlers cannot be
    killed would otherwise pile up queued work forever. A cancelled submission is
    dropped from the queue on the next submit, and an over-limit submit fails
    fast (never blocking the event loop) instead of growing without bound.
    """

    def __init__(self, max_workers: int, max_pending: int | None = None) -> None:
        self._max_workers = max(1, int(max_workers))
        self._max_pending = max(1, int(max_pending) if max_pending else self._max_workers)
        self._lock = threading.Lock()
        self._work_ready = threading.Condition(self._lock)
        self._work: deque[
            tuple[concurrent.futures.Future[Any], Callable[..., Any], tuple[Any, ...]]
        ] = deque()
        self._started = 0

    def submit(self, fn: Callable[..., Any], *args: Any) -> concurrent.futures.Future[Any]:
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        with self._work_ready:
            self._drop_cancelled_locked()
            if len(self._work) >= self._max_pending:
                raise _DispatchQueueFull(
                    f"sync dispatch queue is full ({self._max_pending} pending)"
                )
            self._work.append((future, fn, args))
            self._ensure_worker_locked()
            self._work_ready.notify()
        return future

    def _drop_cancelled_locked(self) -> None:
        """Remove queued submissions whose future was already cancelled."""
        if any(item[0].cancelled() for item in self._work):
            self._work = deque(item for item in self._work if not item[0].cancelled())

    def _ensure_worker_locked(self) -> None:
        if self._started >= self._max_workers:
            return
        self._started += 1
        threading.Thread(target=self._worker, name="ptc-sync", daemon=True).start()

    def _worker(self) -> None:
        while True:
            with self._work_ready:
                while not self._work:
                    self._work_ready.wait()
                future, fn, args = self._work.popleft()
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn(*args)
            except BaseException as exc:  # noqa: BLE001 - delivered to the awaiting coroutine
                future.set_exception(exc)
            else:
                future.set_result(result)


_SYNC_DISPATCH_POOL = _DaemonDispatchPool(
    _MAX_SYNC_DISPATCH_WORKERS, _MAX_SYNC_DISPATCH_PENDING
)


def _retrieve_quietly(future: asyncio.Future[Any]) -> None:
    """Consume a completed asyncio future's outcome so a late error is not "never retrieved"."""
    if future.cancelled():
        return
    with contextlib.suppress(BaseException):
        future.exception()


def _retrieve_concurrent_quietly(future: concurrent.futures.Future[Any]) -> None:
    """Consume a completed concurrent future's outcome so a late error is not lost."""
    if future.cancelled():
        return
    with contextlib.suppress(BaseException):
        future.exception()


def _is_blocking_sync_tool(tool: Any) -> bool:
    """Whether invoking ``tool`` runs a sync call that cancellation cannot stop.

    A ``BaseTool`` that only has a sync ``func`` (``coroutine is None``) is run
    on a thread by LangChain's ``to_thread``; cancelling the awaiting coroutine
    leaves that thread running, so the scheduler ticket must be held until the
    call really returns. A tool whose ``coroutine`` only wraps a thread is caught
    separately by :func:`_is_deepagents_blocking_wrapper`.
    """
    return getattr(tool, "coroutine", None) is None


#: The module that defines the deepagents filesystem tool wrappers.
_DEEPAGENTS_FS_MODULE = "deepagents.middleware.filesystem"

#: The deepagents filesystem wrappers whose ``coroutine`` only *looks* async:
#: each body awaits ``backend.aread``/``awrite``/``aedit``/``aexecute``, which is
#: itself ``asyncio.to_thread`` over a synchronous backend. The other wrappers
#: (``ls``/``glob``/``grep``) share the same body shape but are read-only and are
#: deliberately left on the origin loop.
_DEEPAGENTS_BLOCKING_FS_TOOLS = frozenset({"execute", "read_file", "write_file", "edit_file"})


def _is_deepagents_blocking_wrapper(tool: Any) -> bool:
    """Whether ``tool`` is a deepagents filesystem wrapper backed by a thread.

    These tools *have* a ``coroutine`` (so :func:`_is_blocking_sync_tool` calls
    them async) but their body is ``to_thread`` over a sync backend, so the same
    non-killable-worker risk applies. Detection is deliberately narrow -- the
    wrapper's defining module plus its name -- so a genuinely async tool that
    merely shares a name is never misclassified and never forced onto another
    loop.
    """
    coroutine = getattr(tool, "coroutine", None)
    if coroutine is None:
        return False
    if getattr(coroutine, "__module__", None) != _DEEPAGENTS_FS_MODULE:
        return False
    return str(getattr(tool, "name", "") or "") in _DEEPAGENTS_BLOCKING_FS_TOOLS


def _is_blocking_worker_tool(tool: Any) -> bool:
    """Whether invoking ``tool`` runs work that cancellation cannot stop.

    Covers a sync-only ``BaseTool`` and the deepagents filesystem wrappers. A
    genuinely async tool is *not* included: it unwinds on cancellation and its
    loop/thread affinity must be preserved (never forced onto another loop).
    """
    return _is_blocking_sync_tool(tool) or _is_deepagents_blocking_wrapper(tool)


def _run_handler_in_worker(handler: Callable[..., Any], request: Any) -> Any:
    """Invoke ``handler`` on a daemon worker thread.

    The synchronous ``run_code`` path passes a sync handler and gets its return
    value directly. The async path passes the graph's async handler; it is driven
    on a *fresh* ``asyncio.run`` loop owned by this worker, never the origin loop.
    That loop is torn down with its own default-executor join, so it only returns
    once the blocking worker (LangChain's ``to_thread`` for a sync tool,
    ``backend.aexecute`` for a deepagents FS tool) really finished -- and the
    origin loop's teardown can no longer cancel it and release the scheduler
    ticket early.
    """
    result = handler(request)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def content_to_text(content: Any) -> str:
    """Flatten a ``str``/block-list message content into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                text = block.get("text")
                if text is not None:
                    parts.append(str(text))
                else:
                    parts.append(json.dumps(block, default=str))
            else:
                parts.append(str(block))
        return "".join(parts)
    return "" if content is None else str(content)


def envelope_from_message(message: Any) -> dict[str, Any]:
    """Convert a child ``ToolMessage`` into the shared envelope contract.

    ``data``/``truncated`` come from ``ToolMessage.artifact["ptc"]``. Without a
    canonical artifact the content is complete and ``data``/``truncated`` stay
    ``None`` -- "unknown", never a guessed "complete".
    """
    artifact = getattr(message, "artifact", None)
    ptc = artifact.get("ptc") if isinstance(artifact, Mapping) else None
    data: Any = None
    truncated: bool | None = None
    if isinstance(ptc, Mapping):
        data = ptc.get("data")
        raw_truncated = ptc.get("truncated")
        if raw_truncated is not None:
            truncated = bool(raw_truncated)
    return {
        "content": getattr(message, "content", ""),
        "data": data,
        "truncated": truncated,
    }


def _content_summary(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        return {"bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()[:16]}
    return {"type": type(value).__name__}


def _bounded_value(value: Any) -> Any:
    if isinstance(value, bool | int | float | type(None)):
        return value
    if isinstance(value, str):
        if len(value) <= _MAX_EVENT_VALUE_CHARS:
            return value
        return value[:_MAX_EVENT_VALUE_CHARS] + "\u2026"
    if isinstance(value, list):
        return f"<list len={len(value)}>"
    if isinstance(value, Mapping):
        return f"<dict keys={len(value)}>"
    return str(value)[:_MAX_EVENT_VALUE_CHARS]


def sanitize_args(args: Any) -> dict[str, Any]:
    """A bounded, secret-free summary of child tool args for UI streaming.

    The full args are never emitted. Credential-looking keys are dropped and
    content-bearing keys (file bodies, code, prompts) collapse to a size digest,
    so a stream consumer cannot leak secrets or whole payloads.
    """
    if not isinstance(args, Mapping):
        return {}
    summary: dict[str, Any] = {}
    for key, value in args.items():
        name = str(key)
        lowered = name.lower()
        if any(marker in lowered for marker in _SECRET_MARKERS):
            continue
        if any(marker in lowered for marker in _CONTENT_MARKERS):
            summary[name] = _content_summary(value)
        else:
            summary[name] = _bounded_value(value)
        if len(summary) >= _MAX_EVENT_ARG_KEYS:
            break
    while len(summary) > 1 and len(json.dumps(summary, default=str)) > _MAX_EVENT_ARGS_CHARS:
        summary.pop(next(reversed(summary)))
    return summary


def _bound(text: Any, limit: int) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[: max(0, limit - 1)] + "\u2026"


def _replace(obj: Any, **changes: Any) -> Any:
    """``dataclasses.replace`` that tolerates non-dataclass stand-ins."""
    try:
        return dataclasses.replace(obj, **changes)
    except Exception:  # noqa: BLE001 - defensive fallback for test doubles
        clone = copy.copy(obj)
        for key, value in changes.items():
            object.__setattr__(clone, key, value)
        return clone


def _skip_whitespace(text: str, start: int) -> int:
    """Index of the first non-whitespace character at or after ``start``."""
    index = start
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _literal_first_argument(text: str, start: int) -> str | None:
    """The string literal that is ``tools.call``'s first argument, or ``None``.

    ``start`` is the index just past the opening ``(``. Returns the literal's
    text only when the first argument is a plain string literal followed by ``,``
    or ``)``; returns ``None`` for a variable, an f-string, a concatenation or
    any other non-literal, so the pre-flight scan can decline to guess instead of
    reporting a wrong name.
    """
    index = _skip_whitespace(text, start)
    if index >= len(text) or text[index] not in "\"'":
        return None
    quote = text[index]
    cursor = index + 1
    while cursor < len(text) and text[cursor] != quote:
        cursor += 2 if text[cursor] == "\\" else 1
    if cursor >= len(text):
        return None
    literal = text[index + 1 : cursor]
    after = _skip_whitespace(text, cursor + 1)
    if after < len(text) and text[after] not in ",)":
        return None
    return literal


def nested_call_id(parent_call_id: str, seq: int) -> str:
    """A child call id that is unique under its parent ``run_code`` call."""
    return f"{parent_call_id or 'ptc'}:ptc:{seq}"


@dataclass(slots=True)
class CallContext:
    """Everything one ``run_code`` invocation needs to dispatch children."""

    parent_call_id: str
    handler: Callable[..., Any]
    request: Any
    runtime: Any
    tools: dict[str, Any]
    stream_writer: Any = None
    offload: bool = False
    tool_names: tuple[str, ...] = ()
    #: Names the sandbox may call (``available_names``); the runner forwards them
    #: so the script can self-check with ``tools.available``.
    available: tuple[str, ...] = ()
    seq: int = 0
    calls: int = 0


class PtcBridge:
    """Policy, scheduling and envelope conversion for one PTC middleware."""

    def __init__(
        self,
        *,
        project_root: Path,
        excluded_tools: Collection[str],
        require_approval: bool,
        readonly: bool,
        limits: PtcLimits,
        mode: str = "both",
        scheduler: FairReadWriteScheduler | None = None,
        run_code: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._excluded = frozenset(str(name) for name in excluded_tools if name)
        self._require_approval = bool(require_approval)
        self._readonly = bool(readonly)
        self._limits = limits
        self._mode = str(mode)
        max_parallel = int(getattr(limits, "max_parallel", 8) or 8)
        self._scheduler = scheduler or FairReadWriteScheduler(max_parallel)
        self._runner = run_code
        self._calls = 0
        self._sync_lock = threading.Lock()
        self._sync_inflight = 0

    # -- policy --------------------------------------------------------------
    @property
    def mode(self) -> str:
        return self._mode

    @property
    def total_calls(self) -> int:
        """Child calls dispatched by this bridge (statistics only)."""
        return self._calls

    @property
    def limits(self) -> PtcLimits:
        """The resource budget this bridge runs under."""
        return self._limits

    @property
    def sync_inflight(self) -> int:
        """Blocking handlers currently running on the daemon pool.

        Counts both sync-only tools and thread-backed async wrappers (deepagents
        filesystem tools). A handler cannot be killed, so this can be non-zero
        after a run has returned; :meth:`_warn_if_sync_still_running` surfaces
        that fact.
        """
        with self._sync_lock:
            return self._sync_inflight

    @property
    def run_code_enabled(self) -> bool:
        """Whether ``run_code`` may be exposed and executed."""
        return (
            self._mode in {"both", "code"} and not self._readonly and RUN_CODE not in self._excluded
        )

    def is_excluded(self, name: str) -> bool:
        return name in self._excluded

    def is_orchestratable(self, name: str) -> bool:
        """Whether ``name`` is folded into the SDK in code mode.

        Session-state tools are never folded. A write/execute tool *is* foldable
        when approval is off (it would run without a prompt either way), but
        approval mode keeps it native so the interactive prompt still happens.
        Unknown (e.g. MCP) tools autopass unless approval mode forbids an unknown
        contract.
        """
        if not name or name in DENIED_TOOLS:
            return False
        if self._readonly:
            return False
        contract = tool_contract(name)
        if contract is None:
            return not self._require_approval
        if contract.side_effect_scope == SESSION_SCOPE:
            return False
        if self._require_approval and contract.needs_approval:
            return False
        return True

    def denial_reason(self, name: str, tools: Mapping[str, Any]) -> str | None:
        """Why a child call must be refused, or ``None`` when it may proceed."""
        if not name:
            return "empty tool name"
        if self._readonly:
            # Read-only mode never exposes or runs ``run_code``; refusing every
            # name keeps ``available_names`` empty and consistent with folding.
            return f"tool '{name}' cannot be called from code in read-only mode"
        if name == RUN_CODE:
            return "recursive run_code calls are not allowed"
        if name in DENIED_TOOLS:
            return f"tool '{name}' cannot be orchestrated from code; use the native tool"
        if name in self._excluded:
            return f"tool '{name}' is excluded by policy"
        if name not in tools:
            return f"tool '{name}' is not registered in this agent"
        contract = tool_contract(name)
        if contract is not None and contract.side_effect_scope == SESSION_SCOPE:
            return f"tool '{name}' mutates session state; use the native tool"
        if contract is None:
            if self._require_approval:
                return f"tool '{name}' has no approval contract; use the native tool"
            return None
        if self._require_approval and contract.needs_approval:
            return f"tool '{name}' requires approval; use the native tool"
        return None

    def available_names(self, tools: Mapping[str, Any]) -> list[str]:
        """Sorted names the sandbox may call: single source of truth for the SDK
        allowlist, the pre-flight check, ``tools.available`` and folding."""
        return sorted(name for name in tools if self.denial_reason(name, tools) is None)

    def dispatchable_names(self, tools: Mapping[str, Any]) -> list[str]:
        """Backward-compatible alias of :meth:`available_names`."""
        return self.available_names(tools)

    def preflight(self, code: str, tools: Mapping[str, Any]) -> list[tuple[str, str]]:
        """Statically flag literal tool references the sandbox may not call.

        Scans ``code`` for the two literal call shapes the SDK advertises --
        ``tools.<name>`` attribute access and ``tools.call("<name>", ...)`` -- and
        returns ``[(name, reason)]`` for every referenced name that
        :meth:`denial_reason` refuses, de-duplicated, name-sorted and capped at
        :data:`_MAX_PREFLIGHT_NAMES`.

        The scan is deliberately conservative. When a tool name cannot be read
        off the source -- ``getattr(tools, ...)`` or a ``tools.call(...)`` whose
        first argument is a variable, a concatenation or an f-string -- it
        returns ``[]`` and leaves the refusal to the runtime
        :class:`ToolCallError`, rather than guessing. ``code`` is already bounded
        by ``max_code_bytes``, so a regular-expression scan is sufficient; there
        is no Python parser here.
        """
        text = code or ""
        if _GETATTR_TOOLS_RE.search(text):
            return []
        referenced: set[str] = set()
        for match in _TOOLS_CALL_OPEN_RE.finditer(text):
            literal = _literal_first_argument(text, match.end())
            if literal is None:
                return []
            referenced.add(literal)
        for match in _TOOLS_ATTR_RE.finditer(text):
            name = match.group(1)
            if name not in _TOOLS_NON_TOOL_ATTRS:
                referenced.add(name)
        blocked = [
            (name, reason)
            for name in referenced
            if (reason := self.denial_reason(name, tools)) is not None
        ]
        blocked.sort()
        return blocked[:_MAX_PREFLIGHT_NAMES]

    def denial_message(self, name: str, tools: Mapping[str, Any], reason: str) -> str:
        """A refusal message that names the tools the sandbox may call instead.

        ``reason`` is the pure :meth:`denial_reason` text; the available-tool
        listing is appended here -- never inside ``denial_reason``, which
        :meth:`available_names` calls -- and bounded to about
        :data:`_MAX_DENIAL_MESSAGE_CHARS` characters, ending with an explicit
        ``… and N more`` when names had to be dropped.
        """
        names = self.available_names(tools)
        prefix = f"{reason}. Available tools: "
        if not names:
            return _bound(prefix + "(none)", _MAX_DENIAL_MESSAGE_CHARS)
        total = len(names)
        for count in range(total, 0, -1):
            omitted = total - count
            tail = "" if omitted == 0 else f", and {omitted} more \u2026"
            message = prefix + ", ".join(names[:count]) + tail
            if len(message) <= _MAX_DENIAL_MESSAGE_CHARS:
                return message
        return _bound(prefix + names[0], _MAX_DENIAL_MESSAGE_CHARS)

    def classify(self, name: str) -> str:
        """Scheduling class for ``name``: parallel read or exclusive write."""
        contract = tool_contract(name)
        if contract is not None and contract.read_only and contract.concurrent_safe:
            return READ
        return WRITE

    def registered_names(self, tools: Mapping[str, Any]) -> list[str]:
        """Every registered tool name, handed to the subprocess as its whitelist.

        Passing *all* registered names (not just the dispatchable ones) means the
        subprocess only reports ``unknown`` for a name the host never registered;
        exclusion, approval and recursion are then enforced by
        :meth:`denial_reason`, so a registered-but-denied tool reports ``denied``
        instead of being masked as ``unknown``.
        """
        return sorted(name for name in tools if name)

    # -- execution -----------------------------------------------------------
    async def invoke(self, ctx: CallContext, *, code: str) -> dict[str, Any]:
        """Run ``code`` through the process runner with this bridge's dispatch."""
        runner = self._runner or _resolve_run_code()
        result = await runner(
            code=code,
            tool_names=list(ctx.tool_names),
            available_names=list(ctx.available),
            dispatch=self._make_dispatch(ctx),
            cwd=self._project_root,
            limits=self._limits,
        )
        return self._warn_if_sync_still_running(result)

    def _warn_if_sync_still_running(self, result: Any) -> Any:
        """Annotate a result whose sync handlers outlived the (timed-out) run.

        A daemon thread cannot be killed, so the run may return while a blocking
        handler is still executing. We surface that instead of implying it
        stopped.
        """
        with self._sync_lock:
            inflight = self._sync_inflight
        if inflight <= 0 or not isinstance(result, Mapping):
            return result
        warning = (
            f"{inflight} blocking tool call(s) may still be running in the "
            "background; the run ended before they finished"
        )
        updated = dict(result)
        logs = list(updated.get("logs") or [])
        logs.append(f"[ptc] {warning}")
        updated["logs"] = logs
        updated["warning"] = warning
        return updated

    def _make_dispatch(self, ctx: CallContext) -> Callable[[str, Any], Awaitable[Any]]:
        async def dispatch(name: str, args: Any) -> Any:
            return await self.dispatch(ctx, name, args)

        return dispatch

    async def dispatch(self, ctx: CallContext, name: Any, args: Any) -> dict[str, Any]:
        """Execute one child call and return a ``ToolEnvelope`` dict.

        Raises :class:`ToolCallError` when the call is refused or the tool
        fails; the process runtime is expected to surface that as its own error.
        """
        tool_name = str(name or "")
        call_id = nested_call_id(ctx.parent_call_id, ctx.seq)
        ctx.seq += 1
        ctx.calls += 1
        self._calls += 1

        reason = self.denial_reason(tool_name, ctx.tools)
        if reason is not None:
            unregistered = tool_name not in ctx.tools and tool_name not in DENIED_TOOLS
            kind = "unknown" if unregistered else "denied"
            message = self.denial_message(tool_name, ctx.tools, reason)
            error = ToolCallError(message, kind=kind, name=tool_name)
            self._emit(ctx, event="started", call_id=call_id, name=tool_name, args=args)
            self._emit(
                ctx,
                event="finished",
                call_id=call_id,
                name=tool_name,
                args=args,
                status="error",
                preview=message,
            )
            raise error

        call_kind = self.classify(tool_name)
        self._emit(ctx, event="started", call_id=call_id, name=tool_name, args=args)
        ticket = await self._scheduler.acquire(call_kind)
        try:
            message = await self._invoke_tool(
                ctx, name=tool_name, args=args, call_id=call_id, ticket=ticket
            )
        except BaseException as exc:  # noqa: BLE001 - emit then re-raise
            # The ticket is released by _invoke_tool exactly once: immediately
            # for a pure async handler, or when the shielded task / offloaded
            # daemon thread really finishes for a blocking sync tool.
            self._emit(
                ctx,
                event="finished",
                call_id=call_id,
                name=tool_name,
                args=args,
                status="error",
                preview=str(exc),
            )
            raise

        if isinstance(message, Command):
            error = ToolCallError(
                f"tool '{tool_name}' returned a graph Command, which cannot be "
                "applied from a child call; its state update was not merged",
                kind="command",
                name=tool_name,
            )
            self._emit(
                ctx,
                event="finished",
                call_id=call_id,
                name=tool_name,
                args=args,
                status="error",
                preview=error.message,
            )
            raise error

        status = str(getattr(message, "status", "") or "")
        if status == "error":
            text = content_to_text(getattr(message, "content", ""))
            error = ToolCallError(text or "tool failed", kind="tool_error", name=tool_name)
            self._emit(
                ctx,
                event="finished",
                call_id=call_id,
                name=tool_name,
                args=args,
                status="error",
                preview=text or "tool failed",
            )
            raise error

        envelope = envelope_from_message(message)
        self._emit(
            ctx,
            event="finished",
            call_id=call_id,
            name=tool_name,
            args=args,
            status="success",
            preview=_envelope_preview(envelope),
        )
        return envelope

    async def _invoke_tool(
        self,
        ctx: CallContext,
        *,
        name: str,
        args: Any,
        call_id: str,
        ticket: Any,
    ) -> Any:
        """Re-enter the tool node for one nested call.

        Uses the captured handler with an overridden request, so argument
        injection/validation still run and the runtime's ``tool_call_id`` is the
        nested id. Never calls ``BaseTool.ainvoke`` directly.
        """
        tool = ctx.tools[name]
        nested_args = dict(args) if isinstance(args, Mapping) else {}
        nested_call = {"name": name, "args": nested_args, "id": call_id, "type": "tool_call"}
        nested_runtime = (
            _replace(ctx.runtime, tool_call_id=call_id) if ctx.runtime is not None else None
        )
        nested_request = _replace(
            ctx.request,
            tool=tool,
            tool_call=nested_call,
            runtime=nested_runtime,
        )
        if ctx.offload or _is_blocking_worker_tool(tool):
            # Both the sync path (``offload=True``) and an async-path tool whose
            # work runs on a thread (a sync ``BaseTool`` or a deepagents FS
            # wrapper) go to the daemon pool: the handler is driven on an
            # independent loop there, so the origin loop's teardown cannot cancel
            # it and release the ticket while its worker thread keeps running.
            return await self._invoke_offloaded(ctx, nested_request, ticket, name=name)
        try:
            return await ctx.handler(nested_request)
        finally:
            self._scheduler.release_nowait(ticket)

    async def _invoke_offloaded(
        self,
        ctx: CallContext,
        nested_request: Any,
        ticket: Any,
        *,
        name: str,
        handler: Callable[..., Any] | None = None,
    ) -> Any:
        """Run a blocking handler on a daemon thread, shielding it from cancel.

        A cancel (e.g. the ``run_code`` timeout) must not cancel the thread: the
        handler cannot be stopped, so the scheduler ticket stays held until the
        thread really finishes -- a write lock is never released while a
        background write is still running. The ticket is released from the
        daemon future's completion callback with the thread-safe
        :meth:`FairReadWriteScheduler.release_nowait`, so it works even after the
        origin loop has closed.

        ``handler`` defaults to ``ctx.handler``. The async path passes its async
        handler so :func:`_run_handler_in_worker` can drive it on a fresh
        ``asyncio.run`` loop owned by the worker, instead of the origin loop
        (which ``asyncio.run`` teardown would cancel out from under it).
        """
        run_handler = ctx.handler if handler is None else handler
        # ContextVars (injected tool runtime, trace ids) do not cross a bare
        # thread boundary; capture them here and re-enter them on the worker.
        # The handler is the graph's tool node, which does not touch the
        # checkpointer, so re-entering a context that captured the origin loop
        # never uses it across loops.
        run_context = contextvars.copy_context()
        try:
            future = _SYNC_DISPATCH_POOL.submit(
                run_context.run, _run_handler_in_worker, run_handler, nested_request
            )
        except _DispatchQueueFull as exc:
            self._scheduler.release_nowait(ticket)
            raise ToolCallError(
                f"cannot start blocking tool '{name}': {exc}",
                kind="limit",
                name=name,
            ) from exc
        with self._sync_lock:
            self._sync_inflight += 1

        loop = asyncio.get_running_loop()
        wrapped: asyncio.Future[Any] = loop.create_future()

        def _finished(_future: concurrent.futures.Future[Any]) -> None:
            with self._sync_lock:
                self._sync_inflight = max(0, self._sync_inflight - 1)
            self._scheduler.release_nowait(ticket)
            _retrieve_concurrent_quietly(_future)

            def _deliver() -> None:
                if wrapped.done():
                    return
                if _future.cancelled():
                    wrapped.cancel()
                elif (error := _future.exception()) is not None:
                    wrapped.set_exception(error)
                else:
                    wrapped.set_result(_future.result())

            try:
                loop.call_soon_threadsafe(_deliver)
            except RuntimeError:
                # The origin loop already closed (a sync run that returned
                # before the handler finished); the ticket is released above, so
                # there is nothing left to wake.
                pass

        try:
            future.add_done_callback(_finished)
        except BaseException:  # pragma: no cover - defensive: never leak a ticket
            with self._sync_lock:
                self._sync_inflight = max(0, self._sync_inflight - 1)
            future.cancel()
            self._scheduler.release_nowait(ticket)
            raise
        wrapped.add_done_callback(_retrieve_quietly)
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            # The caller is gone but the thread cannot be stopped: keep the
            # ticket until the daemon future really finishes. If it has not
            # started yet, cancel it so a dead run leaves no queued work.
            future.cancel()
            raise

    # -- streaming -----------------------------------------------------------
    def _emit(
        self,
        ctx: CallContext,
        *,
        event: str,
        call_id: str,
        name: str,
        args: Any = None,
        status: str | None = None,
        preview: str | None = None,
    ) -> None:
        writer = ctx.stream_writer
        if not callable(writer):
            return
        payload: dict[str, Any] = {
            "type": "ptc_tool",
            "event": event,
            "parent_call_id": ctx.parent_call_id,
            "call_id": call_id,
            "name": name,
        }
        if args is not None:
            payload["args"] = sanitize_args(args)
        if status is not None:
            payload["status"] = status
        if preview is not None:
            payload["preview"] = _bound(preview, _MAX_PREVIEW_CHARS)
        try:
            writer(payload)
        except Exception:  # noqa: BLE001 - streaming is best-effort
            pass


def _envelope_preview(envelope: Mapping[str, Any]) -> str:
    text = content_to_text(envelope.get("content"))
    if text:
        return text
    data = envelope.get("data")
    if data is not None:
        try:
            return json.dumps(data, default=str)
        except (TypeError, ValueError):
            return str(data)
    return ""


def _resolve_run_code() -> Callable[..., Awaitable[dict[str, Any]]]:
    """Import the process runner lazily so this module has no import cycle."""
    from synapse.runtime.ptc.process import run_code

    return run_code


__all__ = [
    "DENIED_TOOLS",
    "RUN_CODE",
    "CallContext",
    "PtcBridge",
    "ToolCallError",
    "content_to_text",
    "envelope_from_message",
    "nested_call_id",
    "sanitize_args",
]
