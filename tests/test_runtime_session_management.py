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


def _settings(
    db_path: Path,
    *,
    checkpoint_path: Path | None = None,
    workspace: Path | None = None,
) -> SimpleNamespace:
    """One project's path information, as the service reads it.

    ``checkpoint_path`` and ``workspace`` are optional because a project may not
    have either; a delete then purges the stores it can locate and reports the
    rest as retained.
    """
    return SimpleNamespace(
        resolved_sessions_path=lambda: db_path,
        checkpoint_path=checkpoint_path,
        workspace=workspace,
    )


def _service(
    db_path: Path,
    *,
    manager: Any | None = None,
    checkpoint_path: Path | None = None,
    workspace: Path | None = None,
) -> SessionMetadataService:
    def provider(project_id: str) -> SessionProjectContext:
        return SessionProjectContext(
            project_id=project_id,
            settings=_settings(
                db_path, checkpoint_path=checkpoint_path, workspace=workspace
            ),
            manager=manager,
        )

    return SessionMetadataService(provider)


def _make_history_stores(tmp_path: Path) -> SimpleNamespace:
    """Create an empty checkpoint store, transcript projection and search index.

    ``_seed_history`` fills them per thread, so a test can seed a thread whose id
    is only known later (an allocated one) and can still prove that a purge
    removed exactly one conversation.
    """
    checkpoint_path = tmp_path / "checkpoints.sqlite"
    with sqlite3.connect(checkpoint_path) as conn:
        conn.execute(
            "CREATE TABLE checkpoints (thread_id TEXT NOT NULL, "
            "checkpoint_ns TEXT NOT NULL DEFAULT '', checkpoint_id TEXT NOT NULL, "
            "parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB, "
            "PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))"
        )
        conn.execute(
            "CREATE TABLE writes (thread_id TEXT NOT NULL, "
            "checkpoint_ns TEXT NOT NULL DEFAULT '', checkpoint_id TEXT NOT NULL, "
            "task_id TEXT NOT NULL, idx INTEGER NOT NULL, channel TEXT NOT NULL, "
            "type TEXT, value BLOB, "
            "PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx))"
        )

    transcript_path = tmp_path / "transcript.sqlite"
    with sqlite3.connect(transcript_path) as conn:
        conn.execute(
            "CREATE TABLE transcript_events (thread_id TEXT NOT NULL, "
            "event_seq INTEGER NOT NULL, turn_seq INTEGER NOT NULL, kind TEXT NOT NULL, "
            "payload_json TEXT NOT NULL, PRIMARY KEY (thread_id, event_seq))"
        )
        conn.execute(
            "CREATE TABLE transcript_meta (thread_id TEXT PRIMARY KEY, "
            "total_turns INTEGER NOT NULL DEFAULT 0, "
            "total_events INTEGER NOT NULL DEFAULT 0, "
            "source_message_count INTEGER NOT NULL DEFAULT 0, "
            "source_checkpoint_id TEXT, "
            "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )

    index_path = tmp_path / "search-index.sqlite"
    with sqlite3.connect(index_path) as conn:
        conn.execute(
            "CREATE TABLE indexed (thread_id TEXT PRIMARY KEY, "
            "checkpoint_id TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE messages (thread_id TEXT NOT NULL, seq INTEGER NOT NULL, "
            "role TEXT NOT NULL, content TEXT NOT NULL, PRIMARY KEY (thread_id, seq))"
        )

    workspace = tmp_path / "workspace"
    (workspace / ".synapse" / "turn-snapshots").mkdir(parents=True)

    return SimpleNamespace(
        checkpoint=checkpoint_path,
        transcript=transcript_path,
        index=index_path,
        workspace=workspace,
    )


def _seed_history(stores: SimpleNamespace, thread_id: str) -> None:
    """Give one thread a conversation in every store.

    The checkpoint store gets a ``tools:`` namespace too: a subagent's state is
    part of the conversation it ran in and must go with it.
    """
    with sqlite3.connect(stores.checkpoint) as conn:
        conn.execute(
            "INSERT INTO checkpoints VALUES (?, '', ?, NULL, NULL, NULL, NULL)",
            (thread_id, f"ckpt-{thread_id}"),
        )
        conn.execute(
            "INSERT INTO checkpoints VALUES (?, 'tools:task-1', ?, NULL, NULL, NULL, NULL)",
            (thread_id, f"ckpt-{thread_id}"),
        )
        conn.execute(
            "INSERT INTO writes VALUES (?, '', ?, 'task-1', 0, 'channel', NULL, NULL)",
            (thread_id, f"ckpt-{thread_id}"),
        )
    with sqlite3.connect(stores.transcript) as conn:
        conn.execute(
            "INSERT INTO transcript_events VALUES (?, 1, 1, 'user', '{}')", (thread_id,)
        )
        conn.execute("INSERT INTO transcript_meta (thread_id) VALUES (?)", (thread_id,))
    with sqlite3.connect(stores.index) as conn:
        conn.execute(
            "INSERT INTO indexed VALUES (?, ?, 'now')", (thread_id, f"ckpt-{thread_id}")
        )
        conn.execute(
            "INSERT INTO messages VALUES (?, 0, 'human', 'hello from the transcript')",
            (thread_id,),
        )
    directory = stores.workspace / ".synapse" / "turn-snapshots" / thread_id
    directory.mkdir(parents=True)
    (directory / "0000000000001-turn-1.json").write_text("{}", encoding="utf-8")


def _store_rows(path: Path, table: str, thread_id: str) -> int:
    """How many rows one store still holds for a thread."""
    with sqlite3.connect(path) as conn:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE thread_id = ?", (thread_id,)
            ).fetchone()[0]
        )


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


def test_touch_names_a_session_from_its_first_user_message(tmp_path: Path) -> None:
    """The store's autotouch rule, reached through the service port."""
    db = tmp_path / "sessions.sqlite"
    service = _service(db)

    async def run() -> None:
        created = await service.create(CreateSessionCommand(project_id="proj", thread_id="t1"))
        assert created.title == "session t1"

        bound = await service.touch(
            SessionRef("proj", "t1"), title_hint="  fix   the\nflaky test "
        )
        assert bound.title == "fix the flaky test"

        # A later turn must not rename the session...
        again = await service.touch(SessionRef("proj", "t1"), title_hint="second turn")
        assert again.title == "fix the flaky test"

        # ...and neither does a hint for a title the user bound themselves.
        await service.create(
            CreateSessionCommand(project_id="proj", thread_id="t2", title="Manual title")
        )
        kept = await service.touch(SessionRef("proj", "t2"), title_hint="from the turn")
        assert kept.title == "Manual title"

        # A session opened without ever persisting metadata is named as well.
        fresh = await service.touch(SessionRef("proj", "t3"), title_hint="opened but unpersisted")
        assert fresh.thread_id == "t3"
        assert fresh.title == "opened but unpersisted"

        # An attachment-only turn carries no hint: the placeholder stays.
        untouched = await service.touch(SessionRef("proj", "t4"), title_hint="")
        assert untouched.title == "session t4"

    asyncio.run(run())


def test_delete_rejects_busy_runtime_and_purges_the_conversation(tmp_path: Path) -> None:
    db = tmp_path / "sessions.sqlite"
    stores = _make_history_stores(tmp_path)
    _seed_history(stores, "t1")
    _seed_history(stores, "t2")

    store = SessionStore(db)
    store.ensure("t1", title="Busy session")
    store.ensure("t2", title="Untouched session")
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
    service = _service(
        db,
        manager=manager,
        checkpoint_path=stores.checkpoint,
        workspace=stores.workspace,
    )
    ref = SessionRef("proj", "t1")

    async def run() -> None:
        handle = await manager.submit_ref(ref, UserTurn("hi"))
        with pytest.raises(ConflictError):
            await service.delete(DeleteSessionCommand(ref))
        # A refused delete purges nothing: the conversation is still readable.
        assert _store_rows(stores.checkpoint, "checkpoints", "t1") == 2

        factory.turns["t1"].future.set_result(_result("t1"))
        await asyncio.wrap_future(handle.future)
        session = manager.get_session_ref(ref)
        assert session is not None
        await session.wait_for_settlement(handle)

        result = await service.delete(DeleteSessionCommand(ref))
        assert result.deleted is True
        # The row is not the session: the conversation is gone with it.
        assert result.retained_history is False
        assert result.purge_failures == ()
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
    # Every store that held t1 is empty -- including the subagent namespace and
    # the full-text index, which are what made a deleted session searchable.
    assert _store_rows(stores.checkpoint, "checkpoints", "t1") == 0
    assert _store_rows(stores.checkpoint, "writes", "t1") == 0
    assert _store_rows(stores.transcript, "transcript_events", "t1") == 0
    assert _store_rows(stores.transcript, "transcript_meta", "t1") == 0
    assert _store_rows(stores.index, "indexed", "t1") == 0
    assert _store_rows(stores.index, "messages", "t1") == 0
    assert not (stores.workspace / ".synapse" / "turn-snapshots" / "t1").exists()
    # ... and every store that held the *other* session is untouched.
    assert _store_rows(stores.checkpoint, "checkpoints", "t2") == 2
    assert _store_rows(stores.checkpoint, "writes", "t2") == 1
    assert _store_rows(stores.transcript, "transcript_events", "t2") == 1
    assert _store_rows(stores.index, "messages", "t2") == 1
    assert (stores.workspace / ".synapse" / "turn-snapshots" / "t2").is_dir()


def test_purge_callback_reports_a_project_it_cannot_locate() -> None:
    """The defensive branch: no resolvable path is a *reported* failure.

    ``_store`` refuses the delete first in practice, so this only pins that the
    fallback is both reachable and honest -- a purge that reported success without
    ever locating a store would be the silent half-delete this change removes.
    """
    from synapse.runtime.service.session_metadata import _thread_purge

    purge = _thread_purge(SimpleNamespace(resolved_sessions_path=lambda: None), "t1")

    report = purge()

    assert report.complete is False
    assert report.failures == ("sessions",)


