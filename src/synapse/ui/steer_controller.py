"""Steer queue binding and user-facing queue actions for the Textual TUI."""

from __future__ import annotations

from typing import Any

from synapse.runtime.steer import SteerQueue, get_agent_steer_queue


class SteerController:
    """Own the UI subscription for the queue consumed by the active turn."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._bound_queue: SteerQueue | None = None
        self._listener: Any | None = None

    def turn_queue(self) -> SteerQueue | None:
        """Return the queue consumed by the active graph run when available."""
        app = self._app
        controller = getattr(app, "_turn", None)
        runtime = getattr(controller, "session_runtime", None)
        if runtime is not None:
            queue = runtime.steer_queue()
            if queue is not None:
                return queue
        busy = bool(getattr(controller, "busy", getattr(app, "_busy", False)))
        active_queue = getattr(app, "_active_steer_queue", None)
        if busy and active_queue is not None:
            # Compatibility for a turn started before SessionRuntime was
            # attached (and for lightweight hosts used outside the full TUI).
            return active_queue
        return get_agent_steer_queue(app.agent)

    def bind_queue(self) -> None:
        """Bind the status widget to the current agent queue, removing stale listeners."""
        app = self._app
        queue = self.turn_queue()
        if queue is self._bound_queue:
            if queue is not None:
                app._on_steer_items_changed(queue.peek_items())
            return

        if self._bound_queue is not None and self._listener is not None:
            self._bound_queue.remove_listener(self._listener)

        self._bound_queue = queue
        self._listener = None
        if queue is None:
            app._on_steer_items_changed([])
            return

        def on_change(items: list[str], *, source: SteerQueue = queue) -> None:
            def apply(snapshot: list[str]) -> None:
                if self._bound_queue is source:
                    app._on_steer_items_changed(snapshot)

            try:
                app.call_from_thread(apply, list(items))
            except Exception:  # noqa: BLE001 - lightweight hosts have no Textual thread
                apply(list(items))

        self._listener = on_change
        queue.add_listener(on_change)
        app._on_steer_items_changed(queue.peek_items())

    def drop_at(self, index: int) -> None:
        """Remove one pending user steer note."""
        queue = self.turn_queue()
        if queue is not None:
            queue.remove_at(int(index))

    def clear(self) -> None:
        """Remove all pending user steer notes."""
        queue = self.turn_queue()
        if queue is not None:
            queue.clear()