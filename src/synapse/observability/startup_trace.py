"""Optional startup timing tracer.

Enable with env ``AGENT_STARTUP_TRACE=1`` (or ``true`` / ``yes``).
Prints cumulative stage timings to stderr when the process finishes
startup-critical work, or on demand via :func:`dump`.
"""

from __future__ import annotations

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
    t0: float = field(default_factory=time.perf_counter)
    # each mark: (name, ms_since_start, ms_step)
    marks: list[tuple[str, float, float]] = field(default_factory=list)
    _last: float = field(default_factory=time.perf_counter)

    def reset(self) -> None:
        self.t0 = time.perf_counter()
        self._last = self.t0
        self.marks.clear()

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

    def dump(self, *, header: str = "startup-trace", file=None) -> None:
        if not self.enabled or not self.marks:
            return
        out = file or sys.stderr
        total = (time.perf_counter() - self.t0) * 1000
        print(f"[{header}] total={total:.1f}ms stages={len(self.marks)}", file=out)
        for name, since, step in self.marks:
            print(f"  +{step:8.1f}ms  @{since:8.1f}ms  {name}", file=out)
        # Top offenders by step cost
        top = sorted(self.marks, key=lambda x: x[2], reverse=True)[:8]
        print(f"[{header}] top stages:", file=out)
        for name, _since, step in top:
            print(f"  {step:8.1f}ms  {name}", file=out)


TRACE = StartupTrace()
_LOCAL = threading.local()


def _current_trace() -> StartupTrace:
    if threading.current_thread() is threading.main_thread():
        return TRACE
    trace = getattr(_LOCAL, "trace", None)
    if trace is None:
        trace = StartupTrace()
        _LOCAL.trace = trace
    return trace


def reset() -> None:
    _current_trace().reset()


def mark(name: str) -> None:
    _current_trace().mark(name)


def span(name: str):
    return _current_trace().span(name)


def dump(**kwargs) -> None:
    _current_trace().dump(**kwargs)


def duration(name: str, started: float, *, file=None, **fields: Any) -> None:
    """Emit one independent duration line without mutating cumulative stage state."""
    if not _current_trace().enabled:
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


def ensure_started() -> None:
    """Call near process entry so t0 is close to CLI start when enabled late."""
    trace = _current_trace()
    if trace.enabled and not trace.marks:
        trace.reset()
        trace.mark("trace-enabled")
