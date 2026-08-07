"""Compatibility exports for runtime-owned LangGraph stream iteration."""
from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

from synapse.runtime.streaming.runtime import (
    _is_sync_only_checkpointer_error as _runtime_sync_error,
)
from synapse.runtime.streaming.runtime import (
    _iter_stream_events as _runtime_iter_stream_events,
)
from synapse.runtime.streaming.runtime import (
    checkpointer_supports_async as _runtime_checkpointer_supports_async,
)


def checkpointer_supports_async(checkpointer: Any) -> bool:
    """Whether a LangGraph checkpointer is safe for agent.astream.

    Sync ``SqliteSaver`` raises RuntimeError under async graph methods.
    """
    return _runtime_checkpointer_supports_async(checkpointer)


def _is_sync_only_checkpointer_error(exc: BaseException) -> bool:
    """True for SqliteSaver/async mismatch errors that should fall back to sync stream."""
    return _runtime_sync_error(exc)




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
    yield from _runtime_iter_stream_events(
        agent,
        payload,
        config,
        token_stream=token_stream,
        prefer_async=prefer_async,
        subgraphs=subgraphs,
        cancel_event=cancel_event,
    )
