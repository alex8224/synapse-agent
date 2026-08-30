import asyncio
import threading
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from synapse.runtime.service import CloseSessionCommand, SessionView, UsageView
from synapse.runtime.sessions.ref import SessionRef
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
