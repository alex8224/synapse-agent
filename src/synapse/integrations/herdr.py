"""Optional herdr lifecycle-state reporting for panes running inside herdr.

herdr (https://herdr.dev) is a terminal workspace manager for AI coding agents.
When synapse runs inside a herdr pane it inherits ``HERDR_ENV=1``,
``HERDR_PANE_ID``, and ``HERDR_BIN_PATH``.  This module mirrors synapse's
existing ``SessionStatus`` transitions into ``herdr pane report-agent`` calls so
the herdr sidebar shows ``working`` / ``blocked`` / ``idle`` without any native
herdr support for synapse.

Every entry point is a no-op when synapse is not running inside herdr, and all
reporting is best-effort: a missing ``herdr`` binary or a dead socket must never
break an agent turn.
"""

from __future__ import annotations

import atexit
import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

_AGENT = "synapse"
_SOURCE = "custom:synapse"
_COMMAND_TIMEOUT = 5.0

_WORKING_STATUSES = frozenset({"queued", "starting", "running", "cancelling"})
_BLOCKED_STATUSES = frozenset({"waiting_approval", "failed"})
_IDLE_STATUSES = frozenset({"idle", "cold", "cancelled"})


def herdr_enabled() -> bool:
    """Return True when this process is a pane managed by herdr."""
    return os.environ.get("HERDR_ENV") == "1" and bool(os.environ.get("HERDR_PANE_ID"))


def _resolve_bin_path() -> str:
    """Return the best-effort path to the herdr CLI.

    ``HERDR_BIN_PATH`` is the documented source of truth, but some herdr
    versions (notably the Windows beta) omit it while still putting ``herdr``
    on PATH.  Fall back to PATH resolution and finally to the bare command so
    ``subprocess`` can resolve it (or fail silently if herdr is unavailable).
    """
    return os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr") or "herdr"


def snapshot_status(snapshot: Any) -> str:
    """Extract the status string from a ``SessionSnapshot``-like object."""
    status = getattr(snapshot, "status", None)
    if status is None:
        return ""
    value = getattr(status, "value", None)
    return str(value) if value is not None else str(status)


def map_status_to_state(status: str) -> str | None:
    """Map a synapse ``SessionStatus`` value to a herdr agent state."""
    if status in _WORKING_STATUSES:
        return "working"
    if status in _BLOCKED_STATUSES:
        return "blocked"
    if status in _IDLE_STATUSES:
        return "idle"
    return None


def build_report_args(
    *,
    bin_path: str,
    pane_id: str,
    source: str,
    agent: str,
    state: str,
    seq: int,
    message: str | None = None,
) -> tuple[str, ...]:
    """Build the ``herdr pane report-agent`` argv without executing it."""
    args = [
        bin_path,
        "pane",
        "report-agent",
        pane_id,
        "--source",
        source,
        "--agent",
        agent,
        "--state",
        state,
        "--seq",
        str(seq),
    ]
    if message:
        args.extend(["--message", message])
    return tuple(args)


def build_release_args(
    *,
    bin_path: str,
    pane_id: str,
    source: str,
    agent: str,
) -> tuple[str, ...]:
    """Build the ``herdr pane release-agent`` argv without executing it."""
    return (bin_path, "pane", "release-agent", pane_id, "--source", source, "--agent", agent)


