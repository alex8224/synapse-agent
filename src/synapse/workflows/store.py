"""SQLite persistence for workflow drafts, runs, calls and events.

One connection per store, guarded by a lock, following ``synapse.sessions.store``.  The
store owns every *durable* workflow decision, so the rules a resumed run depends on are
enforced in one place rather than trusted to callers:

- a run can only be created from an approved draft (and only one run per project is
  active at a time);
- ``start_call`` refuses a duplicate key, a run that is not executing, and a dispatch
  beyond ``max_calls`` / ``max_actors``, so a hard limit cannot be exceeded by
  construction;
- a call only moves forward, and a call left in flight by a crash becomes ``UNCERTAIN``
  instead of being retried implicitly.

Results are stored as JSON text and read through dedicated accessors, so listing runs or
calls never deserializes them.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from synapse.workflows.contract import (
    ACTIVE_RUN_STATUSES,
    CallRequest,
    CallStatus,
    DraftStatus,
    WorkflowDraft,
    WorkflowLimits,
    WorkflowStatus,
    can_transition,
)
from synapse.workflows.errors import (
    BudgetExceededError,
    DuplicateCallKeyError,
    InvalidDraftError,
    WorkflowError,
    WorkflowStateError,
)
from synapse.workflows.records import CallRecord, WorkflowEvent, WorkflowRun

__all__ = ["WorkflowStore", "utcnow"]

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS workflow_drafts (
        workflow_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        goal TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL,
        script_hash TEXT NOT NULL,
        roles_json TEXT NOT NULL DEFAULT '[]',
        limits_json TEXT NOT NULL,
        approved_hash TEXT,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_runs (
        run_id TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        script_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        limits_json TEXT NOT NULL,
        inputs_json TEXT,
        result_json TEXT,
        error TEXT,
        calls INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        approved_at TEXT,
        finished_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS workflow_runs_project "
    "ON workflow_runs (project_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS workflow_calls (
        run_id TEXT NOT NULL,
        call_key TEXT NOT NULL,
        actor_key TEXT NOT NULL,
        role TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        status TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        result_json TEXT,
        error TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        PRIMARY KEY (run_id, call_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_events (
        run_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (run_id, sequence)
    )
    """,
)

#: Call columns shared by every read, so a new column cannot be forgotten in one place.
_CALL_COLUMNS = (
    "run_id, call_key, actor_key, role, fingerprint, status, sequence, attempts, "
    "input_tokens, output_tokens, error, started_at, finished_at"
)

#: Columns added after the first release of this feature.  An older database is migrated
#: in place instead of being refused, because a workflow database is durable state a user
#: may already have runs in.
_ADDED_CALL_COLUMNS: dict[str, str] = {
    "input_tokens": "INTEGER NOT NULL DEFAULT 0",
    "output_tokens": "INTEGER NOT NULL DEFAULT 0",
}

#: Run columns added after the first release, migrated the same way.
_ADDED_RUN_COLUMNS: dict[str, str] = {
    "inputs_json": "TEXT",
}

_RUN_COLUMNS = (
    "run_id, workflow_id, project_id, thread_id, revision, script_hash, status, "
    "limits_json, calls, error, created_at, updated_at, approved_at, finished_at"
)


