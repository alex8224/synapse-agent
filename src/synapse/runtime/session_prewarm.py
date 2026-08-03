"""Background session prewarm: prime the provider's prefix cache after resume.

Resuming a large session means every turn resends the whole history, so the
first model call pays a long prefill (tens of seconds to minutes) before the
first token arrives. When ``session_prewarm_enabled`` is on, the TUI fires one
background no-op model call so the provider pre-fills and caches the shared
history prefix. The user's first real message then reuses that cached prefix
and starts streaming almost immediately.

Design constraints:

- The warm-up must not mutate the real thread. A temporary thread is seeded
  from a checkpoint read of the real thread and deleted afterwards.
- The warm-up must use the exact same graph, system prompt, middleware and
  tool schemas as a real turn, otherwise the provider's prefix cache key
  differs and nothing is cached. This is why the live agent is reused instead
  of rebuilding one.
- The warm-up must never execute tools: the stream is interrupted before the
  ``tools`` node, so the model may emit a tool call but nothing runs.
- It runs on a background thread; the TUI cancels it as soon as the user
  submits a real message so the two large requests never queue concurrently.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from typing import Any

from langchain_core.messages import BaseMessage

from synapse.sessions.transcript import load_messages_from_agent

# Short prompt designed to produce a trivial, tool-free response while still
# carrying the full history prefix that we want cached.
_PREWARM_PROMPT = "Reply with exactly: OK. Do not call any tools."


def _thread_messages(agent: Any, thread_id: str) -> list[BaseMessage]:
    """Read the real thread's messages without mutating it."""
    try:
        return load_messages_from_agent(agent, thread_id) or []
    except Exception:  # noqa: BLE001 - prewarm is best-effort
        return []


def _seed_temporary_thread(agent: Any, thread_id: str, messages: list[BaseMessage]) -> str:
    """Copy messages into a throwaway thread and return its id."""
    tmp_thread = f"prewarm-{thread_id}-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": tmp_thread}}
    runtime = getattr(agent, "_coding_async_runtime", None)
    if runtime is not None and callable(getattr(agent, "aupdate_state", None)):
        runtime.run(agent.aupdate_state(config, {"messages": messages}))
    else:
        agent.update_state(config, {"messages": messages})
    return tmp_thread


def _delete_temporary_thread(agent: Any, thread_id: str) -> None:
    saver = getattr(agent, "_coding_checkpointer", None)
    delete = getattr(saver, "delete_thread", None)
    if not callable(delete):
        return
    try:
        delete(thread_id)
    except Exception:  # noqa: BLE001 - cleanup is best-effort
        pass


def prewarm_session(
    agent: Any,
    thread_id: str,
    *,
    min_messages: int = 100,
    cancel_event: threading.Event | None = None,
    notify: Callable[[str], None] | None = None,
) -> bool:
    """Pre-fill the provider cache for ``thread_id``'s history on the live agent.

    Returns ``True`` when the warm-up stream completed (or was cancelled), and
    ``False`` when it could not start (no messages / no agent / fatal error).
    Failures are swallowed and reported via ``notify`` if provided, because a
    warm-up must never break the session.
    """
    if agent is None or not thread_id:
        return False

    def _note(text: str) -> None:
        if notify is not None:
            try:
                notify(text)
            except Exception:  # noqa: BLE001
                pass

    messages = _thread_messages(agent, thread_id)
    if len(messages) < min_messages:
        return False

    tmp_thread = ""
    try:
        tmp_thread = _seed_temporary_thread(agent, thread_id, messages)
        config = {
            "configurable": {"thread_id": tmp_thread},
            "interrupt_before": ["tools"],
        }
        payload = {"messages": [{"role": "user", "content": _PREWARM_PROMPT}]}
        runtime = getattr(agent, "_coding_async_runtime", None)

        if runtime is not None and callable(getattr(agent, "astream", None)):
            async def _run() -> bool:
                async for _ in agent.astream(
                    payload,
                    config=config,
                    stream_mode=["messages"],
                    version="v2",
                ):
                    if cancel_event is not None and cancel_event.is_set():
                        return False
                return True

            completed = runtime.run(_run())
        else:
            for _ in agent.stream(payload, config=config, stream_mode=["messages"]):
                if cancel_event is not None and cancel_event.is_set():
                    return False
            completed = True
        if completed:
            _note("session prewarmed (provider cache ready)")
        return completed
    except Exception as exc:  # noqa: BLE001 - prewarm must never break the session
        _note(f"session prewarm skipped: {exc}")
        return False
    finally:
        if tmp_thread:
            _delete_temporary_thread(agent, tmp_thread)
