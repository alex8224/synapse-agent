"""Daemon wiring for the collaboration mailbox: additive, and never fatal."""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from synapse.runtime.collaboration.model import CollaborationError
from synapse.runtime.daemon.application import RuntimeDaemon
from synapse.runtime.daemon.config import DaemonConfig
from synapse.runtime.sessions import RuntimeManager
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.sessions.runtime import UserTurn

A = SessionRef('p1', 'a')
B = SessionRef('p1', 'b')
OTHER = SessionRef('p2', 'c')


class _Router:
    """Minimal stand-in: resolves the registered projects and shuts down cleanly."""

    def __init__(self, known: tuple[str, ...] = ()) -> None:
        self.closed = False
        self._known = known

    def __call__(self, project_id: str):
        return object() if project_id in self._known else None

    async def shutdown(self) -> None:
        self.closed = True


class _Catalog:
    """Minimal stand-in for the project catalog: only the exact-id lookup."""

    def __init__(self, known: tuple[str, ...] = ()) -> None:
        self._known = known

    def get_project(self, *, project_id: str):
        return SimpleNamespace(project_id=project_id) if project_id in self._known else None


def test_collaboration_starts_and_closes_with_the_daemon(tmp_path):
    async def scenario() -> None:
        daemon = RuntimeDaemon(DaemonConfig(state_dir=tmp_path, port=0))
        router = _Router()
        daemon.router = router
        try:
            await daemon._start_collaboration()
            assert daemon.collaboration is not None
            store = daemon._collaboration_store
            assert store is not None
            assert (tmp_path / 'collaboration.sqlite').is_file()
            assert daemon._collaboration_runtime is not None
            # An unwired project resolves to "unavailable", never to an exception.
            assert daemon._collaboration_manager_for('missing') is None
            assert daemon._cancel_collaboration_turn(B, 'turn-1') is False
        finally:
            await daemon.shutdown()
        assert router.closed is True
        with pytest.raises(CollaborationError):
            store.claim_next(B)

    asyncio.run(scenario())


def test_unusable_state_dir_leaves_the_daemon_working(tmp_path):
    blocked = tmp_path / 'blocked'
    blocked.write_text('not a directory', encoding='utf-8')

    async def scenario() -> None:
        daemon = RuntimeDaemon(DaemonConfig(state_dir=blocked, port=0))
        await daemon._start_collaboration()
        assert daemon.collaboration is None
        assert daemon._collaboration_store is None
        assert daemon._collaboration_runtime is None

    asyncio.run(scenario())


def test_foreign_schema_is_refused_without_touching_the_file(tmp_path):
    path = tmp_path / 'collaboration.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE marker (value TEXT)')
        db.execute("INSERT INTO marker VALUES ('preserved')")
        db.execute('PRAGMA user_version=99')

    async def scenario() -> None:
        daemon = RuntimeDaemon(DaemonConfig(state_dir=tmp_path, port=0))
        await daemon._start_collaboration()
        assert daemon.collaboration is None

    asyncio.run(scenario())

    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 99
        assert db.execute('SELECT value FROM marker').fetchone()[0] == 'preserved'


def test_cross_project_policy_defaults_on_and_can_be_disabled(tmp_path):
    allowed = RuntimeDaemon(
        DaemonConfig(state_dir=tmp_path / 'a', port=0), 
    )
    allowed.router = _Router(('p1', 'p2'))
    allowed.catalog = _Catalog(('p1', 'p2'))
    assert allowed.config.allow_cross_project_collaboration is True
    assert allowed._collaboration_allowed(A, B) is True
    assert allowed._collaboration_allowed(A, OTHER) is True
    # Reachability is a separate question from policy: the policy says yes, the
    # catalog says this project is not registered.
    assert (
        allowed._collaboration_reachable(SessionRef('p9', 'x')) == 'project-not-registered'
    )
    assert allowed._collaboration_reachable(OTHER) is None

    bounded = RuntimeDaemon(
        DaemonConfig(
            state_dir=tmp_path / 'b', port=0, allow_cross_project_collaboration=False
        )
    )
    bounded.router = _Router(('p1', 'p2'))
    bounded.catalog = _Catalog(('p1', 'p2'))
    assert bounded._collaboration_allowed(A, B) is True
    assert bounded._collaboration_allowed(A, OTHER) is False
    # The policy refusal never consults the resolver.
    assert bounded._collaboration_reachable(OTHER) is None

    unready = RuntimeDaemon(DaemonConfig(state_dir=tmp_path / 'c', port=0))
    assert unready._collaboration_reachable(A) == 'runtime-not-ready'


def test_collaboration_tools_exist_only_after_the_mailbox_starts(tmp_path):
    async def scenario() -> None:
        daemon = RuntimeDaemon(DaemonConfig(state_dir=tmp_path, port=0))
        daemon.router = _Router(('p1',))
        assert daemon._collaboration_tools('p1') == []
        try:
            await daemon._start_collaboration()
            names = [getattr(tool, 'name', '') for tool in daemon._collaboration_tools('p1')]
            assert names == ['contact_session', 'reply_to_collaboration']
        finally:
            await daemon.shutdown()

    asyncio.run(scenario())


def test_user_turn_rotates_the_generation_and_a_delivery_turn_does_not(tmp_path):
    async def scenario() -> None:
        daemon = RuntimeDaemon(DaemonConfig(state_dir=tmp_path, port=0))
        daemon.router = _Router(('p1',))
        try:
            await daemon._start_collaboration()
            store = daemon._collaboration_store
            daemon._collaboration_turn_started(A, 'turn-1', False)
            first = store.active_scope(A)
            assert first is not None and first.work_id == 'turn-1'
            # A delivery turn must never rotate the source's generation.
            daemon._collaboration_turn_started(B, 'turn-2', True)
            assert store.active_scope(B) is None
            daemon._collaboration_turn_started(A, 'turn-3', False)
            assert store.active_scope(A).work_id == 'turn-3'
            assert store.get_scope(first.id, A).state.value == 'active'
        finally:
            await daemon.shutdown()

    asyncio.run(scenario())


def test_turn_started_reporting_is_best_effort(tmp_path):
    calls: list[tuple[SessionRef, str, bool]] = []
    def record(ref: SessionRef, turn_id: str, is_collaboration: bool) -> None:
        calls.append((ref, turn_id, is_collaboration))
    manager = RuntimeManager(
        settings=None,
        agent_factory=lambda *_args, **_kwargs: None,
        project_id='p1',
        on_turn_started=record,
    )
    handle = SimpleNamespace(turn_id='turn-1')
    manager._notify_turn_started('a', handle, UserTurn(text='hi'))
    manager._notify_turn_started(
        'a',
        handle,
        UserTurn(
            text='hi',
            config_overrides={'configurable': {'collaboration': {'request_id': 'r'}}},
        ),
    )
    assert calls == [(A, 'turn-1', False), (A, 'turn-1', True)]
    manager._notify_turn_started('a', handle, UserTurn(text='', approval_resume=True,
        request=SimpleNamespace(config={'configurable': {'collaboration': {'request_id': 'r'}}}),
    ))
    assert calls[-1] == (A, 'turn-1', True)
    def boom(*_args: object) -> None:
        raise RuntimeError('bookkeeping failed')
    manager.on_turn_started = boom
    manager._notify_turn_started('a', handle, UserTurn(text='hi'))
    daemon = RuntimeDaemon(DaemonConfig(state_dir=tmp_path, port=0))
    daemon._collaboration_turn_started(A, 'turn-1', False)
