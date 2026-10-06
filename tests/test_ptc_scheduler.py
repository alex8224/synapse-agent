"""Regression tests for the shared, cross-loop PTC read/write scheduler.

The scheduler is one instance per bridge, shared by *every* event loop that
bridge dispatches on: the sync path runs each ``run_code`` under its own
``asyncio.run`` loop while the async path shares the graph loop. These tests pin
the properties the old per-loop design got wrong:

* the barrier is shared across loops running in different threads (a write held
  on one loop blocks a write/read on another, and reads share one cap),
* a ticket left on a closed loop does not starve the queue,
* a cancelled queued ticket releases its slot exactly once,
* contended loops are still garbage-collectable afterwards (no loop retention),
* the sync daemon dispatch queue is bounded and drops cancelled entries.
"""

from __future__ import annotations

import asyncio
import gc
import threading
import time
import weakref

import pytest

from synapse.runtime.ptc.bridge import _DaemonDispatchPool, _DispatchQueueFull
from synapse.runtime.ptc.scheduler import READ, WRITE, FairReadWriteScheduler, _Ticket


def test_cross_loop_reads_respect_the_parallel_cap() -> None:
    scheduler = FairReadWriteScheduler(max_parallel=2)
    a_holding = threading.Event()
    release_one = threading.Event()
    release_two = threading.Event()
    b_read_started = threading.Event()

    def loop_a() -> None:
        async def main() -> None:
            loop = asyncio.get_running_loop()
            first = await scheduler.acquire(READ)
            second = await scheduler.acquire(READ)
            a_holding.set()
            await loop.run_in_executor(None, release_one.wait, 10)
            scheduler.release_nowait(first)
            await loop.run_in_executor(None, release_two.wait, 10)
            scheduler.release_nowait(second)

        asyncio.run(main())

    def loop_b() -> None:
        async def main() -> None:
            assert a_holding.wait(5)
            ticket = await scheduler.acquire(READ)
            b_read_started.set()
            scheduler.release_nowait(ticket)

        asyncio.run(main())

    thread_a = threading.Thread(target=loop_a)
    thread_a.start()
    assert a_holding.wait(5)
    thread_b = threading.Thread(target=loop_b)
    thread_b.start()
    time.sleep(0.2)
    # The cap is global: loop A holds both reader slots.
    assert not b_read_started.is_set()
    release_one.set()
    assert b_read_started.wait(5)
    release_two.set()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)
    assert not thread_a.is_alive() and not thread_b.is_alive()


def test_cross_loop_write_waits_for_a_read_on_another_loop() -> None:
    scheduler = FairReadWriteScheduler(max_parallel=4)
    a_read_held = threading.Event()
    release_a_read = threading.Event()
    b_write_started = threading.Event()

    def loop_a() -> None:
        async def main() -> None:
            ticket = await scheduler.acquire(READ)
            a_read_held.set()
            await asyncio.get_running_loop().run_in_executor(None, release_a_read.wait, 10)
            scheduler.release_nowait(ticket)

        asyncio.run(main())

    def loop_b() -> None:
        async def main() -> None:
            assert a_read_held.wait(5)
            ticket = await scheduler.acquire(WRITE)
            b_write_started.set()
            scheduler.release_nowait(ticket)

        asyncio.run(main())

    thread_a = threading.Thread(target=loop_a)
    thread_a.start()
    assert a_read_held.wait(5)
    thread_b = threading.Thread(target=loop_b)
    thread_b.start()
    time.sleep(0.2)
    # The write is exclusive: it cannot start while another loop holds a read.
    assert not b_write_started.is_set()
    release_a_read.set()
    thread_b.join(timeout=5)
    thread_a.join(timeout=5)
    assert b_write_started.is_set()


