import asyncio
import threading
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from synapse.runtime.service import (
    CloseSessionCommand,
    LocalAgentRuntimeService,
    SessionView,
    UsageView,
)
from synapse.runtime.sessions import RuntimeManager, SessionRuntime
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.streaming import EVENT_VERSION, TextPayload, TurnEvent, TurnEventKind
from synapse.ui.turn.service_session import (
    TUIRuntimeSessionFacade,
    TUISessionBinding,
)

REF = SessionRef("project", "thread")


def test_binding_is_frozen_slotted_and_has_no_runtime_handles():
    binding = TUISessionBinding(REF, object())
    with pytest.raises(FrozenInstanceError):
        binding.session = REF
    assert not hasattr(binding, "task")
    assert not hasattr(binding, "future")


def test_state_view_cache_is_pure_data():
    from synapse.ui.turn.service_session import TUIRuntimeSessionFacade

    facade = TUIRuntimeSessionFacade(binding=TUISessionBinding(REF, object()))
    facade.state.view = SessionView("p", "t", "idle", None, 8, UsageView(), None, "now")
    facade.state.last_sequence = 8
    assert facade.state.view.latest_sequence == 8
    assert facade.state.last_sequence == 8


@pytest.mark.parametrize(
    "name",
    [
        "ensure_open", "get", "watch", "submit", "observe", "cancel", "steer",
        "pending_approval", "resume", "close",
    ],
)
def test_facade_exposes_expected_operations(name):
    assert callable(getattr(TUIRuntimeSessionFacade, name))


@pytest.mark.parametrize("field", ["session", "service", "owner", "agent_metadata", "settings"])
def test_binding_declares_only_allowed_fields(field):
    assert field in TUISessionBinding.__dataclass_fields__


def test_binding_has_exactly_five_fields():
    assert tuple(TUISessionBinding.__dataclass_fields__) == (
        "session", "service", "owner", "agent_metadata", "settings"
    )


def test_binding_metadata_defaults_are_mappings():
    binding = TUISessionBinding(REF, object())
    assert binding.agent_metadata == {}
    assert binding.settings == {}


def test_session_ref_is_frozen_and_slotted():
    assert SessionRef.__dataclass_params__.frozen
    assert not hasattr(REF, "__dict__")


def test_session_view_has_no_runtime_handle_fields():
    fields = SessionView.__dataclass_fields__
    assert "task" not in fields
    assert "future" not in fields


def test_cancel_starting_session_closes_to_revoke_reservation():
    calls = []

    class Service:
        async def close_session(self, command):
            calls.append(command)
            return SimpleNamespace(closed=True, cancellation_requested=False)

    facade = TUIRuntimeSessionFacade(binding=TUISessionBinding(REF, Service()))
    facade.state.view = SessionView("project", "thread", "starting", None, 0, UsageView(), None, "")

    assert asyncio.run(facade.cancel("user")) is True
    assert len(calls) == 1
    assert isinstance(calls[0], CloseSessionCommand)
    assert calls[0].cancel_active is True
    assert facade.state.view is None


def test_submit_with_pre_set_cancel_event_closes_without_submitting():
    calls = []

    class Service:
        async def open_session(self, command):
            calls.append(("open", command))
            return SimpleNamespace(
                view=SessionView("project", "thread", "idle", None, 0, UsageView(), None, "")
            )

        async def submit_turn(self, command):
            calls.append(("submit", command))
            raise AssertionError("submit_turn should not be called after cancellation")

        async def close_session(self, command):
            calls.append(("close", command))
            return SimpleNamespace(closed=True, cancellation_requested=False)

    cancel_event = threading.Event()
    cancel_event.set()
    facade = TUIRuntimeSessionFacade(binding=TUISessionBinding(REF, Service()))

    result = asyncio.run(facade.submit("hello", cancel_event=cancel_event))

    assert result.status == "cancelled"
    assert [name for name, _command in calls] == ["open", "close"]
    assert isinstance(calls[-1][1], CloseSessionCommand)
    assert calls[-1][1].cancel_active is True


