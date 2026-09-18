"""Implementation of the session-management surface (create/rename/delete/search).

Writes (create / rename / delete) resolve the project's session-metadata SQLite
path through the project resolver, so a caller never supplies a path over the
wire.  Search is a strictly read-only query: it never creates a database file,
schema, or agent, and it reads the same metadata fields the store's own
``search`` matches (title, summary, thread_id, model, active_model) with bounded
``limit``/``offset``.

The write store is opened and closed *per operation*: the service owns no
connection between calls, so the caller that invokes an operation is the
explicit resource owner and there is no process-wide store cache to keep alive
(or to leak).  Serialization moves to SQLite itself — each read-modify-write
runs inside the store's ``BEGIN IMMEDIATE`` write transaction with the
connection's busy timeout, so concurrent callers on independent handles wait
and then observe the committed row instead of racing it.  That is what keeps
``create`` idempotent (exactly one caller sees ``created=True``) and ``rename``
last-writer-wins without a shared in-process connection.

Deletion goes through :meth:`RuntimeManager.delete_session_ref` when a live
manager exists, so a running turn is rejected atomically and a concurrent
open/submit cannot resurrect the row.  It removes the metadata row and the thread
goal *and* purges the thread from the stores the row does not cover --
checkpoints, the transcript projection, the full-text search index and the
thread's turn snapshots -- because a session whose row is gone is still readable
and searchable otherwise.  A store that refused is named in
``DeleteSessionResult.purge_failures`` and ``retained_history`` stays ``True``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from synapse.sessions.thread_purge import ThreadPurgeReport

from synapse.runtime.service.errors import (
    ClosedError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
)
from synapse.runtime.service.history import SessionMetadataItem
from synapse.runtime.service.history_store import (
    _available_columns,
    _connect_readonly,
    _has_table,
    _session_item,
    resolve_sessions_path,
)
from synapse.runtime.service.session_management import (
    CreateSessionCommand,
    CreateSessionResult,
    DeleteSessionCommand,
    DeleteSessionResult,
    RenameSessionCommand,
    RenameSessionResult,
    SearchSessionsQuery,
    SessionMetadataStore,
    SessionProjectContext,
    SessionSearchPage,
)
from synapse.runtime.sessions.errors import RuntimeClosedError, SessionBusyError
from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "SessionMetadataService",
    "SessionStoreMetadataStore",
    "open_session_metadata_store",
    "search_sessions_readonly",
]

#: Metadata columns matched by the search predicate, in the same order and with
#: the same semantics as ``SessionStore.search``.
_SEARCH_COLUMNS = ("title", "summary", "thread_id", "model", "active_model")
#: Columns that may be NULL in older databases and therefore need ``IFNULL``.
_NULLABLE_SEARCH_COLUMNS = frozenset({"summary", "model", "active_model"})


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _item_from_info(info: Any) -> SessionMetadataItem:
    return SessionMetadataItem(
        thread_id=str(getattr(info, "thread_id", "") or ""),
        title=str(getattr(info, "title", "") or ""),
        model=_optional_text(getattr(info, "model", None)),
        active_model=_optional_text(getattr(info, "active_model", None)),
        created_at=str(getattr(info, "created_at", "") or ""),
        updated_at=str(getattr(info, "updated_at", "") or ""),
        summary=_optional_text(getattr(info, "summary", None)),
    )


def _search_predicate(text: str, available: tuple[str, ...]) -> tuple[str, list[str]]:
    """Build the bounded LIKE predicate over the available metadata columns."""
    needle = f"%{text.strip()}%"
    clauses: list[str] = []
    params: list[str] = []
    for column in _SEARCH_COLUMNS:
        if column not in available:
            continue
        if column in _NULLABLE_SEARCH_COLUMNS:
            clauses.append(f"IFNULL({column}, '') LIKE ?")
        else:
            clauses.append(f"{column} LIKE ?")
        params.append(needle)
    if not clauses:
        return "1 = 0", []
    return " OR ".join(clauses), params


def search_sessions_readonly(
    settings: object, query: SearchSessionsQuery
) -> SessionSearchPage:
    """Return one bounded page of matching session metadata, newest-first.

    A missing database file, or one without a ``sessions`` table, yields an
    empty page; no file, directory, or schema is ever created and no agent or
    session is built.  The count and the page read share one explicit read-only
    snapshot.
    """
    path = resolve_sessions_path(settings)
    if path is None or not path.is_file():
        return SessionSearchPage(items=(), next_offset=None, total=0)
    connection = _connect_readonly(path)
    try:
        connection.execute("BEGIN")
        if not _has_table(connection, "sessions"):
            return SessionSearchPage(items=(), next_offset=None, total=0)
        available = _available_columns(connection, "sessions")
        where, params = _search_predicate(query.text, available)
        total_row = connection.execute(
            f"SELECT COUNT(*) AS n FROM sessions WHERE {where}", params
        ).fetchone()
        total = int(total_row["n"] if total_row else 0)
        limit = int(query.limit)
        offset = int(query.offset)
        rows = connection.execute(
            f"SELECT * FROM sessions WHERE {where} "
            "ORDER BY updated_at DESC, thread_id LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        items = tuple(_session_item(row) for row in rows)
        consumed = offset + len(items)
        next_offset = consumed if consumed < total else None
        return SessionSearchPage(items=items, next_offset=next_offset, total=total)
    finally:
        connection.rollback()
        connection.close()


@contextmanager
def _write_transaction(store: Any) -> Iterator[None]:
    """Bracket one read-modify-write with the store's SQLite write transaction.

    The concrete session store exposes ``immediate``.  An injected double may
    not, and then the operation runs unwrapped: that compatibility branch keeps
    ``store_factory`` injection working for callers whose fake owns its own
    serialization.
    """
    immediate = getattr(store, "immediate", None)
    if not callable(immediate):
        yield
        return
    with immediate():
        yield


class SessionStoreMetadataStore:
    """Short-lived adapter over the project's SQLite session metadata store.

    Every operation opens the underlying store, performs one atomic
    read-modify-write, and closes the store again, so the adapter holds no
    connection between calls and owns nothing to leak: the caller that invokes
    the operation is the resource owner.  ``rename``/``delete`` never create the
    database (a missing file means the session does not exist); ``ensure`` is the
    create path, so it opens the write store, creating the file and schema.

    Serialization is SQLite's.  The read-then-write runs inside the store's
    ``BEGIN IMMEDIATE`` write transaction, so a competing writer on another
    handle waits on the busy timeout and then reads the committed row.  ``ensure``
    deliberately reuses the store's own ``ensure`` transaction (one
    ``get``+``ensure`` inside one write lock), which is what makes ``created``
    exact when two connections create the same session concurrently.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        store_factory: Callable[[Path], Any] | None = None,
    ) -> None:
        self._path = Path(path).expanduser()
        self._store_factory = store_factory or _default_store_factory
        # A caller may hold one adapter across several operations; this keeps
        # those calls from interleaving.  Independent adapters serialize in
        # SQLite instead, which is why the lock is not shared process-wide.
        self._lock = threading.Lock()

    @contextmanager
    def _open(self) -> Iterator[Any]:
        store = self._store_factory(self._path)
        try:
            with _write_transaction(store):
                yield store
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

    def ensure(
        self, thread_id: str, *, title: str | None = None
    ) -> tuple[SessionMetadataItem, bool]:
        with self._lock, self._open() as store:
            existing = store.get(thread_id)
            info = store.ensure(thread_id, title=title)
        return _item_from_info(info), existing is None

    def rename(self, thread_id: str, title: str) -> SessionMetadataItem | None:
        with self._lock:
            if not self._path.is_file():
                return None
            with self._open() as store:
                info = store.rename(thread_id, title)
        return _item_from_info(info) if info is not None else None

    def touch(
        self, thread_id: str, *, title_hint: str | None = None
    ) -> SessionMetadataItem:
        """Bind a hint as the title while the stored title is still a placeholder.

        The store owns the rule (``SessionStore.touch`` / ``is_default_session_title``):
        a bound title is never overwritten by a later hint, while an unbound row --
        including one created without a title -- takes the first user message as its
        name.  Unlike ``rename`` this also creates a missing row, which is what lets
        a client that opened a session without persisting metadata still name it.
        """
        with self._lock, self._open() as store:
            info = store.touch(thread_id, title_hint=title_hint)
        return _item_from_info(info)

    def delete(self, thread_id: str) -> bool:
        with self._lock:
            if not self._path.is_file():
                return False
            with self._open() as store:
                deleted = store.delete(thread_id)
        return bool(deleted)


