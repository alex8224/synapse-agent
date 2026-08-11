"""Unit tests for the TUI turn controller (result application / goal flow)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage

from synapse.runtime.agent_loop import TurnContext, TurnResult, TurnStatus
from synapse.runtime.agent_loop.request import build_turn_request
from synapse.runtime.sessions import SessionRuntime, SessionStatus
from synapse.runtime.steer import SteerQueue
from synapse.sessions.transcript_projection import TranscriptProjection
from synapse.ui.turn.controller import TurnController


class _FakeApp:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._skip_steer_followup = False
        self._busy = False
        self.thread_id = "t1"
        self.agent = SimpleNamespace(_coding_goal_service=None)

    def call_from_thread(self, callback: Any, *args: Any, **kwargs: Any) -> None:
        self.calls.append((callback, args, kwargs))
        callback(*args, **kwargs)

    def call_after_refresh(self, callback: Any, *args: Any, **kwargs: Any) -> bool:
        """Non-blocking UI scheduling used by the session status observer."""
        self.calls.append((callback, args, kwargs))
        callback(*args, **kwargs)
        return True

    def _call_for_transcript(
        self, generation: int, callback: Any, *args: Any, **kwargs: Any
    ) -> None:
        self.calls.append((callback, args, kwargs))
        callback(*args, **kwargs)

    def append_event(self, message: str, style: str = "dim") -> None:
        self.calls.append(("append_event", (message, style), {}))

    def commit_answer(self, text: str) -> None:
        self.calls.append(("commit_answer", (text,), {}))

    def apply_turn_usage(self, **kwargs: Any) -> None:
        self.calls.append(("apply_turn_usage", (), kwargs))


def test_submit_reserves_session_before_scheduling_worker() -> None:
    app = _FakeApp()
    app.settings = SimpleNamespace(model="test", workspace=".")
    app._prewarm_cancel_event = threading.Event()
    app._prompt = SimpleNamespace(
        add_history=lambda text: None,
        expand_paste=lambda text: (text, text),
    )
    app._image_bank = SimpleNamespace(items={}, clear=lambda: None)
    app._handle_slash = lambda text: False
    app._reload_session_title = lambda: None
    app._refresh_topbar = lambda: None
    app.append_user = lambda *args, **kwargs: None
    app._transcript = SimpleNamespace(reset_for_turn=lambda: None)
    app._subagent_monitor = SimpleNamespace(reset=lambda: None)
    app._subagent_monitor_auto_opened = False
    app._clear_subagent_status = lambda: None
    app.clear_stream = lambda: None
    app.set_activity = lambda *args: None
    app._sync_prompt_placeholder = lambda: None
    app._current_project_id = lambda: "project"
    scheduled: list[Any] = []
    app.run_turn = lambda text, attachments, **kwargs: scheduled.append(
        (text, attachments, kwargs)
    )
    controller = TurnController(app)
    runtime = MagicMock()
    reservation = object()
    runtime.reserve_turn.return_value = reservation
    controller._session_for = MagicMock(return_value=runtime)  # type: ignore[method-assign]
    event = SimpleNamespace(value="hello", input=SimpleNamespace(value="hello"))

    controller.submit(event)

    runtime.reserve_turn.assert_called_once_with()
    assert scheduled == [("hello", None, {"reservation": reservation})]


def test_session_for_keeps_reserved_runtime_when_agent_binding_differs() -> None:
    """goal follow-up reservation 不能被 capture_turn_context 的 agent 重绑定丢弃。"""
    app = _FakeApp()
    app.settings = SimpleNamespace(model="test", workspace=".")
    app._current_project_id = lambda: "project"
    controller = TurnController(app)
    frozen_agent = object()
    runtime = SessionRuntime(
        thread_id="t1",
        project_id="project",
        agent=frozen_agent,
        settings=app.settings,
        turn_runtime=controller._runtime,
    )
    reservation = runtime.reserve_turn()
    assert reservation is not None
    controller._sessions["t1"] = runtime
    controller._session_runtime = runtime
    controller._attached_thread_id = "t1"
    controller.runtime_for = MagicMock(return_value=runtime)  # type: ignore[method-assign]

    selected = controller._session_for(thread_id="t1", agent=object())

    assert selected is runtime
    assert selected.release_turn(reservation) is True


def test_apply_stream_result_cancelled_returns_early() -> None:
    app = _FakeApp()
    controller = TurnController(app)
    result = SimpleNamespace(
        cancelled=True,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        streamed_answer=False,
        state=None,
    )

    assert controller.apply_stream_result(result, transcript_generation=None) is True
    assert app._skip_steer_followup is True
    assert any(name == "append_event" for name, _, _ in app.calls)


def test_apply_stream_result_commits_unstreamed_answer() -> None:
    app = _FakeApp()
    controller = TurnController(app)
    result = SimpleNamespace(
        cancelled=False,
        input_tokens=0,
        output_tokens=0,
        cache_tokens=0,
        total_tokens=0,
        last_input_tokens=0,
        compact_events=0,
        streamed_answer=False,
        final_text="final answer",
        state=None,
        interrupted=False,
    )

    assert controller.apply_stream_result(result, transcript_generation=None) is False
    assert any(name == "commit_answer" for name, _, _ in app.calls)


def test_apply_stream_result_updates_usage_once() -> None:
    app = _FakeApp()
    controller = TurnController(app)
    result = SimpleNamespace(
        cancelled=False,
        input_tokens=10,
        output_tokens=20,
        cache_tokens=5,
        total_tokens=35,
        last_input_tokens=8,
        last_output_tokens=12,
        last_cache_tokens=3,
        last_output_tokens_per_second=None,
        last_ttft_s=None,
        last_rate_basis="end_to_end",
        compact_events=0,
        streamed_answer=True,
        state=None,
        interrupted=False,
    )

    controller.apply_stream_result(result, transcript_generation=1)

    usage_calls = [c for c in app.calls if c[0] == "apply_turn_usage"]
    assert len(usage_calls) == 1
    assert usage_calls[0][2]["turn_input"] == 10
    assert usage_calls[0][2]["turn_output"] == 20


def test_maybe_continue_goal_pushes_continuation_once(tmp_path) -> None:
    from synapse.goals.model import ThreadGoalStatus
    from synapse.goals.steering import GOAL_STEER_PREFIX

    queue = SteerQueue()
    goal = SimpleNamespace(
        status=ThreadGoalStatus.ACTIVE,
        objective="objective",
        remaining_tokens=None,
        tokens_used=0,
        token_budget=None,
    )
    service = SimpleNamespace(get=lambda tid: goal)
    app = _FakeApp()
    app.agent = SimpleNamespace(_coding_goal_service=service)
    app.settings = SimpleNamespace(goal_auto_continue=True)
    app._turn_steer_queue = lambda: queue

    controller = TurnController(app)
    assert controller.maybe_continue_goal() is True
    assert queue.peek_count() == 1
    assert str(queue.peek_items()[0]).startswith(GOAL_STEER_PREFIX)

    # No duplicate push while the continuation is unconsumed.
    assert controller.maybe_continue_goal() is False
    assert queue.peek_count() == 1


def test_maybe_continue_goal_skips_when_busy() -> None:
    app = _FakeApp()
    app._busy = True
    app.settings = SimpleNamespace(goal_auto_continue=True)
    app.agent = SimpleNamespace(_coding_goal_service=SimpleNamespace(get=lambda t: None))
    app._turn_steer_queue = lambda: SteerQueue()

    controller = TurnController(app)
    assert controller.maybe_continue_goal() is False


def test_build_turn_request_payload_and_config() -> None:
    from synapse.ui.turn.request import TurnRequest, build_turn_request

    settings = SimpleNamespace(max_concurrency=3)
    req = build_turn_request(
        text="hello",
        attachments=None,
        settings=settings,
        thread_id="t-1",
        monitor_id="m-9",
    )

    assert isinstance(req, TurnRequest)
    assert req.thread_id == "t-1"
    assert req.config["max_concurrency"] == 3
    assert req.config["configurable"]["thread_id"] == "t-1"
    assert req.config["configurable"]["subagent_monitor_id"] == "m-9"
    messages = req.payload["messages"]
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "hello"


def test_build_turn_request_overrides_concurrency() -> None:
    from synapse.ui.turn.request import build_turn_request

    settings = SimpleNamespace(max_concurrency=3)
    req = build_turn_request(
        text="x",
        attachments=None,
        settings=settings,
        thread_id="t",
        monitor_id="m",
        max_concurrency=8,
    )
    assert req.config["max_concurrency"] == 8


def test_busy_cancel_and_steer_delegate_to_session_runtime() -> None:
    app = _FakeApp()
    controller = TurnController(app)
    calls: list[tuple[str, str]] = []

    class _Runtime:
        def snapshot(self) -> Any:
            from synapse.runtime.sessions import SessionStatus

            return SimpleNamespace(status=SessionStatus.RUNNING)

        def cancel(self, reason: str) -> bool:
            calls.append(("cancel", reason))
            return True

        def steer(self, text: str) -> bool:
            calls.append(("steer", text))
            return True

    controller._session_runtime = _Runtime()  # type: ignore[assignment]

    assert controller.busy is True
    assert controller.cancel("escape") is True
    assert controller.steer("focus") is True
    assert calls == [("cancel", "escape"), ("steer", "focus")]


def test_switch_keeps_background_session_running() -> None:
    """P5-06: detaching one session and attaching another must not cancel the old turn."""
    import concurrent.futures

    from synapse.runtime.agent_loop import CancelToken, TurnHandle, TurnResult, TurnStatus
    from synapse.runtime.sessions import SessionRuntime, UserTurn

    class _Controlled:
        def __init__(self) -> None:
            self.futures: dict[str, concurrent.futures.Future[TurnResult]] = {}

        def submit(
            self, context: Any, *, sink: Any, cancel_token: CancelToken
        ) -> TurnHandle:
            del sink
            future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()
            self.futures[context.thread_id] = future
            return TurnHandle(context.turn_id, future, cancel_token)

    controlled = _Controlled()
    app = _FakeApp()
    app._transcript = SimpleNamespace(  # renderer attachment surface
        call_from_thread=lambda fn, *a, **k: fn(*a, **k),
        transcript_generation=0,
    )
    controller = TurnController(app)

    runtime_a = SessionRuntime(
        thread_id="a",
        agent=object(),
        settings=SimpleNamespace(max_concurrency=2, model="test"),
        turn_runtime=controlled,  # type: ignore[arg-type]
    )
    runtime_b = SessionRuntime(
        thread_id="b",
        agent=object(),
        settings=SimpleNamespace(max_concurrency=2, model="test"),
        turn_runtime=controlled,  # type: ignore[arg-type]
    )
    controller._sessions["a"] = runtime_a
    controller._sessions["b"] = runtime_b

    # Both sessions start a turn (background "a" while "b" is attached).
    handle_a = runtime_a.start(UserTurn("A"))[0]
    runtime_b.start(UserTurn("B"))
    app.thread_id = "b"
    controller.attach("b")
    assert controller.background_running_count() == 1
    assert controller.runtime_status_map()["a"] == SessionStatus.RUNNING.value
    assert controller.runtime_status_map()["b"] == SessionStatus.RUNNING.value

    # Finish the background session; the attached session stays busy.
    controlled.futures["a"].set_result(
        TurnResult(
            turn_id=handle_a.turn_id,
            thread_id="a",
            status=TurnStatus.COMPLETED,
            final_text="A done",
            input_tokens=1,
            output_tokens=1,
        )
    )
    assert controller.runtime_status_map()["a"] in {
        SessionStatus.IDLE.value,
        SessionStatus.RUNNING.value,
    }
    assert controller.busy is True  # session "b" still active
    controller._detach_renderer()


def test_runtime_status_map_only_includes_memory_sessions() -> None:
    app = _FakeApp()
    controller = TurnController(app)
    assert controller.runtime_status_map() == {}
    assert controller.background_running_count() == 0


def test_shutdown_cancels_all_live_sessions_without_waiting() -> None:
    app = _FakeApp()
    controller = TurnController(app)
    calls: list[tuple[str, bool, float | None]] = []

    class _Runtime:
        def __init__(self, thread_id: str) -> None:
            self.thread_id = thread_id

        def close_threadsafe(
            self, *, cancel_active: bool, timeout: float | None
        ) -> None:
            calls.append((self.thread_id, cancel_active, timeout))

    controller._sessions = {
        "a": _Runtime("a"),  # type: ignore[dict-item]
        "b": _Runtime("b"),  # type: ignore[dict-item]
    }

    controller.shutdown()

    assert calls == [("a", True, 5.0), ("b", True, 5.0)]
    assert controller.runtime_status_map() == {}
    assert controller.session_runtime is None


def test_runtime_result_persists_frozen_background_session(tmp_path) -> None:
    settings = SimpleNamespace(
        workspace=str(tmp_path),
        max_concurrency=2,
        session_summary_mode="off",
        session_summary_max_chars=600,
        project_catalog_enabled=False,
        resolved_sessions_path=lambda: tmp_path / "sessions.sqlite",
    )
    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    app = _FakeApp()
    app.settings = settings
    app.thread_id = "foreground"
    app._transcript_projection = projection
    app._summary_store = SimpleNamespace()
    app._session_store = None
    app._project_catalog = None
    controller = TurnController(app)
    context = TurnContext(
        thread_id="background",
        agent=object(),
        settings=settings,
        request=build_turn_request(
            text="background question",
            attachments=None,
            settings=settings,
            thread_id="background",
            monitor_id="m",
            max_concurrency=2,
        ),
    )
    result = TurnResult(
        turn_id=context.turn_id,
        thread_id="background",
        status=TurnStatus.COMPLETED,
        state={"messages": [AIMessage(content="background answer")]},
        streamed_answer=True,
        input_tokens=4,
        output_tokens=5,
    )

    controller._persist_runtime_result(context, result)

    assert projection.total_turns("background") == 1
    assert projection.total_turns("foreground") == 0
    page = projection.load_tail("background", turns=1)
    assert [(event.kind, event.text) for event in page.events] == [
        ("user", "background question"),
        ("answer", "background answer"),
    ]
    projection.close()


def test_tui_unmount_shuts_down_turns_before_projection_close() -> None:
    from synapse.ui.tui import CodingAgentApp

    order: list[str] = []
    app = object.__new__(CodingAgentApp)
    app.__dict__["_turn"] = SimpleNamespace(shutdown=lambda: order.append("turn"))
    app.__dict__["_transcript_projection"] = SimpleNamespace(
        close=lambda: order.append("projection")
    )
    app.__dict__["_summary_store"] = None
    app.__dict__["_session_store"] = None

    CodingAgentApp.on_unmount(app)

    assert order == ["turn", "projection"]


def test_mounted_tui_switches_live_sessions_without_exit(monkeypatch, tmp_path) -> None:
    import asyncio
    import concurrent.futures
    import threading

    from synapse.config import Settings
    from synapse.runtime.agent_loop import CancelToken, TurnHandle
    from synapse.runtime.sessions import SessionRuntime
    from synapse.ui.tui import CodingAgentApp

    class _ControlledRuntime:
        def __init__(self) -> None:
            self.futures: dict[str, concurrent.futures.Future[TurnResult]] = {}

        def submit(self, context: Any, *, sink: Any, cancel_token: CancelToken) -> TurnHandle:
            del sink
            future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()
            self.futures[context.thread_id] = future
            return TurnHandle(context.turn_id, future, cancel_token)

    class _Runtime(SessionRuntime):
        def close_threadsafe(
            self, *, cancel_active: bool = True, timeout: float | None = None
        ) -> None:
            del cancel_active, timeout
            handle = self.active_handle()
            if handle is not None and not handle.done():
                handle.cancel("shutdown")
                future = controlled.futures.get(self.thread_id)
                if future is not None and not future.done():
                    future.set_result(
                        TurnResult(
                            turn_id=handle.turn_id,
                            thread_id=self.thread_id,
                            status=TurnStatus.CANCELLED,
                        )
                    )
            self.broker.close()

    monkeypatch.setattr(
        "synapse.ui.tui.InputHistory.for_project",
        lambda *args, **kwargs: MagicMock(),
    )
    settings = Settings(
        _env_file=None,
        theme="cursor-dark",
        workspace=tmp_path,
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        sessions_path=tmp_path / "sessions.sqlite",
        project_catalog_path=tmp_path / "catalog.sqlite",
        project_catalog_enabled=False,
        session_summary_mode="off",
    )
    agent_a = SimpleNamespace(_coding_goal_service=None, _coding_steer_queue=SteerQueue())
    agent_b = SimpleNamespace(_coding_goal_service=None, _coding_steer_queue=SteerQueue())
    app = CodingAgentApp(
        agent=agent_a,
        settings=settings,
        thread_id="a",
        project_root=tmp_path,
    )
    controlled = _ControlledRuntime()
    runtime_a = _Runtime(
        thread_id="a", agent=agent_a, settings=settings, turn_runtime=controlled
    )
    runtime_b = _Runtime(
        thread_id="b", agent=agent_b, settings=settings, turn_runtime=controlled
    )
    runtime_a._status = SessionStatus.RUNNING
    runtime_b._status = SessionStatus.RUNNING
    app._turn._sessions = {"a": runtime_a, "b": runtime_b}

    async def exercise() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._turn.attach("a")

            app._turn.detach("a")
            app.thread_id = "b"
            app.agent = agent_b
            app._turn.attach("b")
            app._turn.sync_foreground_status()
            assert app.is_running
            assert runtime_a.snapshot().status is SessionStatus.RUNNING
            assert app._turn.background_running_count() == 1

            app._turn.detach("b")
            app.thread_id = "a"
            app.agent = agent_a
            app._turn.attach("a")
            app._turn.sync_foreground_status()
            assert app._turn.session_runtime is runtime_a
            assert app.is_running
            await pilot.press("ctrl+q")

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))

    assert not any(
        thread.is_alive() and thread.name.startswith("agent-turn:")
        for thread in threading.enumerate()
    )


def test_run_turn_worker_uses_session_group() -> None:
    """run_turn/run_resume must route work to a session-scoped worker.

    Regression: ``run_worker`` (Textual 8.2.8) accepts no kwargs, so passing
    turn args positionally hit its ``group`` slot and keyword args were
    rejected outright; the closure form forwards args without colliding.
    """
    from synapse.ui.tui import CodingAgentApp

    worker_calls: list[tuple[Any, dict[str, Any]]] = []
    turn_calls: list[tuple[Any, ...]] = []
    resume_calls: list[tuple[Any, ...]] = []
    frozen_agent = object()

    class _Turn:
        def launch_context(self) -> tuple[str, Any, int, str]:
            return "t-9", frozen_agent, 7, "monitor-1"

        def run_turn(self, text: str, attachments: list[Any] | None = None, **kw: Any) -> None:
            turn_calls.append((text, attachments, kw))

        def run_resume(self, action: str, message: str | None = None, **kw: Any) -> None:
            resume_calls.append((action, message, kw))

    class _Host:
        thread_id = "t-9"

        def __init__(self) -> None:
            self._turn = _Turn()

        def run_worker(self, work: Any, **kwargs: Any) -> None:
            worker_calls.append((work, kwargs))

    host = _Host()
    CodingAgentApp.run_turn(host, "hello", ["img"])
    CodingAgentApp.run_resume(host, "approve", "ok")
    host.thread_id = "switched-before-worker-start"

    assert len(worker_calls) == 2
    turn_work, turn_kw = worker_calls[0]
    resume_work, resume_kw = worker_calls[1]
    assert turn_kw["group"] == "agent-turn:t-9"
    assert turn_kw["thread"] is True
    assert turn_kw["exclusive"] is True
    assert resume_kw["group"] == "agent-turn:t-9"
    assert turn_work is not resume_work

    # The closure must forward the turn arguments intact.
    turn_work()
    resume_work()
    expected = {
        "thread_id": "t-9",
        "agent": frozen_agent,
        "transcript_generation": 7,
        "monitor_id": "monitor-1",
    }
    assert turn_calls == [("hello", ["img"], expected)]
    assert resume_calls == [("approve", "ok", expected)]


def test_run_turn_worker_runs_inside_textual() -> None:
    """Real Textual ``run_worker`` smoke test for the closure form.

    Guards against ``DOMNode.run_worker`` signature drift (positional
    ``group`` collision, unexpected kwargs) by actually starting the
    thread worker and waiting for the closure to run.
    """
    import asyncio

    from textual.app import App, ComposeResult
    from textual.widgets import Static

    from synapse.ui.tui import CodingAgentApp

    async def run() -> None:
        calls: list[tuple[Any, Any]] = []

        class _Turn:
            def run_turn(self, text: str, attachments: list[Any] | None = None) -> None:
                calls.append((text, attachments))

        class _Mini(App[None]):
            def __init__(self) -> None:
                super().__init__()
                self.thread_id = "t-42"
                self._turn = _Turn()

            def compose(self) -> ComposeResult:
                yield Static("x")

        app = _Mini()
        async with app.run_test() as pilot:
            CodingAgentApp.run_turn(app, "hello", ["a"])
            for _ in range(100):
                if calls:
                    break
                await pilot.pause()
            assert calls == [("hello", ["a"])]

    asyncio.run(run())


def test_background_turn_finish_does_not_touch_foreground_ui() -> None:
    """A background session's turn ending must not run the foreground UI
    teardown (``_turn_done``); only topbar chrome is refreshed.

    Integration-style: real ``SessionRuntime`` settlement drives the status
    callback, then the run-turn worker's finally path (``_turn_finished``)
    decides how much UI work runs.
    """
    import asyncio
    import concurrent.futures

    from synapse.runtime.agent_loop import CancelToken, TurnHandle, TurnResult, TurnStatus
    from synapse.runtime.sessions import SessionRuntime, UserTurn

    class _Controlled:
        def __init__(self) -> None:
            self.futures: dict[str, concurrent.futures.Future[TurnResult]] = {}

        def submit(
            self, context: Any, *, sink: Any, cancel_token: CancelToken
        ) -> TurnHandle:
            del sink
            future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()
            self.futures[context.thread_id] = future
            return TurnHandle(context.turn_id, future, cancel_token)

    class _ChromeApp(_FakeApp):
        def __init__(self) -> None:
            super().__init__()
            self.turn_done_calls = 0
            self.topbar_refreshes = 0

        def _turn_done(self) -> None:
            self.turn_done_calls += 1

        def _refresh_topbar(self) -> None:
            self.topbar_refreshes += 1

    controlled = _Controlled()
    app = _ChromeApp()
    controller = TurnController(app)
    runtime_a = SessionRuntime(
        thread_id="a",
        agent=object(),
        settings=SimpleNamespace(max_concurrency=2, model="test"),
        turn_runtime=controlled,  # type: ignore[arg-type]
        on_status_change=controller._on_session_status_changed,
    )
    runtime_b = SessionRuntime(
        thread_id="b",
        agent=object(),
        settings=SimpleNamespace(max_concurrency=2, model="test"),
        turn_runtime=controlled,  # type: ignore[arg-type]
        on_status_change=controller._on_session_status_changed,
    )
    controller._sessions["a"] = runtime_a
    controller._sessions["b"] = runtime_b
    app.thread_id = "b"
    controller.attach("b")

    async def run() -> None:
        # Session "a" starts a turn while "b" is the attached foreground session.
        handle_a = await runtime_a.submit(UserTurn("A"))
        assert runtime_a.snapshot().status is SessionStatus.RUNNING
        assert app.topbar_refreshes >= 1

        # Background "a" completes; settlement flips it to IDLE and fires the
        # status callback (topbar refresh), then the worker finally runs.
        controlled.futures["a"].set_result(
            TurnResult(
                turn_id=handle_a.turn_id,
                thread_id="a",
                status=TurnStatus.COMPLETED,
                final_text="A done",
                input_tokens=1,
                output_tokens=1,
            )
        )
        await asyncio.wrap_future(handle_a.future)
        for _ in range(50):
            if runtime_a.snapshot().status is SessionStatus.IDLE:
                break
            await asyncio.sleep(0)
        controller._turn_finished(runtime_a, app)

        assert app.turn_done_calls == 0, "background turn must not run _turn_done"
        assert app.topbar_refreshes >= 3  # RUNNING + IDLE + _turn_finished
        assert runtime_b.snapshot().status is SessionStatus.IDLE
        assert controller._session_runtime is runtime_b  # attach untouched
        await runtime_a.close(cancel_active=False)
        await runtime_b.close(cancel_active=False)

    asyncio.run(run())
    controller._detach_renderer()


def _make_app(monkeypatch, tmp_path):
    from synapse.config import Settings
    from synapse.runtime.agent_loop import TurnHandle
    from synapse.runtime.sessions import SessionRuntime
    from synapse.ui.tui import CodingAgentApp

    monkeypatch.setattr(
        "synapse.ui.tui.InputHistory.for_project",
        lambda *a, **k: MagicMock(),
    )
    settings = Settings(
        _env_file=None,
        theme="cursor-dark",
        workspace=tmp_path,
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        sessions_path=tmp_path / "sessions.sqlite",
        PROJECT_CATALOG_PATH=str(tmp_path / "catalog.sqlite"),
        project_catalog_enabled=False,
        session_summary_mode="off",
    )
    agent = SimpleNamespace(_coding_goal_service=None, _coding_steer_queue=None)
    app = CodingAgentApp(agent=agent, settings=settings, thread_id="a", project_root=tmp_path)

    class _Controlled:
        def __init__(self):
            self.futures = {}

        def submit(self, context, *, sink, cancel_token):
            future = concurrent.futures.Future()
            self.futures[context.thread_id] = future
            return TurnHandle(context.turn_id, future, cancel_token)

        def submit_coroutine(self, coroutine):
            future = concurrent.futures.Future()

            def run():
                try:
                    future.set_result(asyncio.run(coroutine))
                except BaseException as exc:  # noqa: BLE001 - test harness
                    future.set_exception(exc)

            threading.Thread(target=run, daemon=True).start()
            return future

    controlled = _Controlled()
    runtime = SessionRuntime(
        thread_id="a", agent=agent, settings=settings, turn_runtime=controlled
    )
    runtime_b = SessionRuntime(
        thread_id="b", agent=agent, settings=settings, turn_runtime=controlled
    )
    app._turn._sessions = {"a": runtime, "b": runtime_b}
    return app, runtime, runtime_b, controlled


def test_switch_back_keeps_bridge_alive(monkeypatch, tmp_path) -> None:
    from synapse.runtime.sessions import UserTurn
    from synapse.runtime.streaming import TextPayload, TurnEvent, TurnEventKind

    app, runtime, runtime_b, controlled = _make_app(monkeypatch, tmp_path)

    async def exercise() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # Start turn A (attach happens inside start_threadsafe -> worker thread).
            handle = runtime.start_threadsafe(
                UserTurn(text="hello", monitor_id="m"),
                on_started=lambda ctx: app._turn.attach(runtime),
            )
            await pilot.pause()
            bridge1 = app._turn._event_bridge
            assert bridge1 is not None and not bridge1._closed
            del bridge1

            # Emit one event so the broker retains history to replay on re-attach.
            runtime.broker.emit(
                TurnEvent(
                    version=1,
                    thread_id="a",
                    turn_id=handle.turn_id,
                    sequence=1,
                    kind=TurnEventKind.ANSWER_DELTA,
                    payload=TextPayload("before-switch"),
                )
            )
            await pilot.pause()

            # Switch away (B) then back (A) — attach runs on the UI thread here,
            # and its replay must not close the rebuilt bridge.
            app._turn.detach("a")
            app.thread_id = "b"
            app._turn.attach("b")
            app._turn.detach("b")
            app.thread_id = "a"
            app._turn.attach("a")
            await pilot.pause()

            bridge2 = app._turn._event_bridge
            assert bridge2 is not None, "attach must rebuild a bridge"
            assert not bridge2._closed, (
                "bridge closed after switch-back; live events are dropped"
            )

            # Live event after switch-back must still be delivered to the
            # renderer (this is the regression: it used to be dropped).
            runtime.broker.emit(
                TurnEvent(
                    version=1,
                    thread_id="a",
                    turn_id=handle.turn_id,
                    sequence=2,
                    kind=TurnEventKind.ANSWER_DELTA,
                    payload=TextPayload("live-after-switch"),
                )
            )
            await pilot.pause()
            assert not bridge2._closed
            renderer = bridge2._renderer
            assert renderer.last_sequence >= 2, (
                "live event after switch-back never reached the renderer"
            )
            await pilot.press("ctrl+q")

    asyncio.run(asyncio.wait_for(exercise(), timeout=15))


def test_renderer_replay_bypasses_turn_id_gate() -> None:
    """Replay must render retained events even when the turn rotated.

    ``emit`` filters by the bound turn id (live events); ``replay`` is the
    switch-back path and must not lose content that belongs to a turn which
    finished or rotated while the user was away.
    """
    from unittest.mock import MagicMock

    from synapse.runtime.streaming import TurnEvent, TurnEventKind
    from synapse.ui.turn.event_renderer import TextualTurnEventRenderer

    host = MagicMock()
    host.transcript_generation = 0
    renderer = TextualTurnEventRenderer(host, thread_id="a", turn_id="turn-2")
    old_turn = TurnEvent(
        version=1,
        thread_id="a",
        turn_id="turn-1",
        sequence=1,
        kind=TurnEventKind.INFO,
        payload="retained content",
    )
    # Live path: a different turn id is dropped.
    renderer.emit(old_turn)
    assert renderer.last_sequence == 0
    # Replay path: the same event is consumed (switch-back completeness).
    renderer.replay(old_turn)
    assert renderer.last_sequence == 1


def test_replay_terminal_event_does_not_close_renderer() -> None:
    """Replaying an old turn's terminal event must not kill live rendering.

    ``_render`` closes the renderer on terminal kinds; replaying a finished
    turn's ``TURN_COMPLETED`` would otherwise shut it down and drop the live
    events of the next turn after a switch-back.
    """
    from unittest.mock import MagicMock

    from synapse.runtime.streaming import (
        TextPayload,
        TurnEvent,
        TurnEventKind,
        TurnTerminalPayload,
    )
    from synapse.ui.turn.event_renderer import TextualTurnEventRenderer

    host = MagicMock()
    host.transcript_generation = 0
    renderer = TextualTurnEventRenderer(host, thread_id="a", turn_id="turn-2")

    terminal = TurnEvent(
        version=1,
        thread_id="a",
        turn_id="turn-1",
        sequence=1,
        kind=TurnEventKind.TURN_COMPLETED,
        payload=TurnTerminalPayload(status="completed"),
    )
    renderer.replay(terminal)
    assert not renderer._closed, "replayed terminal event must not close renderer"

    live = TurnEvent(
        version=1,
        thread_id="a",
        turn_id="turn-2",
        sequence=2,
        kind=TurnEventKind.ANSWER_DELTA,
        payload=TextPayload("live after switch-back"),
    )
    renderer.emit(live)
    assert renderer.last_sequence == 2, "live events dropped after replayed terminal"


def test_attach_uses_broker_sequence_across_turn_replay() -> None:
    """A long previous turn must not suppress a new turn's low local sequence."""
    from synapse.runtime.sessions import SessionEventBroker
    from synapse.runtime.streaming import TurnEvent, TurnEventKind

    app = _FakeApp()
    app._transcript = MagicMock()
    app._transcript.transcript_generation = 0
    app._transcript.call_from_thread = lambda callback, *args, **kwargs: callback(
        *args, **kwargs
    )
    controller = TurnController(app)
    broker = SessionEventBroker("t1")
    broker.emit(
        TurnEvent(
            version=1,
            thread_id="t1",
            turn_id="old-turn",
            sequence=10,
            kind=TurnEventKind.INFO,
            payload="old",
        )
    )

    runtime = SimpleNamespace(
        thread_id="t1",
        broker=broker,
        active_context=lambda: SimpleNamespace(thread_id="t1", turn_id="new-turn"),
        snapshot=lambda: SimpleNamespace(latest_sequence=1),
        subscribe=broker.subscribe,
    )
    controller.attach(runtime, after_sequence=0)
    broker.emit(
        TurnEvent(
            version=1,
            thread_id="t1",
            turn_id="new-turn",
            sequence=1,
            kind=TurnEventKind.INFO,
            payload="new",
        )
    )

    assert controller._event_bridge is not None
    assert controller._event_bridge._renderer.last_sequence == 2
    controller._detach_renderer()