def test_watch_widens_the_overflow_kill_threshold() -> None:
    """The TUI asks for the service's largest watch bound instead of the default.

    ``LocalEventWatch`` kills the whole subscription (dropping every accepted
    event) once ``queue_size`` matching events are unconsumed, so the default
    128 only tolerates a ~0.1-0.4s consumer-loop stall.
    """
    from synapse.runtime.service.local import _MAX_QUEUE_SIZE
    from synapse.ui.turn.service_session import _TUI_EVENT_QUEUE_SIZE

    calls: list[tuple[object, int, int]] = []

    class Service:
        def watch_events(self, session, *, after=0, queue_size=0):
            calls.append((session, after, queue_size))
            return object()

    facade = TUIRuntimeSessionFacade(TUISessionBinding(REF, Service()))
    facade.state.last_sequence = 42

    facade.watch()

    assert calls == [(REF, 42, _TUI_EVENT_QUEUE_SIZE)]
    assert _TUI_EVENT_QUEUE_SIZE == _MAX_QUEUE_SIZE


def _view(latest_sequence: int) -> SessionView:
    return SessionView("project", "thread", "idle", None, latest_sequence, UsageView(), None, "")


def test_adopt_view_resets_the_cursor_when_the_stream_was_rebuilt() -> None:
    """A lower ``latest_sequence`` proves a new broker generation."""
    facade = TUIRuntimeSessionFacade(TUISessionBinding(REF, object()))

    facade.adopt_view(_view(7))
    facade.adopt_view(_view(9))
    assert facade.state.last_sequence == 9

    facade.adopt_view(_view(0))
    assert facade.state.last_sequence == 0
    assert facade.state.view.latest_sequence == 0


def test_reopened_session_watches_from_the_new_stream_tip() -> None:
    """A rebuilt stream must not be resumed with the previous stream's cursor."""
    cursors: list[int] = []

    class Service:
        def __init__(self) -> None:
            self.latest = 5

        async def open_session(self, command):
            del command
            return SimpleNamespace(view=_view(self.latest))

        def watch_events(self, session, *, after=0, queue_size=0):
            del session, queue_size
            cursors.append(after)
            return object()

    service = Service()
    facade = TUIRuntimeSessionFacade(TUISessionBinding(REF, service))

    asyncio.run(facade.ensure_open())
    facade.watch()
    service.latest = 0  # the runtime was closed and reopened: fresh broker
    asyncio.run(facade.ensure_open())
    facade.watch()

    assert cursors == [5, 0]


def test_close_releases_the_cursor_only_when_the_runtime_is_gone() -> None:
    results = iter(
        [
            SimpleNamespace(closed=True, cancellation_requested=False),
            SimpleNamespace(closed=False, cancellation_requested=False),
        ]
    )

    class Service:
        async def close_session(self, command):
            del command
            return next(results)

    facade = TUIRuntimeSessionFacade(TUISessionBinding(REF, Service()))

    facade.state.last_sequence = 9
    asyncio.run(facade.close(cancel_active=True))
    assert facade.state.last_sequence == 0

    facade.state.last_sequence = 4
    asyncio.run(facade.close(cancel_active=True))
    assert facade.state.last_sequence == 4


def _live_service() -> tuple[RuntimeManager, LocalAgentRuntimeService]:
    project_id = REF.project_id

    def session_factory(*, thread_id: str, agent: object, settings: object) -> SessionRuntime:
        return SessionRuntime(
            thread_id=thread_id,
            project_id=project_id,
            agent=agent,
            settings=settings,
        )

    manager = RuntimeManager(
        settings=SimpleNamespace(max_concurrency=2, model="test"),
        agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
        session_factory=session_factory,
        max_concurrent_sessions=4,
        project_id=project_id,
    )
    service = LocalAgentRuntimeService(
        lambda requested: manager if requested == project_id else None
    )
    return manager, service


def test_close_then_reopen_never_watches_with_a_stale_cursor() -> None:
    """Regression for ``errors-<pid>.log`` ``tui.submit`` InvalidCursorError.

    Esc-cancelling closes the session runtime while the facade survives, so the
    next prompt used to resume a cursor of the destroyed stream: the service
    rejected it as out of range and the turn was never submitted.
    """
    manager, service = _live_service()
    facade = TUIRuntimeSessionFacade(TUISessionBinding(REF, service))

    async def run() -> None:
        await facade.ensure_open()
        session = manager.get_session(REF.thread_id)
        assert session is not None
        session.broker.emit(
            TurnEvent(
                version=EVENT_VERSION,
                thread_id=REF.thread_id,
                turn_id="turn-1",
                sequence=1,
                kind=TurnEventKind.ANSWER_DELTA,
                payload=TextPayload("hi"),
            )
        )
        await facade.get()
        assert facade.state.last_sequence == 1

        await facade.close(cancel_active=True)
        await facade.ensure_open()
        assert facade.state.last_sequence == 0
        async with facade.watch():
            pass

        await manager.shutdown()

    asyncio.run(run())