def utcnow() -> str:
    """Second-resolution UTC timestamp, matching the other stores' format."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _dump(value: Any) -> str:
    """Serialize a workflow value, refusing anything JSON cannot round-trip.

    A workflow value crosses a process boundary (worker, daemon, console) and is stored
    for a later resume, so a value that cannot survive JSON is a script bug worth
    reporting rather than something to coerce.
    """
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise WorkflowError(
            f"workflow value of type {type(value).__name__!r} is not JSON-serializable"
        ) from exc


def _load(text: str | None) -> Any:
    return None if text is None else json.loads(text)


class WorkflowStore:
    """Durable workflow state for one project's workflow database."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Autocommit mode: every write goes through ``_write`` below, which takes the
        # write lock up front (see its docstring for why that matters here).
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # The orchestration checkpointer writes this same database from the worker's
            # own connection, so WAL plus a busy timeout keeps a short write from failing
            # as "database is locked".  These two pragmas run in autocommit: SQLite
            # refuses a journal-mode change from inside a transaction.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        with self._write():
            for statement in _SCHEMA:
                self._conn.execute(statement)
            self._ensure_columns()

    def _ensure_columns(self) -> None:
        """Add columns an older workflow database predates (called inside a write)."""
        for table, columns in (
            ("workflow_calls", _ADDED_CALL_COLUMNS),
            ("workflow_runs", _ADDED_RUN_COLUMNS),
        ):
            existing = {
                str(row["name"])
                for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, declaration in columns.items():
                if name not in existing:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                    )

    @contextmanager
    def _write(self) -> Iterator[None]:
        """Run one write transaction with the write lock taken immediately.

        ``BEGIN IMMEDIATE`` is not a micro-optimisation here.  The orchestration
        checkpointer writes this same database from the worker's own connection, and
        SQLite refuses to *wait* for a lock upgrade: a transaction that reads first and
        writes second gets an immediate ``database is locked``, ignoring
        ``busy_timeout``.  Taking the write lock first means ``busy_timeout`` applies and
        the two writers simply queue.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> WorkflowStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- drafts ------------------------------------------------------------

    def save_draft(self, draft: WorkflowDraft) -> WorkflowDraft:
        """Insert or replace one draft revision.

        Replacing a draft whose revision *decreased* would silently resurrect an older
        program, so the stored revision may only move forward.
        """
        with self._write():
            row = self._conn.execute(
                "SELECT revision FROM workflow_drafts WHERE workflow_id = ?",
                (draft.workflow_id,),
            ).fetchone()
            if row is not None and int(row["revision"]) > draft.revision:
                raise InvalidDraftError(
                    "draft revision must not move backwards "
                    f"({row['revision']} -> {draft.revision})"
                )
            self._conn.execute(
                """
                INSERT INTO workflow_drafts (
                    workflow_id, project_id, thread_id, revision, title, goal, source,
                    script_hash, roles_json, limits_json, approved_hash, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    revision = excluded.revision,
                    title = excluded.title,
                    goal = excluded.goal,
                    source = excluded.source,
                    script_hash = excluded.script_hash,
                    roles_json = excluded.roles_json,
                    limits_json = excluded.limits_json,
                    approved_hash = excluded.approved_hash,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    draft.workflow_id,
                    draft.project_id,
                    draft.thread_id,
                    draft.revision,
                    draft.title,
                    draft.goal,
                    draft.source,
                    draft.script_hash,
                    _dump(list(draft.roles)),
                    _dump(draft.limits.to_json()),
                    draft.approved_hash,
                    str(draft.status),
                    draft.created_at or utcnow(),
                    draft.updated_at or utcnow(),
                ),
            )
        return draft

    def get_draft(self, workflow_id: str) -> WorkflowDraft | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_drafts WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
        return None if row is None else self._draft_row(row)

    def list_drafts(self, project_id: str, *, limit: int = 20) -> list[WorkflowDraft]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_drafts WHERE project_id = ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (project_id, max(1, int(limit))),
            ).fetchall()
        return [self._draft_row(row) for row in rows]

    def approve_draft(
        self, workflow_id: str, *, revision: int, approved_hash: str, at: str | None = None
    ) -> WorkflowDraft:
        """Approve exactly one revision, refusing a stale or mismatched request."""
        stamp = at or utcnow()
        with self._write():
            draft = self._require_draft(workflow_id)
            if draft.revision != revision:
                raise InvalidDraftError(
                    f"draft revision changed ({draft.revision} != {revision})"
                )
            approved = draft.approve(approved_hash=approved_hash, at=stamp)
            self._write_draft(approved)
        return approved

    def discard_draft(self, workflow_id: str, *, at: str | None = None) -> WorkflowDraft:
        stamp = at or utcnow()
        with self._write():
            draft = self._require_draft(workflow_id)
            discarded = draft.discard(at=stamp)
            self._write_draft(discarded)
        return discarded

    # -- runs --------------------------------------------------------------

    def create_run(
        self,
        workflow_id: str,
        *,
        run_id: str,
        inputs: Any = None,
        at: str | None = None,
    ) -> WorkflowRun:
        """Create the run for an approved draft; one active run per project.

        The draft is re-read from the store instead of taken from the caller, so a stale
        in-memory object can never start a program the user did not approve.
        """
        stamp = at or utcnow()
        with self._write():
            draft = self._require_draft(workflow_id)
            draft.require_approved()
            active = self._active_run_row(draft.project_id)
            if active is not None:
                raise WorkflowStateError(
                    "this project already has an active workflow run"
                )
            self._conn.execute(
                """
                INSERT INTO workflow_runs (
                    run_id, workflow_id, project_id, thread_id, revision, script_hash,
                    status, limits_json, inputs_json, calls, created_at, updated_at,
                    approved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    run_id,
                    draft.workflow_id,
                    draft.project_id,
                    draft.thread_id,
                    draft.revision,
                    draft.script_hash,
                    str(WorkflowStatus.RUNNING),
                    _dump(draft.limits.to_json()),
                    _dump(inputs),
                    stamp,
                    stamp,
                    stamp,
                ),
            )
            row = self._run_row(run_id)
        return self._run(row)

    def get_run(self, run_id: str) -> WorkflowRun | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM workflow_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return None if row is None else self._run(row)

    def list_runs(self, project_id: str, *, limit: int = 20) -> list[WorkflowRun]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM workflow_runs WHERE project_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (project_id, max(1, int(limit))),
            ).fetchall()
        return [self._run(row) for row in rows]

    def active_run(self, project_id: str) -> WorkflowRun | None:
        with self._lock:
            row = self._active_run_row(project_id)
        return None if row is None else self._run(row)

    def set_run_status(
        self,
        run_id: str,
        status: WorkflowStatus,
        *,
        error: str | None = None,
        result: Any = None,
        at: str | None = None,
    ) -> WorkflowRun:
        """Move a run through the state machine, recording its terminal payload."""
        stamp = at or utcnow()
        with self._write():
            run = self._require_run(run_id)
            if run.status is status:
                raise WorkflowStateError(f"run is already {status}")
            if not can_transition(run.status, status):
                raise WorkflowStateError(
                    f"cannot move run from {run.status} to {status}"
                )
            finished = stamp if status in _FINISHED else None
            self._conn.execute(
                "UPDATE workflow_runs SET status = ?, error = ?, result_json = ?, "
                "updated_at = ?, finished_at = COALESCE(?, finished_at) WHERE run_id = ?",
                (
                    str(status),
                    error,
                    None if result is None else _dump(result),
                    stamp,
                    finished,
                    run_id,
                ),
            )
            row = self._run_row(run_id)
        return self._run(row)

    def run_result(self, run_id: str) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT result_json FROM workflow_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise WorkflowStateError(f"unknown workflow run {run_id!r}")
        return _load(row["result_json"])

    def run_inputs(self, run_id: str) -> Any:
        """The inputs this run started with.

        Durable on purpose: a resume must execute the same program with the same inputs,
        or a recorded call would be matched against a different request.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT inputs_json FROM workflow_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise WorkflowStateError(f"unknown workflow run {run_id!r}")
        return _load(row["inputs_json"])

    # -- calls -------------------------------------------------------------

    def start_call(
        self,
        run_id: str,
        request: CallRequest,
        *,
        at: str | None = None,
    ) -> CallRecord:
        """Record the dispatch of one call, enforcing the run's hard limits."""
        stamp = at or utcnow()
        with self._write():
            run = self._require_run(run_id)
            if run.status is not WorkflowStatus.RUNNING:
                raise WorkflowStateError(
                    f"cannot dispatch a call while the run is {run.status}"
                )
            if run.calls >= run.limits.max_calls:
                raise BudgetExceededError(
                    f"workflow reached its call limit ({run.limits.max_calls})"
                )
            existing = self._conn.execute(
                "SELECT call_key FROM workflow_calls WHERE run_id = ? AND call_key = ?",
                (run_id, request.call_key),
            ).fetchone()
            if existing is not None:
                raise DuplicateCallKeyError(
                    f"call key {request.call_key!r} is already recorded for this run"
                )
            known_actor = self._conn.execute(
                "SELECT 1 FROM workflow_calls WHERE run_id = ? AND actor_key = ? LIMIT 1",
                (run_id, request.actor_key),
            ).fetchone()
            if known_actor is None:
                actors = int(
                    self._conn.execute(
                        "SELECT COUNT(DISTINCT actor_key) AS n FROM workflow_calls "
                        "WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()["n"]
                )
                if actors >= run.limits.max_actors:
                    raise BudgetExceededError(
                        f"workflow reached its actor limit ({run.limits.max_actors})"
                    )
            sequence = run.calls + 1
            self._conn.execute(
                """
                INSERT INTO workflow_calls (
                    run_id, call_key, actor_key, role, fingerprint, status, sequence,
                    attempts, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    run_id,
                    request.call_key,
                    request.actor_key,
                    request.role,
                    request.fingerprint(),
                    str(CallStatus.RUNNING),
                    sequence,
                    stamp,
                ),
            )
            self._conn.execute(
                "UPDATE workflow_runs SET calls = ?, updated_at = ? WHERE run_id = ?",
                (sequence, stamp, run_id),
            )
            row = self._call_row(run_id, request.call_key)
        return self._call(row)

    def get_call(self, run_id: str, call_key: str) -> CallRecord | None:
        with self._lock:
            row = self._call_row(run_id, call_key)
        return None if row is None else self._call(row)

    def list_calls(self, run_id: str) -> list[CallRecord]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_CALL_COLUMNS} FROM workflow_calls WHERE run_id = ? "
                "ORDER BY sequence ASC",
                (run_id,),
            ).fetchall()
        return [self._call(row) for row in rows]

    def call_result(self, run_id: str, call_key: str) -> Any:
        with self._lock:
            # ``_CALL_COLUMNS`` deliberately omits the result, so listing calls never
            # deserializes one; this accessor asks for it explicitly.
            row = self._conn.execute(
                "SELECT result_json FROM workflow_calls WHERE run_id = ? AND call_key = ?",
                (run_id, call_key),
            ).fetchone()
        if row is None:
            raise WorkflowStateError(
                f"unknown call {call_key!r} for run {run_id!r}"
            )
        return _load(row["result_json"])

    def record_call_usage(
        self,
        run_id: str,
        call_key: str,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Attach one call's token usage to its record.

        Recorded by whoever ran the call, because that is the only place the numbers
        exist.  The call is still ``RUNNING`` at that point, so the update is additive:
        usage is known before the result is, and a failed call still cost tokens.
        """
        with self._write():
            self._conn.execute(
                "UPDATE workflow_calls SET input_tokens = ?, output_tokens = ? "
                "WHERE run_id = ? AND call_key = ?",
                (
                    max(0, int(input_tokens)),
                    max(0, int(output_tokens)),
                    run_id,
                    call_key,
                ),
            )

    def usage_totals(self, run_id: str) -> tuple[int, int]:
        """``(input_tokens, output_tokens)`` recorded for one run so far."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(input_tokens), 0) AS i, "
                "COALESCE(SUM(output_tokens), 0) AS o "
                "FROM workflow_calls WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row["i"]), int(row["o"])

    def complete_call(
        self,
        run_id: str,
        call_key: str,
        *,
        result: Any,
        attempts: int,
        at: str | None = None,
    ) -> CallRecord:
        """Commit a call's validated result; this is the reuse boundary."""
        return self._finish_call(
            run_id,
            call_key,
            status=CallStatus.COMPLETED,
            result=result,
            error=None,
            attempts=attempts,
            at=at,
        )

    def fail_call(
        self,
        run_id: str,
        call_key: str,
        *,
        error: str,
        attempts: int,
        at: str | None = None,
    ) -> CallRecord:
        return self._finish_call(
            run_id,
            call_key,
            status=CallStatus.FAILED,
            result=None,
            error=error,
            attempts=attempts,
            at=at,
        )

    def mark_call_uncertain(
        self,
        run_id: str,
        call_key: str,
        *,
        reason: str,
        at: str | None = None,
    ) -> CallRecord:
        """Record that a started call's outcome cannot be established."""
        return self._finish_call(
            run_id,
            call_key,
            status=CallStatus.UNCERTAIN,
            result=None,
            error=reason,
            attempts=None,
            at=at,
        )

    def _finish_call(
        self,
        run_id: str,
        call_key: str,
        *,
        status: CallStatus,
        result: Any,
        error: str | None,
        attempts: int | None,
        at: str | None,
    ) -> CallRecord:
        stamp = at or utcnow()
        with self._write():
            row = self._call_row(run_id, call_key)
            if row is None:
                raise WorkflowStateError(
                    f"unknown call {call_key!r} for run {run_id!r}"
                )
            current = CallStatus(row["status"])
            if current is not CallStatus.RUNNING:
                raise WorkflowStateError(
                    f"call {call_key!r} is already {current} and cannot become {status}"
                )
            self._conn.execute(
                "UPDATE workflow_calls SET status = ?, result_json = ?, error = ?, "
                "attempts = COALESCE(?, attempts), finished_at = ? "
                "WHERE run_id = ? AND call_key = ?",
                (
                    str(status),
                    None if result is None else _dump(result),
                    error,
                    attempts,
                    stamp,
                    run_id,
                    call_key,
                ),
            )
            updated = self._call_row(run_id, call_key)
        return self._call(updated)

    # -- events ------------------------------------------------------------

    def append_event(
        self,
        run_id: str,
        kind: str,
        payload: Any = None,
        *,
        at: str | None = None,
    ) -> WorkflowEvent:
        stamp = at or utcnow()
        with self._write():
            row = self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS n FROM workflow_events "
                "WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            sequence = int(row["n"]) + 1
            self._conn.execute(
                "INSERT INTO workflow_events (run_id, sequence, kind, payload_json, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, sequence, kind, _dump(payload), stamp),
            )
        return WorkflowEvent(
            run_id=run_id,
            sequence=sequence,
            kind=kind,
            payload=payload,
            created_at=stamp,
        )

    def read_events(
        self, run_id: str, *, after: int = 0, limit: int = 200
    ) -> list[WorkflowEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, sequence, kind, payload_json, created_at "
                "FROM workflow_events WHERE run_id = ? AND sequence > ? "
                "ORDER BY sequence ASC LIMIT ?",
                (run_id, int(after), max(1, int(limit))),
            ).fetchall()
        return [
            WorkflowEvent(
                run_id=row["run_id"],
                sequence=int(row["sequence"]),
                kind=row["kind"],
                payload=_load(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def approval_decisions(self, run_id: str) -> dict[str, bool]:
        """The last recorded decision for each business-approval key.

        Read from the event log rather than kept in a second table: the decision is
        already durable there, and a resumed run must not ask the user the same question
        again when the answer is on record.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, payload_json FROM workflow_events "
                "WHERE run_id = ? AND kind IN (?, ?) ORDER BY sequence ASC",
                (run_id, "approval.granted", "approval.rejected"),
            ).fetchall()
        decisions: dict[str, bool] = {}
        for row in rows:
            payload = _load(row["payload_json"])
            if not isinstance(payload, dict):
                continue
            key = payload.get("key")
            if isinstance(key, str) and key:
                decisions[key] = row["kind"] == "approval.granted"
        return decisions

    def count_calls(self, run_id: str) -> int:
        """How many call records this run holds (compared against its own counter)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM workflow_calls WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row["n"])

    # -- internals ---------------------------------------------------------

    def _write_draft(self, draft: WorkflowDraft) -> None:
        self._conn.execute(
            "UPDATE workflow_drafts SET source = ?, script_hash = ?, title = ?, "
            "goal = ?, roles_json = ?, limits_json = ?, approved_hash = ?, status = ?, "
            "updated_at = ? WHERE workflow_id = ?",
            (
                draft.source,
                draft.script_hash,
                draft.title,
                draft.goal,
                _dump(list(draft.roles)),
                _dump(draft.limits.to_json()),
                draft.approved_hash,
                str(draft.status),
                draft.updated_at,
                draft.workflow_id,
            ),
        )

    def _require_draft(self, workflow_id: str) -> WorkflowDraft:
        row = self._conn.execute(
            "SELECT * FROM workflow_drafts WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()
        if row is None:
            raise InvalidDraftError(f"unknown workflow draft {workflow_id!r}")
        return self._draft_row(row)

    def _require_run(self, run_id: str) -> WorkflowRun:
        row = self._run_row(run_id)
        if row is None:
            raise WorkflowStateError(f"unknown workflow run {run_id!r}")
        return self._run(row)

    def _run_row(self, run_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM workflow_runs WHERE run_id = ?", (run_id,)
        ).fetchone()

    def _call_row(self, run_id: str, call_key: str) -> sqlite3.Row | None:
        return self._conn.execute(
            f"SELECT {_CALL_COLUMNS} FROM workflow_calls WHERE run_id = ? AND call_key = ?",
            (run_id, call_key),
        ).fetchone()

    def _active_run_row(self, project_id: str) -> sqlite3.Row | None:
        placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)
        return self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM workflow_runs "
            f"WHERE project_id = ? AND status IN ({placeholders}) LIMIT 1",
            (project_id, *sorted(str(status) for status in ACTIVE_RUN_STATUSES)),
        ).fetchone()

    @staticmethod
    def _draft_row(row: sqlite3.Row) -> WorkflowDraft:
        return WorkflowDraft(
            workflow_id=row["workflow_id"],
            project_id=row["project_id"],
            thread_id=row["thread_id"],
            revision=int(row["revision"]),
            title=row["title"],
            goal=row["goal"],
            source=row["source"],
            roles=tuple(_load(row["roles_json"]) or ()),
            limits=WorkflowLimits.from_json(_load(row["limits_json"])),
            approved_hash=row["approved_hash"],
            status=DraftStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _run(row: sqlite3.Row) -> WorkflowRun:
        return WorkflowRun(
            run_id=row["run_id"],
            workflow_id=row["workflow_id"],
            project_id=row["project_id"],
            thread_id=row["thread_id"],
            revision=int(row["revision"]),
            script_hash=row["script_hash"],
            status=WorkflowStatus(row["status"]),
            limits=WorkflowLimits.from_json(_load(row["limits_json"])),
            calls=int(row["calls"]),
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            approved_at=row["approved_at"],
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _call(row: sqlite3.Row) -> CallRecord:
        return CallRecord(
            run_id=row["run_id"],
            call_key=row["call_key"],
            actor_key=row["actor_key"],
            role=row["role"],
            fingerprint=row["fingerprint"],
            status=CallStatus(row["status"]),
            sequence=int(row["sequence"]),
            attempts=int(row["attempts"]),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            error=row["error"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )


#: Statuses that stamp ``finished_at``.
_FINISHED = frozenset(
    {
        WorkflowStatus.CANCELLED,
        WorkflowStatus.COMPLETED,
        WorkflowStatus.FAILED,
    }
)


def calls_by_actor(calls: Iterable[CallRecord]) -> dict[str, list[CallRecord]]:
    """Group calls by actor, preserving dispatch order (a small test/UI helper)."""
    grouped: dict[str, list[CallRecord]] = {}
    for record in calls:
        grouped.setdefault(record.actor_key, []).append(record)
    return grouped
