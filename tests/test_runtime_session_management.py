"""Session-management surface: create/rename/delete/search over metadata only.

Three layers are covered: the pure service (DTOs, metadata semantics, retained
history), the real wire dispatch/ACL path the daemon and the web console use,
and the server-side linearization that makes a busy delete impossible to fake
with a client-side check.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import synapse.runtime.service.session_metadata as session_metadata
from synapse.runtime.agent_loop import CancelToken, TurnHandle, TurnResult, TurnStatus
from synapse.runtime.service.access import (
    SESSION_CREATE,
    SESSION_DELETE,
    SESSION_RENAME,
    SESSION_SEARCH,
    AclAuthorizer,
    AclGrant,
    Principal,
    ProjectScopeAuthorizer,
    bind_access,
)
from synapse.runtime.service.errors import (
    ClosedError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from synapse.runtime.service.local import LocalAgentRuntimeService
from synapse.runtime.service.session_management import (
    CreateSessionCommand,
    CreateSessionResult,
    DeleteSessionCommand,
    RenameSessionCommand,
    SearchSessionsQuery,
    SessionMetadataStore,
    SessionProjectContext,
    SessionSearchPage,
)
from synapse.runtime.service.session_metadata import (
    SessionMetadataService,
    SessionStoreMetadataStore,
)
from synapse.runtime.sessions import RuntimeManager, SessionRuntime, UserTurn
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport import CAPABILITIES, RuntimeWebSocketClient, protocol
from synapse.runtime.transport.client import ProtocolTransportError
from synapse.sessions.store import SessionStore


def _settings(db_path: Path) -> SimpleNamespace:
    return SimpleNamespace(resolved_sessions_path=lambda: db_path)


def _service(db_path: Path, *, manager: Any | None = None) -> SessionMetadataService:
    def provider(project_id: str) -> SessionProjectContext:
        return SessionProjectContext(
            project_id=project_id, settings=_settings(db_path), manager=manager
        )

    return SessionMetadataService(provider)


class _ControlledTurnRuntime:
    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.future: concurrent.futures.Future[TurnResult] = concurrent.futures.Future()

    def submit(self, context: Any, *, sink: Any, cancel_token: CancelToken) -> TurnHandle:
        return TurnHandle(context.turn_id, self.future, cancel_token)


class _SessionFactory:
    def __init__(self) -> None:
        self.turns: dict[str, _ControlledTurnRuntime] = {}

    def __call__(self, *, thread_id: str, agent: Any, settings: Any) -> SessionRuntime:
        controlled = _ControlledTurnRuntime(thread_id)
        self.turns[thread_id] = controlled
        return SessionRuntime(
            thread_id=thread_id,
            agent=agent,
            settings=settings,
            turn_runtime=controlled,  # type: ignore[arg-type]
        )


def _result(thread_id: str) -> TurnResult:
    return TurnResult(
        turn_id=f"turn-{thread_id}",
        thread_id=thread_id,
        status=TurnStatus.COMPLETED,
        final_text="done",
        input_tokens=1,
        output_tokens=1,
    )


def test_create_is_idempotent_and_validates_title(tmp_path: Path) -> None:
    db = tmp_path / "sessions.sqlite"
    service = _service(db)

    async def run() -> None:
        first = await service.create(
            CreateSessionCommand(project_id="proj", thread_id="t1", title="  Hello world  ")
        )
        assert first.created is True
        assert first.session == SessionRef(project_id="proj", thread_id="t1")
        assert first.title == "Hello world"

        again = await service.create(
            CreateSessionCommand(project_id="proj", thread_id="t1", title="ignored")
        )
        assert again.created is False
        assert again.title == "Hello world"

        allocated = await service.create(CreateSessionCommand(project_id="proj"))
        assert allocated.created is True
        assert len(allocated.session.thread_id) == 12
        assert allocated.title == f"session {allocated.session.thread_id}"

    asyncio.run(run())

    ref = SessionRef("proj", "t1")
    with pytest.raises(ValueError):
        RenameSessionCommand(ref, "   ")
    with pytest.raises(ValueError):
        RenameSessionCommand(ref, "x" * 121)
    with pytest.raises(ValueError):
        CreateSessionCommand(project_id="   ")


def test_rename_overrides_autotouch_title_and_search_is_readonly(tmp_path: Path) -> None:
    db = tmp_path / "sessions.sqlite"
    store = SessionStore(db)
    store.ensure("t1", title="session t1")
    store.touch("t1", title_hint="first user message")
    assert store.get("t1").title == "first user message"  # type: ignore[union-attr]
    store.ensure("t2", title="Second session")
    store.close()

    service = _service(db)

    async def run() -> None:
        renamed = await service.rename(
            RenameSessionCommand(SessionRef("proj", "t1"), "Manual title")
        )
        assert renamed.renamed is True
        assert renamed.title == "Manual title"

        with pytest.raises(NotFoundError):
            await service.rename(RenameSessionCommand(SessionRef("proj", "missing"), "Nope"))

        hit = await service.search(SearchSessionsQuery(project_id="proj", text="Manual"))
        assert [item.thread_id for item in hit.items] == ["t1"]
        assert hit.total == 1 and hit.next_offset is None

        stale = await service.search(
            SearchSessionsQuery(project_id="proj", text="first user")
        )
        assert stale.items == () and stale.total == 0

        page = await service.search(
            SearchSessionsQuery(project_id="proj", text="", limit=1, offset=0)
        )
        assert page.total == 2 and len(page.items) == 1 and page.next_offset == 1
        tail = await service.search(
            SearchSessionsQuery(project_id="proj", text="", limit=1, offset=1)
        )
        assert len(tail.items) == 1 and tail.next_offset is None

    asyncio.run(run())

    # A manual rename is never overwritten by a later auto-touch title.
    store = SessionStore(db)
    store.touch("t1", title_hint="second user message")
    assert store.get("t1").title == "Manual title"  # type: ignore[union-attr]
    store.close()

    # A read-only search on a project with no database creates nothing.
    missing = tmp_path / "fresh" / "sessions.sqlite"
    readonly = _service(missing)
    empty = asyncio.run(
        readonly.search(SearchSessionsQuery(project_id="proj", text="anything"))
    )
    assert empty.items == () and empty.total == 0
    assert not missing.exists()


def test_delete_rejects_busy_runtime_and_retains_history(tmp_path: Path) -> None:
    db = tmp_path / "sessions.sqlite"
    checkpoint = tmp_path / "checkpoints.sqlite"
    transcript = tmp_path / "transcript.sqlite"
    checkpoint.write_bytes(b"checkpoint")
    transcript.write_bytes(b"transcript")

    store = SessionStore(db)
    store.ensure("t1", title="Busy session")
    store.close()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS thread_goals ("
            "thread_id TEXT PRIMARY KEY NOT NULL, goal_id TEXT NOT NULL, "
            "objective TEXT NOT NULL, status TEXT NOT NULL, token_budget INTEGER, "
            "tokens_used INTEGER NOT NULL DEFAULT 0, "
            "time_used_seconds INTEGER NOT NULL DEFAULT 0, "
            "created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL)"
        )
        conn.execute("INSERT INTO thread_goals VALUES ('t1','g1','obj','active',NULL,0,0,0,0)")

    factory = _SessionFactory()
    manager = RuntimeManager(
        settings=SimpleNamespace(max_concurrency=2, model="test"),
        agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
        session_factory=factory,
        project_id="proj",
    )
    service = _service(db, manager=manager)
    ref = SessionRef("proj", "t1")

    async def run() -> None:
        handle = await manager.submit_ref(ref, UserTurn("hi"))
        with pytest.raises(ConflictError):
            await service.delete(DeleteSessionCommand(ref))

        factory.turns["t1"].future.set_result(_result("t1"))
        await asyncio.wrap_future(handle.future)
        session = manager.get_session_ref(ref)
        assert session is not None
        await session.wait_for_settlement(handle)

        result = await service.delete(DeleteSessionCommand(ref))
        assert result.deleted is True
        assert result.retained_history is True
        assert manager.get_session("t1") is None
        await manager.shutdown()

    asyncio.run(run())

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE thread_id = 't1'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM thread_goals WHERE thread_id = 't1'"
        ).fetchone()[0] == 0
    # Deletion never removes checkpoints or the transcript projection.
    assert checkpoint.read_bytes() == b"checkpoint"
    assert transcript.read_bytes() == b"transcript"


def _wired_service(
    db: Path,
    *,
    project_id: str = "proj",
    session_factory: Any = None,
    agent_factory: Any = None,
) -> tuple[LocalAgentRuntimeService, RuntimeManager]:
    """Build the real service over a real manager whose settings carry the db path."""
    manager = RuntimeManager(
        settings=SimpleNamespace(
            max_concurrency=2, model="test", sessions_path=str(db)
        ),
        agent_factory=agent_factory
        or (lambda thread_id, shared: SimpleNamespace(thread_id=thread_id)),
        session_factory=session_factory,
        project_id=project_id,
    )
    service = LocalAgentRuntimeService(
        lambda requested: manager if requested == project_id else None
    )
    return service, manager


def _dispatch(
    service: LocalAgentRuntimeService, method: str, params: dict[str, Any]
) -> Any:
    return asyncio.run(protocol.dispatch(service, method, params))


def _ref(project_id: str, thread_id: str) -> dict[str, str]:
    return {"project_id": project_id, "thread_id": thread_id}


def _row_count(db: Path, table: str, thread_id: str) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE thread_id = ?", (thread_id,)
        ).fetchone()[0]


def test_wire_dispatch_persists_metadata_without_opening_a_runtime(tmp_path: Path) -> None:
    """The four frames the console sends hit real persistence, not a runtime."""
    db = tmp_path / "sessions.sqlite"
    checkpoint = tmp_path / "checkpoints.sqlite"
    transcript = tmp_path / "transcript.sqlite"
    checkpoint.write_bytes(b"checkpoint")
    transcript.write_bytes(b"transcript")
    builds = {"agent": 0, "session": 0}

    def agent_factory(thread_id: str, shared: Any) -> Any:
        builds["agent"] += 1
        return SimpleNamespace(thread_id=thread_id)

    def session_factory(**kwargs: Any) -> Any:
        builds["session"] += 1
        raise AssertionError("session management must never open a runtime")

    service, manager = _wired_service(
        db, agent_factory=agent_factory, session_factory=session_factory
    )

    # ``runtime.session.create`` with no thread_id: the server allocates the real
    # id, so the console never invents one of its own.
    created = _dispatch(service, "runtime.session.create", {"project_id": "proj"})
    assert isinstance(created, CreateSessionResult)
    assert created.created is True
    thread_id = created.session.thread_id
    assert thread_id and created.session.project_id == "proj"
    assert created.title == f"session {thread_id}"
    assert builds == {"agent": 0, "session": 0}
    assert manager.get_session(thread_id) is None
    with sqlite3.connect(db) as conn:
        persisted = conn.execute(
            "SELECT title FROM sessions WHERE thread_id = ?", (thread_id,)
        ).fetchone()
    assert persisted is not None and persisted[0] == f"session {thread_id}"

    # Re-creating a supplied id is idempotent and keeps the stored title.
    again = _dispatch(
        service,
        "runtime.session.create",
        {"project_id": "proj", "thread_id": thread_id, "title": "ignored"},
    )
    assert again.created is False and again.title == f"session {thread_id}"

    # A blank or over-long title is rejected at the wire, not silently trimmed.
    for bad_title in ("   ", "x" * 121):
        with pytest.raises(protocol.ProtocolError):
            _dispatch(
                service,
                "runtime.session.create",
                {"project_id": "proj", "title": bad_title},
            )

    page = _dispatch(
        service,
        "runtime.session.search",
        {"project_id": "proj", "text": "", "limit": 50, "offset": 0},
    )
    assert isinstance(page, SessionSearchPage)
    assert [item.thread_id for item in page.items] == [thread_id]

    # Metadata search is not a transcript search: a transcript-only phrase is a miss.
    miss = _dispatch(
        service,
        "runtime.session.search",
        {"project_id": "proj", "text": "hello from the transcript"},
    )
    assert miss.items == () and miss.total == 0

    renamed = _dispatch(
        service,
        "runtime.session.rename",
        {"session": _ref("proj", thread_id), "title": "  Renamed  "},
    )
    assert renamed.title == "Renamed" and renamed.renamed is True
    assert _dispatch(
        service,
        "runtime.session.search",
        {"project_id": "proj", "text": "Renamed"},
    ).total == 1

    deleted = _dispatch(
        service, "runtime.session.delete", {"session": _ref("proj", thread_id)}
    )
    assert deleted.deleted is True
    # The wire frame the UI renders states the retained history explicitly.
    wire = protocol.project_result(deleted)
    assert wire["retained_history"] is True and wire["deleted"] is True
    assert _row_count(db, "sessions", thread_id) == 0
    assert checkpoint.read_bytes() == b"checkpoint"
    assert transcript.read_bytes() == b"transcript"


def test_read_only_search_never_creates_a_database(tmp_path: Path) -> None:
    """A cold project's search is empty and leaves the filesystem untouched."""
    db = tmp_path / "fresh" / "sessions.sqlite"
    service, _manager = _wired_service(db)

    page = _dispatch(
        service, "runtime.session.search", {"project_id": "proj", "text": "anything"}
    )
    assert page.items == () and page.total == 0
    assert not db.exists() and not db.parent.exists()

    with pytest.raises(NotFoundError):
        _dispatch(service, "runtime.session.search", {"project_id": "unknown"})


def test_acl_scopes_the_four_session_management_capabilities(tmp_path: Path) -> None:
    """create/search are project-scoped; rename/delete need the exact thread."""
    db = tmp_path / "sessions.sqlite"
    service, _manager = _wired_service(db)
    ref = SessionRef("proj", "t1")

    async def run() -> None:
        authorizer = AclAuthorizer(
            (
                AclGrant(
                    "user",
                    "proj",
                    frozenset(
                        {SESSION_CREATE, SESSION_RENAME, SESSION_DELETE, SESSION_SEARCH}
                    ),
                ),
            )
        )
        project_wide = bind_access(service, Principal("user"), authorizer)
        created = await project_wide.create_session(
            CreateSessionCommand(project_id="proj", thread_id="t1", title="Mine")
        )
        assert created.created is True
        assert (
            await project_wide.rename_session(RenameSessionCommand(ref, "Renamed"))
        ).title == "Renamed"
        assert (
            await project_wide.search_sessions(
                SearchSessionsQuery(project_id="proj", text="Renamed")
            )
        ).total == 1

        # A thread-scoped grant must never authorize the project-scoped pair.
        thread_scoped = bind_access(
            service,
            Principal("user"),
            AclAuthorizer(
                (
                    AclGrant(
                        "user",
                        "proj",
                        frozenset({SESSION_CREATE, SESSION_SEARCH}),
                        thread_ids=frozenset({"t1"}),
                    ),
                )
            ),
        )
        with pytest.raises(PermissionDeniedError):
            await thread_scoped.create_session(
                CreateSessionCommand(project_id="proj", title="Nope")
            )
        with pytest.raises(PermissionDeniedError):
            await thread_scoped.search_sessions(SearchSessionsQuery(project_id="proj"))

        # A thread-scoped grant does authorize its own session's write.
        thread_write = bind_access(
            service,
            Principal("user"),
            AclAuthorizer(
                (
                    AclGrant(
                        "user",
                        "proj",
                        frozenset({SESSION_RENAME, SESSION_DELETE}),
                        thread_ids=frozenset({"t1"}),
                    ),
                )
            ),
        )
        assert (
            await thread_write.rename_session(RenameSessionCommand(ref, "Scoped"))
        ).title == "Scoped"
        with pytest.raises(PermissionDeniedError):
            await thread_write.rename_session(
                RenameSessionCommand(SessionRef("proj", "other"), "Nope")
            )

        # The connection-scope wrapper narrows every one of the four capabilities.
        scoped = bind_access(
            service,
            Principal("user"),
            ProjectScopeAuthorizer(authorizer, "elsewhere"),
        )
        for call in (
            scoped.create_session(CreateSessionCommand(project_id="proj", title="Nope")),
            scoped.search_sessions(SearchSessionsQuery(project_id="proj")),
            scoped.rename_session(RenameSessionCommand(ref, "Nope")),
            scoped.delete_session(DeleteSessionCommand(ref)),
        ):
            with pytest.raises(PermissionDeniedError):
                await call

        deleted = await project_wide.delete_session(DeleteSessionCommand(ref))
        assert deleted.deleted is True and deleted.retained_history is True

    asyncio.run(run())


