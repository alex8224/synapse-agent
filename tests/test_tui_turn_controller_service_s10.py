from __future__ import annotations

import ast
import concurrent.futures
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from synapse.runtime.sessions.ref import SessionRef
from synapse.ui.turn.controller import TurnController
from synapse.ui.turn.service_session import TUIRuntimeSessionFacade, TUISessionBinding


class App:
    def __init__(self, project: str = "p1", thread: str = "t1") -> None:
        self.thread_id = thread
        self.settings = SimpleNamespace(
            model="m", workspace=".",
            resolved_sessions_path=lambda: ".sessions.sqlite",
        )
        self.agent = object()
        self._current_project_id = lambda: project
        self._agent_ready = SimpleNamespace(wait=lambda timeout: True)
        self._agent_error = None
        self._transcript_generation = 0
        self._busy = False
        self._skip_steer_followup = False
        self.calls: list[tuple[str, tuple, dict]] = []
        self._transcript = SimpleNamespace(transcript_generation=0)
        self._turn_done = lambda: None
        self._refresh_topbar = lambda: None
        self._sync_prompt_placeholder = lambda: None

    def call_from_thread(self, fn, *args, **kwargs):
        self.calls.append((getattr(fn, "__name__", "callback"), args, kwargs))
        return fn(*args, **kwargs)

    def call_after_refresh(self, fn, *args, **kwargs):
        self.calls.append((getattr(fn, "__name__", "callback"), args, kwargs))
        return True

    def _call_for_transcript(self, generation, fn, *args):
        return fn(*args)

    def append_event(self, *args, **kwargs):
        self.calls.append(("append_event", args, kwargs))

    def commit_answer(self, *args, **kwargs):
        self.calls.append(("commit_answer", args, kwargs))


class View:
    def __init__(self, status="idle", active_turn_id=None):
        self.status = status
        self.active_turn_id = active_turn_id
        self.latest_sequence = 0


def facade(project="p1", thread="t1", status="idle", active=None):
    service = MagicMock()
    item = TUIRuntimeSessionFacade(
        TUISessionBinding(SessionRef(project, thread), service)
    )
    item.state.view = View(status, active)
    return item, service


def test_submit_does_not_use_legacy_session_or_reservation():
    app = App()
    controller = TurnController(app)
    controller._service_session_cached = MagicMock(return_value=None)
    controller._session_for = MagicMock()
    app._prompt = SimpleNamespace(add_history=lambda x: None, expand_paste=lambda x: (x, x))
    app._image_bank = SimpleNamespace(items={}, clear=lambda: None)
    app._handle_slash = lambda x: False
    app._prewarm_cancel_event = SimpleNamespace(set=lambda: None)
    app._touch_session_bg = lambda *a, **k: None
    app.append_user = lambda *a, **k: None
    app.refresh_image_preview = lambda: None
    app._transcript = SimpleNamespace(reset_for_turn=lambda: None)
    app.clear_stream = lambda: None
    app.set_activity = lambda *a: None
    app._sync_prompt_placeholder = lambda: None
    app.run_turn = MagicMock()
    controller.submit(SimpleNamespace(value="x", input=SimpleNamespace(value="x")))
    controller._session_for.assert_not_called()
    assert controller._service_session_cached.called
    assert "reservation" not in str(app.run_turn.call_args)


def test_submit_busy_routes_service_steer():
    app = App()
    controller = TurnController(app)
    item, _ = facade(active="turn")
    item.state.view.status = "running"
    controller._service_sessions["p1:t1"] = item
    controller.steer = MagicMock(return_value=True)
    app._prompt = SimpleNamespace(add_history=lambda x: None, expand_paste=lambda x: (x, x))
    app._image_bank = SimpleNamespace(items={})
    app._handle_slash = lambda x: False
    app._prewarm_cancel_event = SimpleNamespace(set=lambda: None)
    event = SimpleNamespace(value="guide", input=SimpleNamespace(value="guide"))
    controller.submit(event)
    controller.steer.assert_called_once_with("guide")


def test_missing_image_does_not_create_service_and_restores_prompt():
    app = App()
    controller = TurnController(app)
    app._prompt = SimpleNamespace(add_history=lambda x: None, expand_paste=lambda x: (x, x))
    app._image_bank = SimpleNamespace(items={}, clear=MagicMock())
    app._handle_slash = lambda x: False
    app._prewarm_cancel_event = SimpleNamespace(set=lambda: None)
    app.append_event = MagicMock()
    prompt = SimpleNamespace(value="", focus=MagicMock())
    app.query_one = lambda *a: prompt
    controller.submit(SimpleNamespace(value="[image#3]", input=SimpleNamespace(value="x")))
    assert not controller._service_sessions
    assert app._image_bank.clear.call_count == 0
    assert prompt.value == "[image#3]"


def test_apply_completed_result_commits_text():
    app = App()
    assert TurnController(app).apply_consumer_result(
        SimpleNamespace(status="completed", final_text="done", already_streamed=False),
        transcript_generation=0,
    ) is False
    assert any(name == "commit_answer" for name, _, _ in app.calls)


def test_cancelled_result_skips_followup():
    app = App()
    assert TurnController(app).apply_consumer_result(
        SimpleNamespace(status="cancelled", final_text="", already_streamed=False),
        transcript_generation=0,
    ) is True
    assert app._skip_steer_followup


