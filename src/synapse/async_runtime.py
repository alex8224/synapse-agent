"""Process-lifetime asyncio loop for AsyncSqliteSaver + astream.

LangGraph's ``AsyncSqliteSaver`` binds to the event loop that created it.
TUI / CLI call sites are mostly synchronous and previously used
``asyncio.run`` in a worker thread (a *new* loop every turn) — that cannot
share an AsyncSqliteSaver.

This module owns one background loop thread for the whole process:

- open/setup AsyncSqliteSaver on that loop
- schedule ``agent.astream`` coroutines onto the same loop
- sync ``get_tuple`` (via LangGraph) still works from other threads via
  ``run_coroutine_threadsafe``
"""

from __future__ import annotations

import asyncio
import atexit
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


class AsyncRuntime:
    """Daemon thread running ``loop.run_forever()`` for checkpoint + astream."""

    def __init__(self, *, name: str = "coding-async-runtime") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._closed = False
        self._owned_conns: list[Any] = []

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        self.start()
        assert self._loop is not None
        return self._loop

    def start(self) -> None:
        with self._lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return
            if self._closed:
                raise RuntimeError("AsyncRuntime is closed")
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._thread_main,
                name=self._name,
                daemon=True,
            )
            self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("AsyncRuntime loop failed to start")

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:  # noqa: BLE001
                pass
            loop.close()

    def submit(self, coro: Coroutine[Any, Any, T]) -> asyncio.Future[T]:
        """Schedule a coroutine on the runtime loop (thread-safe)."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def run(self, coro: Coroutine[Any, Any, T], *, timeout: float | None = None) -> T:
        """Block until the coroutine finishes on the runtime loop."""
        return self.submit(coro).result(timeout=timeout)

    def track_connection(self, conn: Any) -> None:
        """Remember an aiosqlite connection for shutdown."""
        self._owned_conns.append(conn)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            thread = self._thread
        if loop is not None and loop.is_running():

            async def _shutdown() -> None:
                for conn in list(self._owned_conns):
                    try:
                        await conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                self._owned_conns.clear()
                loop.stop()

            try:
                asyncio.run_coroutine_threadsafe(_shutdown(), loop).result(timeout=3.0)
            except Exception:  # noqa: BLE001
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except Exception:  # noqa: BLE001
                    pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._loop = None
        self._thread = None


_RUNTIME = AsyncRuntime()
atexit.register(_RUNTIME.close)


def get_async_runtime() -> AsyncRuntime:
    """Process-global async runtime (lazy-started)."""
    return _RUNTIME


def reset_async_runtime_for_tests() -> AsyncRuntime:
    """Close and replace the global runtime (tests only)."""
    global _RUNTIME
    try:
        _RUNTIME.close()
    except Exception:  # noqa: BLE001
        pass
    _RUNTIME = AsyncRuntime(name="coding-async-runtime-test")
    atexit.register(_RUNTIME.close)
    return _RUNTIME
