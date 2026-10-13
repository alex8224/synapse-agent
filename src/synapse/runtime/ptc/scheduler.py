"""A fair, bounded read/write scheduler for PTC child tool calls.

Programmatic tool calling can fan out many child calls from one ``run_code``
block. We want read-only, concurrency-safe tools to run in parallel (up to
``max_parallel``) while a state-mutating or unknown tool takes the machine
exclusively. Crucially the barrier is *fair* in submission order:

* a writer waits until every read submitted before it has drained (a plain
  ``asyncio.Lock`` would let a writer barge past in-flight reads), and
* a read submitted *after* a queued writer does not jump ahead of it, so a
  stream of readers cannot starve a writer.

One scheduler instance is shared by a whole bridge, so the barrier spans *every*
event loop that bridge dispatches on. That matters because the synchronous
``run_code`` path runs each invocation under its own ``asyncio.run`` loop while
the async path shares the graph's long-lived loop: a background write left over
from a timed-out sync run must still block a later run's write even though the
first loop has closed.

Design
------

The queue and counters live in plain attributes guarded by a
:class:`threading.Lock`, not an ``asyncio`` primitive. Each ticket carries the
loop it was acquired on plus an :class:`asyncio.Future`; ``acquire`` awaits that
future and the state machine wakes it with ``loop.call_soon_threadsafe``. This
makes the barrier correct across loops running in different threads without ever
keying state on (or strongly retaining) a loop -- the old per-loop
``WeakKeyDictionary`` of ``asyncio.Condition`` objects kept each loop alive
through the value->key cycle, so a contended run leaked its loop forever.

``release_nowait`` is the thread-safe release used by the sync offload callback:
it does not touch the origin loop, so a ticket is released correctly even after
that loop has closed. Pending tickets whose loop died are pruned so they can
never starve the queue, and a granted ticket drops its loop/future references so
it cannot retain a finished loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from dataclasses import dataclass

READ = "read"
WRITE = "write"


@dataclass
class _Ticket:
    """A granted or pending request for the critical section."""

    kind: str
    seq: int
    loop: asyncio.AbstractEventLoop | None
    wakeup: asyncio.Future[None] | None
    granted: bool = False
    released: bool = False
    cancelled: bool = False


class FairReadWriteScheduler:
    """Fair reader/writer barrier with a parallel-reader ceiling.

    A single instance is shared across every event loop a bridge runs on. All
    queue mutation happens under a :class:`threading.Lock`, so it is safe to call
    :meth:`acquire` from concurrent loops in different threads and to call
    :meth:`release_nowait` from a daemon thread whose origin loop has closed.
    """

    def __init__(self, max_parallel: int = 8) -> None:
        self._max_parallel = max(1, int(max_parallel))
        self._lock = threading.Lock()
        self._queue: list[_Ticket] = []
        self._active_reads = 0
        self._writer_active = False
        self._next_seq = 0

    @property
    def max_parallel(self) -> int:
        return self._max_parallel

    # -- public API ----------------------------------------------------------
    async def acquire(self, kind: str) -> _Ticket:
        """Acquire a read or write ticket, blocking until it is fair to start.

        Raises ``asyncio.CancelledError`` if the awaiting task is cancelled; the
        pending ticket is removed (or a granted one released) exactly once and
        the queue is woken so nothing behind it is starved.
        """
        if kind not in (READ, WRITE):
            msg = f"unknown scheduler kind: {kind!r}"
            raise ValueError(msg)
        loop = asyncio.get_running_loop()
        ticket = _Ticket(kind=kind, seq=0, loop=loop, wakeup=loop.create_future())
        # A granted ticket clears its own ``wakeup`` so it cannot retain a
        # finished loop; keep a local handle to await it either way.
        wakeup = ticket.wakeup
        with self._lock:
            ticket.seq = self._next_seq
            self._next_seq += 1
            self._queue.append(ticket)
            self._grant_locked()
        try:
            await wakeup
        except BaseException:
            self._abandon(ticket)
            raise
        return ticket

    async def release(self, ticket: _Ticket) -> None:
        """Release a previously granted ticket (awaitable convenience form)."""
        self.release_nowait(ticket)

    def release_nowait(self, ticket: _Ticket) -> None:
        """Thread-safe release that never touches the origin loop.

        Used from a daemon future-completion callback, so it must work even when
        the loop that acquired the ticket has already closed.
        """
        with self._lock:
            if not ticket.granted or ticket.released or ticket.cancelled:
                return
            ticket.released = True
            self._finish_locked(ticket)
            self._grant_locked()

    # -- internals -----------------------------------------------------------
    def _abandon(self, ticket: _Ticket) -> None:
        """Handle an ``acquire`` that was cancelled before/after it was granted."""
        with self._lock:
            if ticket.granted:
                if ticket.released:
                    return
                ticket.released = True
                self._finish_locked(ticket)
            else:
                ticket.cancelled = True
                with contextlib.suppress(ValueError):
                    self._queue.remove(ticket)
            self._grant_locked()

    def _finish_locked(self, ticket: _Ticket) -> None:
        if ticket.kind == WRITE:
            self._writer_active = False
        else:
            self._active_reads = max(0, self._active_reads - 1)

    def _grant_locked(self) -> None:
        """Grant every ticket that may start now, strictly in submission order.

        Because the queue is walked head-first and the loop stops at the first
        ticket that cannot start, a later reader can never jump a queued writer
        and a writer only starts when no earlier read is still running.
        """
        self._prune_locked()
        while self._queue:
            ticket = self._queue[0]
            if not self._can_start_locked(ticket):
                break
            del self._queue[0]
            if not self._wake(ticket):
                # The owner loop died between the liveness check and the wake:
                # drop the ticket instead of granting to a loop that can never
                # run it, so it cannot starve the ones behind it.
                ticket.cancelled = True
                continue
            ticket.granted = True
            if ticket.kind == WRITE:
                self._writer_active = True
            else:
                self._active_reads += 1
            # A granted ticket no longer needs its loop or future: dropping them
            # keeps a finished loop collectable even while the ticket is held by
            # an offloaded handler that has not finished yet.
            ticket.loop = None
            ticket.wakeup = None

    def _prune_locked(self) -> None:
        """Drop pending tickets whose loop has closed.

        Such a ticket's ``acquire`` coroutine can never resume, so leaving it in
        the queue would starve everything submitted after it.
        """
        if not self._queue:
            return
        alive: list[_Ticket] = []
        for ticket in self._queue:
            loop = ticket.loop
            if loop is not None and loop.is_closed():
                ticket.cancelled = True
                continue
            alive.append(ticket)
        self._queue = alive

    def _can_start_locked(self, ticket: _Ticket) -> bool:
        """Whether the head-of-queue ``ticket`` may enter right now."""
        if self._writer_active:
            return False
        if ticket.kind == WRITE:
            return self._active_reads == 0
        return self._active_reads < self._max_parallel

    def _wake(self, ticket: _Ticket) -> bool:
        """Schedule the ticket's future on its origin loop; ``False`` if dead."""
        loop = ticket.loop
        future = ticket.wakeup
        if future is None or loop is None or loop.is_closed():
            return False

        def _set() -> None:
            if not future.done():
                future.set_result(None)

        try:
            loop.call_soon_threadsafe(_set)
        except RuntimeError:
            return False
        return True


__all__ = ["FairReadWriteScheduler", "READ", "WRITE"]