def test_dispatch_delete_refuses_a_running_turn_without_cancelling_it(
    tmp_path: Path,
) -> None:
    """The refusal is server-side linearization, never a client busy check."""
    db = tmp_path / "sessions.sqlite"
    store = SessionStore(db)
    store.ensure("t1", title="Busy session")
    store.close()

    factory = _SessionFactory()
    service, manager = _wired_service(db, session_factory=factory)
    ref = SessionRef("proj", "t1")

    async def run() -> None:
        handle = await manager.submit_ref(ref, UserTurn("hi"))
        with pytest.raises(ConflictError):
            await protocol.dispatch(
                service,
                "runtime.session.delete",
                {"session": _ref("proj", "t1")},
            )
        # The refused delete neither removed the row nor cancelled the turn.
        assert _row_count(db, "sessions", "t1") == 1
        assert manager.get_session("t1") is not None
        assert not factory.turns["t1"].future.cancelled()

        factory.turns["t1"].future.set_result(_result("t1"))
        await asyncio.wrap_future(handle.future)
        session = manager.get_session_ref(ref)
        assert session is not None
        await session.wait_for_settlement(handle)

        result = await protocol.dispatch(
            service, "runtime.session.delete", {"session": _ref("proj", "t1")}
        )
        assert result.deleted is True and result.retained_history is True
        assert _row_count(db, "sessions", "t1") == 0
        await manager.shutdown()

    asyncio.run(run())