def test_cancelled_queued_read_does_not_starve_a_waiting_write() -> None:
    scheduler = FairReadWriteScheduler(max_parallel=1)
    a_acquired = threading.Event()
    release_a = threading.Event()
    write_acquired = threading.Event()

    def loop_a() -> None:
        async def main() -> None:
            ticket = await scheduler.acquire(READ)
            a_acquired.set()
            await asyncio.get_running_loop().run_in_executor(None, release_a.wait, 10)
            scheduler.release_nowait(ticket)

        asyncio.run(main())

    def loop_b() -> None:
        async def main() -> None:
            assert a_acquired.wait(5)
            doomed = asyncio.ensure_future(scheduler.acquire(READ))
            await asyncio.sleep(0.1)  # let the doomed read queue up
            doomed.cancel()
            with pytest.raises(asyncio.CancelledError):
                await doomed
            ticket = await scheduler.acquire(WRITE)
            write_acquired.set()
            scheduler.release_nowait(ticket)

        asyncio.run(main())

    thread_a = threading.Thread(target=loop_a)
    thread_a.start()
    assert a_acquired.wait(5)
    thread_b = threading.Thread(target=loop_b)
    thread_b.start()
    time.sleep(0.3)
    assert not write_acquired.is_set()
    release_a.set()
    thread_b.join(timeout=5)
    thread_a.join(timeout=5)
    # The cancelled read must have left the queue, or the write would starve.
    assert write_acquired.is_set()


def test_closed_loop_ticket_is_pruned() -> None:
    scheduler = FairReadWriteScheduler(max_parallel=1)
    loop = asyncio.new_event_loop()
    ticket = _Ticket(
        kind=READ, seq=0, loop=loop, wakeup=loop.create_future()
    )
    loop.close()
    with scheduler._lock:  # noqa: SLF001
        scheduler._queue.append(ticket)  # noqa: SLF001
        scheduler._grant_locked()  # noqa: SLF001
    assert scheduler._queue == []  # noqa: SLF001
    assert ticket.cancelled


def test_contended_loops_are_collectable() -> None:
    scheduler = FairReadWriteScheduler(max_parallel=1)
    refs: list[weakref.ReferenceType[asyncio.AbstractEventLoop]] = []

    def run() -> None:
        async def main() -> None:
            loop = asyncio.get_running_loop()
            refs.append(weakref.ref(loop))
            ticket = await scheduler.acquire(READ)
            await asyncio.sleep(0.02)
            scheduler.release_nowait(ticket)

        asyncio.run(main())

    threads = [threading.Thread(target=run) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    gc.collect()
    # The old per-loop WeakKeyDictionary kept each loop alive through its state's
    # reference cycle; the shared scheduler must retain none of them.
    assert refs
    assert all(ref() is None for ref in refs)


def test_daemon_pool_bounds_the_pending_queue() -> None:
    pool = _DaemonDispatchPool(max_workers=1, max_pending=1)
    started = threading.Event()
    release = threading.Event()

    def block() -> str:
        started.set()
        release.wait(timeout=10)
        return "done"

    first = pool.submit(block)
    assert started.wait(5)
    queued = pool.submit(block)  # occupies the single pending slot
    with pytest.raises(_DispatchQueueFull):
        pool.submit(block)
    release.set()
    assert first.result(timeout=5) == "done"
    assert queued.result(timeout=5) == "done"


def test_daemon_pool_drops_cancelled_queue_entries() -> None:
    pool = _DaemonDispatchPool(max_workers=1, max_pending=1)
    started = threading.Event()
    release = threading.Event()

    def block() -> str:
        started.set()
        release.wait(timeout=10)
        return "done"

    first = pool.submit(block)
    assert started.wait(5)
    queued = pool.submit(block)
    assert queued.cancel() is True
    # The cancelled entry is dropped, so its slot is reusable.
    replacement = pool.submit(block)
    release.set()
    assert first.result(timeout=5) == "done"
    assert replacement.result(timeout=5) == "done"
    assert queued.cancelled()


def test_release_is_idempotent_and_cancel_releases_once() -> None:
    scheduler = FairReadWriteScheduler(max_parallel=1)

    async def scenario() -> None:
        ticket = await scheduler.acquire(WRITE)
        scheduler.release_nowait(ticket)
        scheduler.release_nowait(ticket)  # a second release must be a no-op
        again = await asyncio.wait_for(scheduler.acquire(WRITE), timeout=2)
        scheduler.release_nowait(again)

        # A cancelled pending acquire leaves the queue empty and never blocks.
        holder = await scheduler.acquire(WRITE)
        waiter = asyncio.ensure_future(scheduler.acquire(READ))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        scheduler.release_nowait(holder)
        assert scheduler._queue == []  # noqa: SLF001
        nxt = await asyncio.wait_for(scheduler.acquire(READ), timeout=2)
        scheduler.release_nowait(nxt)

    asyncio.run(scenario())
