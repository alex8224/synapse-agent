"""Reasoning-level write port: registration, decode, dispatch and authorization.

Covers `runtime.session.thinking.set` (the session-scoped reasoning-level write
the console bottom bar consumes) and the ACL capability split that keeps a
read-only grant from mutating a session's reasoning level.
"""

from __future__ import annotations

import asyncio

import pytest

from synapse.runtime.service import (
    SESSION_READ,
    SESSION_THINKING,
    AclAuthorizer,
    AclGrant,
    Principal,
    RuntimeConfigView,
    SetThinkingLevelCommand,
    SetThinkingLevelResult,
    bind_access,
)
from synapse.runtime.service.access import _REQUIRED_DELEGATE_METHODS
from synapse.runtime.service.errors import PermissionDeniedError
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport.protocol import (
    METHODS,
    ProtocolError,
    decode_params,
    dispatch,
)

SESSION = {"project_id": "p", "thread_id": "t"}
REF = SessionRef("p", "t")
METHOD = "runtime.session.thinking.set"


def _view() -> RuntimeConfigView:
    return RuntimeConfigView(
        current_model="m",
        available_models=("m",),
        thinking_level="high",
        thinking_levels=("low", "high"),
        mcp_servers=(),
        mcp_enabled=False,
        can_set_thinking=True,
    )


class _FakeService:
    def __init__(self) -> None:
        self.seen: object = None

    async def set_thinking_level(self, command: object) -> object:
        self.seen = command
        return SetThinkingLevelResult(
            command.command_id, command.session, "high", _view()  # type: ignore[attr-defined]
        )


def test_thinking_method_is_registered_and_decodes_strictly() -> None:
    assert METHOD in METHODS
    decoded = decode_params(METHOD, {"session": SESSION, "level": "high"})
    assert isinstance(decoded, SetThinkingLevelCommand)
    assert (decoded.session.project_id, decoded.session.thread_id) == ("p", "t")
    assert decoded.level == "high"
    assert decoded.command_id

    with pytest.raises(ProtocolError):
        decode_params(METHOD, {"session": SESSION, "level": "high", "extra": 1})
    with pytest.raises(ProtocolError):
        decode_params(METHOD, {"session": SESSION})
    with pytest.raises(ProtocolError):
        decode_params(METHOD, {"session": SESSION, "level": ""})
    with pytest.raises(ProtocolError):
        decode_params(METHOD, {"session": SESSION, "level": 3})


def test_thinking_dispatch_routes_to_the_service() -> None:
    service = _FakeService()
    result = asyncio.run(dispatch(service, METHOD, {"session": SESSION, "level": "high"}))
    assert isinstance(service.seen, SetThinkingLevelCommand)
    assert isinstance(result, SetThinkingLevelResult)
    assert result.level == "high"


def test_thinking_write_is_not_authorized_by_session_read() -> None:
    """A read-only grant must not be able to change a reasoning level."""
    authorizer = AclAuthorizer(
        [AclGrant("subject", "p", frozenset({SESSION_READ}))]
    )
    principal = Principal("subject")
    authorizer.authorize(principal, SESSION_READ, REF)  # reads stay allowed
    with pytest.raises(PermissionDeniedError):
        authorizer.authorize(principal, SESSION_THINKING, REF)


class _StubDelegate:
    """Minimal delegate satisfying the wrapper's required method set."""

    def __init__(self) -> None:
        self.commands: list[object] = []

        async def _noop(*args: object, **kwargs: object) -> None:
            return None

        for name in _REQUIRED_DELEGATE_METHODS:
            setattr(self, name, _noop)

        async def set_thinking_level(command: SetThinkingLevelCommand) -> object:
            self.commands.append(command)
            return SetThinkingLevelResult(command.command_id, command.session, "high", _view())

        self.set_thinking_level = set_thinking_level  # type: ignore[method-assign]


def _wrapper(*capabilities: str) -> object:
    delegate = _StubDelegate()
    grant = AclGrant("subject", "p", frozenset(capabilities))
    return bind_access(delegate, Principal("subject"), AclAuthorizer([grant]))


def test_wrapper_requires_the_write_capability_and_delegates() -> None:
    command = SetThinkingLevelCommand(REF, "high")

    allowed = _wrapper(SESSION_THINKING)
    result = asyncio.run(allowed.set_thinking_level(command))  # type: ignore[attr-defined]
    assert isinstance(result, SetThinkingLevelResult)
    assert result.command_id == command.command_id

    denied = _wrapper(SESSION_READ)
    with pytest.raises(PermissionDeniedError):
        asyncio.run(denied.set_thinking_level(command))  # type: ignore[attr-defined]


def test_wrapper_reports_the_feature_unavailable_on_an_old_delegate() -> None:
    delegate = _StubDelegate()
    del delegate.set_thinking_level
    grant = AclGrant("subject", "p", frozenset({SESSION_THINKING}))
    wrapper = bind_access(delegate, Principal("subject"), AclAuthorizer([grant]))
    with pytest.raises(Exception) as exc:
        asyncio.run(wrapper.set_thinking_level(SetThinkingLevelCommand(REF, "high")))
    assert getattr(exc.value, "code", None) == "invalid_request"
