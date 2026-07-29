"""LangGraph stream iteration and checkpointer compatibility runtime."""
from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import Iterator
from typing import Any

from synapse.ui.stream_events import _normalize_stream_item


def checkpointer_supports_async(checkpointer: Any) -> bool:
    """Whether a LangGraph checkpointer is safe for agent.astream.

    Sync ``SqliteSaver`` raises RuntimeError under async graph methods.
    """
    if checkpointer is None:
        return True
    cls = type(checkpointer)
    name = cls.__name__
    module = cls.__module__ or ""
    if name == "SqliteSaver" and ".aio" not in module:
        return False
    if name.startswith("Async") and "Saver" in name:
        return True
    # MemorySaver and most modern savers expose aget_tuple.
    if callable(getattr(checkpointer, "aget_tuple", None)):
        return True
    if callable(getattr(checkpointer, "aget", None)):
        return True
    return True


def _bound_async_loop(agent: Any) -> asyncio.AbstractEventLoop | None:
    """Event loop bound to AsyncSqliteSaver / agent async runtime, if any."""
    runtime = getattr(agent, "_coding_async_runtime", None)
    if runtime is not None:
        loop = getattr(runtime, "loop", None)
        if loop is not None:
            try:
                if loop.is_running():
                    return loop
            except Exception:  # noqa: BLE001
                pass
    cp = getattr(agent, "_coding_checkpointer", None)
    loop = getattr(cp, "loop", None) if cp is not None else None
    if loop is not None:
        try:
            if loop.is_running():
                return loop
        except Exception:  # noqa: BLE001
            pass
    return None


def _is_sync_only_checkpointer_error(exc: BaseException) -> bool:
    """True for SqliteSaver/async mismatch errors that should fall back to sync stream."""
    msg = str(exc).lower()
    if "does not support async" in msg:
        return True
    if "asyncsqlitesaver" in msg and "aiosqlite" in msg:
        return True
    if "sqlitesaver" in msg and "async" in msg:
        return True
    return False




def _iter_stream_events(
    agent,
    payload: Any,
    config: dict[str, Any],
    *,
    token_stream: bool,
    prefer_async: bool,
    subgraphs: bool,
    cancel_event: threading.Event | None = None,
) -> Iterator[tuple[str, Any, tuple[str, ...]]]:
    modes: list[str] = ["updates"]
    if token_stream:
        modes = ["messages", "updates"]

    def _put_norm(q: queue.Queue[Any], item: Any) -> None:
        q.put(_normalize_stream_item(item))

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    if prefer_async and hasattr(agent, "astream"):
        q: queue.Queue[Any] = queue.Queue()
        error_box: list[BaseException] = []
        done_box: list[bool] = []

        async def _astream_once(**kwargs: Any):
            async for item in agent.astream(payload, config=config, **kwargs):
                if _cancelled():
                    break
                _put_norm(q, item)

        async def _produce() -> None:
            kwargs: dict[str, Any] = {
                "stream_mode": modes,
                "subgraphs": subgraphs,
            }
            try:
                await _astream_once(version="v2", **kwargs)
            except TypeError:
                try:
                    await _astream_once(**kwargs)
                except TypeError:
                    await _astream_once(stream_mode=modes)
            except asyncio.CancelledError:
                return
            except BaseException as exc:  # noqa: BLE001
                error_box.append(exc)
            finally:
                q.put(None)

        async def _main() -> None:
            prod = asyncio.create_task(_produce())
            if cancel_event is None:
                await prod
                return
            # Poll cancel so ESC can interrupt long model/tool waits.
            while not prod.done():
                if cancel_event.is_set():
                    prod.cancel()
                    try:
                        await prod
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                    # Ensure consumer unblocks even if finally was skipped.
                    try:
                        q.put_nowait(None)
                    except Exception:  # noqa: BLE001
                        q.put(None)
                    return
                await asyncio.sleep(0.05)
            await prod

        bound_loop = _bound_async_loop(agent)
        worker_thread: threading.Thread | None = None
        bound_future: Any | None = None

        if bound_loop is not None and bound_loop.is_running():
            # AsyncSqliteSaver path: schedule on the checkpointer's loop.
            try:
                bound_future = asyncio.run_coroutine_threadsafe(_main(), bound_loop)
            except BaseException as exc:  # noqa: BLE001
                error_box.append(exc)
                q.put(None)
        else:
            # MemorySaver / no bound loop: dedicated worker + asyncio.run.
            def _runner() -> None:
                try:
                    asyncio.run(_main())
                except BaseException as exc:  # noqa: BLE001
                    error_box.append(exc)
                    try:
                        q.put_nowait(None)
                    except Exception:  # noqa: BLE001
                        q.put(None)
                finally:
                    done_box.append(True)

            worker_thread = threading.Thread(
                target=_runner, name="agent-astream", daemon=True
            )
            worker_thread.start()

        while True:
            if _cancelled():
                # Unblock promptly; producer task is being cancelled in parallel.
                try:
                    item = q.get(timeout=0.15)
                except queue.Empty:
                    yield "__cancelled__", None, ()
                    break
                if item is None:
                    yield "__cancelled__", None, ()
                    break
                yield item
                continue
            try:
                item = q.get(timeout=0.2)
            except queue.Empty:
                # If bound future finished without sentinel, stop.
                if bound_future is not None and bound_future.done() and q.empty():
                    break
                yield "__heartbeat__", None, ()
                continue
            if item is None:
                if _cancelled():
                    yield "__cancelled__", None, ()
                break
            yield item

        if worker_thread is not None:
            worker_thread.join(timeout=1.5)
        if bound_future is not None:
            try:
                bound_future.result(timeout=1.5)
            except Exception as exc:  # noqa: BLE001
                if not error_box and not _cancelled():
                    error_box.append(exc)
        if error_box:
            err = error_box[0]
            # Cancellation-induced errors are expected; ignore soft failures.
            if _cancelled() or isinstance(err, asyncio.CancelledError):
                return
            # Fall through to sync stream when:
            # - TypeError: astream kwargs (version/subgraphs) not supported
            # - sync-only checkpointer used under astream (SqliteSaver)
            # Other runtime/API failures must still surface.
            if isinstance(err, TypeError) or _is_sync_only_checkpointer_error(err):
                if bool(getattr(agent, "_coding_async_only", False)):
                    raise err
            else:
                raise err
        else:
            return

    def _sync_iter(**kwargs: Any):
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
            for item in _sync_iter(**attempt):
                if _cancelled():
                    yield "__cancelled__", None, ()
                    return
                yield _normalize_stream_item(item)
            return
        except TypeError as exc:
            sync_errors.append(exc)
            continue
        except asyncio.CancelledError:
            if _cancelled():
                yield "__cancelled__", None, ()
                return
            raise
    if sync_errors:
        raise sync_errors[-1]
