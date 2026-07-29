"""Best-effort process-local notifications for tool-output metrics."""

from __future__ import annotations

import threading
from collections.abc import Callable

_metrics_notifier: Callable[[str], None] | None = None
_metrics_notifier_lock = threading.RLock()


def set_metrics_notifier(notifier: Callable[[str], None] | None) -> None:
    """Install a callback invoked after metrics-changing persistence writes."""
    global _metrics_notifier
    with _metrics_notifier_lock:
        _metrics_notifier = notifier


def clear_metrics_notifier() -> None:
    """Remove the active process-local metrics callback."""
    set_metrics_notifier(None)


def notify_metrics_changed(thread_id: str) -> None:
    """Notify an observer without allowing UI failures to affect tool execution."""
    with _metrics_notifier_lock:
        notifier = _metrics_notifier
    if notifier is not None:
        try:
            notifier(thread_id)
        except Exception:  # noqa: BLE001
            pass
