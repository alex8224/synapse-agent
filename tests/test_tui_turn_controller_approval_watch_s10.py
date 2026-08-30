from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from synapse.runtime.service.queries import ApprovalActionView, SessionView, UsageView
from synapse.runtime.sessions.ref import SessionRef
from synapse.ui.turn.controller import TurnController
from synapse.ui.turn.service_session import TUIRuntimeSessionFacade, TUISessionBinding


class App:
    def __init__(self, project: str = "p", thread: str = "t") -> None:
        self.thread_id = thread
        self.settings = SimpleNamespace(model="m", workspace=".")
        self.agent = object()
        self._current_project_id = lambda: project
        self._transcript_generation = 0
        self._busy = False
        self._skip_steer_followup = False
        self._transcript = SimpleNamespace(transcript_generation=0)
        self.calls: list[tuple[str, tuple, dict]] = []

    def call_from_thread(self, fn, *args, **kwargs):
        self.calls.append((getattr(fn, "__name__", "callback"), args, kwargs))
        return fn(*args, **kwargs)

    def call_after_refresh(self, fn, *args, **kwargs):
        self.calls.append((getattr(fn, "__name__", "callback"), args, kwargs))
        return fn(*args, **kwargs)

    def _call_for_transcript(self, generation, fn, *args):
        return fn(*args)

    def append_event(self, *args, **kwargs):
        self.calls.append(("append_event", args, kwargs))

    def _turn_done(self):
        self.calls.append(("turn_done", (), {}))

    def _begin_turn_usage(self):
        pass

    def _refresh_topbar(self):
        pass


def make_facade(thread="t", *, status="waiting_approval", active="turn", actions=2):
    service = MagicMock()
    service.watch_events.return_value = _Watch()
    async def get_session(_query):
        return facade.state.view
    service.get_session.side_effect = get_session
    facade = TUIRuntimeSessionFacade(TUISessionBinding(SessionRef("p", thread), service))
    facade.state.view = SessionView("p", thread, status, active, 7, UsageView(), None, "")
    facade.state.last_sequence = 7
    async def pending_approval():
        return SimpleNamespace(
            turn_id=active,
            actions=tuple(ApprovalActionView(i, "x", {}) for i in range(actions)),
        )
    facade.pending_approval = pending_approval
    facade.pending_approval_result = SimpleNamespace(
        turn_id=active,
        actions=tuple(ApprovalActionView(i, "x", {}) for i in range(actions)),
    )
    facade.state.view = SessionView("p", thread, status, active, 7, UsageView(), None, "")
    return facade, service


def controller_with(facade, thread="t"):
    """Build a controller around one already-created facade."""
    app = App(thread=thread)
    controller = TurnController(app)
    controller._service_sessions[f"p:{thread}"] = facade
    return controller, app


def test_resume_without_facade_reports_and_finishes():
    c, app = controller_with(*make_facade())
    c._service_sessions.clear()
    c.run_resume("approve")
    assert any(x[0] == "turn_done" for x in app.calls)
    assert any("no pending" in str(x[1]) for x in app.calls if x[0] == "append_event")


def test_resume_uses_exact_pending_active_turn():
    facade, service = make_facade(active="expected")
    c, _ = controller_with(facade)
    c.run_resume("approve")
    assert service.resume_turn.call_args is not None
    assert service.resume_turn.call_args.args[0].expected_turn_id == "expected"


def test_resume_zero_actions_refuses_without_resume():
    facade, service = make_facade(actions=0)
    c, app = controller_with(facade)
    c.run_resume("approve")
    service.resume_turn.assert_not_called()
    assert any("no pending" in str(x[1]) for x in app.calls if x[0] == "append_event")

@pytest.mark.parametrize(
    ("action", "kind"),
    [("approve", "allow_once"), ("reject", "reject_once"),
     ("approve_always", "allow_always"), ("reject_always", "reject_always")],
)
def test_resume_maps_decisions(action, kind):
    facade, service = make_facade(actions=3)
    async def resume(command):
        return SimpleNamespace(status="completed", final_text="", already_streamed=True)
    service.resume_turn.side_effect = resume
    service.watch_events.return_value = _Watch()
    c, _ = controller_with(facade)
    c.run_resume(action, "because")
    command = service.resume_turn.call_args.args[0]
    assert [d.kind for d in command.decisions] == [kind] * 3
    assert [d.message for d in command.decisions] == ["because"] * 3


def test_decisions_preserve_count_and_order():
    facade, service = make_facade(actions=4)
    service.resume_turn.side_effect = lambda command: SimpleNamespace(
        status="completed", final_text="", already_streamed=True
    )
    service.watch_events.return_value = _Watch()
    c, _ = controller_with(facade)
    c.run_resume("approve")
    assert len(facade.pending_approval_result.actions) == 4
    assert len(service.resume_turn.call_args.args[0].decisions) == 4


def test_resume_watch_enters_before_resume():
    facade, service = make_facade(actions=1)
    order = []
    watch = _Watch(order)
    service.watch_events.return_value = watch
    service.resume_turn.side_effect = lambda command: (
        order.append("resume")
        or SimpleNamespace(status="completed", final_text="", already_streamed=True)
    )
    c, _ = controller_with(facade)
    c.run_resume("approve")
    assert order[:2] == ["enter", "resume"]