def test_failed_result_is_reported_by_run():
    app = App()
    controller = TurnController(app)
    facade_obj, _ = facade()
    async def submit(*args, **kwargs):
        return SimpleNamespace(status="failed", final_text="", already_streamed=False)
    facade_obj.submit = submit
    controller._service_facade = lambda thread: facade_obj
    app._begin_turn_usage = lambda: None
    controller.run_turn("x")
    assert any("ERROR" in str(args) for name, args, _ in app.calls if name == "append_event")


def test_waiting_approval_result_is_reported():
    app = App()
    controller = TurnController(app)
    controller.apply_consumer_result(
        SimpleNamespace(status="waiting_approval", final_text="", already_streamed=False),
        transcript_generation=0,
    )
    assert any("HITL" in str(args) for name, args, _ in app.calls if name == "append_event")


def test_facade_is_lazy_and_has_exact_identity(monkeypatch):
    app = App("project-x", "thread-y")
    controller = TurnController(app)
    owner = MagicMock()
    owner.service = MagicMock()
    monkeypatch.setattr(
        "synapse.ui.turn.controller.LocalProjectRuntimeConsumer", lambda **kw: owner
    )
    result = controller._service_facade("thread-y")
    assert result.binding.session == SessionRef("project-x", "thread-y")
    assert controller._service_facade("thread-y") is result


def test_same_project_reuses_owner():
    app = App()
    controller = TurnController(app)
    controller._service_facade("a")
    controller._service_facade("b")
    assert len(controller._service_owners) == 1


def test_projects_have_isolated_owners():
    app = App("a")
    controller = TurnController(app)
    first = controller._service_facade("t")
    app._current_project_id = lambda: "b"
    second = controller._service_facade("t")
    assert first.binding.session.project_id != second.binding.session.project_id
    assert len(controller._service_owners) == 2


def test_bind_agent_updates_dynamic_factory_map():
    app = App()
    controller = TurnController(app)
    agent = object()
    controller.bind_agent("t", agent)
    assert controller._service_agents[("p1", "t")] is agent


def test_cached_lookup_does_not_cross_project_thread_collision():
    app = App("a", "same")
    controller = TurnController(app)
    one, _ = facade("a", "same")
    two, _ = facade("b", "same")
    controller._service_sessions = {"a:same": one, "b:same": two}
    assert controller._service_session_cached("same") is one
    app._current_project_id = lambda: "b"
    assert controller._service_session_cached("same") is two


def test_busy_service_view_active_and_idle():
    app = App()
    controller = TurnController(app)
    item, _ = facade(active="turn", status="running")
    controller._service_sessions["p1:t1"] = item
    assert controller.busy
    item.state.view.status = "idle"
    assert not controller.busy


def test_cancel_without_active_turn_is_bounded_noop():
    app = App()
    controller = TurnController(app)
    item, service = facade(active=None)
    controller._service_sessions["p1:t1"] = item
    assert controller.cancel() is False
    service.cancel_turn.assert_not_called()


def test_steer_without_active_turn_is_bounded_noop():
    app = App()
    controller = TurnController(app)
    item, service = facade(active=None)
    controller._service_sessions["p1:t1"] = item
    assert controller.steer("x") is False
    service.steer_turn.assert_not_called()


def test_shutdown_closes_duplicate_owner_once_and_continues_errors(monkeypatch):
    app = App()
    controller = TurnController(app)
    owner = MagicMock()
    owner.close.side_effect = RuntimeError("bad")
    controller._service_owners = {"a": owner, "alias": owner}
    controller._service_sessions = {}
    class Loop:
        def submit(self, awaitable):
            if hasattr(awaitable, "close"):
                awaitable.close()
            future = concurrent.futures.Future()
            future.set_exception(RuntimeError("bad"))
            return future
    monkeypatch.setattr("synapse.ui.turn.controller.get_async_runtime", lambda: Loop())
    controller.shutdown()
    assert owner.close.call_count == 1


def test_run_turn_uses_runtime_event_renderer_and_exact_payload(monkeypatch):
    app = App()
    app._begin_turn_usage = lambda: None
    controller = TurnController(app)
    item, _ = facade()
    seen = {}
    async def submit(text, *, attachments, on_event):
        seen.update(text=text, attachments=attachments, callback=on_event)
        return SimpleNamespace(status="completed", final_text="", already_streamed=True)
    item = MagicMock()
    item.submit.side_effect = submit
    controller._service_facade = lambda thread: item
    controller.run_turn("hello", ["image"])
    assert seen["text"] == "hello" and seen["attachments"] == ("image",)
    assert seen["callback"] is not None


def test_controller_has_no_asyncio_run():
    tree = ast.parse(Path("src/synapse/ui/turn/controller.py").read_text())
    assert not any(
        isinstance(n, ast.Attribute)
        and n.attr == "run"
        and getattr(n.value, "id", "") == "asyncio"
        for n in ast.walk(tree)
    )


def test_controller_worker_methods_do_not_use_legacy_start_calls():
    tree = ast.parse(Path("src/synapse/ui/turn/controller.py").read_text())
    source = Path("src/synapse/ui/turn/controller.py").read_text()
    assert "def run_turn" in source
    assert "def submit" in source
    assert not any(isinstance(n, ast.Name) and n.id == "start_threadsafe" for n in ast.walk(tree))


def test_service_facade_binding_owner_is_preserved():
    owner = object()
    binding = TUISessionBinding(SessionRef("p", "t"), MagicMock(), owner=owner)
    assert TUIRuntimeSessionFacade(binding).binding.owner is owner
