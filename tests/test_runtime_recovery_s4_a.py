"""Phase-4 slice A offline tests: read-only history/live recovery snapshot.

The durable transcript projection and the in-memory event broker are two
independent stores with no shared sequence watermark; the only durable
identity both understand is ``turn_id``.  These tests drive the real service
(``LocalAgentRuntimeService``) over a ``RuntimeManager`` with a controlled
offline turn runtime (no model/network), a temporary SQLite transcript, and
broker events injected exactly the way a live running turn would emit them.
They exercise the read-only ``reconcile_session`` recovery snapshot across the
five races the slice addresses:

1. attaching while an active turn already produced broker output;
2. the persist-vs-reconnect race (settlement lands between reads);
3. duplicate replay (events whose turn is already durable);
4. broker-restart cursor collisions (new broker, same sequence space);
5. window/prefix gaps (retention eviction inside the newest turn).

The snapshot is *never* claimed to be cross-store atomic: tests assert the
explicit, observable semantics (durable coverage + live epoch/bounds) that a
recovery client must use, and that data insufficiency surfaces as explicit
signals (epoch change, ``latest_turn_intact=False``, covered probes) instead
of a silent ``after=0`` fake restore.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.runtime.agent_loop import TurnHandle, TurnResult, TurnStatus
from synapse.runtime.service import (
    SESSION_READ,
    AclAuthorizer,
    AclGrant,
    LocalAgentRuntimeService,
    Principal,
    ReconcileSessionQuery,
    bind_access,
)
from synapse.runtime.service.commands import (
    CloseSessionCommand,
    OpenSessionCommand,
    SubmitTurnCommand,
)
from synapse.runtime.service.errors import (
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
)
from synapse.runtime.sessions import RuntimeManager, SessionRuntime
from synapse.runtime.sessions.events import SessionEventBroker
from synapse.runtime.sessions.persistence import RuntimeProjectPersistence
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.streaming import EVENT_VERSION, TextPayload, TurnEvent, TurnEventKind


def run(coro):
    return asyncio.run(coro)


def _ev(sequence: int, turn_id: str, text: str = "delta") -> TurnEvent:
    return TurnEvent(
        version=EVENT_VERSION,
        thread_id="t1",
        turn_id=turn_id,
        sequence=sequence,
        kind=TurnEventKind.ANSWER_DELTA,
        payload=TextPayload(text),
    )


class _Turn:
    def __init__(self, turn_id: str, future: concurrent.futures.Future[TurnResult]) -> None:
        self.turn_id = turn_id
        self.future = future
        self.token: Any = None


class _ControlledTurnRuntime:
    """Turn runtime whose settlement is released by the test (offline)."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.turns: list[_Turn] = []

    def submit(self, context: Any, *, sink: Any, cancel_token: Any) -> TurnHandle:
        future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()
        turn = _Turn(context.turn_id, future)
        turn.token = cancel_token
        self.turns.append(turn)
        return TurnHandle(context.turn_id, future, cancel_token)

    def complete(self, status: TurnStatus = TurnStatus.COMPLETED) -> _Turn:
        turn = self.turns[-1]
        turn.future.set_result(
            TurnResult(
                turn_id=turn.turn_id,
                thread_id=self.thread_id,
                status=status,
                state={"messages": []},
                final_text="done" if status is TurnStatus.COMPLETED else "",
                input_tokens=1,
                output_tokens=1,
            )
        )
        return turn


class _SessionFactory:
    def __init__(self, broker_factory: Any | None = None) -> None:
        self.runtimes: dict[str, _ControlledTurnRuntime] = {}
        self.sessions: dict[str, SessionRuntime] = {}
        self._broker_factory = broker_factory

    def __call__(
        self, *, thread_id: str, agent: Any, settings: Any, **kwargs: Any
    ) -> SessionRuntime:
        runtime = _ControlledTurnRuntime(thread_id)
        self.runtimes[thread_id] = runtime
        broker = (
            self._broker_factory(thread_id) if self._broker_factory is not None else None
        )
        session = SessionRuntime(
            thread_id=thread_id,
            project_id="p1",
            agent=agent,
            settings=settings,
            broker=broker,
            turn_runtime=runtime,  # type: ignore[arg-type]
            persist_result=kwargs.get("persist_result"),
        )
        self.sessions[thread_id] = session
        return session


