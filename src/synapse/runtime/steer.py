"""Mid-run user guidance queue (type-B HITL / steer).

While the agent loop is busy, the user can enqueue short instructions.
They are drained at the next model step (after the current tool batch) and
injected as HumanMessages so the LLM can change direction without Esc-cancel.

A note may carry images: the service layer resolves the session's durable
attachment refs into provider-shaped content blocks and enqueues them together
with the text, so the injected ``HumanMessage`` is multimodal exactly like an
ordinary turn.  The queue itself stays I/O-free: it only stores what it is
given.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

STEER_PREFIX = "[Mid-run user guidance]"
# UI-only label used for silent follow-up turns (should never paint in transcript).
STEER_FOLLOWUP_PREFIX = "[steer follow-up]"

SteerListener = Callable[[list[str]], None]

#: Placeholder text used when a note carries images but no visible text.
STEER_IMAGE_ONLY_TEXT = "(image)"


@dataclass(frozen=True, slots=True)
class SteerNote:
    """One queued piece of mid-run guidance.

    ``text`` is the human-readable note (TUI panel, listener snapshots, the
    plain-text injection path).  When the note carries images, ``blocks`` holds
    the provider-shaped content blocks built from them (text and image blocks,
    never the steer header), ``refs`` the JSON-safe durable attachment refs
    recorded on the injected message's metadata, and ``attachments`` the
    composer ``Attachment`` objects so a non-graph consumer (the TUI follow-up
    turn) can replay the same images.
    """

    text: str
    blocks: tuple[dict[str, Any], ...] = ()
    attachments: tuple[Any, ...] = ()
    refs: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "blocks", tuple(self.blocks))
        object.__setattr__(self, "attachments", tuple(self.attachments))
        object.__setattr__(self, "refs", tuple(self.refs))


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
    """Thread-safe FIFO of mid-run guidance notes (text, optionally with images).

    Every read API that existed before the multimodal channel keeps returning
    plain text (``drain``/``peek_items``/listener snapshots), so the TUI panel,
    status bar, and transcript filters are unchanged; the graph middleware uses
    ``drain_notes`` to see the attached content blocks.
    """

    def __init__(self) -> None:
        self._items: deque[SteerNote] = deque()
        self._lock = threading.Lock()
        self._listeners: list[SteerListener] = []
        self._pending_notifications: deque[
            tuple[list[str], list[SteerListener]]
        ] = deque()
        self._notifying = False

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
        return [note.text for note in self._items]

    def _enqueue_notification_unlocked(self) -> bool:
        self._queue_notification_unlocked()
        if self._notifying:
            return False
        self._notifying = True
        return True

    def _queue_notification_unlocked(self) -> None:
        """Queue a snapshot without claiming the lock-free notification drain."""
        self._pending_notifications.append((self._snapshot(), list(self._listeners)))

    def _dispatch_notifications(self) -> None:
        # Preserve mutation order without invoking application code under _lock.
        while True:
            with self._lock:
                if not self._pending_notifications:
                    self._notifying = False
                    return
                snap, listeners = self._pending_notifications.popleft()
            for callback in listeners:
                try:
                    callback(snap)
                except Exception:  # noqa: BLE001
                    pass

    def push(self, item: str | SteerNote) -> int:
        """Enqueue guidance. Returns pending count after push (0 if empty text)."""
        pending = self.push_silent(item)
        self.dispatch_pending()
        return pending

    @staticmethod
    def _coerce(item: str | SteerNote) -> SteerNote | None:
        """Normalize one enqueued item, or ``None`` when it carries nothing."""
        if isinstance(item, SteerNote):
            body = (item.text or "").strip()
            if not body and not item.blocks:
                return None
            if body == item.text:
                return item
            return SteerNote(
                text=body,
                blocks=item.blocks,
                attachments=item.attachments,
                refs=item.refs,
            )
        body = (item or "").strip()
        return SteerNote(text=body) if body else None

    def push_silent(self, item: str | SteerNote) -> int:
        """Enqueue guidance without invoking listener callbacks.

        The change notification is queued and delivered by a later
        :meth:`dispatch_pending` (or any other push/drain/clear), so callers
        that must not run external code under their own lock can append the
        item and dispatch after releasing it.  Returns the pending count after
        push (0 for an empty text-only item).
        """
        note = self._coerce(item)
        if note is None:
            return 0
        with self._lock:
            self._items.append(note)
            n = len(self._items)
            self._queue_notification_unlocked()
        return n

    def dispatch_pending(self) -> None:
        """Deliver queued listener notifications without holding caller locks.

        No-op when another drainer is already dispatching or nothing is
        pending; listeners always run outside this queue's ``_lock``.
        """
        with self._lock:
            if not self._pending_notifications or self._notifying:
                return
            self._notifying = True
        self._dispatch_notifications()

    def drain(self) -> list[str]:
        """Pop all pending guidance text (order preserved)."""
        return [note.text for note in self.drain_notes()]

    def drain_notes(self) -> list[SteerNote]:
        """Pop all pending notes, images included (order preserved)."""
        with self._lock:
            items = list(self._items)
            self._items.clear()
            should_dispatch = bool(items) and self._enqueue_notification_unlocked()
        if should_dispatch:
            self._dispatch_notifications()
        return items

    def peek_count(self) -> int:
        with self._lock:
            return len(self._items)

    def peek_items(self) -> list[str]:
        """Return a copy of pending item texts (order preserved)."""
        with self._lock:
            return self._snapshot()

    def peek_notes(self) -> list[SteerNote]:
        """Return a copy of pending notes, images included (order preserved)."""
        with self._lock:
            return list(self._items)

    def remove_at(self, index: int) -> str | None:
        """Remove one item by index. Returns removed text or None."""
        with self._lock:
            if index < 0 or index >= len(self._items):
                return None
            # deque has no pop(index); rebuild
            items = list(self._items)
            removed = items.pop(index)
            self._items = deque(items)
            should_dispatch = self._enqueue_notification_unlocked()
        if should_dispatch:
            self._dispatch_notifications()
        return removed.text

    def clear(self) -> list[str]:
        """Drop all pending items. Returns the previous texts."""
        with self._lock:
            if not self._items:
                return []
            items = list(self._items)
            self._items.clear()
            should_dispatch = self._enqueue_notification_unlocked()
        if should_dispatch:
            self._dispatch_notifications()
        return [note.text for note in items]

    def clear_silent(self) -> list[str]:
        """Drop all pending items without invoking listener callbacks.

        The change notification is queued and delivered by a later
        :meth:`dispatch_pending`, so callers can clear the queue while holding
        a lock that must never run external code inside it.
        """
        with self._lock:
            if not self._items:
                return []
            items = list(self._items)
            self._items.clear()
            self._queue_notification_unlocked()
        return [note.text for note in items]


def _steer_header() -> str:
    """The model-facing preamble shared by the text and multimodal paths."""
    return (
        f"{STEER_PREFIX}\n"
        "The user is steering the current task. Incorporate the guidance below "
        "into your next actions. Prefer adjusting the plan over restarting "
        "unrelated work.\n"
    )


def format_steer_message(items: list[str]) -> str:
    """Render queued guidance for the model."""
    cleaned = [str(x).strip() for x in items if str(x).strip()]
    if not cleaned:
        return ""
    header = _steer_header()
    if len(cleaned) == 1:
        return f"{header}\n{cleaned[0]}"
    body = "\n".join(f"{i}. {line}" for i, line in enumerate(cleaned, 1))
    return f"{header}\nMultiple notes (in order):\n{body}"


def format_steer_content(notes: list[SteerNote]) -> str | list[dict[str, Any]]:
    """Render queued notes for the model, keeping attached images.

    Text-only queues render exactly like :func:`format_steer_message` (a plain
    string), so nothing changes for the common case.  As soon as one note
    carries images the result becomes a content-block list: the steer preamble
    first, then each note's own blocks in queue order.  A text-only note in a
    mixed queue contributes its text as one block, numbered when several notes
    were drained together.
    """
    cleaned = [note for note in notes if note.text or note.blocks]
    if not cleaned:
        return ""
    if not any(note.blocks for note in cleaned):
        return format_steer_message([note.text for note in cleaned])
    multiple = len(cleaned) > 1
    blocks: list[dict[str, Any]] = [{"type": "text", "text": _steer_header()}]
    for index, note in enumerate(cleaned, 1):
        prefix = f"{index}. " if multiple else ""
        if not note.blocks:
            blocks.append({"type": "text", "text": f"{prefix}{note.text}"})
            continue
        for position, block in enumerate(note.blocks):
            text_block = str(block.get("type") or "") == "text"
            if position == 0 and prefix and text_block:
                blocks.append(
                    {"type": "text", "text": f"{prefix}{block.get('text') or ''}"}
                )
            else:
                blocks.append(dict(block))
    return blocks


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
        notes = queue.drain_notes()
        if not notes:
            return None
        content = format_steer_content(notes)
        if not content:
            return None
        kwargs: dict[str, Any] = {
            "coding_steer": True,
            "steer_count": len(notes),
        }
        # Durable refs are JSON-safe metadata, never a provider field: the same
        # key an ordinary user turn uses, so a rebuild can show the images.
        refs = [ref for note in notes for ref in note.refs]
        if refs:
            from synapse.content.multimodal import attachment_refs_metadata

            kwargs.update(attachment_refs_metadata(refs))
        msg = HumanMessage(
            content=content,
            additional_kwargs=kwargs,
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
