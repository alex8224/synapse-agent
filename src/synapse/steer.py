"""Mid-run user guidance queue (type-B HITL / steer).

While the agent loop is busy, the user can enqueue short instructions.
They are drained at the next model step (after the current tool batch) and
injected as HumanMessages so the LLM can change direction without Esc-cancel.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from typing import Any

STEER_PREFIX = "[Mid-run user guidance]"
# UI-only label used for silent follow-up turns (should never paint in transcript).
STEER_FOLLOWUP_PREFIX = "[steer follow-up]"

SteerListener = Callable[[list[str]], None]


def is_steer_message(msg: Any = None, *, text: str | None = None) -> bool:
    """True if a LangChain message / plain text is mid-run guidance (model-only).

    Used to keep steer chrome out of the transcript and status bar.
    """
    if text is None and msg is not None:
        # Prefer kwargs flag set by middleware.
        ak = getattr(msg, "additional_kwargs", None)
        if isinstance(ak, dict) and ak.get("coding_steer"):
            return True
        if isinstance(msg, dict):
            ak2 = msg.get("additional_kwargs") or {}
            if isinstance(ak2, dict) and ak2.get("coding_steer"):
                return True
            content = msg.get("content")
        else:
            content = getattr(msg, "content", None)
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and block.get("text"):
                    parts.append(str(block.get("text")))
            text = "\n".join(parts)
        else:
            text = str(content or "")
    body = (text or "").strip()
    if not body:
        return False
    if body.startswith(STEER_PREFIX) or STEER_PREFIX in body[:80]:
        return True
    if body.startswith(STEER_FOLLOWUP_PREFIX):
        return True
    return False


class SteerQueue:
    """Thread-safe FIFO of mid-run guidance strings."""

    def __init__(self) -> None:
        self._items: deque[str] = deque()
        self._lock = threading.Lock()
        self._listeners: list[SteerListener] = []

    def add_listener(self, callback: SteerListener) -> None:
        """Register a callback invoked with a snapshot after each change."""
        if callback is None:
            return
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: SteerListener) -> None:
        with self._lock:
            self._listeners = [cb for cb in self._listeners if cb is not callback]

    def _snapshot(self) -> list[str]:
        return list(self._items)

    def _notify_unlocked(self) -> None:
        snap = self._snapshot()
        listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(snap)
            except Exception:  # noqa: BLE001
                pass

    def push(self, text: str) -> int:
        """Enqueue guidance. Returns pending count after push (0 if empty text)."""
        body = (text or "").strip()
        if not body:
            return 0
        with self._lock:
            self._items.append(body)
            n = len(self._items)
            self._notify_unlocked()
            return n

    def drain(self) -> list[str]:
        """Pop all pending guidance (order preserved)."""
        with self._lock:
            items = list(self._items)
            self._items.clear()
            if items:
                self._notify_unlocked()
            return items

    def peek_count(self) -> int:
        with self._lock:
            return len(self._items)

    def peek_items(self) -> list[str]:
        """Return a copy of pending items (order preserved)."""
        with self._lock:
            return self._snapshot()

    def remove_at(self, index: int) -> str | None:
        """Remove one item by index. Returns removed text or None."""
        with self._lock:
            if index < 0 or index >= len(self._items):
                return None
            # deque has no pop(index); rebuild
            items = list(self._items)
            removed = items.pop(index)
            self._items = deque(items)
            self._notify_unlocked()
            return removed

    def clear(self) -> list[str]:
        """Drop all pending items. Returns the previous list."""
        with self._lock:
            if not self._items:
                return []
            items = list(self._items)
            self._items.clear()
            self._notify_unlocked()
            return items


def format_steer_message(items: list[str]) -> str:
    """Render queued guidance for the model."""
    cleaned = [str(x).strip() for x in items if str(x).strip()]
    if not cleaned:
        return ""
    header = (
        f"{STEER_PREFIX}\n"
        "The user is steering the current task. Incorporate the guidance below "
        "into your next actions. Prefer adjusting the plan over restarting "
        "unrelated work.\n"
    )
    if len(cleaned) == 1:
        return f"{header}\n{cleaned[0]}"
    body = "\n".join(f"{i}. {line}" for i, line in enumerate(cleaned, 1))
    return f"{header}\nMultiple notes (in order):\n{body}"


def format_steer_panel(items: list[str], *, max_preview: int = 72) -> str:
    """Render a multi-line panel for TUI (human-facing)."""
    cleaned = [str(x).strip() for x in items if str(x).strip()]
    if not cleaned:
        return ""
    n = len(cleaned)
    lines = [f"  ▸ steer queue · {n} pending · applies before next model step"]
    for i, text in enumerate(cleaned, 1):
        one = " ".join(text.split())
        if len(one) > max_preview:
            one = one[: max(0, max_preview - 1)] + "…"
        lines.append(f"    {i}. {one}")
    return "\n".join(lines)


def build_steer_middleware(queue: SteerQueue):
    """Inject drained queue messages before each model call (post-tool boundary)."""
    from langchain.agents.middleware import AgentMiddleware, AgentState
    from langchain_core.messages import HumanMessage

    def _logic(state: Any, runtime: Any) -> dict[str, Any] | None:  # noqa: ARG001
        items = queue.drain()
        if not items:
            return None
        content = format_steer_message(items)
        if not content:
            return None
        msg = HumanMessage(
            content=content,
            additional_kwargs={"coding_steer": True, "steer_count": len(items)},
        )
        return {"messages": [msg]}

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:  # noqa: ANN001
        return _logic(state, runtime)

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:  # noqa: ANN001
        return _logic(state, runtime)

    return type(
        "inject_steer_queue",
        (AgentMiddleware,),
        {
            "state_schema": AgentState,
            "tools": [],
            "before_model": before_model,
            "abefore_model": abefore_model,
        },
    )()


def get_agent_steer_queue(agent: Any) -> SteerQueue | None:
    q = getattr(agent, "_coding_steer_queue", None)
    return q if isinstance(q, SteerQueue) else None