def _settings(tmp_path: Path, *, backend: str = "sqlite") -> SimpleNamespace:
    return SimpleNamespace(
        workspace=str(tmp_path),
        checkpoint_backend=backend,
        model="test-model",
        active_model=None,
        thinking=None,
        resolved_sessions_path=lambda: tmp_path / "sessions.sqlite",
        session_summary_mode="local",
        session_summary_max_chars=600,
        project_catalog_enabled=False,
    )


def _manager(
    settings: Any,
    factory: _SessionFactory,
    persistence: RuntimeProjectPersistence | None,
):
    return RuntimeManager(
        settings=settings,
        project_id="p1",
        agent_factory=lambda thread_id, _shared: SimpleNamespace(thread_id=thread_id),
        session_factory=factory,  # type: ignore[arg-type]
        max_concurrent_sessions=4,
        persist_result=persistence.persist_result if persistence is not None else None,
        persist_resources=persistence,
    )


def _service(*managers: RuntimeManager) -> LocalAgentRuntimeService:
    by_project = {m.project_id: m for m in managers}
    return LocalAgentRuntimeService(lambda project_id: by_project.get(project_id))


async def _settle(
    factory: _SessionFactory,
    thread_id: str,
    status: TurnStatus = TurnStatus.COMPLETED,
) -> None:
    runtime = factory.runtimes[thread_id]
    turn = runtime.complete(status)
    session = factory.sessions[thread_id]
    await session.wait_for_settlement(
        TurnHandle(turn.turn_id, turn.future, turn.token)
    )


# -- 1. active turn already produced output before a late consumer attaches ---


def test_active_turn_output_is_live_not_durable_until_settlement(tmp_path: Path) -> None:
    async def body() -> None:
        settings = _settings(tmp_path)
        binder = RuntimeProjectPersistence(settings)
        factory = _SessionFactory()
        manager = _manager(settings, factory, binder)
        service = _service(manager)
        ref = SessionRef("p1", "t1")
        await service.open_session(OpenSessionCommand(ref))
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        session = factory.sessions["t1"]
        turn_id = receipt.turn_id
        # The active turn has already streamed output into the broker while the
        # transcript projection has not seen any settlement yet.
        session.broker.emit(_ev(1, turn_id, "one"))
        session.broker.emit(_ev(2, turn_id, "two"))

        view = await service.reconcile_session(
            ReconcileSessionQuery(session=ref, probe_turn_ids=(turn_id,))
        )
        assert view.history_available is False
        assert view.history_total_turns == 0
        assert view.active_turn_id == turn_id
        assert view.latest_turn_id == turn_id
        assert view.latest_turn_first_sequence == 1
        assert view.latest_turn_retained_from == 1
        assert view.latest_turn_intact is True
        assert view.live_latest_sequence == 2
        assert len(view.probe) == 1
        assert view.probe[0].turn_id == turn_id
        assert view.probe[0].covered is False, "unsettled turn must not be durable"

        # Settle -> the binder persists the turn; coverage flips to durable.
        await _settle(factory, "t1")
        settled = await service.reconcile_session(
            ReconcileSessionQuery(session=ref, probe_turn_ids=(turn_id,))
        )
        assert settled.history_available is True
        assert settled.history_total_turns == 1
        assert settled.probe[0].covered is True
        assert settled.active_turn_id is None
        assert settled.live_epoch == view.live_epoch
        await manager.shutdown()

    run(body())


# -- 2. persist-vs-reconnect race: coverage grows while a consumer is away -----


