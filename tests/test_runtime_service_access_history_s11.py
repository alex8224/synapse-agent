"""S11 Agent Runtime Service: ACL guards for session list + history reads."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from synapse.runtime.service import (
    SESSION_LIST,
    SESSION_READ,
    AccessControlledAgentRuntimeService,
    AclAuthorizer,
    AclGrant,
    InvalidRequestError,
    ListSessionsQuery,
    PermissionDeniedError,
    Principal,
    ReadSessionHistoryQuery,
    bind_access,
)
from synapse.runtime.sessions.ref import SessionRef

SUBJECT = "subject-a"
PRINCIPAL = Principal(SUBJECT)
PROJECT = "project-a"
REF = SessionRef(project_id=PROJECT, thread_id="thread-a")


def run(coro):
    return asyncio.run(coro)


class Delegate:
    """Old-shape delegate: no session list / history methods by default."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def submit_turn(self, command):
        del command
        self.calls.append("submit_turn")

    async def open_session(self, command):
        del command
        self.calls.append("open_session")

    async def cancel_turn(self, command):
        del command
        self.calls.append("cancel_turn")

    async def steer_turn(self, command):
        del command
        self.calls.append("steer_turn")

    async def resume_turn(self, command):
        del command
        self.calls.append("resume_turn")

    async def pending_approval(self, query):
        del query
        self.calls.append("pending_approval")

    async def close_session(self, command):
        del command
        self.calls.append("close_session")

    async def get_session(self, query):
        del query
        self.calls.append("get_session")

    async def stat_artifact(self, query):
        del query
        self.calls.append("stat_artifact")

    async def list_artifacts(self, query):
        del query
        self.calls.append("list_artifacts")

    async def read_artifact(self, query):
        del query
        self.calls.append("read_artifact")

    async def read_events(self, query):
        del query
        self.calls.append("read_events")

    def watch_events(self, session, **kwargs):
        del session, kwargs
        self.calls.append("watch_events")


class HistoryDelegate(Delegate):
    """Delegate that implements the new session list / history methods."""

    async def list_sessions(self, query):
        del query
        self.calls.append("list_sessions")
        return SimpleNamespace(items=(), next_offset=None, total=0)

    async def read_session_history(self, query):
        del query
        self.calls.append("read_session_history")
        return SimpleNamespace(events=(), available=True)


def _authorizer(*capabilities: str, thread_ids=None) -> AclAuthorizer:
    return AclAuthorizer(
        [AclGrant(SUBJECT, PROJECT, frozenset(capabilities), thread_ids)]
    )


def test_old_delegate_without_new_methods_still_initializes() -> None:
    wrapper = bind_access(Delegate(), PRINCIPAL, _authorizer(SESSION_LIST))
    assert isinstance(wrapper, AccessControlledAgentRuntimeService)


def test_authorize_project_requires_unscoped_grant() -> None:
    authorizer = _authorizer(SESSION_LIST, thread_ids=None)
    authorizer.authorize_project(PRINCIPAL, SESSION_LIST, PROJECT)
    with pytest.raises(PermissionDeniedError):
        authorizer.authorize_project(PRINCIPAL, SESSION_LIST, "other-project")

    scoped = _authorizer(SESSION_LIST, thread_ids=frozenset({"thread-a"}))
    with pytest.raises(PermissionDeniedError):
        scoped.authorize_project(PRINCIPAL, SESSION_LIST, PROJECT)

    missing = _authorizer(SESSION_READ)
    with pytest.raises(PermissionDeniedError):
        missing.authorize_project(PRINCIPAL, SESSION_LIST, PROJECT)

    with pytest.raises(ValueError):
        authorizer.authorize_project(PRINCIPAL, "not-a-capability", PROJECT)


def test_wrapper_list_sessions_allows_project_grant_only() -> None:
    allowed = bind_access(HistoryDelegate(), PRINCIPAL, _authorizer(SESSION_LIST))
    page = run(allowed.list_sessions(ListSessionsQuery(PROJECT, limit=1)))
    assert page.total == 0

    scoped = bind_access(
        HistoryDelegate(), PRINCIPAL, _authorizer(SESSION_LIST, thread_ids=frozenset({"x"}))
    )
    with pytest.raises(PermissionDeniedError):
        run(scoped.list_sessions(ListSessionsQuery(PROJECT)))

    denied = bind_access(HistoryDelegate(), PRINCIPAL, _authorizer(SESSION_READ))
    with pytest.raises(PermissionDeniedError):
        run(denied.list_sessions(ListSessionsQuery(PROJECT)))


def test_wrapper_history_uses_session_read_capability() -> None:
    delegate = HistoryDelegate()
    allowed = bind_access(
        delegate,
        PRINCIPAL,
        _authorizer(SESSION_READ, thread_ids=frozenset({REF.thread_id})),
    )
    page = run(allowed.read_session_history(ReadSessionHistoryQuery(REF)))
    assert page.available is True
    assert delegate.calls == ["read_session_history"]

    other = SessionRef(project_id=PROJECT, thread_id="thread-b")
    denied = bind_access(
        HistoryDelegate(),
        PRINCIPAL,
        _authorizer(SESSION_READ, thread_ids=frozenset({REF.thread_id})),
    )
    with pytest.raises(PermissionDeniedError):
        run(denied.read_session_history(ReadSessionHistoryQuery(other)))


def test_wrapper_new_methods_unavailable_raise_invalid_request() -> None:
    old = bind_access(Delegate(), PRINCIPAL, _authorizer(SESSION_LIST))
    with pytest.raises(InvalidRequestError):
        run(old.list_sessions(ListSessionsQuery(PROJECT)))

    read_grant = bind_access(
        Delegate(), PRINCIPAL, _authorizer(SESSION_READ, thread_ids=frozenset({REF.thread_id}))
    )
    with pytest.raises(InvalidRequestError):
        run(read_grant.read_session_history(ReadSessionHistoryQuery(REF)))


def test_wrapper_list_sessions_rejects_wrong_dto_shape() -> None:
    wrapper = bind_access(HistoryDelegate(), PRINCIPAL, _authorizer(SESSION_LIST))
    with pytest.raises(InvalidRequestError):
        run(wrapper.list_sessions(ReadSessionHistoryQuery(REF)))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ListSessionsQuery(project_id="")
