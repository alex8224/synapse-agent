from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

from textual.app import App

from synapse.runtime.async_runtime import AsyncRuntime
from synapse.runtime.service.local import LocalAgentRuntimeService
from synapse.runtime.sessions.manager import RuntimeManager
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.sessions.runtime import SessionRuntime, SessionStatus
from synapse.runtime.steer import SteerQueue
from synapse.ui.steer_controller import SteerController
from synapse.ui.turn.controller import TurnController
from synapse.ui.turn.service_session import TUIRuntimeSessionFacade, TUISessionBinding


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


def test_turn_queue_prefers_active_queue_while_busy() -> None:
    app = _App()
    active = SteerQueue()
    app._active_steer_queue = active
    app._busy = True

    assert SteerController(app).turn_queue() is active


def test_turn_queue_falls_back_to_session_agent_and_app_agent() -> None:
    app = _App()
    fallback_queue = SteerQueue()
    app.agent = SimpleNamespace(_coding_steer_queue=fallback_queue)
    assert SteerController(app).turn_queue() is fallback_queue

    session_queue = SteerQueue()
    session_agent = SimpleNamespace(_coding_steer_queue=session_queue)
    app.thread_id = "test-thread"
    app._turn = SimpleNamespace(
        agent_for_session=lambda thread: session_agent if thread == "test-thread" else None
    )
    assert SteerController(app).turn_queue() is session_queue

    active_queue = SteerQueue()
    app._active_steer_queue = active_queue
    assert SteerController(app).turn_queue() is active_queue


def test_bind_queue_replaces_old_listener() -> None:
    app = _App()
    first = SteerQueue()
    second = SteerQueue()
    app._active_steer_queue = first
    controller = SteerController(app)

    controller.bind_queue()
    first.push("first")
    assert app.snapshots[-1] == ["first"]

    app._active_steer_queue = second
    controller.bind_queue()
    first.push("stale")
    assert app.snapshots[-1] == []

    second.push("current")
    assert app.snapshots[-1] == ["current"]


def test_drop_and_clear_delegate_to_current_queue() -> None:
    app = _App()
    queue = SteerQueue()
    app._active_steer_queue = queue
    controller = SteerController(app)
    queue.push("one")
    queue.push("two")

    controller.drop_at(0)
    assert queue.peek_items() == ["two"]
    controller.clear()
    assert queue.peek_items() == []


def test_delayed_notification_cannot_update_a_rebound_queue() -> None:
    app = _App()
    callbacks = []
    app.call_after_refresh = lambda fn, *args: callbacks.append(lambda: fn(*args))
    first, second = SteerQueue(), SteerQueue()
    app._active_steer_queue = first
    controller = SteerController(app)
    controller.bind_queue()
    first.push("old")
    app._active_steer_queue = second
    controller.bind_queue()
    for callback in callbacks:
        callback()
    assert app.snapshots[-1] == []


def test_real_textual_submit_steer_does_not_wait_for_ui_notification(monkeypatch) -> None:
    """Keep the real two-loop boundary: inline call_from_thread fakes hide this deadlock."""
    runtime = AsyncRuntime(name="test-steer-ui")
    monkeypatch.setattr("synapse.ui.turn.controller.get_async_runtime", lambda: runtime)

    class SteerApp(App):
        def __init__(self) -> None:
            super().__init__()
            self.thread_id = "thread"
            self.settings = SimpleNamespace()
            self.queue = SteerQueue()
            self.agent = SimpleNamespace(_coding_steer_queue=self.queue)
            self._active_steer_queue = self.queue
            self._turn = TurnController(self)
            self._steer = SteerController(self)
            self.snapshots = []
            self.warnings = []
            self.submitted = asyncio.Event()
            self.updated = asyncio.Event()
            self._prewarm_cancel_event = threading.Event()
            self._prompt = SimpleNamespace(
                expand_paste=lambda text: (text, text), add_history=lambda text: None
            )
            self._image_bank = SimpleNamespace(items={})
            session = SessionRuntime(
                thread_id=self.thread_id, project_id="project", agent=self.agent,
                settings=self.settings, turn_runtime=object(),
            )
            session._status = SessionStatus.RUNNING
            session._active_handle = SimpleNamespace(turn_id="turn", done=lambda: False)
            manager = RuntimeManager(
                settings=self.settings, agent_factory=lambda *args: self.agent,
                project_id="project", async_runtime=runtime,
            )
            manager._sessions[self.thread_id] = session
            service = LocalAgentRuntimeService(lambda project: manager)
            facade = TUIRuntimeSessionFacade(
                TUISessionBinding(SessionRef("project", self.thread_id), service)
            )
            facade.state.view = SimpleNamespace(
                status="running", active_turn_id="turn", latest_sequence=0
            )
            self._turn._service_sessions["project:thread"] = facade

        def _current_project_id(self) -> str:
            return "project"

        def _handle_slash(self, text: str) -> bool:
            return False

        def append_event(self, text: str, *args: Any) -> None:
            self.warnings.append(text)

        def _on_steer_items_changed(self, items: list[str]) -> None:
            self.snapshots.append(items)
            if items == ["guidance"]:
                self.updated.set()

        def submit_guidance(self) -> None:
            self._steer.bind_queue()
            self._turn.submit(
                SimpleNamespace(value="guidance", input=SimpleNamespace(value="guidance"))
            )
            self.submitted.set()

    async def run() -> None:
        app = SteerApp()
        async with app.run_test():
            app.call_later(app.submit_guidance)
            await asyncio.wait_for(app.submitted.wait(), timeout=1.0)
            await asyncio.wait_for(app.updated.wait(), timeout=1.0)
            assert app.queue.peek_items() == ["guidance"]
            assert app.warnings == []

    try:
        asyncio.run(run())
    finally:
        runtime.close()