def test_reconcile_reports_persist_race_as_covered_probe(tmp_path: Path) -> None:
    async def body() -> None:
        settings = _settings(tmp_path)
        binder = RuntimeProjectPersistence(settings)
        factory = _SessionFactory()
        manager = _manager(settings, factory, binder)
        service = _service(manager)
        ref = SessionRef("p1", "t1")
        await service.open_session(OpenSessionCommand(ref))
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hello"))
        session = factory.sessions["t1"]
        turn_id = receipt.turn_id
        session.broker.emit(_ev(1, turn_id, "partial"))

        # A consumer that was away reconnects; the turn settled *and* persisted
        # while it was gone (the persist/reconnect competition: the client only
        # learns the durable side on its next reconcile).
        await _settle(factory, "t1")
        view = await service.reconcile_session(
            ReconcileSessionQuery(session=ref, probe_turn_ids=(turn_id,))
        )
        assert view.probe[0].covered is True
        assert view.history_total_turns == 1
        assert view.live_epoch is not None
        await manager.shutdown()

    run(body())


# -- 3. duplicate replay: live events whose turn is already durable ------------


def test_duplicate_replay_of_covered_turn_is_detectable(tmp_path: Path) -> None:
    async def body() -> None:
        settings = _settings(tmp_path)
        binder = RuntimeProjectPersistence(settings)
        factory = _SessionFactory()
        manager = _manager(settings, factory, binder)
        service = _service(manager)
        ref = SessionRef("p1", "t1")
        await service.open_session(OpenSessionCommand(ref))
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="hi"))
        await _settle(factory, "t1")
        turn_id = receipt.turn_id
        view = await service.reconcile_session(
            ReconcileSessionQuery(session=ref, probe_turn_ids=(turn_id,))
        )
        assert view.history_total_turns == 1 and view.probe[0].covered is True

        # A duplicate settlement of the identical durable turn id is deduped by
        # the B-slice projection; coverage stays at one turn.
        context = SimpleNamespace(
            thread_id="t1",
            settings=settings,
            request=SimpleNamespace(resume=False, input="hi"),
        )
        result = TurnResult(
            turn_id=turn_id,
            thread_id="t1",
            status=TurnStatus.COMPLETED,
            state={"messages": []},
            final_text="hi",
            input_tokens=1,
            output_tokens=1,
        )
        binder.persist_result(context, result)
        after = await service.reconcile_session(
            ReconcileSessionQuery(session=ref, probe_turn_ids=(turn_id,))
        )
        assert after.history_total_turns == 1 and after.probe[0].covered is True
        await manager.shutdown()

    run(body())


# -- 4. broker-restart cursor collision: only the epoch makes it detectable ----


def test_reopened_session_epoch_guards_against_cursor_collision(tmp_path: Path) -> None:
    async def body() -> None:
        settings = _settings(tmp_path)
        binder = RuntimeProjectPersistence(settings)
        factory = _SessionFactory()
        manager = _manager(settings, factory, binder)
        service = _service(manager)
        ref = SessionRef("p1", "t1")
        await service.open_session(OpenSessionCommand(ref))
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="one"))
        first = factory.sessions["t1"]
        first.broker.emit(_ev(1, receipt.turn_id, "a"))
        first.broker.emit(_ev(2, receipt.turn_id, "b"))
        await _settle(factory, "t1")
        before = await service.reconcile_session(
            ReconcileSessionQuery(session=ref, probe_turn_ids=(receipt.turn_id,))
        )
        assert before.live_latest_sequence == 2
        assert before.history_total_turns == 1 and before.probe[0].covered is True

        # Close and reopen: a fresh SessionRuntime owns a fresh broker whose
        # sequence space restarts at 0 (a daemon restart is the same shape).
        await service.close_session(CloseSessionCommand(session=ref))
        await service.open_session(OpenSessionCommand(ref))
        second = factory.sessions["t1"]
        assert second.broker is not first.broker
        # Two brand-new events occupy the same sequence values (1,2) as before.
        second.broker.emit(_ev(1, "next-turn", "x"))
        second.broker.emit(_ev(2, "next-turn", "y"))

        after = await service.reconcile_session(
            ReconcileSessionQuery(session=ref, probe_turn_ids=(receipt.turn_id,))
        )
        assert after.live_epoch != before.live_epoch, "stream identity must change"
        assert after.live_latest_sequence == 2

        # A naive resume with the *stored old cursor* (= 2) is silently valid on
        # the new broker (cursor <= latest, no eviction watermark) but skips
        # both new events: the collision is only detectable through the epoch.
        window = second.broker.read_after(2)
        assert window.gap is False
        assert window.events == ()
        assert after.live_epoch is not None
        # History coverage survived the restart (durable store).
        assert after.history_total_turns == 1 and after.probe[0].covered is True
        await manager.shutdown()

    run(body())