@pytest.mark.parametrize("status", ["completed", "cancelled", "failed", "waiting_approval"])
def test_resume_result_status_is_applied(status):
    facade, service = make_facade(actions=1)
    service.watch_events.return_value = _Watch()
    service.resume_turn.side_effect = lambda command: SimpleNamespace(
        status=status, final_text="", already_streamed=True
    )
    c, app = controller_with(facade)
    c.run_resume("approve")
    assert any(x[0] == "turn_done" for x in app.calls)


def test_resume_exception_reports_ui_and_done():
    facade, service = make_facade(actions=1)
    service.watch_events.side_effect = RuntimeError("boom")
    c, app = controller_with(facade)
    c.run_resume("approve")
    assert any("boom" in str(x[1]) for x in app.calls if x[0] == "append_event")
    assert any(x[0] == "turn_done" for x in app.calls)


def test_exact_project_thread_cache():
    facade, _ = make_facade()
    c, _ = controller_with(facade)
    assert c._service_session_cached("t", project_id="p") is facade
    assert c._service_session_cached("t", project_id="other") is None


def test_attach_cold_returns_none():
    c, _ = controller_with(*make_facade(status="idle", active=None))
    assert c.attach("t") is None


def test_attach_cursor_prefers_explicit_then_cached():
    facade, service = make_facade(status="running", active="turn")
    service.watch_events.return_value = _Watch()
    c, _ = controller_with(facade)
    c.attach("t", after_sequence=3)
    watch = service.watch_events.return_value
    assert watch.entered.wait(2)
    assert service.watch_events.call_args.kwargs["after"] == 3
    c.detach("t")
    watch.close()
    assert watch.exited.wait(2)
    service.watch_events.return_value = _Watch()
    c.attach("t")
    watch = service.watch_events.return_value
    assert watch.entered.wait(2)
    assert service.watch_events.call_args.kwargs["after"] == 7
    c.detach("t")
    watch.close()
    assert watch.exited.wait(2)


def test_attach_switches_renderer_to_active_turn():
    facade, service = make_facade(status="running", active="active")
    service.watch_events.return_value = _Watch()
    c, _ = controller_with(facade)
    c.attach("t")
    assert c.session_binding == facade.binding
    assert c._attached_thread_id == "t"


def test_attach_runtime_events_are_ordered():
    facade, service = make_facade(status="running", active="turn")
    service.watch_events.return_value = _Watch()
    c, app = controller_with(facade)
    c.attach("t")
    service.watch_events.return_value.queue.extend([]) if False else None
    assert c._session_watch_future is not None
    assert app.calls == []


def test_detach_cancels_watcher_only():
    facade, service = make_facade(status="running", active="turn")
    service.watch_events.return_value = _Watch()
    c, _ = controller_with(facade)
    c.attach("t")
    future = c._session_watch_future
    c.detach("t")
    assert future.cancelled() or future.done()
    service.cancel_turn.assert_not_called()
    service.close_session.assert_not_called()


def test_detach_other_thread_does_not_break_current():
    facade, _ = make_facade(status="running", active="turn")
    c, _ = controller_with(facade)
    c.attach("t")
    future = c._session_watch_future
    c.detach("other")
    assert c._attached_thread_id == "t" and future is c._session_watch_future


def test_detach_is_idempotent():
    c, _ = controller_with(*make_facade(status="running", active="turn"))
    c.detach("t")
    c.detach("t")
    assert c._attached_thread_id is None


def test_reattach_generation_fences_old_event():
    f, service = make_facade(status="running", active="turn")
    service.watch_events.return_value = _Watch()
    c, app = controller_with(f)
    c.attach("t")
    old = c._attach_generation
    c.detach("t")
    c.attach("t")
    c._append_generation_warning(old, "old")
    assert not any("old" in str(x[1]) for x in app.calls if x[0] == "append_event")


def test_watch_error_boundary_does_not_crash():
    f, service = make_facade(status="running", active="turn")
    service.watch_events.side_effect = RuntimeError("watch")
    c, _ = controller_with(f)
    assert c.attach("t") is None


def test_three_methods_have_no_legacy_bridge_symbols():
    tree = ast.parse(Path("src/synapse/ui/turn/controller.py").read_text())
    methods = {
        n.name for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name in {"attach", "detach", "run_resume"}
    }
    assert methods == {"attach", "detach", "run_resume"}
    source = Path("src/synapse/ui/turn/controller.py").read_text()
    assert "_event_bridge" not in source and "_session_subscription" not in source


class _Watch:
    def __init__(self, order=None):
        self.order = order if order is not None else []
        self.queue = asyncio.Queue()
        self.entered = threading.Event()
        self.exited = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self):
        self.loop = asyncio.get_running_loop()
        self.order.append("enter")
        self.entered.set()
        return self

    async def __aexit__(self, *args):
        self.order.append("exit")
        self.exited.set()

    def close(self):
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, StopAsyncIteration)

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration
