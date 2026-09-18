"""Deterministic S5 project routing and lifecycle contracts."""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import dataclasses
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.projects.catalog import ProjectCatalog
from synapse.runtime.agent_loop import CancelToken, TurnHandle, TurnResult, TurnStatus
from synapse.runtime.service import (
    ArtifactRef,
    CatalogProjectProvider,
    RouterClosedError,
    RuntimeManagerRouter,
    RuntimeProject,
)
from synapse.runtime.service.artifacts import (
    ListArtifactsQuery,
    ReadArtifactQuery,
    StatArtifactQuery,
)
from synapse.runtime.service.commands import (
    CancelTurnCommand,
    CloseSessionCommand,
    OpenSessionCommand,
    SteerTurnCommand,
    SubmitTurnCommand,
)
from synapse.runtime.service.errors import ClosedError, NotFoundError
from synapse.runtime.service.events import ReadEventsQuery
from synapse.runtime.service.local import LocalAgentRuntimeService
from synapse.runtime.service.queries import GetSessionQuery
from synapse.runtime.sessions import RuntimeManager, SessionRef, SessionRuntime, SessionStatus
from synapse.runtime.streaming import EVENT_VERSION, TextPayload, TurnEvent, TurnEventKind


def _manager(project_id: str) -> RuntimeManager:
    return RuntimeManager(
        settings=SimpleNamespace(model="test", max_concurrency=2),
        agent_factory=lambda thread_id, shared: SimpleNamespace(
            thread_id=thread_id, shared=shared
        ),
        project_id=project_id,
    )


class _CountingManager(RuntimeManager):
    def __init__(
        self,
        project_id: str | None,
        *,
        entered: threading.Event | None = None,
        release: threading.Event | None = None,
        failure: BaseException | None = None,
    ) -> None:
        super().__init__(
            settings=SimpleNamespace(model="test", max_concurrency=2),
            agent_factory=lambda thread_id, shared: SimpleNamespace(
                thread_id=thread_id, shared=shared
            ),
            project_id=project_id,
        )
        self.shutdown_calls = 0
        self.shutdown_entered = entered
        self.shutdown_release = release
        self.shutdown_failure = failure

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self.shutdown_entered is not None:
            self.shutdown_entered.set()
        if self.shutdown_release is not None:
            await asyncio.to_thread(self.shutdown_release.wait)
        if self.shutdown_failure is not None:
            raise self.shutdown_failure
        await super().shutdown()


class _ControlledTurnRuntime:
    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()
        self.started = threading.Event()
        self.submit_calls = 0

    def submit(self, context: Any, *, sink: Any, cancel_token: CancelToken) -> TurnHandle:
        del sink
        self.submit_calls += 1
        self.started.set()
        return TurnHandle(context.turn_id, self.future, cancel_token)


class _SessionFactory:
    def __init__(self, project_id: str, workspace: Path) -> None:
        self.project_id = project_id
        self.workspace = workspace
        self.turns: dict[str, _ControlledTurnRuntime] = {}

    def __call__(self, *, thread_id: str, agent: Any, settings: Any) -> SessionRuntime:
        controlled = _ControlledTurnRuntime(thread_id)
        self.turns[thread_id] = controlled
        return SessionRuntime(
            thread_id=thread_id,
            project_id=self.project_id,
            agent=agent,
            settings=settings,
            workspace=self.workspace,
            turn_runtime=controlled,  # type: ignore[arg-type]
        )


def _controlled_manager(
    project_id: str, workspace: Path, *, limit: int = 2
) -> tuple[RuntimeManager, _SessionFactory]:
    session_factory = _SessionFactory(project_id, workspace)
    manager = RuntimeManager(
        settings=SimpleNamespace(model="test", max_concurrency=2, workspace=workspace),
        agent_factory=lambda thread_id, shared: SimpleNamespace(
            thread_id=thread_id, shared=shared
        ),
        project_id=project_id,
        session_factory=session_factory,
        max_concurrent_sessions=limit,
    )
    return manager, session_factory


def _event(thread_id: str, sequence: int, text: str) -> TurnEvent:
    return TurnEvent(
        version=EVENT_VERSION,
        thread_id=thread_id,
        turn_id=f"turn-{thread_id}",
        sequence=sequence,
        kind=TurnEventKind.ANSWER_DELTA,
        payload=TextPayload(text),
    )


def test_runtime_project_is_frozen_slotted_and_json_safe() -> None:
    project = RuntimeProject("p1", "/workspace/p1")
    assert dataclasses.asdict(project) == {
        "project_id": "p1",
        "workspace": "/workspace/p1",
    }
    assert RuntimeProject.__slots__ == ("project_id", "workspace")
    with pytest.raises(dataclasses.FrozenInstanceError):
        project.workspace = "/other"  # type: ignore[misc]


def test_invalid_ids_and_descriptor_mismatch_are_unknown() -> None:
    calls: list[str] = []
    router = RuntimeManagerRouter(
        lambda project_id: RuntimeProject("other", "/workspace")
        if project_id == "p1"
        else RuntimeProject(project_id, "/workspace"),
        lambda project: calls.append(project.project_id) or _manager(project.project_id),
    )
    assert router("") is None
    assert router(1) is None  # type: ignore[arg-type]
    assert router("bad\x00id") is None
    assert router("p1") is None
    assert calls == []
    assert router("p2") is not None


def test_catalog_provider_is_exact_only(tmp_path: Path) -> None:
    catalog = ProjectCatalog(tmp_path / "catalog.sqlite")
    try:
        workspace = tmp_path / "named-project"
        info = catalog.register_project(workspace, detect_git=False)
        provider = CatalogProjectProvider(catalog)
        assert provider(info.project_id) == RuntimeProject(info.project_id, info.workspace_path)
        assert provider(info.project_id[:8]) is None
        assert provider(info.name) is None
        assert provider(str(workspace)) is None
    finally:
        catalog.close()


def test_same_project_single_flight_returns_one_manager() -> None:
    callers = 12
    start = threading.Barrier(callers)
    provider_count = 0
    factory_count = 0
    count_lock = threading.Lock()
    build_started = threading.Event()
    release_build = threading.Event()

    def provider(project_id: str) -> RuntimeProject:
        nonlocal provider_count
        with count_lock:
            provider_count += 1
        return RuntimeProject(project_id, "/workspace")

    def factory(project: RuntimeProject) -> RuntimeManager:
        nonlocal factory_count
        with count_lock:
            factory_count += 1
        build_started.set()
        assert release_build.wait(5)
        return _manager(project.project_id)

    router = RuntimeManagerRouter(provider, factory)
    results: list[RuntimeManager | None] = []
    errors: list[BaseException] = []

    def resolve() -> None:
        try:
            start.wait()
            results.append(router("p1"))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=resolve) for _ in range(callers)]
    for thread in threads:
        thread.start()
    assert build_started.wait(5)
    release_build.set()
    for thread in threads:
        thread.join(5)
    assert errors == []
    assert len(results) == callers
    assert len({id(manager) for manager in results}) == 1
    assert provider_count == factory_count == 1
    asyncio.run(router.shutdown())


def test_published_generation_keeps_original_descriptor() -> None:
    descriptors = iter(
        (
            RuntimeProject("p1", "/workspace/one"),
            RuntimeProject("p1", "/workspace/two"),
        )
    )
    provider_calls = 0
    factory_calls = 0

    def provider(project_id: str) -> RuntimeProject:
        nonlocal provider_calls
        provider_calls += 1
        return next(descriptors)

    def factory(project: RuntimeProject) -> RuntimeManager:
        nonlocal factory_calls
        factory_calls += 1
        return _manager(project.project_id)

    router = RuntimeManagerRouter(provider, factory)
    first = router("p1")
    second = router("p1")
    assert first is second
    assert provider_calls == factory_calls == 1
    asyncio.run(router.shutdown())