def test_delete_names_a_store_it_could_not_purge(tmp_path: Path) -> None:
    """A store that cannot be purged is reported, never silently skipped.

    The row still goes -- a caller that asked for the session to be gone must not
    keep it in the list because one store was unreadable -- so the report is the
    only thing that keeps the partial state honest.
    """
    db = tmp_path / "sessions.sqlite"
    stores = _make_history_stores(tmp_path)
    _seed_history(stores, "t1")
    # A transcript file that is not a database: opening it raises, which the purge
    # must survive and name.
    stores.transcript.write_bytes(b"not a database")
    store = SessionStore(db)
    store.ensure("t1", title="Broken store")
    store.close()

    service = _service(db, checkpoint_path=stores.checkpoint, workspace=stores.workspace)
    result = asyncio.run(
        service.delete(DeleteSessionCommand(SessionRef("proj", "t1")))
    )

    assert result.deleted is True
    assert result.retained_history is True
    assert result.purge_failures == ("transcript",)
    # The stores it could reach are still purged: one broken store does not stop
    # the others.
    assert _store_rows(stores.checkpoint, "checkpoints", "t1") == 0
    assert _store_rows(stores.index, "messages", "t1") == 0


def _wired_service(
    db: Path,
    *,
    project_id: str = "proj",
    session_factory: Any = None,
    agent_factory: Any = None,
    checkpoint_path: Path | None = None,
    workspace: Path | None = None,
) -> tuple[LocalAgentRuntimeService, RuntimeManager]:
    """Build the real service over a real manager whose settings carry the db path.

    The checkpoint store and the workspace default to the metadata database's own
    directory, which is where a real project keeps them, so a delete here purges
    the same three siblings a real one does.  A test that seeds those stores with
    ``_make_history_stores`` gets them for free.
    """
    if checkpoint_path is None:
        checkpoint_path = db.parent / "checkpoints.sqlite"
    if workspace is None:
        workspace = db.parent / "workspace"
    manager = RuntimeManager(
        settings=SimpleNamespace(
            max_concurrency=2,
            model="test",
            sessions_path=str(db),
            checkpoint_path=checkpoint_path,
            workspace=workspace,
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
    stores = _make_history_stores(tmp_path)
    builds = {"agent": 0, "session": 0}

    def agent_factory(thread_id: str, shared: Any) -> Any:
        builds["agent"] += 1
        return SimpleNamespace(thread_id=thread_id)

    def session_factory(**kwargs: Any) -> Any:
        builds["session"] += 1
        raise AssertionError("session management must never open a runtime")

    service, manager = _wired_service(
        db,
        agent_factory=agent_factory,
        session_factory=session_factory,
        checkpoint_path=stores.checkpoint,
        workspace=stores.workspace,
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

    # The conversation this thread holds in every store (the id was allocated, so
    # the stores are seeded only now).
    _seed_history(stores, thread_id)

    deleted = _dispatch(
        service, "runtime.session.delete", {"session": _ref("proj", thread_id)}
    )
    assert deleted.deleted is True
    # The wire frame the UI renders reports the erasure explicitly, so the console
    # never has to guess what a delete removed.
    wire = protocol.project_result(deleted)
    assert wire["deleted"] is True
    assert wire["retained_history"] is False and wire["purge_failures"] == []
    assert _row_count(db, "sessions", thread_id) == 0
    assert _store_rows(stores.checkpoint, "checkpoints", thread_id) == 0
    assert _store_rows(stores.checkpoint, "writes", thread_id) == 0
    assert _store_rows(stores.transcript, "transcript_events", thread_id) == 0
    assert _store_rows(stores.index, "messages", thread_id) == 0
    assert not (stores.workspace / ".synapse" / "turn-snapshots" / thread_id).exists()


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
        assert deleted.deleted is True
        # No store in this fixture holds the thread, so there is nothing left to
        # report as retained: a clean purge is not the same as an unlocated store.
        assert deleted.retained_history is False
        assert deleted.purge_failures == ()

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
        assert result.deleted is True
        assert result.retained_history is False and result.purge_failures == ()
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
            if params["session"]["thread_id"] == "lying":
                # The two fields must agree: a peer that admits a failure while
                # claiming nothing was retained is a protocol violation, not a
                # "better" delete.
                return {
                    "command_id": params["command_id"],
                    "session": params["session"],
                    "deleted": True,
                    "retained_history": False,
                    "purge_failures": ["transcript"],
                }
            # A clean erasure is the normal outcome now, and the frame says which
            # stores were purged.
            return {
                "command_id": params["command_id"],
                "session": params["session"],
                "deleted": True,
                "retained_history": False,
                "purge_failures": [],
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

        deleted = await client.delete_session(DeleteSessionCommand(ref))
        assert deleted.deleted is True
        assert deleted.retained_history is False and deleted.purge_failures == ()

        # A peer whose two fields disagree is rejected rather than decoded.
        with pytest.raises(ProtocolTransportError):
            await client.delete_session(
                DeleteSessionCommand(SessionRef("proj", "lying"))
            )

        frames = fake.business_frames()
        assert [frame["method"] for frame in frames] == [
            "runtime.session.create",
            "runtime.session.search",
            "runtime.session.rename",
            "runtime.session.delete",
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
