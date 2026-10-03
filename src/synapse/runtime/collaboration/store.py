"""One transactional SQLite mailbox, independent of graphs and browser connections.

Public actor arguments must come from a trusted adapter, not from a model's
arguments. Participant checks here are defense in depth, not project ACLs.
Claimed work is never automatically requeued: a crash can hide external effects.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Self

from synapse.runtime.collaboration.model import (
    MAX_BODY_BYTES,
    MAX_MESSAGES,
    CapacityError,
    CollaborationError,
    ConflictError,
    ContinuationScope,
    Delivery,
    DeliveryState,
    Message,
    MessageKind,
    NotFoundError,
    Request,
    RequestState,
    SchemaVersionError,
    ScopeState,
    bounded_text,
    check_transition,
    optional_text,
    page_size,
    validate_ref,
)
from synapse.runtime.sessions.ref import SessionRef

_SCHEMA_VERSION = 2
#: Bound for a delivery's short machine-readable reason (never free-form prose).
_MAX_REASON_BYTES = 64
_SCHEMA = (
    """CREATE TABLE scopes (
        id TEXT PRIMARY KEY, project TEXT NOT NULL, thread TEXT NOT NULL,
        work_id TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
        UNIQUE(project, thread, work_id))""",
    """CREATE TABLE requests (
        id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES scopes(id),
        source_project TEXT NOT NULL, source_thread TEXT NOT NULL,
        target_project TEXT NOT NULL, target_thread TEXT NOT NULL,
        operation_key TEXT NOT NULL, body TEXT NOT NULL, anchor_turn_id TEXT NOT NULL,
        state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
        created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL,
        continuation TEXT NOT NULL DEFAULT 'active',
        control_version INTEGER NOT NULL DEFAULT 0,
        UNIQUE(scope_id, operation_key))""",
    """CREATE TABLE messages (
        id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests(id),
        sequence INTEGER NOT NULL, sender_project TEXT NOT NULL, sender_thread TEXT NOT NULL,
        kind TEXT NOT NULL, body TEXT NOT NULL, operation_key TEXT NOT NULL,
        anchor_turn_id TEXT NOT NULL, created_at_ms INTEGER NOT NULL,
        UNIQUE(request_id, sequence), UNIQUE(request_id, sender_project, sender_thread,
        operation_key))""",
    """CREATE TABLE deliveries (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
        message_id TEXT NOT NULL UNIQUE REFERENCES messages(id),
        request_id TEXT NOT NULL REFERENCES requests(id),
        recipient_project TEXT NOT NULL, recipient_thread TEXT NOT NULL,
        scope_id TEXT NOT NULL REFERENCES scopes(id), state TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 0, turn_id TEXT, reason TEXT)""",
    """CREATE UNIQUE INDEX one_delivery_owner ON deliveries(recipient_project, recipient_thread)
        WHERE state IN ('claimed', 'running', 'review')""",
    """CREATE INDEX mailbox ON deliveries(recipient_project, recipient_thread, state, sequence)""",
    'CREATE INDEX request_scope ON requests(scope_id)',
)


def _id() -> str:
    return uuid.uuid4().hex


def _now() -> int:
    return time.time_ns() // 1_000_000


def _version(value: int) -> None:
    if type(value) is not int or value < 0:
        raise CollaborationError('invalid expected version')


def _scope(row: sqlite3.Row) -> ContinuationScope:
    return ContinuationScope(
        row['id'], SessionRef(row['project'], row['thread']), row['work_id'],
        ScopeState(row['state']), row['version'],
    )


def _request(row: sqlite3.Row) -> Request:
    return Request(
        row['id'], row['scope_id'], SessionRef(row['source_project'], row['source_thread']),
        SessionRef(row['target_project'], row['target_thread']), row['operation_key'],
        row['body'], row['anchor_turn_id'], RequestState(row['state']), row['version'],
        row['created_at_ms'], row['updated_at_ms'],
        ScopeState(row['continuation']), row['control_version'],
    )


def _message(row: sqlite3.Row) -> Message:
    return Message(
        row['id'], row['request_id'], row['sequence'],
        SessionRef(row['sender_project'], row['sender_thread']), MessageKind(row['kind']),
        row['body'], row['operation_key'], row['anchor_turn_id'], row['created_at_ms'],
    )


def _delivery(row: sqlite3.Row) -> Delivery:
    return Delivery(
        row['id'], row['message_id'], row['request_id'],
        SessionRef(row['recipient_project'], row['recipient_thread']), row['scope_id'],
        DeliveryState(row['state']), row['version'], row['turn_id'], row['reason'], row['sequence'],
    )


class CollaborationStore:
    """Serialized transactions, including across separately opened connections.

    The daemon will own the parent directory's privacy and lifetime. No runtime
    is started by constructing this store. Unknown schemas are refused, not migrated.
    """

    def __init__(self, path: Path | str, *, max_pending: int = 256) -> None:
        if type(max_pending) is not int or not 1 <= max_pending <= 4096:
            raise CollaborationError('invalid mailbox capacity')
        path = Path(path)
        if path.is_symlink():
            raise CollaborationError('database must not be a symlink')
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._max_pending = max_pending
        self._db = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute('PRAGMA foreign_keys=ON')
            with self._transaction() as db:
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if version not in (0, 1, _SCHEMA_VERSION):
                    raise SchemaVersionError('unsupported collaboration schema')
                if version == 0:
                    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                        raise SchemaVersionError('unversioned database is not empty')
                    for statement in _SCHEMA:
                        db.execute(statement)
                    db.execute(f'PRAGMA user_version={_SCHEMA_VERSION}')
                elif version == 1:
                    db.execute('DROP INDEX one_active_scope')
                    db.execute("ALTER TABLE requests ADD COLUMN continuation TEXT NOT NULL "
                               "DEFAULT 'active'")
                    db.execute('ALTER TABLE requests ADD COLUMN control_version INTEGER NOT NULL '
                               'DEFAULT 0')
                    db.execute(f'PRAGMA user_version={_SCHEMA_VERSION}')
            self._db.execute('PRAGMA journal_mode=WAL')
            self._db.execute('PRAGMA synchronous=FULL')
        except BaseException:
            self._db.close()
            self._closed = True
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise CollaborationError('store is closed')
            self._db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    @staticmethod
    def _read_scope(db: sqlite3.Connection, scope_id: str) -> ContinuationScope:
        bounded_text(scope_id)
        row = db.execute('SELECT * FROM scopes WHERE id=?', (scope_id,)).fetchone()
        if row is None:
            raise NotFoundError('scope not found')
        return _scope(row)

    @staticmethod
    def _read_request(db: sqlite3.Connection, request_id: str, actor: SessionRef) -> Request:
        bounded_text(request_id)
        validate_ref(actor)
        row = db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
        if row is None:
            raise NotFoundError('request not found')
        request = _request(row)
        if actor not in (request.source, request.target):
            raise NotFoundError('request not found')
        return request

    def get_scope(self, scope_id: str, actor: SessionRef) -> ContinuationScope:
        validate_ref(actor)
        with self._transaction(write=False) as db:
            scope = self._read_scope(db, scope_id)
            if scope.source != actor:
                raise NotFoundError('scope not found')
            return scope

    def active_scope(self, source: SessionRef) -> ContinuationScope | None:
        """The source session's current generation, when it has one."""
        validate_ref(source)
        with self._transaction(write=False) as db:
            row = db.execute(
                "SELECT * FROM scopes WHERE project=? AND thread=? AND state='active' "
                'ORDER BY rowid DESC LIMIT 1',
                (source.project_id, source.thread_id),
            ).fetchone()
            return None if row is None else _scope(row)

    def begin_scope(self, source: SessionRef, work_id: str) -> ContinuationScope:
        """Open a collaboration group without retiring other ongoing work.

        Ordinary user turns do not revoke assignments or their reply permission.
        The work id groups assignments; it is not the session's idle/busy state.
        """
        validate_ref(source)
        bounded_text(work_id)
        with self._transaction() as db:
            existing = db.execute(
                'SELECT * FROM scopes WHERE project=? AND thread=? AND work_id=?',
                (source.project_id, source.thread_id, work_id),
            ).fetchone()
            if existing is not None:
                return _scope(existing)
            scope_id = _id()
            db.execute(
                "INSERT INTO scopes(id,project,thread,work_id,state) VALUES (?,?,?,?,'active')",
                (scope_id, source.project_id, source.thread_id, work_id),
            )
            return self._read_scope(db, scope_id)

    def restrict_scope(
        self, scope_id: str, actor: SessionRef, state: ScopeState, *, expected_version: int,
    ) -> ContinuationScope:
        """Persist the stop fence; actual active-turn cancellation belongs to P2."""
        validate_ref(actor)
        _version(expected_version)
        with self._transaction() as db:
            scope = self._read_scope(db, scope_id)
            if scope.source != actor:
                raise NotFoundError('scope not found')
            if scope.version != expected_version:
                raise ConflictError('scope version changed')
            check_transition(scope.state, state)
            self._restrict_scope(db, scope, state)
            return self._read_scope(db, scope_id)

    def _restrict_scope(
        self, db: sqlite3.Connection, scope: ContinuationScope, state: ScopeState,
    ) -> None:
        db.execute('UPDATE scopes SET state=?,version=version+1 WHERE id=?', (state, scope.id))
        # A receipt is still a message even when its execution is cancelled.
        where = 'scope_id=?'
        args: tuple[object, ...] = (scope.id,)
        if state is ScopeState.RECEIVE_ONLY:
            # Only the automatic *result* continuation lapses. A question still
            # reaches the source, because a question nobody reads strands the
            # target; a result that arrives later is recorded, not acted on.
            where += ' AND recipient_project=? AND recipient_thread=?'
            args += (scope.source.project_id, scope.source.thread_id)
            where += (
                " AND message_id IN (SELECT id FROM messages m WHERE "
                "m.request_id=deliveries.request_id AND m.kind='reply')"
            )
        db.execute(
            f"UPDATE deliveries SET state='cancelled',version=version+1,reason='scope_disabled' "
            f"WHERE {where} AND state='pending'", args,
        )
        # A claimed delivery may already have crossed the external start boundary.
        # Preserve that uncertainty, including its exclusive recipient ownership.
        db.execute(
            f"UPDATE deliveries SET state='review',version=version+1,reason='scope_disabled' "
            f"WHERE {where} AND state='claimed'", args,
        )
        if state is ScopeState.REVOKED:
            db.execute(
                "UPDATE requests SET state='cancelled',version=version+1,updated_at_ms=? "
                "WHERE scope_id=? AND state='pending' AND NOT EXISTS "
                "(SELECT 1 FROM deliveries d WHERE d.request_id=requests.id "
                "AND d.state IN ('claimed','running','review'))", (_now(), scope.id),
            )

    def get_request(self, request_id: str, actor: SessionRef) -> Request:
        with self._transaction(write=False) as db:
            return self._read_request(db, request_id, actor)

    def control_request(
        self, request_id: str, source: SessionRef, *, expected_version: int,
        receive_only: bool,
    ) -> Request:
        """Change one assignment, never the session's other work."""
        _version(expected_version)
        with self._transaction() as db:
            request = self._read_request(db, request_id, source)
            if request.source != source:
                raise NotFoundError('assignment not found')
            if request.control_version != expected_version:
                raise ConflictError('assignment control changed')
            state = ScopeState.RECEIVE_ONLY if receive_only else ScopeState.REVOKED
            check_transition(request.continuation, state)
            db.execute('UPDATE requests SET continuation=?,control_version=control_version+1,'
                       'updated_at_ms=? WHERE id=?', (state, _now(), request.id))
            where = 'request_id=?'
            args: tuple[object, ...] = (request.id,)
            if receive_only:
                where += ' AND recipient_project=? AND recipient_thread=?'
                args += (source.project_id, source.thread_id)
            db.execute(f"UPDATE deliveries SET state='cancelled',version=version+1,"
                       f"reason='assignment_disabled' WHERE {where} AND state='pending'", args)
            db.execute(f"UPDATE deliveries SET state='review',version=version+1,"
                       f"reason='assignment_disabled' WHERE {where} AND state='claimed'", args)
            if not receive_only and request.state in (
                RequestState.PENDING, RequestState.WORKING, RequestState.WAITING_INPUT,
            ):
                db.execute("UPDATE requests SET state='cancelled',version=version+1 WHERE id=?",
                           (request.id,))
            return self._read_request(db, request.id, source)

    def request_deliveries(self, request_id: str, actor: SessionRef) -> tuple[Delivery, ...]:
        """Execution facts for one visible assignment, bounded by its message limit."""
        with self._transaction(write=False) as db:
            self._read_request(db, request_id, actor)
            return tuple(_delivery(row) for row in db.execute(
                'SELECT * FROM deliveries WHERE request_id=? ORDER BY sequence LIMIT ?',
                (request_id, MAX_MESSAGES),
            ))

    def link_resumed_turn(self, delivery_id: str, recipient: SessionRef, turn_id: str) -> None:
        """Replace the execution link only for an authorized approval resume."""
        bounded_text(turn_id)
        with self._transaction() as db:
            row = db.execute('SELECT * FROM deliveries WHERE id=?', (delivery_id,)).fetchone()
            if row is None or _delivery(row).recipient != recipient:
                raise NotFoundError('delivery not found')
            delivery = _delivery(row)
            if delivery.turn_id == turn_id:
                return
            request = self._read_request(db, delivery.request_id, recipient)
            scope = self._read_scope(db, request.scope_id)
            if (delivery.state is not DeliveryState.RUNNING
                    or request.continuation is ScopeState.REVOKED
                    or scope.state is ScopeState.REVOKED
                    or (recipient == request.source and (
                        request.continuation is not ScopeState.ACTIVE
                        or scope.state is not ScopeState.ACTIVE
                    ))):
                raise ConflictError('assignment no longer permits execution')
            db.execute('UPDATE deliveries SET turn_id=?,version=version+1 WHERE id=?',
                       (turn_id, delivery_id))

    def create_request(
        self, scope_id: str, source: SessionRef, target: SessionRef, *,
        operation_key: str, body: str, anchor_turn_id: str,
    ) -> Request:
        validate_ref(source)
        validate_ref(target)
        if source == target:
            raise CollaborationError('cannot request the same session')
        bounded_text(operation_key)
        bounded_text(body, maximum=MAX_BODY_BYTES)
        optional_text(anchor_turn_id)
        with self._transaction() as db:
            scope = self._read_scope(db, scope_id)
            if scope.source != source:
                raise NotFoundError('scope not found')
            existing = db.execute(
                'SELECT * FROM requests WHERE scope_id=? AND operation_key=?',
                (scope_id, operation_key),
            ).fetchone()
            if existing is not None:
                request = _request(existing)
                if (request.target, request.body, request.anchor_turn_id) != (
                    target, body, anchor_turn_id,
                ):
                    raise ConflictError('operation key already has different content')
                return request
            if scope.state is not ScopeState.ACTIVE:
                raise ConflictError('scope is not active')
            now, request_id = _now(), _id()
            db.execute(
                'INSERT INTO requests(id,scope_id,source_project,source_thread,target_project,'
                'target_thread,operation_key,body,anchor_turn_id,state,'
                'created_at_ms,updated_at_ms) '
                "VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)",
                (request_id, scope_id, source.project_id, source.thread_id, target.project_id,
                 target.thread_id, operation_key, body, anchor_turn_id, now, now),
            )
            request = self._read_request(db, request_id, source)
            self._append(
                db, request, source, MessageKind.REQUEST, body, operation_key, anchor_turn_id,
            )
            return request

    def _append(
        self, db: sqlite3.Connection, request: Request, sender: SessionRef, kind: MessageKind,
        body: str, operation_key: str, anchor_turn_id: str,
    ) -> Message:
        count = db.execute(
            'SELECT COUNT(*) FROM messages WHERE request_id=?', (request.id,),
        ).fetchone()[0]
        # Reserve the last slot for an explicit final reply, even after many
        # clarification/supplement exchanges. The initial request counts too.
        ceiling = MAX_MESSAGES if kind is MessageKind.REPLY else MAX_MESSAGES - 1
        if count >= ceiling:
            raise CapacityError('request message limit reached')
        recipient = request.target if sender == request.source else request.source
        scope = self._read_scope(db, request.scope_id)
        executable = (
            kind is not MessageKind.PROGRESS
            and request.continuation is not ScopeState.REVOKED
            and (recipient != request.source or (
                scope.state is ScopeState.ACTIVE
                and request.continuation is ScopeState.ACTIVE
            ))
        )
        if executable:
            count_pending = db.execute(
                "SELECT COUNT(*) FROM deliveries WHERE recipient_project=? AND recipient_thread=? "
                "AND state IN ('pending','claimed','running','review')",
                (recipient.project_id, recipient.thread_id),
            ).fetchone()[0]
            if count_pending >= self._max_pending:
                raise CapacityError('recipient mailbox is full')
        message_id = _id()
        db.execute(
            'INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)',
            (message_id, request.id, count + 1, sender.project_id, sender.thread_id,
             kind, body, operation_key, anchor_turn_id, _now()),
        )
        db.execute(
            'INSERT INTO deliveries(id,message_id,request_id,recipient_project,recipient_thread,'
            'scope_id,state,reason) VALUES (?,?,?,?,?,?,?,?)',
            (_id(), message_id, request.id, recipient.project_id, recipient.thread_id,
             request.scope_id, ('pending' if executable else
                               'settled' if kind is MessageKind.PROGRESS else 'cancelled'),
             None if executable else ('notification' if kind is MessageKind.PROGRESS
                                      else 'scope_disabled')),
        )
        return _message(db.execute('SELECT * FROM messages WHERE id=?', (message_id,)).fetchone())

    @staticmethod
    def _cancel_pending_clarifications(db: sqlite3.Connection, request_id: str) -> None:
        """Retire unanswered receipts without erasing claimed or running work."""
        db.execute(
            "UPDATE deliveries SET state='cancelled',version=version+1,reason='superseded' "
            "WHERE request_id=? AND state='pending' AND message_id IN "
            "(SELECT id FROM messages WHERE request_id=? AND kind='clarification')",
            (request_id, request_id),
        )

    def post_message(
        self, request_id: str, actor: SessionRef, *, kind: MessageKind, body: str,
        operation_key: str, anchor_turn_id: str,
    ) -> Message:
        """Save a reply/clarification plus its delivery in the same transaction."""
        if type(kind) is not MessageKind or kind is MessageKind.REQUEST:
            raise CollaborationError('invalid response kind')
        bounded_text(body, maximum=MAX_BODY_BYTES)
        bounded_text(operation_key)
        optional_text(anchor_turn_id)
        with self._transaction() as db:
            request = self._read_request(db, request_id, actor)
            required_actor = request.source if kind is MessageKind.SUPPLEMENT else request.target
            if actor != required_actor:
                raise ConflictError('message role is not allowed')
            existing = db.execute(
                'SELECT * FROM messages WHERE request_id=? AND sender_project=? '
                'AND sender_thread=? AND operation_key=?',
                (request_id, actor.project_id, actor.thread_id, operation_key),
            ).fetchone()
            if existing is not None:
                message = _message(existing)
                if (message.kind, message.body, message.anchor_turn_id) != (
                    kind, body, anchor_turn_id,
                ):
                    raise ConflictError('operation key already has different content')
                return message
            if kind is MessageKind.PROGRESS:
                if request.state not in (RequestState.WORKING, RequestState.WAITING_INPUT):
                    raise ConflictError('assignment is not running')
                message = self._append(
                    db, request, actor, kind, body, operation_key, anchor_turn_id,
                )
                db.execute('UPDATE requests SET updated_at_ms=? WHERE id=?',
                           (_now(), request.id))
                return message
            next_state = {
                MessageKind.CLARIFICATION: RequestState.WAITING_INPUT,
                MessageKind.SUPPLEMENT: RequestState.PENDING,
                MessageKind.REPLY: RequestState.REPLIED,
            }[kind]
            if kind is MessageKind.SUPPLEMENT:
                scope = self._read_scope(db, request.scope_id)
                if (scope.state is not ScopeState.ACTIVE
                        or request.continuation is not ScopeState.ACTIVE):
                    raise ConflictError('scope is not active')
            check_transition(request.state, next_state)
            if kind in (MessageKind.SUPPLEMENT, MessageKind.REPLY):
                self._cancel_pending_clarifications(db, request.id)
            message = self._append(db, request, actor, kind, body, operation_key, anchor_turn_id)
            db.execute(
                'UPDATE requests SET state=?,version=version+1,updated_at_ms=? WHERE id=?',
                (next_state, _now(), request.id),
            )
            return message

    def messages(
        self, request_id: str, actor: SessionRef, *, after: int = 0, limit: int = 50,
    ) -> tuple[Message, ...]:
        page_size(limit)
        if type(after) is not int or not 0 <= after <= MAX_MESSAGES:
            raise CollaborationError('invalid message cursor')
        with self._transaction(write=False) as db:
            self._read_request(db, request_id, actor)
            return tuple(_message(row) for row in db.execute(
                'SELECT * FROM messages WHERE request_id=? AND sequence>? '
                'ORDER BY sequence LIMIT ?',
                (request_id, after, limit),
            ))

    def fail_request(
        self, request_id: str, actor: SessionRef, *, expected_version: int,
    ) -> Request:
        """Record a target's explicit failure; settling a delivery alone never implies success.

        A waiting-input request may fail if the target abandons it. A pending
        request must first be started or cancelled, not silently called failed.
        """
        _version(expected_version)
        with self._transaction() as db:
            request = self._read_request(db, request_id, actor)
            if actor != request.target:
                raise ConflictError('only target can report failure')
            if request.version != expected_version:
                raise ConflictError('request version changed')
            check_transition(request.state, RequestState.FAILED)
            self._cancel_pending_clarifications(db, request.id)
            db.execute(
                "UPDATE requests SET state='failed',version=version+1,updated_at_ms=? WHERE id=?",
                (_now(), request_id),
            )
            return self._read_request(db, request_id, actor)

    def deliveries(
        self, recipient: SessionRef, *, after: int = 0, limit: int = 50,
    ) -> tuple[Delivery, ...]:
        """Bounded recipient feed; service adapters must authorize the recipient."""
        validate_ref(recipient)
        page_size(limit)
        if type(after) is not int or not 0 <= after < 2**63:
            raise CollaborationError('invalid delivery cursor')
        with self._transaction(write=False) as db:
            return tuple(_delivery(row) for row in db.execute(
                'SELECT * FROM deliveries WHERE recipient_project=? AND recipient_thread=? '
                'AND sequence>? ORDER BY sequence LIMIT ?',
                (recipient.project_id, recipient.thread_id, after, limit),
            ))

    def claim_next(self, recipient: SessionRef) -> Delivery | None:
        """Claim oldest ready item, never stepping past unresolved recipient ownership."""
        validate_ref(recipient)
        with self._transaction() as db:
            args = (recipient.project_id, recipient.thread_id)
            if db.execute(
                "SELECT 1 FROM deliveries WHERE recipient_project=? AND recipient_thread=? "
                "AND state IN ('claimed','running','review') LIMIT 1", args,
            ).fetchone():
                return None
            row = db.execute(
                "SELECT * FROM deliveries WHERE recipient_project=? AND recipient_thread=? "
                "AND state='pending' ORDER BY sequence LIMIT 1", args,
            ).fetchone()
            if row is None:
                return None
            delivery = _delivery(row)
            db.execute(
                "UPDATE deliveries SET state='claimed',version=version+1 WHERE id=?",
                (delivery.id,),
            )
            return _delivery(db.execute(
                'SELECT * FROM deliveries WHERE id=?', (delivery.id,),
            ).fetchone())

    def transition_delivery(
        self, delivery_id: str, recipient: SessionRef, state: DeliveryState, *,
        expected_version: int, turn_id: str | None = None, reason: str | None = None,
    ) -> Delivery:
        """CAS transition for the trusted runtime adapter, not an Agent tool API.

        REVIEW -> SETTLED requires the adapter to establish external execution
        evidence first. It releases ownership but never replays the work.
        """
        bounded_text(delivery_id)
        validate_ref(recipient)
        _version(expected_version)
        if turn_id is not None:
            bounded_text(turn_id)
        if reason is not None:
            bounded_text(reason, maximum=_MAX_REASON_BYTES)
        with self._transaction() as db:
            row = db.execute('SELECT * FROM deliveries WHERE id=?', (delivery_id,)).fetchone()
            if row is None or _delivery(row).recipient != recipient:
                raise NotFoundError('delivery not found')
            delivery = _delivery(row)
            if delivery.version != expected_version:
                raise ConflictError('delivery version changed')
            check_transition(delivery.state, state)
            if state is DeliveryState.RUNNING and turn_id is None:
                raise CollaborationError('running delivery requires a turn identity')
            if delivery.turn_id is not None and turn_id not in (None, delivery.turn_id):
                raise ConflictError('delivery already bound to another turn')
            request = self._read_request(db, delivery.request_id, recipient)
            if state is DeliveryState.RUNNING:
                scope = self._read_scope(db, delivery.scope_id)
                if (scope.state is ScopeState.REVOKED
                        or request.continuation is ScopeState.REVOKED) or (
                    recipient == request.source and (
                        scope.state is not ScopeState.ACTIVE
                        or request.continuation is not ScopeState.ACTIVE
                    )
                ):
                    raise ConflictError('scope no longer permits execution')
                kind = MessageKind(db.execute(
                    'SELECT kind FROM messages WHERE id=?', (delivery.message_id,),
                ).fetchone()['kind'])
                allowed = {
                    MessageKind.REQUEST: (recipient == request.target and
                                          request.state is RequestState.PENDING),
                    MessageKind.SUPPLEMENT: (recipient == request.target and
                                             request.state is RequestState.PENDING),
                    MessageKind.CLARIFICATION: (recipient == request.source and
                                                request.state is RequestState.WAITING_INPUT),
                    MessageKind.REPLY: (recipient == request.source and
                                        request.state is RequestState.REPLIED),
                }
                if not allowed[kind]:
                    raise ConflictError('request no longer permits execution')
                if recipient == request.target and request.state is RequestState.PENDING:
                    db.execute(
                        "UPDATE requests SET state='working',version=version+1,updated_at_ms=? "
                        'WHERE id=?', (_now(), request.id),
                    )
            db.execute(
                'UPDATE deliveries SET state=?,version=version+1,'
                'turn_id=COALESCE(turn_id,?),reason=COALESCE(?,reason) WHERE id=?',
                (state, turn_id, reason, delivery.id),
            )
            return _delivery(db.execute(
                'SELECT * FROM deliveries WHERE id=?', (delivery.id,),
            ).fetchone())

    def recover_uncertain(self, *, limit: int = 100) -> int:
        """Startup-only, after exclusive daemon ownership; never call on a live worker.

        A crashed claim/run needs evidence before its recipient can run again.
        Call in bounded batches until zero. Reopening the database alone has no
        execution side effects and does not silently release claims.
        """
        page_size(limit)
        with self._transaction() as db:
            rows = db.execute(
                "SELECT id FROM deliveries WHERE state IN ('claimed','running') "
                'ORDER BY sequence LIMIT ?', (limit,),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE deliveries SET state='review',version=version+1,reason='restart' "
                    'WHERE id=?', (row['id'],),
                )
            return len(rows)

    # -- trusted runtime adapter surface ------------------------------------

    def get_delivery(self, delivery_id: str, recipient: SessionRef) -> Delivery:
        """Read one delivery the recipient owns; never another session's work."""
        bounded_text(delivery_id)
        validate_ref(recipient)
        with self._transaction(write=False) as db:
            row = db.execute(
                'SELECT * FROM deliveries WHERE id=?', (delivery_id,),
            ).fetchone()
            if row is None or _delivery(row).recipient != recipient:
                raise NotFoundError('delivery not found')
            return _delivery(row)

    def execution_context(
        self, delivery_id: str,
    ) -> tuple[Request, ContinuationScope, Message]:
        """Trusted adapter only: request, scope and message behind one delivery.

        Deliberately not participant-checked: the delivery worker acts for the
        daemon and the recipient already owns the delivery it claimed. Every
        decision it takes from this snapshot is re-validated by a CAS write, so
        the read is an optimization, not the authority.
        """
        bounded_text(delivery_id)
        with self._transaction(write=False) as db:
            row = db.execute(
                'SELECT * FROM deliveries WHERE id=?', (delivery_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError('delivery not found')
            delivery = _delivery(row)
            request_row = db.execute(
                'SELECT * FROM requests WHERE id=?', (delivery.request_id,),
            ).fetchone()
            message_row = db.execute(
                'SELECT * FROM messages WHERE id=?', (delivery.message_id,),
            ).fetchone()
            if request_row is None or message_row is None:
                raise NotFoundError('delivery record is incomplete')
            return (
                _request(request_row),
                self._read_scope(db, delivery.scope_id),
                _message(message_row),
            )

    def running_deliveries(self, scope_id: str) -> tuple[Delivery, ...]:
        """Trusted adapter only: deliveries of one scope that own a live turn."""
        bounded_text(scope_id)
        with self._transaction(write=False) as db:
            return tuple(_delivery(row) for row in db.execute(
                "SELECT * FROM deliveries WHERE scope_id=? AND state='running' "
                'ORDER BY sequence', (scope_id,),
            ))

    def pending_recipients(self, *, limit: int = 50) -> tuple[SessionRef, ...]:
        """Recipients with claimable work and no unresolved owner, oldest first."""
        page_size(limit)
        with self._transaction(write=False) as db:
            rows = db.execute(
                'SELECT recipient_project, recipient_thread, MIN(sequence) AS seq '
                "FROM deliveries WHERE state='pending' AND NOT EXISTS ("
                'SELECT 1 FROM deliveries owned WHERE '
                'owned.recipient_project=deliveries.recipient_project AND '
                'owned.recipient_thread=deliveries.recipient_thread AND '
                "owned.state IN ('claimed','running','review')) "
                'GROUP BY recipient_project, recipient_thread ORDER BY seq LIMIT ?',
                (limit,),
            ).fetchall()
            return tuple(
                SessionRef(row['recipient_project'], row['recipient_thread']) for row in rows
            )

    def abandon_claim(
        self, delivery_id: str, recipient: SessionRef, *, expected_version: int, reason: str,
    ) -> Delivery:
        """Release a claim whose turn provably never started; never a replay."""
        bounded_text(reason, maximum=_MAX_REASON_BYTES)
        return self.transition_delivery(
            delivery_id, recipient, DeliveryState.CANCELLED,
            expected_version=expected_version, reason=reason,
        )

    def resolve_uncertain(
        self, delivery_id: str, recipient: SessionRef, *, expected_version: int, reason: str,
    ) -> Delivery:
        """Close a REVIEW delivery once the adapter holds execution evidence.

        The evidence is that the turn is over: settlement was observed, or the
        adapter cancelled it itself. This releases the recipient's mailbox
        without ever re-submitting the message.
        """
        bounded_text(reason, maximum=_MAX_REASON_BYTES)
        return self.transition_delivery(
            delivery_id, recipient, DeliveryState.SETTLED,
            expected_version=expected_version, reason=reason,
        )

    def settle_interrupted(self, *, limit: int = 100) -> int:
        """Startup-only, after ``recover_uncertain`` and exclusive ownership.

        The previous process is gone, so none of its turns is still executing:
        an interrupted delivery is finished, not replayable. This records that
        instead of re-submitting, and fails a request whose target never
        answered rather than leaving it pending forever.
        """
        page_size(limit)
        with self._transaction() as db:
            rows = db.execute(
                "SELECT * FROM deliveries WHERE state='review' ORDER BY sequence LIMIT ?",
                (limit,),
            ).fetchall()
            for row in rows:
                delivery = _delivery(row)
                db.execute(
                    "UPDATE deliveries SET state='settled',version=version+1,"
                    "reason='interrupted' WHERE id=?", (delivery.id,),
                )
                request_row = db.execute(
                    'SELECT * FROM requests WHERE id=?', (delivery.request_id,),
                ).fetchone()
                if request_row is None:
                    continue
                request = _request(request_row)
                if delivery.recipient != request.target or request.state not in (
                    RequestState.PENDING, RequestState.WORKING,
                ):
                    continue
                db.execute(
                    "UPDATE requests SET state='failed',version=version+1,updated_at_ms=? "
                    'WHERE id=?', (_now(), request.id),
                )
            return len(rows)

    def reconcile_stopped_requests(self, *, limit: int = 100) -> int:
        """Cancel requests whose scope stopped and whose deliveries are resolved.

        Called by the worker loop in bounded batches: a request left pending when
        its generation was revoked would otherwise never be delivered nor closed.
        """
        page_size(limit)
        with self._transaction() as db:
            rows = db.execute(
                'SELECT r.id FROM requests r JOIN scopes s ON s.id=r.scope_id '
                "WHERE r.state='pending' AND s.state!='active' AND NOT EXISTS ("
                'SELECT 1 FROM deliveries d WHERE d.request_id=r.id AND '
                "d.state IN ('pending','claimed','running','review')) LIMIT ?", (limit,),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE requests SET state='cancelled',version=version+1,updated_at_ms=? "
                    'WHERE id=?', (_now(), row['id']),
                )
            return len(rows)

    # -- read surface for the console ---------------------------------------

    def requests_for_session(
        self, session: SessionRef, *, limit: int = 20,
    ) -> tuple[Request, ...]:
        """Requests this session takes part in, newest activity first."""
        validate_ref(session)
        page_size(limit)
        with self._transaction(write=False) as db:
            rows = db.execute(
                'SELECT * FROM requests WHERE (source_project=? AND source_thread=?) '
                'OR (target_project=? AND target_thread=?) '
                'ORDER BY updated_at_ms DESC, id DESC LIMIT ?',
                (session.project_id, session.thread_id) * 2 + (limit,),
            ).fetchall()
            return tuple(_request(row) for row in rows)

    def recent_messages(
        self, request_id: str, actor: SessionRef, *, limit: int = 8,
    ) -> tuple[Message, ...]:
        """The newest ``limit`` messages of one request, oldest first."""
        page_size(limit)
        with self._transaction(write=False) as db:
            self._read_request(db, request_id, actor)
            rows = db.execute(
                'SELECT * FROM messages WHERE request_id=? ORDER BY sequence DESC LIMIT ?',
                (request_id, limit),
            ).fetchall()
            return tuple(_message(row) for row in reversed(rows))

    def request_counts(self, request_id: str) -> tuple[int, int]:
        """``(messages, unresolved deliveries)`` for one request."""
        bounded_text(request_id)
        with self._transaction(write=False) as db:
            messages = db.execute(
                'SELECT COUNT(*) FROM messages WHERE request_id=?', (request_id,),
            ).fetchone()[0]
            unresolved = db.execute(
                "SELECT COUNT(*) FROM deliveries WHERE request_id=? AND "
                "state NOT IN ('settled','cancelled')", (request_id,),
            ).fetchone()[0]
            return int(messages), int(unresolved)