class HerdrReporter:
    """Fire-and-forget reporter that serializes state reports on one worker.

    Reports are queued so the agent runtime thread never blocks on the ``herdr``
    CLI, and a monotonic ``--seq`` keeps out-of-order writers from regressing
    state in herdr.  On process exit the reporter releases its lifecycle
    authority exactly once.
    """

    def __init__(
        self,
        *,
        pane_id: str,
        bin_path: str,
        agent: str = _AGENT,
        source: str = _SOURCE,
        command_timeout: float = _COMMAND_TIMEOUT,
    ) -> None:
        self._pane_id = pane_id
        self._bin_path = bin_path
        self._agent = agent
        self._source = source
        self._command_timeout = command_timeout
        self._seq = 0
        self._closed = False
        self._lock = threading.Lock()
        self._queue: queue.Queue[tuple[str, ...] | None] = queue.Queue()
        self._worker = threading.Thread(
            target=self._drain, name="herdr-reporter", daemon=True
        )
        self._worker.start()
        atexit.register(self._shutdown)

    def report(self, state: str, message: str | None = None) -> None:
        """Queue a lifecycle state report."""
        args = build_report_args(
            bin_path=self._bin_path,
            pane_id=self._pane_id,
            source=self._source,
            agent=self._agent,
            state=state,
            seq=self._next_seq(),
            message=message,
        )
        self._enqueue(args)

    def release(self) -> None:
        """Queue a release of this source's lifecycle authority."""
        self._enqueue(
            build_release_args(
                bin_path=self._bin_path,
                pane_id=self._pane_id,
                source=self._source,
                agent=self._agent,
            )
        )

    def flush(self, timeout: float = _COMMAND_TIMEOUT) -> None:
        """Wait for queued reports to be processed (mainly for tests)."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks > 0:
            if time.monotonic() > deadline:
                return
            time.sleep(0.001)

    def close(self) -> None:
        """Synchronously release authority and stop the worker (idempotent)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._run(
            build_release_args(
                bin_path=self._bin_path,
                pane_id=self._pane_id,
                source=self._source,
                agent=self._agent,
            )
        )
        try:
            self._queue.put(None)
        except Exception:  # noqa: BLE001 - shutdown is best-effort
            pass
        self._worker.join(timeout=self._command_timeout)

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _enqueue(self, args: tuple[str, ...]) -> None:
        with self._lock:
            if self._closed:
                return
        try:
            self._queue.put(args)
        except Exception:  # noqa: BLE001 - reporting is best-effort
            pass

    def _run(self, args: tuple[str, ...]) -> None:
        try:
            subprocess.run(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=self._command_timeout,
            )
        except Exception:  # noqa: BLE001 - a dead herdr socket must not fail the turn
            pass

    def _drain(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                self._run(item)
            finally:
                self._queue.task_done()

    def _shutdown(self) -> None:
        self.close()


_reporter: HerdrReporter | None = None
_reporter_lock = threading.Lock()


def _get_reporter() -> HerdrReporter | None:
    """Return the process-wide reporter, or None when herdr is absent."""
    global _reporter
    if _reporter is not None:
        return _reporter
    pane_id = os.environ.get("HERDR_PANE_ID", "")
    if not pane_id:
        return None
    with _reporter_lock:
        if _reporter is None:
            _reporter = HerdrReporter(
                pane_id=pane_id, bin_path=_resolve_bin_path()
            )
            # Announce the pane as soon as the first session starts so the
            # herdr sidebar shows synapse even before the first turn runs.
            _reporter.report("idle")
    return _reporter


def _status_observer() -> Callable[[Any], None] | None:
    """Build a ``SessionSnapshot`` observer, or None when herdr is absent."""
    if not herdr_enabled():
        return None
    reporter = _get_reporter()
    if reporter is None:
        return None

    def on_status(snapshot: Any) -> None:
        status = snapshot_status(snapshot)
        state = map_status_to_state(status)
        if state is None:
            return
        message: str | None = None
        if state == "blocked" and status == "failed":
            last_error = getattr(snapshot, "last_error", None)
            message = (str(last_error) if last_error else "turn failed")[:500]
        reporter.report(state, message=message)

    return on_status


def attach_status_observer(
    callback: Callable[[Any], None] | None,
) -> Callable[[Any], None] | None:
    """Compose the herdr observer with an existing status callback.

    Returns ``callback`` unchanged when herdr is not active so callers can use
    this unconditionally without any herdr-specific branching.
    """
    herdr_observer = _status_observer()
    if herdr_observer is None:
        return callback
    if callback is None:
        return herdr_observer

    def combined(snapshot: Any) -> None:
        try:
            herdr_observer(snapshot)
        finally:
            callback(snapshot)

    return combined
