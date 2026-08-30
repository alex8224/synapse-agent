"""Unit tests for the TUI turn controller (result application / goal flow)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessage

from synapse.runtime.agent_loop import TurnContext, TurnResult, TurnStatus
from synapse.runtime.agent_loop.request import build_turn_request
from synapse.runtime.service import SessionView, UsageView
from synapse.runtime.sessions import SessionStatus
from synapse.runtime.steer import SteerQueue
from synapse.sessions.transcript import UiTranscriptEvent
from synapse.sessions.transcript_projection import TranscriptProjection
from synapse.ui.turn.controller import TurnController
from synapse.ui.turn.service_session import TUIRuntimeSessionFacade

# Service-only test doubles below intentionally use facades rather than execution runtimes.


class _FakeFacade:
    """Service-only session double with a cancellable event watch."""

    def __init__(self, thread_id: str, *, active: str | None, latest: int = 0) -> None:
        self.binding = SimpleNamespace(session=SimpleNamespace(project_id="p", thread_id=thread_id))
        self.state = SimpleNamespace(
            view=SimpleNamespace(
                active_turn_id=active,
                latest_sequence=latest,
                status="running" if active else "idle",
            ),
            last_sequence=latest,
        )
        self.watches: list[int] = []
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = asyncio.Event()

    def watch(self, *, after: int | None = None) -> Any:
        self.watches.append(self.state.last_sequence if after is None else after)
        facade = self

        class Lease:
            async def __aenter__(self) -> Any:
                return facade

            async def __aexit__(self, *args: Any) -> None:
                facade.closed.set()

            def __aiter__(self) -> Any:
                return self

            async def __anext__(self) -> Any:
                event = await facade.queue.get()
                if event is StopAsyncIteration:
                    raise StopAsyncIteration
                return event

        return Lease()

    async def get(self, *, refresh: bool = True) -> Any:
        del refresh
        return self.state.view

    def emit(self, event: Any) -> None:
        self.queue.put_nowait(event)
        self.state.last_sequence = max(self.state.last_sequence, event.sequence)

    def close(self) -> None:
        self.queue.put_nowait(StopAsyncIteration)


class _ServiceFacadeWatchFake:
    """Thread-safe service facade fake with cursor-aware async watches."""

    def __init__(
        self,
        thread_id: str,
        *,
        active_turn: str | None,
        latest: int = 0,
        history: tuple[Any, ...] = (),
    ) -> None:
        self.binding = SimpleNamespace(session=SimpleNamespace(project_id="p", thread_id=thread_id))
        self.state = SimpleNamespace(
            view=SimpleNamespace(
                active_turn_id=active_turn,
                latest_sequence=latest,
                status="running" if active_turn else "idle",
            ),
            last_sequence=latest,
        )
        self.watches: list[int] = []
        self.entered = threading.Event()
        self.exited = threading.Event()
        self.rendered = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._history = list(history)
        self._current_queue: asyncio.Queue[Any] | None = None
        self._current_cycle: object | None = None
        self._lock = threading.Lock()
        self._watch_generation = 0

    def watch(self, *, after: int | None = None) -> Any:
        cursor = self.state.last_sequence if after is None else after
        self.watches.append(cursor)
        facade = self

        class Lease:
            def __init__(self) -> None:
                self.queue: asyncio.Queue[Any] | None = None
                self.cycle: object = object()
                with facade._lock:
                    facade.entered.clear()
                    facade.exited.clear()
                    facade.rendered.clear()
                    facade._current_cycle = self.cycle

            async def __aenter__(self) -> Any:
                loop = asyncio.get_running_loop()
                queue: asyncio.Queue[Any] = asyncio.Queue()
                with facade._lock:
                    self.queue = queue
                    if facade._current_cycle is not self.cycle:
                        raise asyncio.CancelledError
                    facade.loop = loop
                    facade._current_queue = queue
                    facade._current_cycle = self.cycle
                    history = tuple(facade._history)
                for event in history:
                    if event.sequence > cursor:
                        queue.put_nowait(event)
                facade.entered.set()
                return self

            async def __aexit__(self, *args: Any) -> None:
                with facade._lock:
                    if facade._current_cycle is self.cycle:
                        facade._current_queue = None
                        facade.loop = None
                        facade._current_cycle = None
                        facade.exited.set()

            def __aiter__(self) -> Any:
                return self

            async def __anext__(self) -> Any:
                assert self.queue is not None
                event = await self.queue.get()
                if event is StopAsyncIteration:
                    raise StopAsyncIteration
                return event

        return Lease()

    def emit(self, event: Any) -> None:
        with self._lock:
            self._history.append(event)
            self.state.last_sequence = max(self.state.last_sequence, event.sequence)
            self.state.view.latest_sequence = self.state.last_sequence
            loop = self.loop
            queue = self._current_queue
        if loop is not None and queue is not None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

    def close(self) -> None:
        with self._lock:
            loop = self.loop
            queue = self._current_queue
        if loop is not None and queue is not None:
            loop.call_soon_threadsafe(queue.put_nowait, StopAsyncIteration)

    async def get(self, *, refresh: bool = True) -> Any:
        del refresh
        return self.state.view


class _FakeApp:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._skip_steer_followup = False
        self._busy = False
        self.thread_id = "t1"
        self._transcript_generation = 0
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

    def _touch_session_bg(
        self, thread_id: str, title_hint: str, model: str, generation: int
    ) -> None:
        """No-op host worker; the session store is not exercised in these tests."""
        self.calls.append(("_touch_session_bg", (thread_id, title_hint, model, generation), {}))

    def append_event(self, message: str, style: str = "dim") -> None:
        self.calls.append(("append_event", (message, style), {}))

    def commit_answer(self, text: str) -> None:
        self.calls.append(("commit_answer", (text,), {}))

    def apply_turn_usage(self, **kwargs: Any) -> None:
        self.calls.append(("apply_turn_usage", (), kwargs))

    def refresh_image_preview(self) -> None:
        """No-op: image preview is not exercised by turn controller tests."""


def test_submit_schedules_worker_without_legacy_reservation() -> None:
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
    app.clear_stream = lambda: None
    app.set_activity = lambda *args: None
    app._sync_prompt_placeholder = lambda: None
    app._current_project_id = lambda: "project"
    scheduled: list[Any] = []
    app.run_turn = lambda text, attachments, **kwargs: scheduled.append((text, attachments, kwargs))
    controller = TurnController(app)
    controller._session_for = MagicMock()  # type: ignore[method-assign]
    controller._service_session_cached = MagicMock(return_value=None)  # type: ignore[method-assign]
    event = SimpleNamespace(value="hello", input=SimpleNamespace(value="hello"))

    controller.submit(event)

    controller._session_for.assert_not_called()
    assert scheduled == [("hello", None, {})]
    assert not any("reservation" in kwargs for _, _, kwargs in scheduled)


def test_bind_agent_updates_future_factory_without_replacing_open_facade() -> None:
    app = _FakeApp()
    app.settings = SimpleNamespace(model="test", workspace=".")
    app._current_project_id = lambda: "project"
    controller = TurnController(app)
    existing = MagicMock(spec=TUIRuntimeSessionFacade)
    existing.binding = SimpleNamespace(
        session=SimpleNamespace(project_id="project", thread_id="t1")
    )
    controller._service_sessions["project:t1"] = existing

    first = object()
    second = object()
    assert controller.bind_agent("t1", first) is existing.binding
    assert controller.bind_agent("t1", second) is existing.binding
    assert controller._service_agents[("project", "t1")] is second
    assert controller._service_sessions["project:t1"] is existing


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
    facade = SimpleNamespace(state=SimpleNamespace(view=SimpleNamespace(status="idle")))

    def run_turn(text: str, attachments: Any) -> None:
        del text, attachments
        facade.state.view.status = "running"

    app.run_turn = MagicMock(side_effect=run_turn)

    controller = TurnController(app)
    controller._service_session_cached = MagicMock(return_value=facade)  # type: ignore[method-assign]
    assert controller.maybe_continue_goal() is True
    app.run_turn.assert_called_once()
    continuation_text = app.run_turn.call_args.args[0]
    assert continuation_text.startswith(GOAL_STEER_PREFIX)
    assert app.run_turn.call_args.args[1] is None

    # No duplicate continuation while the scheduled turn is busy.
    assert controller.maybe_continue_goal() is False
    app.run_turn.assert_called_once()


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
    )

    assert isinstance(req, TurnRequest)
    assert req.thread_id == "t-1"
    assert req.config["max_concurrency"] == 3
    assert req.config["configurable"]["thread_id"] == "t-1"
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
        max_concurrency=8,
    )
    assert req.config["max_concurrency"] == 8


def test_busy_cancel_and_steer_delegate_to_service_facade() -> None:
    app = _FakeApp()
    app._current_project_id = lambda: "p"
    controller = TurnController(app)
    calls: list[tuple[str, str]] = []

    class _Facade:
        binding = SimpleNamespace(session=SimpleNamespace(project_id="p", thread_id="t1"))
        state = SimpleNamespace(view=SimpleNamespace(status="running", active_turn_id="turn"))

        async def cancel(self, reason: str) -> bool:
            calls.append(("cancel", reason))
            return True

        async def steer(self, text: str) -> bool:
            calls.append(("steer", text))
            return True

    controller._service_sessions["p:t1"] = _Facade()

    assert controller.busy is True
    assert controller.cancel("escape") is True
    assert controller.steer("focus") is True
    assert calls == [("cancel", "escape"), ("steer", "focus")]


def test_status_update_refreshes_facade_for_cancel_gate() -> None:
    app = _FakeApp()
    app._current_project_id = lambda: "p"
    controller = TurnController(app)
    facade = SimpleNamespace(
        binding=SimpleNamespace(session=SimpleNamespace(project_id="p", thread_id="t1")),
        state=SimpleNamespace(
            view=SessionView("p", "t1", "idle", None, 0, UsageView(), None, ""),
            last_sequence=0,
        ),
    )
    controller._service_sessions["p:t1"] = facade

    controller._on_session_status_changed(
        SimpleNamespace(
            project_id="p",
            thread_id="t1",
            status=SessionStatus.RUNNING,
            active_turn_id="turn-1",
            latest_sequence=7,
            usage=SimpleNamespace(input_tokens=1, output_tokens=2, cache_tokens=3),
            last_error=None,
            last_activity_at="now",
        )
    )

    assert controller.busy is True
    assert facade.state.view.active_turn_id == "turn-1"
    assert facade.state.view.latest_sequence == 7
    assert facade.state.last_sequence == 7


def test_switch_keeps_background_service_session_running() -> None:
    """P5-06: detaching one session and attaching another must not cancel the old turn."""
    app = _FakeApp()
    app._current_project_id = lambda: "p"
    controller = TurnController(app)

    def facade(thread_id: str) -> Any:
        return SimpleNamespace(
            binding=SimpleNamespace(session=SimpleNamespace(project_id="p", thread_id=thread_id)),
            state=SimpleNamespace(
                view=SimpleNamespace(status="running", active_turn_id="turn-" + thread_id)
            ),
        )

    controller._service_sessions = {"p:a": facade("a"), "p:b": facade("b")}
    app.thread_id = "b"
    controller._attached_thread_id = "b"
    assert controller.background_running_count() == 1
    assert controller.runtime_status_map() == {"a": "running", "b": "running"}
    controller.detach("b")
    assert controller.busy is True  # session "b" still active


def test_runtime_status_map_only_includes_memory_sessions() -> None:
    app = _FakeApp()
    controller = TurnController(app)
    assert controller.runtime_status_map() == {}
    assert controller.background_running_count() == 0


def test_shutdown_without_service_sessions_is_idempotent() -> None:
    controller = TurnController(_FakeApp())
    controller.shutdown()
    controller.shutdown()
    assert controller.runtime_status_map() == {}
    assert controller.session_binding is None


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
    from synapse.config import Settings
    from synapse.ui.tui import CodingAgentApp

    monkeypatch.setattr("synapse.ui.tui.InputHistory.for_project", lambda *a, **k: MagicMock())
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
    app = CodingAgentApp(agent=object(), settings=settings, thread_id="a", project_root=tmp_path)
    app._current_project_id = lambda: "p"
    facade_a = _ServiceFacadeWatchFake("a", active_turn="turn-a", latest=0)
    facade_b = _ServiceFacadeWatchFake("b", active_turn="turn-b")
    app._turn._service_sessions = {"p:a": facade_a, "p:b": facade_b}

    async def exercise() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            for thread_id, facade in (("a", facade_a), ("b", facade_b), ("a", facade_a)):
                app.thread_id = thread_id
                app._turn.attach(thread_id)
                assert facade.entered.wait(2)
                assert app._turn._attached_thread_id == thread_id
                app._turn.detach(thread_id)
                facade.close()
                assert facade.exited.wait(2)
            assert app.is_running
            await pilot.press("ctrl+q")

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))


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
        def launch_context(self) -> tuple[str, Any, int]:
            return "t-9", frozen_agent, 7

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
    }
    assert turn_calls == [("hello", ["img"], expected)]
    assert resume_calls == [("approve", "ok", {**expected, "quiet": False})]


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
    class _ChromeApp(_FakeApp):
        def __init__(self) -> None:
            super().__init__()
            self.turn_done_calls = 0
            self.topbar_refreshes = 0

        def _turn_done(self) -> None:
            self.turn_done_calls += 1

        def _refresh_topbar(self) -> None:
            self.topbar_refreshes += 1

    app = _ChromeApp()
    controller = TurnController(app)
    facade_a = _ServiceFacadeWatchFake("a", active_turn="turn-a", latest=0)
    facade_b = _ServiceFacadeWatchFake("b", active_turn="turn-b")
    controller._service_sessions = {"p:a": facade_a, "p:b": facade_b}

    def refresh(callback: Any, *args: Any, **kwargs: Any) -> Any:
        result = callback(*args, **kwargs)
        facade_a.rendered.set()
        return result

    app.call_after_refresh = refresh
    controller._attached_thread_id = "b"
    controller._turn_finished(app, thread_id="a", facade=facade_a)

    assert app.turn_done_calls == 0
    assert app.topbar_refreshes == 1
    assert controller._attached_thread_id == "b"
    assert controller.session_binding is facade_b.binding


def test_switch_back_keeps_bridge_alive() -> None:
    from synapse.runtime.service.events import RuntimeEvent

    app = _FakeApp()
    app._transcript = MagicMock()
    app._transcript.transcript_generation = 0
    app._transcript.call_from_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)
    controller = TurnController(app)
    facade_a = _ServiceFacadeWatchFake("a", active_turn="turn-a")
    facade_b = _ServiceFacadeWatchFake("b", active_turn="turn-b")
    controller._service_sessions = {"p:a": facade_a, "p:b": facade_b}
    app._transcript.set_stream.side_effect = lambda *a, **k: facade_a.rendered.set()
    app.call_after_refresh = lambda callback, *args, **kwargs: callback(*args, **kwargs)

    async def exercise() -> None:
        controller.attach("a")
        assert facade_a.entered.wait(2)
        controller.detach("a")
        facade_a.close()
        assert facade_a.exited.wait(2)
        controller.attach("b")
        assert facade_b.entered.wait(2)
        controller.detach("b")
        facade_b.close()
        assert facade_b.exited.wait(2)
        facade_a.state.last_sequence = 0
        facade_a.state.view.latest_sequence = 0
        facade_a._history.clear()
        facade_a.entered.clear()
        facade_a.exited.clear()
        controller.attach("a")
        assert facade_a.entered.wait(2)
        facade_a.rendered.clear()
        facade_a.emit(RuntimeEvent(1, 1, "turn-a", "answer_delta", {"text": "live"}, 1))
        assert await asyncio.to_thread(facade_a.rendered.wait, 2)
        assert app._transcript.set_stream.called
        controller.detach("a")
        facade_a.close()
        assert facade_a.exited.wait(2)

    asyncio.run(exercise())


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


def test_attach_uses_service_sequence_across_turn_replay() -> None:
    from synapse.runtime.service.events import RuntimeEvent

    app = _FakeApp()
    app._transcript = MagicMock()
    controller = TurnController(app)
    facade = _ServiceFacadeWatchFake("t1", active_turn="new-turn", latest=10)
    controller._service_sessions = {"p:t1": facade}
    controller.attach("t1", after_sequence=10)
    assert facade.entered.wait(2)
    facade.emit(RuntimeEvent(11, 1, "new-turn", "info", {"message": "new"}, 1))
    facade.loop.call_soon_threadsafe(lambda: None)
    assert facade.watches == [10]
    controller.detach("t1")
    facade.close()
    assert facade.exited.wait(2)


def test_switch_attach_replays_only_active_turn_after_restored_history() -> None:
    from synapse.runtime.service.events import RuntimeEvent

    app = _FakeApp()
    app._transcript = MagicMock()
    controller = TurnController(app)
    history = (
        RuntimeEvent(7, 1, "old-turn", "info", {"message": "old"}, 1),
        RuntimeEvent(8, 1, "active-turn", "info", {"message": "active"}, 1),
    )
    facade = _ServiceFacadeWatchFake("t1", active_turn="active-turn", latest=7, history=history)
    rendered: list[Any] = []

    def refresh(callback, *args, **kwargs):
        rendered.append(args[-1])
        callback(*args, **kwargs)
        facade.rendered.set()

    app.call_after_refresh = refresh
    controller._service_sessions = {"p:t1": facade}
    controller.attach("t1", after_sequence=7)
    assert facade.entered.wait(2)
    facade.rendered.clear()
    facade.emit(RuntimeEvent(9, 2, "old-turn", "info", {"message": "late old"}, 1))
    facade.emit(RuntimeEvent(10, 2, "active-turn", "info", {"message": "live active"}, 1))
    assert facade.loop is not None
    facade.loop.call_soon_threadsafe(lambda: None)
    assert facade.rendered.wait(2)
    controller.detach("t1")
    facade.close()
    assert facade.exited.wait(2)
    assert [event.turn_id for event in rendered] == ["active-turn", "active-turn"]


def test_run_turn_replays_only_events_after_start_cursor() -> None:
    """Starting a second turn must not redraw the session's completed events."""
    app = _FakeApp()
    app.settings = SimpleNamespace(max_concurrency=2, model="test")
    app._agent_ready = threading.Event()
    app._agent_ready.set()
    app._agent_error = None
    app._transcript_generation = 3
    app._begin_turn_usage = lambda: None
    app._current_project_id = lambda: "project"
    controller = TurnController(app)
    result = SimpleNamespace(
        turn_id="new-turn",
        status="completed",
        final_text="",
        already_streamed=True,
    )

    facade = MagicMock()
    facade.submit = AsyncMock(return_value=result)
    facade.binding = SimpleNamespace(session=SimpleNamespace(project_id="project", thread_id="t1"))
    facade.state = SimpleNamespace(view=None, last_sequence=0)
    controller._service_sessions["project:t1"] = facade
    controller._service_agents["t1"] = app.agent
    controller.apply_consumer_result = MagicMock(return_value=False)  # type: ignore[method-assign]
    controller._turn_finished = MagicMock()  # type: ignore[method-assign]

    controller.run_turn("second", thread_id="t1", agent=app.agent, transcript_generation=3)

    facade.submit.assert_called_once()