class _FakeTransport:
    """Client transport double: answers the handshake and one scripted reply."""

    def __init__(self, respond: Any) -> None:
        self._respond = respond
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.frames: list[dict[str, Any]] = []

    async def send(self, message: str) -> None:
        frame = json.loads(message)
        self.frames.append(frame)
        if frame["method"] == "runtime.protocol.negotiate":
            result: Any = {
                "wire_version": "1",
                "supported_versions": ["1"],
                "capabilities": dict(CAPABILITIES),
            }
        else:
            result = self._respond(frame)
        await self.inbox.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": frame["id"],
                    "meta": {"wire_version": "1"},
                    "result": result,
                }
            )
        )

    async def recv(self) -> str:
        return await self.inbox.get()

    async def close(self) -> None:
        return None

    def business_frames(self) -> list[dict[str, Any]]:
        return [
            frame
            for frame in self.frames
            if frame["method"] != "runtime.protocol.negotiate"
        ]


def _client(fake: _FakeTransport) -> RuntimeWebSocketClient:
    return RuntimeWebSocketClient(
        "ws://loopback", connect_factory=lambda *args, **kwargs: fake
    )


def test_python_client_frames_and_strict_result_decoding() -> None:
    """The client sends the documented frames and rejects a lying peer."""
    ref = SessionRef("proj", "t1")

    async def run() -> None:
        def respond(frame: dict[str, Any]) -> Any:
            params = frame["params"]
            if frame["method"] == "runtime.session.create":
                return {
                    "command_id": params["command_id"],
                    "session": {"project_id": "proj", "thread_id": "allocated"},
                    "created": True,
                    "title": "session allocated",
                }
            if frame["method"] == "runtime.session.search":
                return {
                    "items": [
                        {
                            "thread_id": "t1",
                            "title": "Mine",
                            "model": None,
                            "active_model": None,
                            "created_at": "now",
                            "updated_at": "later",
                            "summary": None,
                        }
                    ],
                    "next_offset": None,
                    "total": 1,
                }
            if frame["method"] == "runtime.session.rename":
                return {
                    "command_id": params["command_id"],
                    "session": params["session"],
                    "title": params["title"],
                    "renamed": True,
                }
            # A peer that claims the conversation was erased violates the contract.
            return {
                "command_id": params["command_id"],
                "session": params["session"],
                "deleted": True,
                "retained_history": False,
            }

        client = _client(fake := _FakeTransport(respond))
        created = await client.create_session(CreateSessionCommand(project_id="proj"))
        assert created.session.thread_id == "allocated"
        assert created.created is True

        page = await client.search_sessions(
            SearchSessionsQuery(project_id="proj", text="Mine")
        )
        assert [item.thread_id for item in page.items] == ["t1"] and page.total == 1

        renamed = await client.rename_session(RenameSessionCommand(ref, "Renamed"))
        assert renamed.title == "Renamed"

        with pytest.raises(ProtocolTransportError):
            await client.delete_session(DeleteSessionCommand(ref))

        frames = fake.business_frames()
        assert [frame["method"] for frame in frames] == [
            "runtime.session.create",
            "runtime.session.search",
            "runtime.session.rename",
            "runtime.session.delete",
        ]
        # The create frame carries no thread id: the server allocates it.
        assert "thread_id" not in frames[0]["params"]
        assert frames[1]["params"]["text"] == "Mine"

    asyncio.run(run())