def _default_store_factory(path: Path) -> Any:
    from synapse.sessions.store import SessionStore

    return SessionStore(path)


def _thread_purge(
    settings: object, thread_id: str
) -> Callable[[], ThreadPurgeReport]:
    """Build the history-purge callback for one thread of one project.

    One resolved path locates every store: the transcript projection and the
    search index are siblings of the metadata database, and the checkpoint store
    and the workspace are read from the same settings object.

    The callback is total.  A project whose metadata path cannot be resolved is
    *not* claimed clean: it reports the metadata store itself as the failure, so
    ``retained_history`` stays true and agrees with ``purge_failures``.  That path
    is defensive -- ``_store`` resolves the same path first and refuses the delete
    when it is unavailable -- but a purge that silently reported success for a
    store it never located is exactly the half-delete this change removes.
    """
    # Imported in the function body, like ``_default_store_factory``: the service
    # module keeps no ``synapse.sessions`` import at module load, and the report
    # type is needed by a *callable* here, not only by an annotation.
    from synapse.sessions.thread_purge import ThreadPurgeReport

    sessions_path = resolve_sessions_path(settings)
    if sessions_path is None:
        return lambda: ThreadPurgeReport(thread_id=thread_id, failures=("sessions",))
    checkpoint_path = getattr(settings, "checkpoint_path", None)
    workspace = getattr(settings, "workspace", None)

    def purge() -> ThreadPurgeReport:
        # Imported here so the service module keeps its "no synapse.sessions at
        # import time" shape, matching the store factory above.
        from synapse.sessions.thread_purge import purge_thread

        return purge_thread(
            thread_id,
            checkpoint_path=checkpoint_path,
            sessions_path=sessions_path,
            workspace=workspace,
        )

    return purge


