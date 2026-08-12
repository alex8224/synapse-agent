"""Optional startup timing tracer.

Enable with env ``AGENT_STARTUP_TRACE=1`` (or ``true`` / ``yes``).
Prints cumulative stage timings to stderr on demand via :func:`dump`
(stage reports) and via :func:`duration` (independent one-shot lines).
When enabled, the remaining stages are printed to stderr once more when
the process exits, so the full startup path shows up in the exit log.
"""

from __future__ import annotations

import atexit
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


def _env_enabled() -> bool:
    raw = (os.environ.get("AGENT_STARTUP_TRACE") or "").strip().casefold()
    return raw in {"1", "true", "yes", "on"}


@dataclass
class StartupTrace:
    enabled: bool = field(default_factory=_env_enabled)
    thread_name: str = field(default_factory=lambda: threading.current_thread().name)
    t0: float = field(default_factory=time.perf_counter)
    # each mark: (name, ms_since_start, ms_step)
    marks: list[tuple[str, float, float]] = field(default_factory=list)
    _last: float = field(default_factory=time.perf_counter)
    _dumped: int = 0

    def reset(self) -> None:
        self.t0 = time.perf_counter()
        self._last = self.t0
        self.marks.clear()
        self._dumped = 0

    def ensure_started(self) -> None:
        """Set t0 close to process entry when the trace was enabled late."""
        if self.enabled and not self.marks:
            self.reset()
            self.mark("trace-enabled")

    def duration(
        self, name: str, started: float, *, file=None, **fields: Any
    ) -> None:
        """Emit one independent duration line without mutating stage state."""
        if not self.enabled:
            return
        elapsed = (time.perf_counter() - started) * 1000
        suffix = " ".join(f"{key}={value}" for key, value in fields.items())
        detail = f" {suffix}" if suffix else ""
        out = file or sys.stderr
        print(
            f"[perf] thread={threading.current_thread().name} event={name} "
            f"elapsed_ms={elapsed:.1f}{detail}",
            file=out,
        )

    def mark(self, name: str) -> None:
        if not self.enabled:
            return
        now = time.perf_counter()
        since = (now - self.t0) * 1000
        step = (now - self._last) * 1000
        self._last = now
        self.marks.append((name, since, step))

    @contextmanager
    def span(self, name: str) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        t0 = time.perf_counter()
        try:
            yield
        finally:
            now = time.perf_counter()
            since = (now - self.t0) * 1000
            step = (now - t0) * 1000
            self._last = now
            self.marks.append((name, since, step))

    def dump(
        self, *, header: str = "startup-trace", file=None, force: bool = False
    ) -> None:
        """Print accumulated stages; ``force`` also repeats already-reported ones.

        Without ``force`` only stages since the last dump are printed, so
        repeated callers (build completion, process exit) do not duplicate
        output. ``force`` is used for thread-local traces at exit: their
        earlier report may have been swallowed by the full-screen TUI, so the
        exit log always shows the complete per-thread picture.
        """
        if not self.enabled:
            return
        out = file or sys.stderr
        pending = self.marks if force else self.marks[self._dumped :]
        if not pending:
            return
        total = (time.perf_counter() - self.t0) * 1000
        print(f"[{header}] total={total:.1f}ms stages={len(pending)}", file=out)
        for name, since, step in pending:
            print(f"  +{step:8.1f}ms  @{since:8.1f}ms  {name}", file=out)
        # Top offenders by step cost
        top = sorted(pending, key=lambda x: x[2], reverse=True)[:8]
        print(f"[{header}] top stages:", file=out)
        for name, _since, step in top:
            print(f"  {step:8.1f}ms  {name}", file=out)
        if not force:
            self._dumped = len(self.marks)


TRACE = StartupTrace()
_LOCAL = threading.local()
_THREAD_TRACES: list[StartupTrace] = []
_THREAD_TRACES_LOCK = threading.Lock()


def _current_trace() -> StartupTrace:
    if threading.current_thread() is threading.main_thread():
        return TRACE
    trace = getattr(_LOCAL, "trace", None)
    if trace is None:
        trace = StartupTrace()
        _LOCAL.trace = trace
        with _THREAD_TRACES_LOCK:
            _THREAD_TRACES.append(trace)
    return trace


def reset() -> None:
    _current_trace().reset()


def mark(name: str) -> None:
    _current_trace().mark(name)


def span(name: str):
    return _current_trace().span(name)


def _atexit_dump() -> None:
    """Print startup stages not yet reported when the process exits."""
    try:
        TRACE.dump(header="startup-trace")
    except Exception:  # noqa: BLE001 - diagnostics must never break shutdown
        pass
    try:
        with _THREAD_TRACES_LOCK:
            traces = list(_THREAD_TRACES)
        for trace in traces:
            if trace.enabled and trace.marks:
                trace.dump(
                    header=f"startup-trace:{trace.thread_name}", force=True
                )
    except Exception:  # noqa: BLE001 - diagnostics must never break shutdown
        pass


atexit.register(_atexit_dump)


def dump(**kwargs) -> None:
    _current_trace().dump(**kwargs)


def duration(name: str, started: float, *, file=None, **fields: Any) -> None:
    """Emit one independent duration line without mutating cumulative stage state."""
    _current_trace().duration(name, started, file=file, **fields)


def ensure_started() -> None:
    """Call near process entry so t0 is close to CLI start when enabled late."""
    _current_trace().ensure_started()