class _LedgerStore:
    """A real session store that records its own open/close for lifecycle checks.

    Every other attribute is delegated, so the adapter sees the concrete
    ``SessionStore`` API (``get``/``ensure``/``rename``/``delete``/``immediate``).
    """

    def __init__(self, path: Path, ledger: dict[str, int]) -> None:
        self._store = SessionStore(path)
        self._ledger = ledger
        ledger["open"] += 1

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def close(self) -> None:
        self._ledger["close"] += 1
        self._store.close()


def _ledger_service(db: Path, ledger: dict[str, int]) -> SessionMetadataService:
    def provider(project_id: str) -> SessionProjectContext:
        return SessionProjectContext(
            project_id=project_id, settings=_settings(db), manager=None
        )

    def store_provider(_settings: object) -> SessionMetadataStore:
        return SessionStoreMetadataStore(
            db, store_factory=lambda path: _LedgerStore(path, ledger)
        )

    return SessionMetadataService(provider, store_provider=store_provider)


def test_every_operation_opens_and_closes_its_own_store(tmp_path: Path) -> None:
    """Resource ownership: no connection outlives the operation that opened it.

    The service caches nothing process-wide (a leaked cache would show up as an
    ``open`` without a matching ``close``), and the error path closes too.
    """
    # There is no module-level store cache to leak in the first place: the
    # default store provider is the stateless ``open_session_metadata_store``.
    assert not hasattr(session_metadata, "_DEFAULT_STORE_PROVIDER")
    assert not hasattr(session_metadata, "SessionMetadataStoreProvider")
    assert session_metadata.SessionMetadataService(
        lambda project_id: SessionProjectContext(project_id=project_id, settings=None)
    )._store_provider is session_metadata.open_session_metadata_store

    db = tmp_path / "sessions.sqlite"
    ledger = {"open": 0, "close": 0}
    service = _ledger_service(db, ledger)
    ref = SessionRef("proj", "t1")

    async def run() -> None:
        created = await service.create(
            CreateSessionCommand(project_id="proj", thread_id="t1", title="One")
        )
        assert created.created is True
        await service.rename(RenameSessionCommand(ref, "Two"))
        # The not-found path opens the store and must still close it.
        with pytest.raises(NotFoundError):
            await service.rename(RenameSessionCommand(SessionRef("proj", "missing"), "X"))
        await service.delete(DeleteSessionCommand(ref))

    asyncio.run(run())

    assert ledger == {"open": 4, "close": 4}
    # The file exists but holds no session row, and no handle was left behind.
    assert db.is_file()
    store = SessionStore(db)
    try:
        assert store.list(limit=10) == []
    finally:
        store.close()

    # Closing the service rejects new mutations; there is nothing else to
    # release because each call already released its own store.
    service.close()
    with pytest.raises(ClosedError):
        asyncio.run(service.create(CreateSessionCommand(project_id="proj")))
    with pytest.raises(ClosedError):
        asyncio.run(service.delete(DeleteSessionCommand(ref)))
    assert ledger == {"open": 4, "close": 4}


