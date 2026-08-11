"""Process-exit timing trace for the TUI shutdown path.

Collects stage timings across the exit chain (Ctrl+C / ``/exit`` → Textual
``on_unmount`` → session/project teardown → ``asyncio.run`` executor join →
atexit MCP/async-runtime close) and prints a report to **stderr** once the
terminal has been restored, so the user can see exactly which stage is slow.

Timing only starts when :func:`begin` is called (i.e. a real exit was
requested); modules imported during tests never emit lines on their own.
"""

from __future__ import annotations

import sys
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
        """Emit one independent line for a late (atexit-phase) stage.

        Only prints after :meth:`begin` so plain test processes stay quiet.
        ``started`` must be a ``time.perf_counter()`` value captured when the
        stage began.
        """
        with self._lock:
            if not self._started:
                return
        elapsed = (time.perf_counter() - started) * 1000
        suffix = " ".join(f"{key}={value}" for key, value in fields.items())
        detail = f" {suffix}" if suffix else ""
        print(
            f"[exit-trace] thread={threading.current_thread().name} "
            f"stage={name} elapsed_ms={elapsed:.1f}{detail}",
            file=sys.stderr,
        )

    def dump(self, *, header: str = "exit-trace") -> None:
        """Print the accumulated stage report (call after the terminal is up)."""
        with self._lock:
            if not self._started or not self.marks or self.t0 is None:
                return
            marks = list(self.marks)
            total = (time.perf_counter() - self.t0) * 1000
        print(f"[{header}] total={total:.1f}ms stages={len(marks)}", file=sys.stderr)
        for name, since, step in sorted(marks, key=lambda x: x[1]):
            print(f"  +{step:8.1f}ms  @{since:8.1f}ms  {name}", file=sys.stderr)
        top = sorted(marks, key=lambda x: x[2], reverse=True)[:10]
        print(f"[{header}] top stages:", file=sys.stderr)
        for name, _since, step in top:
            print(f"  {step:8.1f}ms  {name}", file=sys.stderr)


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
