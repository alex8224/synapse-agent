from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from synapse.runtime.steer import SteerQueue
from synapse.ui.steer_controller import SteerController


class _App:
    def __init__(self) -> None:
        self.agent = SimpleNamespace()
        self._active_steer_queue: SteerQueue | None = None
        self._busy = False
        self.snapshots: list[list[str]] = []

    def _on_steer_items_changed(self, items: list[str]) -> None:
        self.snapshots.append(list(items))

    def call_from_thread(self, callback: Any, *args: Any) -> None:
        callback(*args)


def test_turn_queue_prefers_active_queue_while_busy(monkeypatch) -> None:
    app = _App()
    active = SteerQueue()
    fallback = SteerQueue()
    app._active_steer_queue = active
    app._busy = True
    monkeypatch.setattr("synapse.ui.steer_controller.get_agent_steer_queue", lambda agent: fallback)

    assert SteerController(app).turn_queue() is active


def test_bind_queue_replaces_old_listener(monkeypatch) -> None:
    app = _App()
    first = SteerQueue()
    second = SteerQueue()
    current = [first]
    monkeypatch.setattr(
        "synapse.ui.steer_controller.get_agent_steer_queue", lambda agent: current[0]
    )
    controller = SteerController(app)

    controller.bind_queue()
    first.push("first")
    assert app.snapshots[-1] == ["first"]

    current[0] = second
    controller.bind_queue()
    first.push("stale")
    assert app.snapshots[-1] == []

    second.push("current")
    assert app.snapshots[-1] == ["current"]


def test_drop_and_clear_delegate_to_current_queue(monkeypatch) -> None:
    app = _App()
    queue = SteerQueue()
    monkeypatch.setattr("synapse.ui.steer_controller.get_agent_steer_queue", lambda agent: queue)
    controller = SteerController(app)
    queue.push("one")
    queue.push("two")

    controller.drop_at(0)
    assert queue.peek_items() == ["two"]
    controller.clear()
    assert queue.peek_items() == []