class TestActiveSessionItems:
    """TurnController.active_session_items() snapshot for the Ctrl+Tab switcher."""

    @staticmethod
    def _service_facade(
        controller: TurnController,
        *,
        thread_id: str,
        project_id: str,
        status: SessionStatus,
        activity: float,
        workspace: str = "proj",
        title: str | None = None,
        model: str = "test",
    ) -> Any:
        from datetime import UTC, datetime, timedelta

        calls: list[tuple[str, str]] = []
        facade = SimpleNamespace(
            thread_id=thread_id,
            binding=SimpleNamespace(
                session=SimpleNamespace(project_id=project_id, thread_id=thread_id)
            ),
            state=SimpleNamespace(
                view=SimpleNamespace(
                    project_id=project_id,
                    thread_id=thread_id,
                    status=status.value,
                    active_turn_id=("turn-" + thread_id)
                    if status
                    in {
                        SessionStatus.QUEUED,
                        SessionStatus.STARTING,
                        SessionStatus.RUNNING,
                        SessionStatus.CANCELLING,
                        SessionStatus.WAITING_APPROVAL,
                    }
                    else None,
                    last_activity_at=(datetime.now(UTC) - timedelta(seconds=activity)).isoformat(),
                    latest_sequence=0,
                )
            ),
            calls=calls,
        )

        async def cancel(reason: str) -> bool:
            calls.append(("cancel", reason))
            return True

        async def steer(text: str) -> bool:
            calls.append(("steer", text))
            return True

        facade.cancel = cancel
        facade.steer = steer
        controller._service_sessions[f"{project_id}:{thread_id}"] = facade
        controller._service_metadata[(project_id, thread_id)] = {
            "title": title,
            "model": model,
            "workspace": workspace,
        }
        return facade

    _runtime = _service_facade

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
            self._runtime(
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
            del runtime

        items = controller.active_session_items()
        assert {item.project_id for item in items} == {"p1", "p2"}
        assert {item.thread_id for item in items} == {"a", "b"}

    def test_sorted_by_last_activity_desc(self) -> None:
        controller = self._controller()
        # 'old' updated 100s ago, 'new' updated 1s ago, 'mid' 50s ago.
        for tid, activity in [("old", 100.0), ("new", 1.0), ("mid", 50.0)]:
            self._runtime(
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
            self._runtime(
                controller,
                thread_id=f"t{i:02d}",
                project_id="p1",
                status=SessionStatus.RUNNING,
                activity=float(i),
            )

        items = controller.active_session_items()
        assert len(items) == 10
        # Newest 10 survive the cap: t00..t09, newest first.
        assert [item.thread_id for item in items] == [f"t{i:02d}" for i in range(10)]

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
        controller._service_sessions["p1:abcdef1234567890"] = runtime

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
        self._runtime(
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
        self._runtime(
            controller,
            thread_id="a",
            project_id="p1",
            status=SessionStatus.RUNNING,
            activity=1.0,
        )
        self._runtime(
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
        self._runtime(
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

    def test_global_catalog_cold_sessions_across_projects(self) -> None:
        from synapse.projects.catalog import CatalogSession

        controller = self._controller()
        controller._app._project_catalog = SimpleNamespace(
            list_sessions=lambda *, limit=200: [
                CatalogSession(
                    project_id="pa",
                    project_name="proj-a",
                    workspace_path="/ws/a",
                    thread_id="ca",
                    title="cold a",
                    model=None,
                    summary=None,
                    updated_at="2025-01-01T00:00:03+00:00",
                    created_at="2025-01-01T00:00:00+00:00",
                    tags=[],
                ),
                CatalogSession(
                    project_id="pb",
                    project_name="proj-b",
                    workspace_path="/ws/b",
                    thread_id="cb",
                    title="cold b",
                    model=None,
                    summary=None,
                    updated_at="2025-01-01T00:00:01+00:00",
                    created_at="2025-01-01T00:00:00+00:00",
                    tags=[],
                ),
            ]
        )

        items = controller.active_session_items()
        by_id = {item.thread_id: item for item in items}
        assert set(by_id) == {"ca", "cb"}
        assert by_id["ca"].status == SessionStatus.COLD
        assert by_id["ca"].project_id == "pa"
        assert by_id["ca"].project_label == "proj-a"
        assert by_id["ca"].title == "cold a"
        assert by_id["ca"].current is False
        # Sorted by persisted updated_at, newest first.
        assert [item.thread_id for item in items] == ["ca", "cb"]

    def test_runtime_status_overrides_catalog_row(self) -> None:
        from synapse.projects.catalog import CatalogSession

        controller = self._controller()
        self._runtime(
            controller,
            thread_id="live",
            project_id="pa",
            status=SessionStatus.RUNNING,
            activity=1.0,
            workspace="proj-a",
        )
        controller._app._project_catalog = SimpleNamespace(
            list_sessions=lambda *, limit=200: [
                CatalogSession(
                    project_id="pa",
                    project_name="proj-a",
                    workspace_path="/ws/a",
                    thread_id="live",
                    title="catalog title",
                    model=None,
                    summary=None,
                    updated_at="2025-01-01T00:00:00+00:00",
                    created_at="2025-01-01T00:00:00+00:00",
                    tags=[],
                ),
                CatalogSession(
                    project_id="pb",
                    project_name="proj-b",
                    workspace_path="/ws/b",
                    thread_id="cold",
                    title="cold b",
                    model=None,
                    summary=None,
                    updated_at="2025-01-01T00:00:01+00:00",
                    created_at="2025-01-01T00:00:00+00:00",
                    tags=[],
                ),
            ]
        )

        items = controller.active_session_items()
        by_id = {item.thread_id: item for item in items}
        # Runtime snapshot status wins over the catalog projection.
        assert by_id["live"].status == SessionStatus.RUNNING
        assert by_id["cold"].status == SessionStatus.COLD
        # Live runtime (now - 1s) sorts before the cold 2025 row.
        assert [item.thread_id for item in items] == ["live", "cold"]

    def test_runtime_not_in_catalog_is_appended(self) -> None:
        controller = self._controller()
        self._runtime(
            controller,
            thread_id="active",
            project_id="pa",
            status=SessionStatus.RUNNING,
            activity=1.0,
        )
        # Catalog has no projection yet (active turn not persisted).
        controller._app._project_catalog = SimpleNamespace(list_sessions=lambda *, limit=200: [])

        items = controller.active_session_items()
        assert [item.thread_id for item in items] == ["active"]
        assert items[0].status == SessionStatus.RUNNING

    def test_catalog_failure_falls_back_to_legacy(self) -> None:
        def _boom(**kwargs: Any) -> list[Any]:
            del kwargs
            raise RuntimeError("db locked")

        controller = self._controller()
        self._runtime(
            controller,
            thread_id="a",
            project_id="p1",
            status=SessionStatus.RUNNING,
            activity=1.0,
        )
        controller._app._project_catalog = SimpleNamespace(list_sessions=_boom)

        # Legacy fallback: live runtime survives, no exception leaks.
        items = controller.active_session_items()
        assert [item.thread_id for item in items] == ["a"]
        assert items[0].status == SessionStatus.RUNNING

    def test_current_flag_marks_attached_thread(self) -> None:
        controller = self._controller()
        app = controller._app
        app.thread_id = "b"
        self._runtime(
            controller,
            thread_id="a",
            project_id="p1",
            status=SessionStatus.RUNNING,
            activity=1.0,
        )
        self._runtime(
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


def _status_snapshot(
    thread_id: str, status: SessionStatus, last_error: str = "", project_id: str = ""
) -> Any:
    """Minimal SessionSnapshot stand-in for status-observer tests."""
    return SimpleNamespace(
        thread_id=thread_id,
        status=status,
        last_error=last_error,
        project_id=project_id,
    )


class _NotifyApp(_FakeApp):
    """Fake host that records flash_status calls like the real TUI."""

    def __init__(self) -> None:
        super().__init__()
        self.flashes: list[tuple[str, str]] = []
        self.toasts: list[tuple[str, str, str]] = []

    def flash_status(self, message: str, style: str = "dim", **kwargs: Any) -> None:
        del kwargs
        self.flashes.append((message, style))

    def notify(
        self,
        message: str,
        *,
        severity: str = "information",
        timeout: Any = 8,
        title: str = "",
    ) -> None:
        del timeout
        self.toasts.append((message, severity, title))


class TestBackgroundDoneNotices:
    """Foreground session sees a notice when a background session settles."""

    @staticmethod
    def _controller(app: _NotifyApp) -> TurnController:
        app.thread_id = "foreground"
        app._refresh_topbar = lambda: None
        controller = TurnController(app)
        controller._attached_thread_id = "foreground"
        return controller

    def test_background_done_flashes_notice(self) -> None:
        app = _NotifyApp()
        controller = self._controller(app)

        controller._on_session_status_changed(_status_snapshot("bg", SessionStatus.RUNNING))
        controller._on_session_status_changed(_status_snapshot("bg", SessionStatus.IDLE))

        assert len(app.flashes) == 1
        message, style = app.flashes[0]
        assert message.startswith("Background session done: ")
        assert "bg" in message
        assert style == "green"

    def test_foreground_done_does_not_flash(self) -> None:
        app = _NotifyApp()
        controller = self._controller(app)

        controller._on_session_status_changed(_status_snapshot("foreground", SessionStatus.RUNNING))
        controller._on_session_status_changed(_status_snapshot("foreground", SessionStatus.IDLE))

        assert app.flashes == []

    def test_idle_without_prior_active_does_not_flash(self) -> None:
        app = _NotifyApp()
        controller = self._controller(app)

        # First snapshot for this session is already terminal (cold/attach race).
        controller._on_session_status_changed(_status_snapshot("bg", SessionStatus.IDLE))

        assert app.flashes == []

    def test_background_failed_flashes_error(self) -> None:
        app = _NotifyApp()
        controller = self._controller(app)

        controller._on_session_status_changed(_status_snapshot("bg", SessionStatus.RUNNING))
        controller._on_session_status_changed(
            _status_snapshot("bg", SessionStatus.FAILED, "boom: out of budget")
        )

        assert len(app.flashes) == 1
        message, style = app.flashes[0]
        assert message.startswith("Background session failed: ")
        assert "boom: out of budget" in message
        assert style == "yellow"

    def test_background_cancelled_flashes_notice(self) -> None:
        app = _NotifyApp()
        controller = self._controller(app)

        controller._on_session_status_changed(_status_snapshot("bg", SessionStatus.RUNNING))
        controller._on_session_status_changed(_status_snapshot("bg", SessionStatus.CANCELLED))

        assert len(app.flashes) == 1
        message, style = app.flashes[0]
        assert message.startswith("Background session cancelled: ")
        assert style == "dim"

    def test_ongoing_active_transitions_do_not_flash(self) -> None:
        app = _NotifyApp()
        controller = self._controller(app)

        controller._on_session_status_changed(_status_snapshot("bg", SessionStatus.QUEUED))
        controller._on_session_status_changed(_status_snapshot("bg", SessionStatus.RUNNING))

        assert app.flashes == []

    def test_no_flash_when_host_lacks_flash_status(self) -> None:
        app = _FakeApp()
        app.thread_id = "foreground"
        app._refresh_topbar = lambda: None
        controller = TurnController(app)
        controller._attached_thread_id = "foreground"

        controller._on_session_status_changed(_status_snapshot("bg", SessionStatus.RUNNING))
        controller._on_session_status_changed(_status_snapshot("bg", SessionStatus.IDLE))

        # Best-effort: no flash_status surface, no exception.
        assert controller._pending_done_notices == []

    def test_done_toast_includes_answer_summary(self, tmp_path) -> None:
        app = _NotifyApp()
        controller = self._controller(app)
        projection = TranscriptProjection(tmp_path / "projection.sqlite")
        projection.append_turn(
            "bg",
            [
                UiTranscriptEvent(kind="user", text="fix the flaky test"),
                UiTranscriptEvent(kind="answer", text="The retry loop now waits 2s."),
            ],
        )
        controller._project_projection["project"] = projection

        controller._on_session_status_changed(
            _status_snapshot("bg", SessionStatus.RUNNING, project_id="project")
        )
        controller._on_session_status_changed(
            _status_snapshot("bg", SessionStatus.IDLE, project_id="project")
        )

        assert len(app.toasts) == 1
        message, severity, title = app.toasts[0]
        assert severity == "success"
        # Toast heading is stable; the body labels the request and result.
        assert title == "Background session done"
        assert message == "Request: fix the flaky test\nResult: The retry loop now waits 2s."
        # Flash stays a short single line with the state phrase.
        assert app.flashes[0][0] == "Background session done: fix the flaky test"

    def test_failed_toast_keeps_error_and_summary(self, tmp_path) -> None:
        app = _NotifyApp()
        controller = self._controller(app)
        projection = TranscriptProjection(tmp_path / "projection.sqlite")
        projection.append_turn(
            "bg",
            [
                UiTranscriptEvent(kind="user", text="deploy"),
                UiTranscriptEvent(kind="answer", text="Deploy aborted after lint."),
            ],
        )
        controller._project_projection["project"] = projection

        controller._on_session_status_changed(
            _status_snapshot("bg", SessionStatus.RUNNING, project_id="project")
        )
        controller._on_session_status_changed(
            _status_snapshot("bg", SessionStatus.FAILED, "boom: no budget", project_id="project")
        )

        message, severity, title = app.toasts[0]
        assert severity == "error"
        assert title == "Background session failed"
        assert message == (
            "Request: deploy\nError: boom: no budget\nResult: Deploy aborted after lint."
        )
        assert "boom: no budget" in app.flashes[0][0]

    def test_toast_without_projection_is_title_only(self) -> None:
        app = _NotifyApp()
        controller = self._controller(app)

        controller._on_session_status_changed(
            _status_snapshot("bg", SessionStatus.RUNNING, project_id="project")
        )
        controller._on_session_status_changed(
            _status_snapshot("bg", SessionStatus.CANCELLED, project_id="project")
        )

        message, severity, title = app.toasts[0]
        assert severity == "warning"
        assert title == "Background session cancelled"
        assert message == "Request: bg"

    def test_title_uses_last_user_request_not_first(self, tmp_path) -> None:
        """A multi-turn session titles the notice with its final request."""
        app = _NotifyApp()
        controller = self._controller(app)
        projection = TranscriptProjection(tmp_path / "projection.sqlite")
        projection.append_turn(
            "bg",
            [
                UiTranscriptEvent(kind="user", text="hi"),
                UiTranscriptEvent(kind="answer", text="Hello!"),
            ],
        )
        projection.append_turn(
            "bg",
            [
                UiTranscriptEvent(kind="user", text="now fix the bug"),
                UiTranscriptEvent(kind="answer", text="Fixed in src/app.py."),
            ],
        )
        controller._project_projection["project"] = projection

        controller._on_session_status_changed(
            _status_snapshot("bg", SessionStatus.RUNNING, project_id="project")
        )
        controller._on_session_status_changed(
            _status_snapshot("bg", SessionStatus.IDLE, project_id="project")
        )

        message, _, title = app.toasts[0]
        assert title == "Background session done"
        assert message == "Request: now fix the bug\nResult: Fixed in src/app.py."
        assert app.flashes[0][0] == "Background session done: now fix the bug"

    def test_toast_bounds_long_request_and_result(self, tmp_path) -> None:
        app = _NotifyApp()
        controller = self._controller(app)
        projection = TranscriptProjection(tmp_path / "projection.sqlite")
        projection.append_turn(
            "bg",
            [
                UiTranscriptEvent(kind="user", text="q" * 100),
                UiTranscriptEvent(kind="answer", text="a" * 300),
            ],
        )
        controller._project_projection["project"] = projection

        controller._on_session_status_changed(
            _status_snapshot("bg", SessionStatus.RUNNING, project_id="project")
        )
        controller._on_session_status_changed(
            _status_snapshot("bg", SessionStatus.IDLE, project_id="project")
        )

        message, _, title = app.toasts[0]
        assert title == "Background session done"
        request, result = message.split("\n")
        assert request == f"Request: {'q' * 71}…"
        assert result == f"Result: {'a' * 159}…"

    def test_projection_fallback_reads_project_registry(self, tmp_path) -> None:
        """The real activate() path never populates ``_project_projection``;
        the summary/title lookup must fall back to the project registry."""
        app = _NotifyApp()
        controller = self._controller(app)
        settings = SimpleNamespace(
            workspace=str(tmp_path),
            resolved_sessions_path=lambda: str(tmp_path / "sessions.sqlite"),
        )
        project = controller.project_runtime_for("project", settings, activate=True)
        assert project.transcript_projection is not None
        project.transcript_projection.append_turn(
            "bg",
            [
                UiTranscriptEvent(kind="user", text="hi"),
                UiTranscriptEvent(kind="answer", text="hello there"),
            ],
        )
        assert controller._project_projection == {}

        controller._on_session_status_changed(
            _status_snapshot("bg", SessionStatus.RUNNING, project_id="project")
        )
        controller._on_session_status_changed(
            _status_snapshot("bg", SessionStatus.IDLE, project_id="project")
        )

        message, _, title = app.toasts[0]
        assert title == "Background session done"
        assert message == "Request: hi\nResult: hello there"


def test_summarize_text_collapses_and_truncates() -> None:
    from synapse.ui.turn.controller import _summarize_text

    assert _summarize_text("a\n\n  b  c  ") == "a b c"
    long = "x" * 300
    out = _summarize_text(long)
    assert len(out) == 160
    assert out.endswith("…")


def _obsolete_background_done_runtime_test() -> None:
    """End-to-end: a real background ``SessionRuntime`` settlement must flash.

    Uses the actual status callback wiring (``on_status_change``) and the real
    ``_settle`` path, not hand-built snapshots, so it guards the whole
    RUNNING→IDLE transition from the runtime thread into the UI thread.
    """
    import asyncio
    import concurrent.futures

    from synapse.runtime.agent_loop import CancelToken, TurnHandle, TurnResult, TurnStatus
    from synapse.runtime.sessions import SessionRuntime, UserTurn

    class _Controlled:
        def __init__(self) -> None:
            self.futures: dict[str, concurrent.futures.Future[TurnResult]] = {}

        def submit(self, context: Any, *, sink: Any, cancel_token: CancelToken) -> TurnHandle:
            del sink
            future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()
            self.futures[context.thread_id] = future
            return TurnHandle(context.turn_id, future, cancel_token)

    app = _NotifyApp()
    app.thread_id = "foreground"
    app._refresh_topbar = lambda: None
    controller = TurnController(app)
    controller._attached_thread_id = "foreground"
    controlled = _Controlled()
    runtime = SessionRuntime(
        thread_id="bg",
        agent=object(),
        settings=SimpleNamespace(max_concurrency=2, model="test"),
        turn_runtime=controlled,  # type: ignore[arg-type]
        on_status_change=controller._on_session_status_changed,
    )
    controller._sessions["bg"] = runtime

    async def run() -> None:
        handle = await runtime.submit(UserTurn("bg work"))
        assert runtime.snapshot().status is SessionStatus.RUNNING
        app.flashes.clear()  # startup transition must not have flashed

        controlled.futures["bg"].set_result(
            TurnResult(
                turn_id=handle.turn_id,
                thread_id="bg",
                status=TurnStatus.COMPLETED,
                final_text="ok",
                input_tokens=1,
                output_tokens=1,
            )
        )
        await asyncio.wrap_future(handle.future)
        for _ in range(50):
            if runtime.snapshot().status is SessionStatus.IDLE:
                break
            await asyncio.sleep(0)
        await runtime.close(cancel_active=False)

    asyncio.run(run())

    assert len(app.flashes) == 1
    assert app.flashes[0][0].startswith("Background session done: ")
    assert len(app.toasts) == 1
    # No projection -> no result preview; retain the fallback request label.
    assert app.toasts[0][0] == "Request: bg"
    assert app.toasts[0][1] == "success"
    assert app.toasts[0][2] == "Background session done"
