"""LangGraph stream iteration and checkpointer compatibility runtime."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections.abc import Iterator
from typing import Any

from synapse.runtime.async_runtime import await_cancel_cleanup
from synapse.runtime.streaming.events import normalize_stream_item

_STREAM_QUEUE_MAXSIZE = 256

logger = logging.getLogger(__name__)

# Normal-path bound for graph/checkpointer cleanup after event production has
# finished. Cleanup is not a provider failure: waiting past this bound only
# logs a bounded diagnostic and lets the cleanup finish on its own thread/loop.
# The cancelled path waits indefinitely so the turn never reports CANCELLED
# while LangGraph is still writing checkpoint state in the background.
_CLEANUP_WAIT_TIMEOUT = 30.0


def checkpointer_supports_async(checkpointer: Any) -> bool:
    """Whether a LangGraph checkpointer is safe for ``agent.astream``."""
    if checkpointer is None:
        return True
    cls = type(checkpointer)
    name = cls.__name__
    module = cls.__module__ or ""
    if name == "SqliteSaver" and ".aio" not in module:
        return False
    if name.startswith("Async") and "Saver" in name:
        return True
    if callable(getattr(checkpointer, "aget_tuple", None)):
        return True
    if callable(getattr(checkpointer, "aget", None)):
        return True
    return True


def _bound_async_loop(agent: Any) -> asyncio.AbstractEventLoop | None:
    """Return the event loop bound to the agent/checkpointer, if available."""
    runtime = getattr(agent, "_coding_async_runtime", None)
    if runtime is not None:
        loop = getattr(runtime, "loop", None)
        if loop is not None:
            try:
                if loop.is_running():
                    return loop
            except Exception:  # noqa: BLE001
                pass
    checkpointer = getattr(agent, "_coding_checkpointer", None)
    loop = getattr(checkpointer, "loop", None) if checkpointer is not None else None
    if loop is not None:
        try:
            if loop.is_running():
                return loop
        except Exception:  # noqa: BLE001
            pass
    return None


def is_sync_only_checkpointer_error(exc: BaseException) -> bool:
    """Whether an async stream failed because its saver is sync-only."""
    message = str(exc).lower()
    if "does not support async" in message:
        return True
    if "asyncsqlitesaver" in message and "aiosqlite" in message:
        return True
    if "sqlitesaver" in message and "async" in message:
        return True
    return False


def iter_stream_events(
    agent: Any,
    payload: Any,
    config: dict[str, Any],
    *,
    token_stream: bool,
    prefer_async: bool,
    subgraphs: bool,
    cancel_event: threading.Event | None = None,
) -> Iterator[tuple[str, Any, tuple[str, ...]]]:
    """Yield normalized stream events with async/sync saver compatibility."""
    modes: list[str] = ["messages", "updates"] if token_stream else ["updates"]

    def cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    if prefer_async and hasattr(agent, "astream"):
        # Bound cross-thread buffering. A runaway graph can otherwise enqueue
        # updates faster than the renderer consumes them and retain the whole
        # turn in memory even after the user has requested cancellation.
        event_queue: queue.Queue[Any] = queue.Queue(maxsize=_STREAM_QUEUE_MAXSIZE)
        errors: list[BaseException] = []

        async def put_normalized(item: Any) -> bool:
            normalized = normalize_stream_item(item)
            while not cancelled():
                try:
                    event_queue.put_nowait(normalized)
                    return True
                except queue.Full:
                    await asyncio.sleep(0.01)
            return False

        async def astream_once(**kwargs: Any) -> None:
            stream = agent.astream(payload, config=config, **kwargs)
            try:
                async for item in stream:
                    if cancelled() or not await put_normalized(item):
                        break
            finally:
                close = getattr(stream, "aclose", None)
                if callable(close):
                    try:
                        await close()
                    except asyncio.CancelledError as exc:
                        await await_cancel_cleanup(exc)
                        raise

        async def produce() -> None:
            kwargs: dict[str, Any] = {"stream_mode": modes, "subgraphs": subgraphs}
            try:
                await astream_once(version="v2", **kwargs)
            except TypeError:
                try:
                    await astream_once(**kwargs)
                except TypeError:
                    await astream_once(stream_mode=modes)
            except asyncio.CancelledError as exc:
                # LangGraph may attach AsyncPregelLoop's pending async-exit
                # task to this exception. Swallowing it before awaiting that
                # task leaves retry/checkpointer coroutines alive.
                await await_cancel_cleanup(exc)
                return
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                try:
                    event_queue.put_nowait(None)
                except queue.Full:
                    # The consumer also observes producer completion. Never
                    # block the checkpointer-bound event loop on a full queue.
                    pass

        async def main() -> None:
            producer = asyncio.create_task(produce())
            if cancel_event is None:
                await producer
                return
            while not producer.done():
                if cancel_event.is_set():
                    producer.cancel()
                    try:
                        await producer
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                    try:
                        event_queue.put_nowait(None)
                    except Exception:  # noqa: BLE001
                        event_queue.put(None)
                    return
                await asyncio.sleep(0.05)
            await producer

        bound_loop = _bound_async_loop(agent)
        worker_thread: threading.Thread | None = None
        bound_future: Any | None = None

        if bound_loop is not None and bound_loop.is_running():
            try:
                bound_future = asyncio.run_coroutine_threadsafe(main(), bound_loop)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                event_queue.put(None)
        else:

            def runner() -> None:
                try:
                    asyncio.run(main())
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                    try:
                        event_queue.put_nowait(None)
                    except Exception:  # noqa: BLE001
                        event_queue.put(None)

            worker_thread = threading.Thread(
                target=runner,
                name="agent-astream",
                daemon=True,
            )
            worker_thread.start()

        while True:
            if cancelled():
                # Drop buffered graph events immediately. Rendering stale
                # updates after Esc both delays cancellation and retains large
                # message/tool payloads unnecessarily.
                while True:
                    try:
                        event_queue.get_nowait()
                    except queue.Empty:
                        break
                yield "__cancelled__", None, ()
                break
            try:
                item = event_queue.get(timeout=0.2)
            except queue.Empty:
                if bound_future is not None and bound_future.done() and event_queue.empty():
                    break
                yield "__heartbeat__", None, ()
                continue
            if item is None:
                if cancelled():
                    yield "__cancelled__", None, ()
                break
            yield item

        if worker_thread is not None:
            worker_thread.join(timeout=None if cancelled() else 1.5)
            if not cancelled() and worker_thread.is_alive():
                logger.warning(
                    "stream cleanup thread still alive after %.1fs; "
                    "turn events were already produced",
                    _CLEANUP_WAIT_TIMEOUT,
                )
        if bound_future is not None:
            try:
                # A cancelled turn is not terminal until LangGraph has exited
                # its executor/checkpointer contexts. Waiting here keeps the
                # SessionRuntime in CANCELLING instead of reporting a false
                # stop while the graph continues in the background.
                bound_future.result(timeout=None if cancelled() else _CLEANUP_WAIT_TIMEOUT)
            except TimeoutError:
                # Event production already finished (the consumer observed the
                # queue sentinel or a done future). A slow graph/checkpointer
                # cleanup is diagnostic only, never a provider failure: do not
                # turn this into a FAILED turn.
                logger.warning(
                    "stream cleanup still running after %.1fs (cancelled=%s); "
                    "turn events were already produced",
                    _CLEANUP_WAIT_TIMEOUT,
                    cancelled(),
                )
            except Exception as exc:  # noqa: BLE001
                if not errors and not cancelled():
                    errors.append(exc)
        while True:
            try:
                event_queue.get_nowait()
            except queue.Empty:
                break
        if errors:
            error = errors[0]
            if cancelled() or isinstance(error, asyncio.CancelledError):
                return
            if isinstance(error, TypeError) or is_sync_only_checkpointer_error(error):
                if bool(getattr(agent, "_coding_async_only", False)):
                    raise error
            else:
                raise error
        else:
            return

    def sync_iter(**kwargs: Any) -> Any:
        return agent.stream(payload, config=config, **kwargs)

    sync_errors: list[BaseException] = []
    for attempt in (
        {"stream_mode": modes, "subgraphs": subgraphs, "version": "v2"},
        {"stream_mode": modes, "subgraphs": subgraphs},
        {"stream_mode": modes, "version": "v2"},
        {"stream_mode": modes},
        {"stream_mode": "updates"},
    ):
        try:
            for item in sync_iter(**attempt):
                if cancelled():
                    yield "__cancelled__", None, ()
                    return
                yield normalize_stream_item(item)
            return
        except TypeError as exc:
            sync_errors.append(exc)
            continue
        except asyncio.CancelledError:
            if cancelled():
                yield "__cancelled__", None, ()
                return
            raise
    if sync_errors:
        raise sync_errors[-1]


# Compatibility aliases retained for existing callers/tests.
_iter_stream_events = iter_stream_events
_is_sync_only_checkpointer_error = is_sync_only_checkpointer_error