def test_switch_attach_replays_only_active_turn_after_restored_history() -> None:
    """Switch-back keeps projected completed turns and replays the active turn only."""
    from synapse.runtime.sessions import SessionEventBroker
    from synapse.runtime.streaming import TurnEvent, TurnEventKind

    app = _FakeApp()
    app._transcript = MagicMock()
    app._transcript.transcript_generation = 0
    app._transcript.call_from_thread = lambda callback, *args, **kwargs: callback(
        *args, **kwargs
    )
    controller = TurnController(app)
    broker = SessionEventBroker("t1")
    for turn_id, text, sequence in (
        ("old-turn", "old thought", 7),
        ("active-turn", "active thought", 1),
        ("active-turn", "active tool", 2),
    ):
        broker.emit(
            TurnEvent(
                version=1,
                thread_id="t1",
                turn_id=turn_id,
                sequence=sequence,
                kind=TurnEventKind.INFO,
                payload=text,
            )
        )

    runtime = SimpleNamespace(
        thread_id="t1",
        broker=broker,
        active_context=lambda: SimpleNamespace(
            thread_id="t1",
            turn_id="active-turn",
        ),
        snapshot=lambda: SimpleNamespace(latest_sequence=3),
        subscribe=broker.subscribe,
    )
    controller.attach(runtime)

    assert app._transcript.append_meta.call_args_list == [
        (("active thought",), {}),
        (("active tool",), {}),
    ]
    assert controller._event_bridge is not None
    assert controller._event_bridge._renderer.last_sequence == 3
    controller._detach_renderer()