# -- 5. window/prefix gap: retention eviction inside the newest turn -----------


def test_active_turn_prefix_eviction_is_explicit_not_silent(tmp_path: Path) -> None:
    def tiny_broker(thread_id: str) -> SessionEventBroker:
        return SessionEventBroker(thread_id, max_events=2, hard_cap=2)

    async def body() -> None:
        settings = _settings(tmp_path)
        binder = RuntimeProjectPersistence(settings)
        factory = _SessionFactory(broker_factory=tiny_broker)
        manager = _manager(settings, factory, binder)
        service = _service(manager)
        ref = SessionRef("p1", "t1")
        await service.open_session(OpenSessionCommand(ref))
        receipt = await service.submit_turn(SubmitTurnCommand(session=ref, text="long"))
        session = factory.sessions["t1"]
        turn_id = receipt.turn_id
        # Emit more preview deltas than the (minimum 16) retention window, so
        # the broker must evict the oldest previews of the running turn.
        for sequence in range(1, 25):
            session.broker.emit(_ev(sequence, turn_id, f"d{sequence}"))

        view = await service.reconcile_session(
            ReconcileSessionQuery(session=ref, probe_turn_ids=(turn_id,))
        )
        assert view.active_turn_id == turn_id
        assert view.latest_turn_id == turn_id
        assert view.latest_turn_first_sequence == 1
        assert view.latest_turn_intact is False, "prefix eviction must be observable"
        # The retained replay cannot start at the true turn start any more.
        assert view.latest_turn_retained_from is not None
        assert view.latest_turn_retained_from > view.latest_turn_first_sequence
        assert view.live_dropped_through >= view.latest_turn_first_sequence
        # A cursor at 0 on this broker reports an explicit gap (not a fake
        # full replay): recovery must come from history once the turn settles.
        window = session.broker.read_after(0)
        assert window.gap is True
        # Settle so the manager can shut down without a pending live turn.
        await _settle(factory, "t1")
        await manager.shutdown()

    run(body())


# -- DTO validation and ACL gating ---------------------------------------------


def test_reconcile_query_probe_limits_are_strict() -> None:
    ref = SessionRef("p1", "t1")
    with pytest.raises(ValueError):
        ReconcileSessionQuery(session=ref, probe_turn_ids=("",))
    with pytest.raises(ValueError):
        ReconcileSessionQuery(session=ref, probe_turn_ids=("x" * 300,))
    with pytest.raises(ValueError):
        ReconcileSessionQuery(
            session=ref, probe_turn_ids=tuple(str(i) for i in range(40))
        )
    # Duplicates collapse and oversized probe sets shrink below the cap.
    dedup = ReconcileSessionQuery(session=ref, probe_turn_ids=("a", "a", "b"))
    assert dedup.probe_turn_ids == ("a", "b")
    ok = ReconcileSessionQuery(session=ref)
    assert ok.probe_turn_ids == ()
    with pytest.raises(ValueError):
        ReconcileSessionQuery(session=object(), probe_turn_ids=())  # type: ignore[arg-type]


