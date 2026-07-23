"""Idempotent, compensating import of safe Codex text snapshots.

The ledger intentionally lives beside Synapse session metadata rather than in
Codex state. It coordinates two separate SQLite databases: LangGraph
checkpoints and Synapse session metadata. It cannot make them atomic, so each
pending import carries a short lease and is reconciled by verifying the seeded
terminal checkpoint before marking the import complete.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from synapse.checkpoint_seed import CheckpointSeeder, CheckpointSeedError
from synapse.codex_history import PARSER_VERSION, PROJECTION_KIND
from synapse.sessions import SessionInfo, SessionStore, title_from_user_message

if TYPE_CHECKING:
    from synapse.codex_history import CodexTextSnapshot

_LEASE_SECONDS = 120


class CodexImportError(RuntimeError):
    """A Codex snapshot cannot be imported or recovered safely."""


@dataclass(frozen=True)
class CodexImportResult:
    """One completed or idempotently reused imported Synapse session."""

    thread_id: str
    snapshot_digest: str
    reused: bool
    recovered: bool


@dataclass(frozen=True)
class _LedgerEntry:
    source_id: str
    snapshot_digest: str
    thread_id: str
    status: str
    lease_until: str | None


class CodexImportLedger:
    """Durable source-to-thread mapping with short leases for crash recovery."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def close(self) -> None:
        self._conn.close()

    def claim(self, source_id: str, snapshot_digest: str, thread_id: str) -> Literal[
        "new", "completed", "recover"
    ]:
        """Claim a source, reuse completion, or take over an expired lease."""
        now = _utcnow()
        lease_until = _lease_expiry()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT * FROM codex_imports WHERE source_id = ?", (source_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO codex_imports(
                        source_id, snapshot_digest, thread_id, status, lease_until,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (source_id, snapshot_digest, thread_id, lease_until, now, now),
                )
                self._conn.commit()
                return "new"

            entry = _entry_from_row(row)
            if entry.snapshot_digest != snapshot_digest:
                raise CodexImportError("Codex source changed after its first import attempt")
            if entry.status == "completed":
                self._conn.commit()
                return "completed"
            if entry.status != "pending":
                raise CodexImportError("Codex import ledger has an invalid state")
            if not _lease_is_expired(entry.lease_until):
                raise CodexImportError("Codex import is already in progress")
            self._conn.execute(
                "UPDATE codex_imports SET lease_until = ?, updated_at = ? WHERE source_id = ?",
                (lease_until, now, source_id),
            )
            self._conn.commit()
            return "recover"
        except Exception:
            self._conn.rollback()
            raise

    def entry(self, source_id: str) -> _LedgerEntry | None:
        row = self._conn.execute(
            "SELECT * FROM codex_imports WHERE source_id = ?", (source_id,)
        ).fetchone()
        return _entry_from_row(row) if row is not None else None

    def complete(self, source_id: str, thread_id: str) -> None:
        now = _utcnow()
        updated = self._conn.execute(
            """
            UPDATE codex_imports
               SET status = 'completed', lease_until = NULL, updated_at = ?
             WHERE source_id = ? AND thread_id = ? AND status = 'pending'
            """,
            (now, source_id, thread_id),
        )
        self._conn.commit()
        if updated.rowcount != 1:
            raise CodexImportError("Codex import ledger completion was lost")

    def abandon(self, source_id: str, thread_id: str) -> None:
        """Remove an uncompleted ledger row after confirmed compensation."""
        self._conn.execute(
            """
            DELETE FROM codex_imports
             WHERE source_id = ? AND thread_id = ? AND status = 'pending'
            """,
            (source_id, thread_id),
        )
        self._conn.commit()

    def _init_db(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS codex_imports (
                source_id TEXT PRIMARY KEY,
                snapshot_digest TEXT NOT NULL,
                thread_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK(status IN ('pending', 'completed')),
                lease_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()


def import_codex_session(
    *,
    native_id: str,
    settings: Any,
    agent: Any,
    workspace: Path | None = None,
    codex_home: Path | None = None,
) -> CodexImportResult:
    """Discover, project, and import one Codex session through one agent graph."""
    from synapse.codex_history import CodexHistoryProjector
    from synapse.codex_sessions import CodexSessionScanner

    target_workspace = workspace or Path(getattr(settings, "workspace", Path.cwd()))
    session = CodexSessionScanner(codex_home).inspect(native_id, workspace=target_workspace)
    if session is None:
        raise CodexImportError(f"Codex session not found: {native_id}")
    snapshot = CodexHistoryProjector().project_path(session.rollout_path)
    if not snapshot.importable:
        warning_codes = ",".join(warning.code for warning in snapshot.warnings)
        raise CodexImportError(
            f"Codex session cannot be imported safely: {warning_codes or 'unknown'}"
        )

    sessions_path = settings.resolved_sessions_path()
    store = SessionStore(sessions_path)
    ledger = CodexImportLedger(default_codex_import_ledger_path(sessions_path))
    try:
        service = CodexImportService(
            seeder=CheckpointSeeder(agent),
            sessions=store,
            ledger=ledger,
        )
        return service.import_snapshot(
            native_id=session.native_id,
            snapshot=snapshot,
            title=session.title,
        )
    finally:
        ledger.close()
        store.close()


class CodexImportService:
    """Coordinate immutable snapshot import across checkpoint and metadata stores."""

    def __init__(
        self,
        *,
        seeder: CheckpointSeeder,
        sessions: SessionStore,
        ledger: CodexImportLedger,
    ) -> None:
        self._seeder = seeder
        self._sessions = sessions
        self._ledger = ledger

    def import_snapshot(
        self,
        *,
        native_id: str,
        snapshot: CodexTextSnapshot,
        title: str,
    ) -> CodexImportResult:
        """Create or recover exactly one Synapse session for one Codex source."""
        digest = snapshot_digest(snapshot)
        source_id = _source_id(native_id)
        proposed_thread_id = f"codex-{uuid.uuid4().hex[:20]}"
        claim = self._ledger.claim(source_id, digest, proposed_thread_id)
        entry = self._ledger.entry(source_id)
        if entry is None:
            raise CodexImportError("Codex import ledger claim disappeared")
        if claim == "completed":
            self._verify_completed(entry, snapshot)
            return CodexImportResult(entry.thread_id, digest, reused=True, recovered=False)
        if claim == "recover":
            return self._recover(entry, snapshot, title, digest)
        return self._seed_new(entry, snapshot, title, digest)

    def _seed_new(
        self,
        entry: _LedgerEntry,
        snapshot: CodexTextSnapshot,
        title: str,
        digest: str,
    ) -> CodexImportResult:
        try:
            self._seeder.seed_snapshot(entry.thread_id, snapshot)
            self._ensure_session(entry.thread_id, title)
            self._ledger.complete(entry.source_id, entry.thread_id)
        except Exception as exc:
            self._compensate_new(entry, exc)
            if isinstance(exc, CodexImportError):
                raise
            raise CodexImportError("Codex snapshot import failed") from exc
        return CodexImportResult(entry.thread_id, digest, reused=False, recovered=False)

    def _recover(
        self,
        entry: _LedgerEntry,
        snapshot: CodexTextSnapshot,
        title: str,
        digest: str,
    ) -> CodexImportResult:
        try:
            self._seeder.verify_snapshot(entry.thread_id, snapshot)
        except CheckpointSeedError as exc:
            if self._thread_exists(entry.thread_id):
                raise CodexImportError(
                    "pending import checkpoint does not match its immutable snapshot"
                ) from exc
            self._sessions.delete(entry.thread_id)
            try:
                self._seeder.seed_snapshot(entry.thread_id, snapshot)
            except Exception as seed_error:
                self._ledger.abandon(entry.source_id, entry.thread_id)
                raise CodexImportError("pending import could not be reseeded") from seed_error
        try:
            self._ensure_session(entry.thread_id, title)
            self._ledger.complete(entry.source_id, entry.thread_id)
        except Exception as exc:
            raise CodexImportError("pending import could not be reconciled") from exc
        return CodexImportResult(entry.thread_id, digest, reused=False, recovered=True)

    def _verify_completed(self, entry: _LedgerEntry, snapshot: CodexTextSnapshot) -> None:
        try:
            self._seeder.verify_snapshot(entry.thread_id, snapshot)
        except CheckpointSeedError as exc:
            raise CodexImportError(
                "completed import checkpoint no longer matches its snapshot"
            ) from exc
        if self._sessions.get(entry.thread_id) is None:
            raise CodexImportError("completed import session metadata is missing")

    def _ensure_session(self, thread_id: str, title: str) -> SessionInfo:
        safe_title = title_from_user_message(title) or "Imported Codex session"
        return self._sessions.ensure(thread_id, title=safe_title)

    def _thread_exists(self, thread_id: str) -> bool:
        return self._seeder.has_thread(thread_id)

    def _compensate_new(self, entry: _LedgerEntry, cause: Exception) -> None:
        cleanup_error: Exception | None = None
        try:
            self._sessions.delete(entry.thread_id)
            self._seeder.delete_thread(entry.thread_id)
            self._ledger.abandon(entry.source_id, entry.thread_id)
        except Exception as exc:  # noqa: BLE001
            cleanup_error = exc
        if cleanup_error is not None:
            raise CodexImportError(
                "Codex import failed and compensation was incomplete"
            ) from cleanup_error


def default_codex_import_ledger_path(sessions_path: Path | str) -> Path:
    """Place the ledger next to project-local Synapse session metadata."""
    return Path(sessions_path).expanduser().resolve().parent / "codex-imports.sqlite"


def snapshot_digest(snapshot: CodexTextSnapshot) -> str:
    """Hash the immutable supported visible-text projection, never raw rollout bytes."""
    if (
        not snapshot.importable
        or snapshot.projection_kind != PROJECTION_KIND
        or snapshot.parser_version != PARSER_VERSION
    ):
        raise CodexImportError("Codex snapshot is not importable under the current contract")
    payload = {
        "projection_kind": snapshot.projection_kind,
        "parser_version": snapshot.parser_version,
        "messages": [
            {
                "source_id": message.source_id,
                "turn_id": message.turn_id,
                "role": message.role,
                "text": message.text,
            }
            for message in snapshot.messages
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _source_id(native_id: str) -> str:
    value = native_id.strip()
    if not value or len(value) > 200 or "\x00" in value:
        raise CodexImportError("Codex native session id is invalid")
    return f"codex:{value.casefold()}"


def _entry_from_row(row: sqlite3.Row) -> _LedgerEntry:
    return _LedgerEntry(
        source_id=str(row["source_id"]),
        snapshot_digest=str(row["snapshot_digest"]),
        thread_id=str(row["thread_id"]),
        status=str(row["status"]),
        lease_until=str(row["lease_until"]) if row["lease_until"] is not None else None,
    )


def _utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _lease_expiry() -> str:
    expiry = datetime.now(UTC) + timedelta(seconds=_LEASE_SECONDS)
    return expiry.replace(microsecond=0).isoformat()


def _lease_is_expired(value: str | None) -> bool:
    if value is None:
        return True
    try:
        return datetime.fromisoformat(value) <= datetime.now(UTC)
    except ValueError:
        return False