def test_run_turn_replays_only_events_after_start_cursor() -> None:
    """Starting a second turn must not redraw the session's completed events."""
    from synapse.runtime.agent_loop import TurnHandle
    from synapse.runtime.sessions import SessionStatus

    app = _FakeApp()
    app.settings = SimpleNamespace(max_concurrency=2, model="test")
    app._agent_ready = threading.Event()
    app._agent_ready.set()
    app._agent_error = None
    app._transcript_generation = 3
    app._subagent_monitor = SimpleNamespace(monitor_id="monitor")
    app._begin_turn_usage = lambda: None
    app._current_project_id = lambda: "project"
    controller = TurnController(app)
    result = TurnResult(
        turn_id="new-turn",
        thread_id="t1",
        status=TurnStatus.COMPLETED,
        streamed_answer=True,
    )
    future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()
    future.set_result(result)

    class _Runtime:
        thread_id = "t1"
        project_id = "project"
        agent = app.agent

        def snapshot(self) -> Any:
            return SimpleNamespace(
                latest_sequence=12,
                status=SessionStatus.IDLE,
                active_turn_id=None,
            )

        def start_threadsafe(self, message: Any, *, on_started: Any) -> TurnHandle:
            del message
            on_started(SimpleNamespace(thread_id="t1", turn_id="new-turn"))
            return TurnHandle("new-turn", future, SimpleNamespace())

        def wait_threadsafe(self, handle: TurnHandle) -> tuple[TurnResult, Any]:
            del handle
            return result, self.snapshot()

    runtime = _Runtime()
    controller._sessions["t1"] = runtime  # type: ignore[assignment]
    controller._attached_thread_id = "t1"
    controller._session_runtime = runtime  # type: ignore[assignment]
    controller.attach = MagicMock(return_value=runtime)  # type: ignore[method-assign]
    controller.apply_stream_result = MagicMock(return_value=False)  # type: ignore[method-assign]
    controller._turn_finished = MagicMock()  # type: ignore[method-assign]

    controller.run_turn(
        "second",
        thread_id="t1",
        agent=app.agent,
        transcript_generation=3,
        monitor_id="monitor",
    )

    controller.attach.assert_called_once_with(runtime, after_sequence=12)