def test_concurrent_creates_on_independent_stores_keep_created_exact(
    tmp_path: Path,
) -> None:
    """Independent handles, one row: exactly one caller reports ``created=True``.

    Each thread opens its own short-lived store, so this is the cross-connection
    race the shared in-process connection used to hide.  The read-then-write runs
    inside SQLite's ``BEGIN IMMEDIATE`` write transaction, so the losers wait and
    then observe the committed row instead of inserting a duplicate (which would
    surface as ``IntegrityError``).
    """
    db = tmp_path / "sessions.sqlite"
    SessionStore(db).close()

    count = 6
    barrier = threading.Barrier(count)
    created_flags: list[bool] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def work() -> None:
        adapter = SessionStoreMetadataStore(db)
        try:
            barrier.wait(timeout=10)
            flag = adapter.ensure("t1", title="Raced")[1]
        except BaseException as exc:  # noqa: BLE001 - asserted below, never swallowed
            with lock:
                errors.append(exc)
            return
        with lock:
            created_flags.append(flag)

    threads = [threading.Thread(target=work) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert sorted(created_flags) == [False] * (count - 1) + [True]

    store = SessionStore(db)
    try:
        info = store.get("t1")
        assert info is not None and info.title == "Raced"
        assert len(store.list(limit=10)) == 1
    finally:
        store.close()