@pytest.mark.parametrize("kind", ["provider", "factory"])
@pytest.mark.parametrize("failure", [RuntimeError("boom"), KeyboardInterrupt()])
def test_provider_and_factory_unknown_exceptions_propagate(
    kind: str, failure: BaseException
) -> None:
    if kind == "provider":
        router = RuntimeManagerRouter(
            lambda _project_id: (_ for _ in ()).throw(failure),
            _manager,
        )
    else:
        router = RuntimeManagerRouter(
            lambda project_id: RuntimeProject(project_id, "/workspace"),
            lambda _project: (_ for _ in ()).throw(failure),
        )
    with pytest.raises(type(failure)) as exc:
        router("p1")
    assert exc.value is failure


def test_shutdown_failure_is_shared_and_other_managers_still_close() -> None:
    first_failure = RuntimeError("first manager close failed")
    managers = {
        project_id: _CountingManager(project_id, failure=first_failure)
        if project_id == "p1"
        else _CountingManager(project_id)
        for project_id in ("p1", "p2")
    }
    router = RuntimeManagerRouter(
        lambda project_id: RuntimeProject(project_id, "/workspace"),
        lambda project: managers[project.project_id],
    )
    router("p1")
    router("p2")
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def join() -> None:
        try:
            barrier.wait()
            asyncio.run(router.shutdown())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=join) for _ in range(5)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(5)
    assert len(errors) == 5
    assert all(error is first_failure for error in errors)
    assert managers["p1"].shutdown_calls == managers["p2"].shutdown_calls == 1
    with pytest.raises(RuntimeError) as repeated:
        asyncio.run(router.shutdown())
    assert repeated.value is first_failure
    assert managers["p1"].shutdown_calls == managers["p2"].shutdown_calls == 1


def test_cancelled_shutdown_joiner_does_not_cancel_internal_shutdown() -> None:
    entered = threading.Event()
    release = threading.Event()
    manager = _CountingManager("p1", entered=entered, release=release)
    router = RuntimeManagerRouter(
        lambda project_id: RuntimeProject(project_id, "/workspace"), lambda _project: manager
    )
    assert router("p1") is manager

    async def run() -> None:
        first = asyncio.create_task(router.shutdown())
        await asyncio.to_thread(entered.wait)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(router.shutdown())
        release.set()
        await second
        assert manager.shutdown_calls == 1

    asyncio.run(run())


