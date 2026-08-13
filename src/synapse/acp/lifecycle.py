"""Persistent ACP session metadata and opaque pagination."""

from __future__ import annotations

import base64
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_PAGE_SIZE = 50
_MAX_UPDATES_PER_SESSION = 2000


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def default_catalog_path() -> Path:
    """Return the user-scoped ACP catalog path without storing secrets."""
    return Path.home() / ".synapse" / "acp-sessions.sqlite"


def _encode_cursor(updated_at: str, session_id: str) -> str:
    raw = json.dumps([updated_at, session_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode())
        if not isinstance(value, list) or len(value) != 2:
            return None
        return str(value[0]), str(value[1])
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None


@dataclass(frozen=True, slots=True)
class ACPStoredSession:
    session_id: str
    thread_id: str
    cwd: Path
    additional_directories: tuple[Path, ...]
    title: str | None
    created_at: str
    updated_at: str
    mode_id: str = "default"
    config: dict[str, Any] | None = None
    mcp_required: bool = False


class ACPSessionCatalog:
    """SQLite metadata catalog shared by ACP server processes."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path).expanduser().resolve() if path else default_catalog_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS acp_sessions (
                session_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL UNIQUE,
                cwd TEXT NOT NULL,
                additional_directories_json TEXT NOT NULL DEFAULT '[]',
                title TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                mode_id TEXT NOT NULL DEFAULT 'default',
                config_json TEXT NOT NULL DEFAULT '{}',
                mcp_required INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        columns = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(acp_sessions)").fetchall()
        }
        if "mode_id" not in columns:
            self._conn.execute(
                "ALTER TABLE acp_sessions ADD COLUMN mode_id TEXT NOT NULL DEFAULT 'default'"
            )
        if "config_json" not in columns:
            self._conn.execute(
                "ALTER TABLE acp_sessions ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "mcp_required" not in columns:
            self._conn.execute(
                "ALTER TABLE acp_sessions ADD COLUMN mcp_required INTEGER NOT NULL DEFAULT 0"
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_acp_sessions_page "
            "ON acp_sessions(updated_at DESC, session_id DESC)"
        )
        self._ensure_updates_schema()
        self._conn.commit()

    def _ensure_updates_schema(self) -> None:
        """Create or migrate history storage to support projected update batches."""
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS acp_updates ("
            "session_id TEXT NOT NULL, sequence INTEGER NOT NULL, update_index INTEGER NOT NULL, "
            "update_json TEXT NOT NULL, PRIMARY KEY(session_id, sequence, update_index))"
        )
        columns = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(acp_updates)").fetchall()
        }
        if "update_index" in columns:
            return
        self._conn.execute("ALTER TABLE acp_updates RENAME TO acp_updates_legacy")
        self._conn.execute(
            "CREATE TABLE acp_updates ("
            "session_id TEXT NOT NULL, sequence INTEGER NOT NULL, update_index INTEGER NOT NULL, "
            "update_json TEXT NOT NULL, PRIMARY KEY(session_id, sequence, update_index))"
        )
        self._conn.execute(
            "INSERT INTO acp_updates(session_id, sequence, update_index, update_json) "
            "SELECT session_id, sequence, 0, update_json FROM acp_updates_legacy"
        )
        self._conn.execute("DROP TABLE acp_updates_legacy")

    def append_update(
        self, session_id: str, sequence: int, update: Any, *, update_index: int = 0
    ) -> None:
        if self.get(session_id) is None:
            return
        payload = update.model_dump(by_alias=True, exclude_none=True)
        self._ensure_updates_schema()
        self._conn.execute(
            "INSERT OR REPLACE INTO acp_updates(session_id, sequence, update_index, update_json) "
            "VALUES (?, ?, ?, ?)",
            (
                session_id,
                int(sequence),
                int(update_index),
                json.dumps(payload, separators=(",", ":")),
            ),
        )
        self._conn.execute(
            "UPDATE acp_sessions SET updated_at = ? WHERE session_id = ?",
            (_now(), session_id),
        )
        self._prune_updates(session_id)
        self._conn.commit()

    def _prune_updates(self, session_id: str) -> None:
        """Keep replay history bounded to the most recent updates per session."""
        self._conn.execute(
            "DELETE FROM acp_updates WHERE session_id = ? AND rowid NOT IN ("
            "SELECT rowid FROM acp_updates WHERE session_id = ? "
            "ORDER BY sequence DESC, update_index DESC LIMIT ?)",
            (session_id, session_id, _MAX_UPDATES_PER_SESSION),
        )

    def updates(self, session_id: str) -> tuple[dict[str, Any], ...]:
        rows = self._conn.execute(
            "SELECT update_json FROM acp_updates WHERE session_id = ? "
            "ORDER BY sequence, update_index",
            (session_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                value = json.loads(row["update_json"])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                result.append(value)
        return tuple(result)

    def next_update_sequence(self, session_id: str) -> int:
        """Return the next sequence after all persisted runtime and ACP updates."""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS last_sequence "
            "FROM acp_updates WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["last_sequence"] or 0) + 1

    def close(self) -> None:
        self._conn.close()

    def create(
        self,
        *,
        cwd: Path,
        additional_directories: tuple[Path, ...] = (),
        title: str | None = None,
        session_id: str | None = None,
        mode_id: str = "default",
        config: dict[str, Any] | None = None,
        mcp_required: bool = False,
    ) -> ACPStoredSession:
        now = _now()
        value = ACPStoredSession(
            session_id=session_id or f"sess_{uuid.uuid4().hex}",
            thread_id=session_id or "",
            cwd=cwd,
            additional_directories=additional_directories,
            title=title,
            created_at=now,
            updated_at=now,
            mode_id=mode_id,
            config=dict(config or {}),
            mcp_required=mcp_required,
        )
        if not value.thread_id:
            value = ACPStoredSession(
                session_id=value.session_id,
                thread_id=value.session_id,
                cwd=value.cwd,
                additional_directories=value.additional_directories,
                title=value.title,
                created_at=value.created_at,
                updated_at=value.updated_at,
                mode_id=value.mode_id,
                config=value.config,
                mcp_required=value.mcp_required,
            )
        self._conn.execute(
            """
            INSERT INTO acp_sessions(
                session_id, thread_id, cwd, additional_directories_json,
                title, created_at, updated_at, mode_id, config_json, mcp_required
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                value.session_id,
                value.thread_id,
                str(value.cwd),
                json.dumps([str(item) for item in value.additional_directories]),
                value.title,
                value.created_at,
                value.updated_at,
                value.mode_id,
                json.dumps(value.config or {}, separators=(",", ":")),
                int(value.mcp_required),
            ),
        )
        self._conn.commit()
        return value

    def get(self, session_id: str) -> ACPStoredSession | None:
        row = self._conn.execute(
            "SELECT * FROM acp_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return self._row(row) if row else None

    def touch(self, session_id: str, *, title: str | None = None) -> ACPStoredSession | None:
        current = self.get(session_id)
        if current is None:
            return None
        updated = _now()
        self._conn.execute(
            "UPDATE acp_sessions SET title = COALESCE(?, title), updated_at = ? "
            "WHERE session_id = ?",
            (title, updated, session_id),
        )
        self._conn.commit()
        return self.get(session_id)

    def delete(self, session_id: str) -> bool:
        result = self._conn.execute(
            "DELETE FROM acp_sessions WHERE session_id = ?", (session_id,)
        )
        self._conn.execute("DELETE FROM acp_updates WHERE session_id = ?", (session_id,))
        self._conn.commit()
        return result.rowcount > 0

    def update_config(self, session_id: str, key: str, value: Any) -> ACPStoredSession | None:
        current = self.get(session_id)
        if current is None:
            return None
        config = dict(current.config or {})
        config[key] = value
        return self.replace_config(session_id, config)

    def replace_config(
        self, session_id: str, config: dict[str, Any]
    ) -> ACPStoredSession | None:
        """Replace one session config atomically for update rollback paths."""
        if self.get(session_id) is None:
            return None
        self._conn.execute(
            "UPDATE acp_sessions SET config_json = ?, updated_at = ? WHERE session_id = ?",
            (json.dumps(config, separators=(",", ":")), _now(), session_id),
        )
        self._conn.commit()
        return self.get(session_id)

    def update_mode(self, session_id: str, mode_id: str) -> ACPStoredSession | None:
        if self.get(session_id) is None:
            return None
        self._conn.execute(
            "UPDATE acp_sessions SET mode_id = ?, updated_at = ? WHERE session_id = ?",
            (mode_id, _now(), session_id),
        )
        self._conn.commit()
        return self.get(session_id)

    def fork(
        self,
        session_id: str,
        *,
        cwd: Path,
        additional_directories: tuple[Path, ...],
    ) -> ACPStoredSession | None:
        source = self.get(session_id)
        if source is None:
            return None
        return self.create(
            cwd=cwd,
            additional_directories=additional_directories,
            title=source.title,
            mode_id=source.mode_id,
            config=source.config,
            mcp_required=source.mcp_required,
        )

    def list_page(
        self,
        *,
        cwd: Path | None = None,
        cursor: str | None = None,
        limit: int = _PAGE_SIZE,
    ) -> tuple[list[ACPStoredSession], str | None]:
        marker = _decode_cursor(cursor)
        params: list[Any] = []
        clauses: list[str] = []
        if cwd is not None:
            clauses.append("cwd = ?")
            params.append(str(cwd))
        if marker is not None:
            clauses.append("(updated_at < ? OR (updated_at = ? AND session_id < ?))")
            params.extend([marker[0], marker[0], marker[1]])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._conn.execute(
            "SELECT * FROM acp_sessions" + where
            + " ORDER BY updated_at DESC, session_id DESC LIMIT ?",
            (*params, max(1, min(int(limit), _PAGE_SIZE)) + 1),
        ).fetchall()
        items = [self._row(row) for row in rows[:_PAGE_SIZE]]
        next_cursor = None
        if len(rows) > _PAGE_SIZE and items:
            last = items[-1]
            next_cursor = _encode_cursor(last.updated_at, last.session_id)
        return items, next_cursor

    def _row(self, row: sqlite3.Row) -> ACPStoredSession:
        try:
            directories = json.loads(row["additional_directories_json"] or "[]")
        except json.JSONDecodeError:
            directories = []
        if not isinstance(directories, list):
            directories = []
        try:
            config = json.loads(row["config_json"] or "{}")
        except (KeyError, TypeError, json.JSONDecodeError):
            config = {}
        if not isinstance(config, dict):
            config = {}
        return ACPStoredSession(
            session_id=str(row["session_id"]),
            thread_id=str(row["thread_id"]),
            cwd=Path(str(row["cwd"])),
            additional_directories=tuple(Path(str(item)) for item in directories),
            title=row["title"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            mode_id=str(row["mode_id"] or "default"),
            config=config,
            mcp_required=bool(row["mcp_required"] if "mcp_required" in row.keys() else 0),
        )