class TestActiveSessionItems:
    """TurnController.active_session_items() snapshot for the Ctrl+Tab switcher."""

    @staticmethod
    def _runtime(
        controller: TurnController,
        *,
        thread_id: str,
        project_id: str,
        status: SessionStatus,
        activity: float,
        workspace: str = "proj",
    ) -> SessionRuntime:
        from datetime import UTC, datetime, timedelta

        runtime = SessionRuntime(
            thread_id=thread_id,
            project_id=project_id,
            agent=SimpleNamespace(_coding_goal_service=None),
            settings=SimpleNamespace(model="test", workspace="."),
            turn_runtime=controller._runtime,
            workspace=workspace,
        )
        # White-box: drive the snapshot status/activity directly; the public
        # transitions need a live turn handle we do not want to spin here.
        runtime._status = status  # type: ignore[attr-defined]
        runtime._last_activity_at = datetime.now(UTC) - timedelta(seconds=activity)
        return runtime

    def _controller(self) -> TurnController:
        app = _FakeApp()
        app.settings = SimpleNamespace(model="test", workspace=".")
        app._current_project_id = lambda: "p1"
        controller = TurnController(app)
        # Isolate the persisted-store probe: no real SQLite in unit tests.
        store = MagicMock()
        store.list_nonempty.return_value = []
        store.get.return_value = None
        controller._project_store["p1"] = store
        return controller

    def test_includes_recent_sessions_of_all_statuses(self) -> None:
        controller = self._controller()
        for tid, status in [
            ("a", SessionStatus.RUNNING),
            ("b", SessionStatus.QUEUED),
            ("c", SessionStatus.WAITING_APPROVAL),
            ("d", SessionStatus.IDLE),
            ("e", SessionStatus.FAILED),
            ("f", SessionStatus.CLOSED),
            ("g", SessionStatus.CANCELLED),
        ]:
            controller._sessions[tid] = self._runtime(
                controller,
                thread_id=tid,
                project_id="p1",
                status=status,
                activity=1.0,
            )

        items = controller.active_session_items()
        assert {item.thread_id for item in items} == {"a", "b", "c", "d", "e", "f", "g"}

    def test_includes_cross_project_active_runtimes(self) -> None:
        controller = self._controller()
        for project_id, tid in [("p1", "a"), ("p2", "b")]:
            runtime = self._runtime(
                controller,
                thread_id=tid,
                project_id=project_id,
                status=SessionStatus.RUNNING,
                activity=1.0,
            )
            project = controller.project_runtime_for(
                project_id, SimpleNamespace(model="test", workspace=".")
            )
            project.register_session(runtime)

        items = controller.active_session_items()
        assert {item.project_id for item in items} == {"p1", "p2"}
        assert {item.thread_id for item in items} == {"a", "b"}

    def test_sorted_by_last_activity_desc(self) -> None:
        controller = self._controller()
        # 'old' updated 100s ago, 'new' updated 1s ago, 'mid' 50s ago.
        for tid, activity in [("old", 100.0), ("new", 1.0), ("mid", 50.0)]:
            controller._sessions[tid] = self._runtime(
                controller,
                thread_id=tid,
                project_id="p1",
                status=SessionStatus.RUNNING,
                activity=activity,
            )

        items = controller.active_session_items()
        assert [item.thread_id for item in items] == ["new", "mid", "old"]

    def test_limits_to_most_recent_ten(self) -> None:
        controller = self._controller()
        # 12 sessions; activity 0..11 seconds ago (0 = newest).
        for i in range(12):
            controller._sessions[f"t{i:02d}"] = self._runtime(
                controller,
                thread_id=f"t{i:02d}",
                project_id="p1",
                status=SessionStatus.RUNNING,
                activity=float(i),
            )

        items = controller.active_session_items()
        assert len(items) == 10
        # Newest 10 survive the cap: t00..t09, newest first.
        assert [item.thread_id for item in items] == [
            f"t{i:02d}" for i in range(10)
        ]

    def test_title_falls_back_to_thread_id(self) -> None:
        controller = self._controller()
        runtime = self._runtime(
            controller,
            thread_id="abcdef1234567890",
            project_id="p1",
            status=SessionStatus.RUNNING,
            activity=1.0,
        )
        runtime.active_context = MagicMock(return_value=None)  # type: ignore[method-assign]
        controller._sessions[runtime.thread_id] = runtime

        items = controller.active_session_items()
        assert items[0].title == "abcdef12"

    def test_title_from_store(self) -> None:
        from synapse.sessions.store import SessionInfo

        controller = self._controller()
        store = MagicMock()
        store.get.return_value = SessionInfo(
            thread_id="a",
            title="  stored title  ",
            model="test",
            created_at="2026-08-01 09:00:00",
            updated_at="2026-08-01 09:00:00",
            tags=[],
        )
        controller._project_store["p1"] = store
        controller._sessions["a"] = self._runtime(
            controller,
            thread_id="a",
            project_id="p1",
            status=SessionStatus.RUNNING,
            activity=1.0,
        )

        items = controller.active_session_items()
        assert items[0].title == "stored title"

    def test_item_contains_project_id_and_current_flag(self) -> None:
        controller = self._controller()
        controller._sessions["a"] = self._runtime(
            controller,
            thread_id="a",
            project_id="p1",
            status=SessionStatus.RUNNING,
            activity=1.0,
        )
        controller._sessions["b"] = self._runtime(
            controller,
            thread_id="b",
            project_id="p2",
            status=SessionStatus.RUNNING,
            activity=2.0,
        )

        items = controller.active_session_items()
        by_id = {item.thread_id: item for item in items}
        assert by_id["a"].project_id == "p1"
        assert by_id["b"].project_id == "p2"

    def test_empty_when_no_runtimes(self) -> None:
        controller = self._controller()
        assert controller.active_session_items() == ()

    def test_merges_persisted_cold_sessions(self) -> None:
        from synapse.sessions.store import SessionInfo

        controller = self._controller()
        # One live runtime (5s ago) in the current project.
        controller._sessions["live"] = self._runtime(
            controller,
            thread_id="live",
            project_id="p1",
            status=SessionStatus.RUNNING,
            activity=5.0,
        )
        # Cold history: one new session, plus a duplicate of the live one.
        store = controller._project_store["p1"]
        store.list_nonempty.return_value = [
            SessionInfo(
                thread_id="cold-a",
                title="  cold title  ",
                model=None,
                created_at="2025-01-01T00:00:00+00:00",
                updated_at="2025-01-01T00:00:01+00:00",
                tags=[],
            ),
            SessionInfo(
                thread_id="live",
                title="dup",
                model=None,
                created_at="2025-01-01T00:00:00+00:00",
                updated_at="2025-01-01T00:00:02+00:00",
                tags=[],
            ),
        ]

        items = controller.active_session_items()
        by_id = {item.thread_id: item for item in items}
        # Duplicate resolved to the live runtime (only one row).
        assert set(by_id) == {"live", "cold-a"}
        assert by_id["cold-a"].status == SessionStatus.COLD
        assert by_id["cold-a"].title == "cold title"
        assert by_id["cold-a"].project_id == "p1"
        assert by_id["cold-a"].current is False
        # Cold row (persisted 2025) sorts after the live one (now - 5s).
        assert [item.thread_id for item in items] == ["live", "cold-a"]

    def test_cold_store_failure_yields_no_extra_rows(self) -> None:
        controller = self._controller()
        store = controller._project_store["p1"]
        store.list_nonempty.side_effect = RuntimeError("db locked")

        assert controller.active_session_items() == ()


    def test_current_flag_marks_attached_thread(self) -> None:
        controller = self._controller()
        app = controller._app
        app.thread_id = "b"
        controller._sessions["a"] = self._runtime(
            controller,
            thread_id="a",
            project_id="p1",
            status=SessionStatus.RUNNING,
            activity=1.0,
        )
        controller._sessions["b"] = self._runtime(
            controller,
            thread_id="b",
            project_id="p1",
            status=SessionStatus.RUNNING,
            activity=2.0,
        )

        items = controller.active_session_items()
        by_id = {item.thread_id: item for item in items}
        assert by_id["a"].current is False
        assert by_id["b"].current is True


