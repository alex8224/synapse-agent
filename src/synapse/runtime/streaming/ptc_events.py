"""Helpers for PTC (programmatic tool calling) ``custom`` stream events.

The PTC middleware writes plain dicts through ``ToolRuntime.stream_writer`` into
LangGraph's ``custom`` stream mode.  Those events are *not* model history: they
only describe the tool calls a ``run_code`` sandbox makes on the model's behalf,
so the semantic parser turns them into the ordinary nested ``ToolItem`` lifecycle
and never into a message.  Keeping the recognition and the parent/child
bookkeeping here stops the parser loop from growing a second event vocabulary.

A PTC event names its parent by the *provider tool call id* of the enclosing
``run_code`` call.  That is a different namespace from the parser's ``ToolItem.id``
(``g1-0`` ...), so every child is attached through the live parent item resolved
by ``call_id``.  An event whose parent is not yet known is deferred (bounded)
until the parent item appears; it is never attached to an unrelated running task.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from synapse.runtime.streaming.tool_model import ToolItem
from synapse.runtime.timeline import build_tool_item

PTC_EVENT_TYPE = "ptc_tool"
_EVENT_STARTED = "started"
_EVENT_FINISHED = "finished"
_MAX_PREVIEW_CHARS = 400
# A sandbox child emits a handful of events per call.  The bound only exists so a
# parent that never appears cannot retain an unbounded number of orphans.
_MAX_DEFERRED_EVENTS = 64


@dataclass(frozen=True)
class PtcToolEvent:
    """One recognized PTC tool lifecycle event."""

    event: str
    parent_call_id: str
    call_id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    status: str | None = None
    preview: str | None = None


def parse_ptc_event(data: Any) -> PtcToolEvent | None:
    """Recognize only a well-formed PTC tool event; ignore anything else.

    ``custom`` is a shared channel, so the ``type`` discriminator is the only
    thing that may turn a payload into a tool item.  Any other dict, or any
    malformed PTC event, is dropped without touching the timeline.
    """
    if not isinstance(data, dict):
        return None
    if str(data.get("type") or "") != PTC_EVENT_TYPE:
        return None
    event = str(data.get("event") or "").strip().lower()
    if event not in {_EVENT_STARTED, _EVENT_FINISHED}:
        return None
    parent_call_id = str(data.get("parent_call_id") or "").strip()
    call_id = str(data.get("call_id") or "").strip()
    name = str(data.get("name") or "").strip()
    if not parent_call_id or not call_id or not name:
        return None
    raw_args = data.get("args")
    args = dict(raw_args) if isinstance(raw_args, dict) else {}
    status = data.get("status")
    status = str(status).strip().lower() if status is not None else None
    return PtcToolEvent(
        event=event,
        parent_call_id=parent_call_id,
        call_id=call_id,
        name=name,
        args=args,
        status=status,
        preview=_bounded_preview(data.get("preview")),
    )


def _bounded_preview(value: Any, *, limit: int = _MAX_PREVIEW_CHARS) -> str | None:
    """Collapse an arbitrary preview payload to a bounded, non-empty string."""
    if value is None:
        return None
    text = str(value)
    if not text.strip():
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _resolve_parent(pending: list[ToolItem], parent_call_id: str) -> ToolItem | None:
    """Find the top-level item whose provider call id matches, if it is live.

    Only a non-nested item can be a PTC parent, so a child of one sandbox can
    never be mistaken for the parent of another.
    """
    wanted = str(parent_call_id or "").strip()
    if not wanted:
        return None
    for item in pending:
        if getattr(item, "sub", False):
            continue
        if str(getattr(item, "call_id", "") or "") == wanted:
            return item
    return None


def _discard(pending: list[ToolItem], item: ToolItem) -> None:
    try:
        pending.remove(item)
    except ValueError:
        pass


class PtcToolTracker:
    """Turn PTC ``custom`` events into nested ``ToolItem`` lifecycle calls.

    One tracker lives for a single turn.  It owns the child bookkeeping (so a
    ``finished`` event can find the item its ``started`` event created) and the
    bounded orphan queue used when a child is seen before its parent.
    """

    def __init__(self, *, max_deferred: int = _MAX_DEFERRED_EVENTS) -> None:
        self._max_deferred = max(0, int(max_deferred))
        self._children: dict[str, ToolItem] = {}
        self._deferred: dict[str, list[PtcToolEvent]] = {}
        self._deferred_count = 0

    def handle(self, data: Any, pending: list[ToolItem], sink: Any) -> None:
        """Apply one ``custom`` payload; unknown or orphaned events are dropped."""
        event = parse_ptc_event(data)
        if event is None:
            return
        parent = _resolve_parent(pending, event.parent_call_id)
        if parent is None:
            self._defer(event)
            return
        self._apply(event, parent, pending, sink)
        # Drain any other event that was queued while the parent was missing.
        self._flush(event.parent_call_id, pending, sink)

    def handle_parent_started(self, parent: ToolItem, pending: list[ToolItem], sink: Any) -> None:
        """Flush children that arrived before their parent item existed."""
        call_id = str(getattr(parent, "call_id", "") or "")
        if call_id:
            self._flush(call_id, pending, sink)

    def finish_parent_children(
        self,
        parent: ToolItem,
        pending: list[ToolItem],
        sink: Any,
    ) -> None:
        """Seal a parent's still-running children so no spinner is left behind.

        The parent's tool result is the sandbox's terminal event; a child whose
        own ``finished`` never arrived (cut short, dropped, or cancelled) must
        still land in a terminal state instead of spinning forever.
        """
        parent_id = str(getattr(parent, "id", "") or "")
        if not parent_id:
            return
        parent_status = str(getattr(parent, "status", "") or "")
        failed = bool(getattr(parent, "error", False)) or parent_status in {
            "error",
            "failed",
            "cancelled",
            "canceled",
        }
        for child in [c for c in pending if c.sub and c.parent_id == parent_id]:
            if child.status != "running":
                continue
            status = "error" if failed else "ok"
            child.status = status
            child.error = failed
            sink.tool_item_finished(
                child.id,
                status=status,
                preview=child.preview,
                error=failed,
            )
            _discard(pending, child)
            self._children.pop(str(getattr(child, "call_id", "") or ""), None)
        self._drop_deferred(str(getattr(parent, "call_id", "") or ""))

    def _apply(
        self,
        event: PtcToolEvent,
        parent: ToolItem,
        pending: list[ToolItem],
        sink: Any,
    ) -> None:
        if event.event == _EVENT_STARTED:
            self._start(event, parent, pending, sink)
        else:
            self._finish(event, pending, sink)

    def _start(
        self,
        event: PtcToolEvent,
        parent: ToolItem,
        pending: list[ToolItem],
        sink: Any,
    ) -> None:
        if event.call_id in self._children:
            return  # a duplicate start never paints a second row
        child = build_ptc_child(event, parent)
        self._children[event.call_id] = child
        pending.append(child)
        sink.tool_item_started(child)

    def _finish(self, event: PtcToolEvent, pending: list[ToolItem], sink: Any) -> None:
        child = self._children.get(event.call_id)
        if child is None:
            return  # a finish without a start has nothing to close
        failed = str(event.status or "").lower() in {"error", "failed", "failure"}
        status = "error" if failed else "ok"
        child.status = status
        child.error = failed
        if event.preview is not None:
            child.preview = event.preview
        sink.tool_item_finished(
            child.id,
            status=status,
            preview=child.preview,
            error=failed,
        )
        _discard(pending, child)
        self._children.pop(event.call_id, None)

    def _defer(self, event: PtcToolEvent) -> None:
        if self._max_deferred <= 0 or self._deferred_count >= self._max_deferred:
            return
        self._deferred.setdefault(event.parent_call_id, []).append(event)
        self._deferred_count += 1

    def _flush(self, parent_call_id: str, pending: list[ToolItem], sink: Any) -> None:
        key = str(parent_call_id or "")
        events = self._deferred.pop(key, None)
        if not events:
            return
        self._deferred_count -= len(events)
        parent = _resolve_parent(pending, key)
        if parent is None:
            return
        for event in events:
            self._apply(event, parent, pending, sink)

    def _drop_deferred(self, parent_call_id: str) -> None:
        events = self._deferred.pop(str(parent_call_id or ""), None)
        if events:
            self._deferred_count -= len(events)


def build_ptc_child(event: PtcToolEvent, parent: ToolItem) -> ToolItem:
    """Build the nested item one PTC ``started`` event describes."""
    child = build_tool_item(
        {"name": event.name, "args": event.args, "id": event.call_id},
        item_id=f"{parent.id}-ptc-{event.call_id}",
        sub=True,
    )
    child.sub = True
    child.parent_id = str(parent.id)
    child.call_id = event.call_id
    return child
