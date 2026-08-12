"""Cancellation metadata shared across runtime compatibility boundaries."""

from __future__ import annotations

import threading

_CANCEL_REASON_ATTR = "_synapse_cancel_reason"


def mark_cancel_event(event: threading.Event, reason: str) -> None:
    """Attach a cancellation origin before setting a compatibility event."""
    try:
        setattr(event, _CANCEL_REASON_ATTR, reason)
    except (AttributeError, TypeError):
        # Some Event-compatible implementations may reject custom attributes.
        pass


def cancel_reason_from_event(event: threading.Event | None) -> str | None:
    """Return Synapse cancellation metadata attached to a thread event."""
    if event is None:
        return None
    reason = getattr(event, _CANCEL_REASON_ATTR, None)
    return str(reason) if reason else None