def test_reconcile_read_only_requires_open_session(tmp_path: Path) -> None:
    async def body() -> None:
        settings = _settings(tmp_path)
        binder = RuntimeProjectPersistence(settings)
        factory = _SessionFactory()
        manager = _manager(settings, factory, binder)
        service = _service(manager)
        ref = SessionRef("p1", "t1")
        with pytest.raises(NotFoundError):
            await service.reconcile_session(ReconcileSessionQuery(session=ref))
        # Closed/never-open session surfaces not_found, mirroring get_session:
        # reconcile never implicitly opens or creates anything.
        await manager.shutdown()

    run(body())


class _LegacyDelegate:
    """An older delegate that predates ``reconcile_session``."""

    async def submit_turn(self, command):  # pragma: no cover - stub
        raise NotImplementedError

    async def resume_turn(self, command):  # pragma: no cover - stub
        raise NotImplementedError

    async def open_session(self, command):  # pragma: no cover - stub
        raise NotImplementedError

    async def rebind_session(self, command):  # pragma: no cover - stub
        raise NotImplementedError

    async def cancel_turn(self, command):  # pragma: no cover - stub
        raise NotImplementedError

    async def steer_turn(self, command):  # pragma: no cover - stub
        raise NotImplementedError

    async def pending_approval(self, query):  # pragma: no cover - stub
        raise NotImplementedError

    async def close_session(self, command):  # pragma: no cover - stub
        raise NotImplementedError

    async def get_session(self, query):  # pragma: no cover - stub
        raise NotImplementedError

    async def stat_artifact(self, query):  # pragma: no cover - stub
        raise NotImplementedError

    async def list_artifacts(self, query):  # pragma: no cover - stub
        raise NotImplementedError

    async def read_artifact(self, query):  # pragma: no cover - stub
        raise NotImplementedError

    async def read_events(self, query):  # pragma: no cover - stub
        raise NotImplementedError

    def watch_events(self, session, *, after=0, queue_size=128, **kwargs):  # pragma: no cover
        raise NotImplementedError


def test_reconcile_acl_read_first_and_old_delegate_unavailable(tmp_path: Path) -> None:
    async def body() -> None:
        settings = _settings(tmp_path)
        binder = RuntimeProjectPersistence(settings)
        factory = _SessionFactory()
        manager = _manager(settings, factory, binder)
        service = _service(manager)
        ref = SessionRef("p1", "t1")
        await service.open_session(OpenSessionCommand(ref))
        await service.submit_turn(SubmitTurnCommand(session=ref, text="x"))
        await _settle(factory, "t1")

        # Denied without session.read (delegate has the method, but ACL runs
        # first and denies before any delegate code could run).
        principal = Principal("alice")
        denied_grants = AclAuthorizer([])
        denied = bind_access(service, principal, denied_grants)
        with pytest.raises(PermissionDeniedError):
            await denied.reconcile_session(ReconcileSessionQuery(session=ref))

        # Allowed with session.read on the exact thread.
        grants = AclAuthorizer(
            [
                AclGrant(
                    subject="alice",
                    project_id="p1",
                    capabilities=frozenset({SESSION_READ}),
                    thread_ids=frozenset({"t1"}),
                )
            ]
        )
        allowed = bind_access(service, principal, grants)
        view = await allowed.reconcile_session(
            ReconcileSessionQuery(session=ref, probe_turn_ids=())
        )
        assert view.history_available is True and view.history_total_turns == 1

        # Old delegate without the method: predictable unavailable error.
        legacy = bind_access(_LegacyDelegate(), principal, grants)
        with pytest.raises(InvalidRequestError) as excinfo:
            await legacy.reconcile_session(ReconcileSessionQuery(session=ref))
        assert "session recovery is unavailable" in str(excinfo.value)
        await manager.shutdown()

    run(body())


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