def test_routing_import_does_not_pull_unrelated_runtime_dependencies() -> None:
    """Import the routing module from a clean graph and check its boundaries."""
    code = (
        "import sys;"
        "import synapse.runtime.service;"
        "prefixes=('synapse.projects', 'synapse.ui', 'synapse.acp', "
        "'synapse.transport', 'synapse.runtime.transport', 'deepagents');"
        "[sys.modules.pop(name) for name in list(sys.modules) if any(name == prefix or "
        "name.startswith(prefix + '.') for prefix in prefixes)];"
        "sys.modules.pop('synapse.runtime.service.routing', None);"
        "import synapse.runtime.service.routing;"
        "bad=sorted(m for m in sys.modules if m == 'synapse.projects' or "
        "m.startswith(('synapse.projects.', 'synapse.ui', 'synapse.acp', "
        "'synapse.transport', 'synapse.runtime.transport', 'deepagents')));"
        "assert not bad, bad"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=Path(__file__).parents[1],
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_cross_manager_turn_quotas_are_isolated_and_same_project_is_bounded(
    tmp_path: Path,
) -> None:
    """Each project has an independent permit while one project still queues."""
    workspaces = {project: tmp_path / project for project in ("p1", "p2")}
    for workspace in workspaces.values():
        workspace.mkdir()
    managers: dict[str, RuntimeManager] = {}
    factories: dict[str, _SessionFactory] = {}
    for project, workspace in workspaces.items():
        managers[project], factories[project] = _controlled_manager(
            project, workspace, limit=1
        )
    router = RuntimeManagerRouter(
        lambda project_id: RuntimeProject(project_id, str(workspaces[project_id])),
        lambda project: managers[project.project_id],
    )
    service = LocalAgentRuntimeService(router)

    async def run() -> None:
        p1_a = SessionRef("p1", "session-a")
        p1_b = SessionRef("p1", "session-b")
        p2_same = SessionRef("p2", "same-session")
        await service.open_session(OpenSessionCommand(p1_a))
        first_receipt = await service.submit_turn(SubmitTurnCommand(p1_a, "p1-a"))
        p1_a_runtime = factories["p1"].turns["session-a"]
        assert p1_a_runtime.started.is_set()
        assert first_receipt.accepted is True

        p1_b_task = asyncio.create_task(
            service.submit_turn(SubmitTurnCommand(p1_b, "p1-b"))
        )
        for _ in range(32):
            snapshot = managers["p1"].snapshot("session-b")
            if snapshot is not None and snapshot.status is SessionStatus.QUEUED:
                break
            await asyncio.sleep(0)
        assert managers["p1"].snapshot("session-b").status is SessionStatus.QUEUED  # type: ignore[union-attr]
        assert not p1_b_task.done()
        assert factories["p1"].turns["session-b"].submit_calls == 0

        p2_receipt = await asyncio.wait_for(
            service.submit_turn(SubmitTurnCommand(p2_same, "p2")), timeout=2
        )
        assert p2_receipt.accepted is True
        assert p2_receipt.session == p2_same
        assert factories["p2"].turns["same-session"].started.is_set()
        assert factories["p1"].turns["session-b"].submit_calls == 0

        p1_a_runtime.future.set_result(
            TurnResult(
                turn_id=first_receipt.turn_id,
                thread_id="session-a",
                status=TurnStatus.COMPLETED,
            )
        )
        p1_a_session = managers["p1"].get_session("session-a")
        assert p1_a_session is not None
        first_handle = p1_a_session.active_handle()
        assert first_handle is not None
        await p1_a_session.wait_for_settlement(first_handle)

        p1_b_receipt = await asyncio.wait_for(p1_b_task, timeout=2)
        assert p1_b_receipt.accepted is True
        assert factories["p1"].turns["session-b"].started.is_set()
        assert managers["p1"].snapshot("session-b").status is SessionStatus.RUNNING  # type: ignore[union-attr]

        sessions_and_handles = []
        for project, thread_id, receipt in (
            ("p1", "session-b", p1_b_receipt),
            ("p2", "same-session", p2_receipt),
        ):
            session = managers[project].get_session(thread_id)
            assert session is not None
            handle = session.active_handle()
            assert handle is not None and handle.turn_id == receipt.turn_id
            sessions_and_handles.append((session, handle))
        factories["p1"].turns["session-b"].future.set_result(
            TurnResult(
                turn_id=p1_b_receipt.turn_id,
                thread_id="session-b",
                status=TurnStatus.COMPLETED,
            )
        )
        factories["p2"].turns["same-session"].future.set_result(
            TurnResult(
                turn_id=p2_receipt.turn_id,
                thread_id="same-session",
                status=TurnStatus.COMPLETED,
            )
        )
        for session, handle in sessions_and_handles:
            await session.wait_for_settlement(handle)
        await router.shutdown()

    asyncio.run(run())


def test_closed_router_maps_every_service_entrypoint() -> None:
    ref = SessionRef("p1", "thread")
    service = LocalAgentRuntimeService(
        RuntimeManagerRouter(
            lambda project_id: RuntimeProject(project_id, "/workspace"),
            lambda project: _manager(project.project_id),
        )
    )
    router = service._manager_provider
    assert isinstance(router, RuntimeManagerRouter)

    async def run() -> None:
        await router.shutdown()
        calls = (
            lambda: service.open_session(OpenSessionCommand(ref)),
            lambda: service.submit_turn(SubmitTurnCommand(ref, "text")),
            lambda: service.get_session(GetSessionQuery(ref)),
            lambda: service.read_events(ReadEventsQuery(ref)),
            lambda: service.stat_artifact(StatArtifactQuery(ArtifactRef(ref, "x"))),
            lambda: service.list_artifacts(ListArtifactsQuery(ref)),
            lambda: service.read_artifact(ReadArtifactQuery(ArtifactRef(ref, "x"))),
            lambda: service.cancel_turn(CancelTurnCommand(ref, "turn")),
            lambda: service.steer_turn(SteerTurnCommand(ref, "turn", "text")),
            lambda: service.close_session(CloseSessionCommand(ref)),
        )
        for call in calls:
            with pytest.raises(ClosedError) as error:
                await call()
            assert error.value.code == "closed"
        with pytest.raises(ClosedError) as error:
            service.watch_events(ref)
        assert error.value.code == "closed"

    asyncio.run(run())


def test_wrong_manager_provider_is_rejected_before_any_manager_method() -> None:
    class SpyManager(RuntimeManager):
        def __init__(self) -> None:
            super().__init__(
                settings=SimpleNamespace(model="test", max_concurrency=2),
                agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
                project_id="right",
            )
            self.calls = 0

        def _touched(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            self.calls += 1
            raise AssertionError("wrong manager was touched")

        async def open_session_ref(self, ref: SessionRef) -> Any:
            self._touched(ref)

        async def submit_ref(self, ref: SessionRef, message: Any) -> Any:
            self._touched(ref, message)

        def get_session_ref(self, ref: SessionRef) -> Any:
            self._touched(ref)

        def cancel_turn_ref(self, *args: Any, **kwargs: Any) -> Any:
            self._touched(*args, **kwargs)

        def steer_turn_ref(self, *args: Any, **kwargs: Any) -> Any:
            self._touched(*args, **kwargs)

        async def close_session_ref(self, *args: Any, **kwargs: Any) -> Any:
            self._touched(*args, **kwargs)

    manager = SpyManager()
    service = LocalAgentRuntimeService(lambda _project_id: manager)
    ref = SessionRef("wrong", "thread")

    async def run() -> None:
        calls = (
            lambda: service.open_session(OpenSessionCommand(ref)),
            lambda: service.submit_turn(SubmitTurnCommand(ref, "text")),
            lambda: service.get_session(GetSessionQuery(ref)),
            lambda: service.read_events(ReadEventsQuery(ref)),
            lambda: service.stat_artifact(StatArtifactQuery(ArtifactRef(ref, "x"))),
            lambda: service.list_artifacts(ListArtifactsQuery(ref)),
            lambda: service.read_artifact(ReadArtifactQuery(ArtifactRef(ref, "x"))),
            lambda: service.cancel_turn(CancelTurnCommand(ref, "turn")),
            lambda: service.steer_turn(SteerTurnCommand(ref, "turn", "text")),
            lambda: service.close_session(CloseSessionCommand(ref)),
        )
        for call in calls:
            with pytest.raises(NotFoundError):
                await call()
        with pytest.raises(NotFoundError):
            service.watch_events(ref)
        assert manager.calls == 0
        await manager.shutdown()

    asyncio.run(run())


def test_same_thread_id_projects_are_isolated_in_sessions_events_and_workspace(
    tmp_path: Path,
) -> None:
    workspaces = {project: tmp_path / project for project in ("p1", "p2")}
    for project, workspace in workspaces.items():
        workspace.mkdir()
        (workspace / "artifact.txt").write_text(project, encoding="utf-8")
    # Keep a separate factory and manager per project; neither side is shared.
    managers: dict[str, RuntimeManager] = {}
    factories: dict[str, _SessionFactory] = {}
    for project, workspace in workspaces.items():
        managers[project], factories[project] = _controlled_manager(project, workspace)
    router = RuntimeManagerRouter(
        lambda project_id: RuntimeProject(project_id, str(workspaces[project_id])),
        lambda project: managers[project.project_id],
    )
    service = LocalAgentRuntimeService(router)

    async def run() -> None:
        refs = {project: SessionRef(project, "same-thread") for project in workspaces}
        for ref in refs.values():
            await service.open_session(OpenSessionCommand(ref))
        for project, ref in refs.items():
            metadata = await service.stat_artifact(
                StatArtifactQuery(ArtifactRef(ref, "artifact.txt"))
            )
            chunk = await service.read_artifact(ReadArtifactQuery(ArtifactRef(ref, "artifact.txt")))
            assert metadata.ref.session.project_id == project
            assert base64.b64decode(chunk.data_base64) == project.encode()
            listing = await service.list_artifacts(ListArtifactsQuery(ref))
            assert [entry.path for entry in listing.entries] == ["artifact.txt"]
        for project, ref in refs.items():
            session = managers[project].get_session_ref(ref)
            assert session is not None
            session.broker.emit(_event("same-thread", 1, project))
            page = await service.read_events(ReadEventsQuery(ref))
            assert [event.payload["text"] for event in page.events] == [project]
            watch = service.watch_events(ref, after=1)
            async with watch as stream:
                session.broker.emit(_event("same-thread", 2, f"watch-{project}"))
                event = await stream.__anext__()
                assert event.payload["text"] == f"watch-{project}"
        await service.close_session(CloseSessionCommand(refs["p1"]))
        with pytest.raises(NotFoundError):
            await service.get_session(GetSessionQuery(refs["p1"]))
        assert (workspaces["p2"] / "artifact.txt").read_text(encoding="utf-8") == "p2"
        assert await service.get_session(GetSessionQuery(refs["p2"]))
        await router.shutdown()

    asyncio.run(run())


def test_different_projects_build_in_parallel() -> None:
    entered = threading.Barrier(2)
    factories: list[str] = []
    lock = threading.Lock()

    def factory(project: RuntimeProject) -> RuntimeManager:
        with lock:
            factories.append(project.project_id)
        entered.wait(5)
        return _manager(project.project_id)

    router = RuntimeManagerRouter(
        lambda project_id: RuntimeProject(project_id, "/workspace"), factory
    )
    results: list[RuntimeManager | None] = []
    threads = [
        threading.Thread(target=lambda project=project: results.append(router(project)))
        for project in ("p1", "p2")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert {manager.project_id for manager in results if manager is not None} == {"p1", "p2"}
    assert factories == ["p1", "p2"] or factories == ["p2", "p1"]
    asyncio.run(router.shutdown())


def test_failed_single_flight_is_shared_and_next_call_retries() -> None:
    failures = 0
    lock = threading.Lock()
    failure = RuntimeError("factory failed")
    retry = threading.Event()
    provider_started = threading.Event()
    provider_release = threading.Event()

    def factory(project: RuntimeProject) -> RuntimeManager:
        nonlocal failures
        with lock:
            failures += 1
            attempt = failures
        if attempt == 1:
            retry.wait(5)
            raise failure
        return _manager(project.project_id)

    def provider(project_id: str) -> RuntimeProject:
        provider_started.set()
        assert provider_release.wait(5)
        return RuntimeProject(project_id, "/workspace")

    router = RuntimeManagerRouter(
        provider, factory
    )
    errors: list[BaseException] = []
    leader = threading.Thread(target=lambda: _capture_error(router, errors))
    leader.start()
    assert provider_started.wait(5)
    threads = [threading.Thread(target=lambda: _capture_error(router, errors)) for _ in range(9)]
    for thread in threads:
        thread.start()
    provider_release.set()
    retry.set()
    threads.append(leader)
    for thread in threads:
        thread.join(5)
    assert len(errors) == 10
    assert all(error is failure for error in errors)
    assert router.manager_count == 0
    manager = router("p1")
    assert manager is not None
    assert failures == 2
    asyncio.run(router.shutdown())


def _capture_error(
    router: RuntimeManagerRouter,
    errors: list[BaseException],
) -> None:
    try:
        router("p1")
    except BaseException as exc:
        errors.append(exc)


def test_router_and_local_service_close_mapping() -> None:
    router = RuntimeManagerRouter(
        lambda project_id: RuntimeProject(project_id, "/workspace"),
        lambda project: _manager(project.project_id),
    )
    service = LocalAgentRuntimeService(router)

    async def run() -> None:
        await service.open_session(OpenSessionCommand(SessionRef("p1", "thread")))
        await router.shutdown()
        with pytest.raises(ClosedError) as exc:
            await service.get_session(GetSessionQuery(SessionRef("p1", "thread")))
        assert exc.value.code == "closed"
        with pytest.raises(RouterClosedError):
            router("p1")

    asyncio.run(run())


def test_shutdown_racing_successful_build_closes_manager_and_all_resolvers() -> None:
    build_started = threading.Event()
    release_build = threading.Event()
    shutdown_finished = threading.Event()
    manager_holder: list[_CountingManager] = []

    def factory(project: RuntimeProject) -> RuntimeManager:
        build_started.set()
        assert release_build.wait(5)
        manager = _CountingManager(project.project_id)
        manager_holder.append(manager)
        return manager

    router = RuntimeManagerRouter(
        lambda project_id: RuntimeProject(project_id, "/workspace"), factory
    )
    outcomes: list[BaseException] = []

    def resolve() -> None:
        try:
            router("p1")
        except BaseException as exc:
            outcomes.append(exc)

    leader = threading.Thread(target=resolve)
    leader.start()
    assert build_started.wait(5)

    async def close() -> None:
        await router.shutdown()
        shutdown_finished.set()

    shutdown_thread = threading.Thread(target=lambda: asyncio.run(close()))
    shutdown_thread.start()
    assert not shutdown_finished.wait(0.05)
    with pytest.raises(RouterClosedError):
        router("p2")
    release_build.set()
    leader.join(5)
    shutdown_thread.join(5)
    assert shutdown_finished.is_set()
    assert len(outcomes) == 1 and isinstance(outcomes[0], RouterClosedError)
    assert len(manager_holder) == 1
    assert manager_holder[0].shutdown_calls == 1
    assert router.manager_count == 0
    with pytest.raises(RouterClosedError):
        router("p1")


def test_shutdown_racing_failing_build_finishes_and_shares_build_error() -> None:
    build_started = threading.Event()
    release_build = threading.Event()
    failure = RuntimeError("blocked build failed")

    def factory(_project: RuntimeProject) -> RuntimeManager:
        build_started.set()
        assert release_build.wait(5)
        raise failure

    router = RuntimeManagerRouter(
        lambda project_id: RuntimeProject(project_id, "/workspace"),
        factory,
    )
    errors: list[BaseException] = []
    threads = [threading.Thread(target=lambda: _capture_error(router, errors)) for _ in range(3)]
    for thread in threads:
        thread.start()
    assert build_started.wait(5)

    async def close() -> None:
        await router.shutdown()

    shutdown_thread = threading.Thread(target=lambda: asyncio.run(close()))
    shutdown_thread.start()
    release_build.set()
    for thread in threads:
        thread.join(5)
    shutdown_thread.join(5)
    assert len(errors) == 3 and all(error is failure for error in errors)
    assert not shutdown_thread.is_alive()
    with pytest.raises(RouterClosedError):
        router("p1")


def test_rejected_real_managers_are_owned_until_shutdown() -> None:
    rejected = [_CountingManager(None), _CountingManager("other")]
    calls = 0

    def factory(project: RuntimeProject) -> RuntimeManager:
        nonlocal calls
        result = rejected[calls]
        calls += 1
        return result

    router = RuntimeManagerRouter(
        lambda project_id: RuntimeProject(project_id, "/workspace"), factory
    )
    with pytest.raises(RuntimeError):
        router("p1")
    with pytest.raises(RuntimeError):
        router("p2")
    assert router.manager_count == 0
    assert router.project_ids == ()
    asyncio.run(router.shutdown())
    assert [manager.shutdown_calls for manager in rejected] == [1, 1]
    asyncio.run(router.shutdown())
    assert [manager.shutdown_calls for manager in rejected] == [1, 1]


def test_project_ids_are_stably_sorted() -> None:
    router = RuntimeManagerRouter(
        lambda project_id: RuntimeProject(project_id, "/workspace"),
        lambda project: _manager(project.project_id),
    )
    assert router("p2") is not None
    assert router("p1") is not None
    assert router.project_ids == ("p1", "p2")
    asyncio.run(router.shutdown())


def test_wrong_manager_is_not_found_before_session_call() -> None:
    manager = _manager("right")
    service = LocalAgentRuntimeService(lambda _project_id: manager)

    async def run() -> None:
        with pytest.raises(NotFoundError):
            await service.get_session(GetSessionQuery(SessionRef("wrong", "thread")))
        await manager.shutdown()

    asyncio.run(run())