def open_session_metadata_store(settings: object) -> SessionMetadataStore | None:
    """Build a write-capable metadata store from a project's settings.

    The database path comes from the project resolver (``settings``); the
    caller never supplies a path.  ``None`` means the settings carry no path
    information, which the service reports as an unavailable store.

    The returned adapter is stateless between calls (it opens and closes the
    store per operation), so it is safe to build one per operation and there is
    no process-global cache: this function *is* the default store provider.
    """
    path = resolve_sessions_path(settings)
    if path is None:
        return None
    return SessionStoreMetadataStore(path)


class SessionMetadataService:
    """Serialized session-management operations over one or more projects.

    Every mutation (create / rename / delete) runs under one bounded
    service-level lock, so resolving the project context (which may build the
    manager through the router) and mutating the metadata store cannot
    interleave.  A single slot replaces the previous per-session lock map: that
    map only ever grew, one entry per session the connection touched, for the
    whole lifetime of the service.  Concurrent callers on *different*
    connections still serialize, in SQLite's write transaction.

    Delete additionally routes through the manager's atomic lifecycle gate,
    which rejects a busy runtime and detaches it before the metadata row is
    removed.  Search is a pure read and never opens a session or a database.
    """

    def __init__(
        self,
        context_provider: Callable[[str], SessionProjectContext],
        *,
        store_provider: Callable[[object], SessionMetadataStore | None] | None = None,
    ) -> None:
        self._context_provider = context_provider
        self._store_provider = store_provider or open_session_metadata_store
        self._lock = threading.Lock()
        self._mutation_lock = asyncio.Lock()
        self._closed = False

    def close(self) -> None:
        """Reject further mutations.

        Nothing else is released here: each operation already closed its own
        store, so the service owns no connection to tear down.
        """
        with self._lock:
            self._closed = True

    def _raise_if_closed(self) -> None:
        with self._lock:
            if self._closed:
                raise ClosedError("session metadata service is closed")

    async def _context(self, project_id: str) -> SessionProjectContext:
        context = await asyncio.to_thread(self._context_provider, project_id)
        if type(context) is not SessionProjectContext:
            raise InvalidRequestError(
                "project context provider returned an invalid context"
            )
        return context

    def _store(self, context: SessionProjectContext) -> SessionMetadataStore:
        store = self._store_provider(context.settings)
        if store is None:
            raise InvalidRequestError("session metadata store is unavailable")
        return store

    async def create(self, command: CreateSessionCommand) -> CreateSessionResult:
        """Create (idempotently) one persisted session metadata row."""
        if type(command) is not CreateSessionCommand:
            raise InvalidRequestError(
                "create command must be a CreateSessionCommand, "
                f"got type {type(command).__name__!r}"
            )
        from synapse.sessions.store import allocate_thread_id

        thread_id = command.thread_id or allocate_thread_id()
        ref = SessionRef(project_id=command.project_id, thread_id=thread_id)
        self._raise_if_closed()
        async with self._mutation_lock:
            context = await self._context(command.project_id)
            store = self._store(context)
            item, created = await asyncio.to_thread(
                store.ensure, thread_id, title=command.title
            )
        return CreateSessionResult(
            command_id=command.command_id,
            session=ref,
            created=created,
            title=item.title,
        )

    async def rename(self, command: RenameSessionCommand) -> RenameSessionResult:
        """Rename one session's title; a missing session is ``NotFoundError``."""
        if type(command) is not RenameSessionCommand:
            raise InvalidRequestError(
                "rename command must be a RenameSessionCommand, "
                f"got type {type(command).__name__!r}"
            )
        self._raise_if_closed()
        async with self._mutation_lock:
            context = await self._context(command.session.project_id)
            store = self._store(context)
            item = await asyncio.to_thread(
                store.rename, command.session.thread_id, command.title
            )
        if item is None:
            raise NotFoundError("session not found")
        return RenameSessionResult(
            command_id=command.command_id,
            session=command.session,
            title=item.title,
            renamed=True,
        )

    async def touch(
        self, session: SessionRef, *, title_hint: str | None = None
    ) -> SessionMetadataItem:
        """Bind one turn's user message as the session title while it is a placeholder.

        This is the TUI's rule (``store.touch(title_hint=...)`` on every turn) moved
        where the metadata lives, so every consumer gets a named session without
        re-implementing the derivation: the first user message names the session,
        and a title the user typed later is never overwritten.
        """
        self._raise_if_closed()
        async with self._mutation_lock:
            context = await self._context(session.project_id)
            store = self._store(context)
            item = await asyncio.to_thread(
                store.touch, session.thread_id, title_hint=title_hint
            )
        return item

    async def delete(self, command: DeleteSessionCommand) -> DeleteSessionResult:
        """Delete one session and its conversation (busy rejected).

        With a live manager the delete is atomic against a running turn and a
        concurrent open/submit; without one the metadata row is the only
        in-process state, so it is removed directly.

        The row is not the session: the conversation lives in the checkpoint
        store, the transcript projection and the full-text search index, all
        three of which outlive a row deletion and stay readable by thread id
        (``search_session``'s full-text branch reads the index without ever
        consulting the metadata row, so a deleted session would still be
        findable).  ``purge_thread`` therefore runs before the row is removed and
        its report is returned: a store that refused is named in
        ``purge_failures`` and ``retained_history`` stays ``True``.
        """
        if type(command) is not DeleteSessionCommand:
            raise InvalidRequestError(
                "delete command must be a DeleteSessionCommand, "
                f"got type {type(command).__name__!r}"
            )
        self._raise_if_closed()
        async with self._mutation_lock:
            context = await self._context(command.session.project_id)
            store = self._store(context)
            thread_id = command.session.thread_id
            purge = _thread_purge(context.settings, thread_id)
            gate = getattr(context.manager, "delete_session_ref", None)
            if callable(gate):
                try:
                    deleted, report = await gate(
                        command.session,
                        delete_metadata=lambda: store.delete(thread_id),
                        purge_history=purge,
                    )
                except SessionBusyError as exc:
                    raise ConflictError(str(exc)) from exc
                except RuntimeClosedError as exc:
                    raise ClosedError(str(exc)) from exc
                except ValueError as exc:
                    raise InvalidRequestError(str(exc)) from exc
            else:
                # No live manager: nothing in this process holds the thread, so
                # the purge runs first here too -- the row must not disappear
                # while the conversation is still findable.
                report = await asyncio.to_thread(purge)
                deleted = await asyncio.to_thread(store.delete, thread_id)
        return DeleteSessionResult(
            command_id=command.command_id,
            session=command.session,
            deleted=bool(deleted),
            # A missing report is not evidence that the history is gone, so it is
            # reported as retained rather than assumed erased.
            retained_history=report is None or not report.complete,
            purge_failures=report.failures if report is not None else (),
        )

    async def search(self, query: SearchSessionsQuery) -> SessionSearchPage:
        """Read-only bounded search; never creates a database or an agent."""
        if type(query) is not SearchSessionsQuery:
            raise InvalidRequestError(
                "search query must be a SearchSessionsQuery, "
                f"got type {type(query).__name__!r}"
            )
        context = await self._context(query.project_id)
        return await asyncio.to_thread(search_sessions_readonly, context.settings, query)
