"""Process-exit timing trace for the TUI shutdown path.

Collects stage timings across the exit chain (Ctrl+C / ``/exit`` → Textual
``on_unmount`` → session/project teardown → ``asyncio.run`` executor join →
atexit MCP/async-runtime close).  The instrumentation (``begin``/``mark``/
``span``) is kept, but the collected report is **no longer printed to stderr**:
``duration`` and ``dump`` are no-ops kept only for call-site compatibility.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExitTrace:
    # each mark: (name, ms_since_begin, ms_step)
    marks: list[tuple[str, float, float]] = field(default_factory=list)
    t0: float | None = None
    _last: float | None = None
    _started: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def begin(self) -> None:
        """Start tracing; called when the user requests app exit."""
        with self._lock:
            if self._started:
                return
            self._started = True
            self.t0 = time.perf_counter()
            self._last = self.t0
            self.marks = [("exit.requested", 0.0, 0.0)]

    @property
    def started(self) -> bool:
        return self._started

    def mark(self, name: str) -> None:
        with self._lock:
            if not self._started or self.t0 is None:
                return
            now = time.perf_counter()
            since = (now - self.t0) * 1000
            step = (now - self._last) * 1000 if self._last is not None else since
            self._last = now
            self.marks.append((name, since, step))

    @contextmanager
    def span(self, name: str) -> Iterator[None]:
        """Time one shutdown stage into the accumulated report."""
        with self._lock:
            if not self._started:
                skip = True
            else:
                skip = False
        if skip:
            yield
            return
        try:
            yield
        finally:
            with self._lock:
                now = time.perf_counter()
                since = (now - self.t0) * 1000 if self.t0 is not None else 0.0
                step = (now - self._last) * 1000 if self._last is not None else since
                self._last = now
                self.marks.append((name, since, step))

    def duration(self, name: str, started: float, **fields: Any) -> None:
        """No-op: exit stages are no longer reported to stderr.

        Kept so existing call sites (atexit MCP / async-runtime close) stay
        untouched while the shutdown trace stays silent.
        """
        del name, started, fields

    def dump(self, *, header: str = "exit-trace") -> None:
        """No-op: the accumulated stage report is no longer printed.

        Kept for call-site compatibility (``tui_launch`` teardown).
        """
        del header


EXIT_TRACE = ExitTrace()


def begin() -> None:
    EXIT_TRACE.begin()


def mark(name: str) -> None:
    EXIT_TRACE.mark(name)


def span(name: str):
    return EXIT_TRACE.span(name)


def duration(name: str, started: float, **fields: Any) -> None:
    EXIT_TRACE.duration(name, started, **fields)


def dump(**kwargs: Any) -> None:
    EXIT_TRACE.dump(**kwargs)