def test_close_threadsafe_does_not_deadlock_on_busy_ui() -> None:
    """Regression: session close must never block the Agent loop on UI work.

    During app exit the UI thread blocks inside ``close_threadsafe`` while the
    Agent runtime loop runs ``close()``. The session status observer therefore
    must not synchronously wait for the UI thread (Textual ``call_from_thread``
    semantics) or the two threads deadlock until the close timeout fires.
    """
    from synapse.runtime.async_runtime import reset_async_runtime_for_tests

    class _BusyUiApp(_FakeApp):
        def __init__(self) -> None:
            super().__init__()
            # 未设置 = UI 线程忙（正阻塞在 close_threadsafe 的 future.result）。
            self.ui_released = threading.Event()

        def call_from_thread(self, callback: Any, *args: Any, **kwargs: Any) -> None:
            # Textual semantics: block until the UI thread can run the callback.
            self.ui_released.wait(timeout=None)
            callback(*args, **kwargs)

    reset_async_runtime_for_tests()
    app = _BusyUiApp()
    controller = TurnController(app)
    session = SessionRuntime(
        thread_id="deadlock",
        agent=object(),
        settings=SimpleNamespace(max_concurrency=2, model="test"),
        on_status_change=controller._on_session_status_changed,
    )
    started = time.perf_counter()
    # UI thread is "busy" (on_unmount); close must finish well under the 5s timeout.
    session.close_threadsafe(cancel_active=True, timeout=5.0)
    elapsed = time.perf_counter() - started
    app.ui_released.set()
    assert elapsed < 2.0, f"close blocked on UI observer for {elapsed:.2f}s"
    assert session.snapshot().status is SessionStatus.CLOSED
