"""Session metadata store (product shell around LangGraph checkpointer).

LangGraph/deepagents persist conversation state by thread_id.
This module stores human-facing metadata: title, timestamps, model binding, tags.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass
class ModelBinding:
    """Model + thinking preference bound to a session (or last-used prefs)."""

    active_model: str | None = None  # profile alias, e.g. "deep"
    model: str | None = None  # concrete id, e.g. "openai:deepseek-v4-pro"
    thinking: str | None = None  # off|minimal|low|medium|high|max|on

    def has_data(self) -> bool:
        return bool(self.active_model or self.model or self.thinking)

    def display(self) -> str:
        mid = (self.active_model or self.model or "-").strip() or "-"
        if self.thinking:
            return f"{mid} · {self.thinking}"
        return mid

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_model": self.active_model,
            "model": self.model,
            "thinking": self.thinking,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ModelBinding:
        if not data:
            return cls()
        return cls(
            active_model=_opt_str(data.get("active_model")),
            model=_opt_str(data.get("model")),
            thinking=_opt_str(data.get("thinking")),
        )


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def is_default_session_title(title: str | None, thread_id: str) -> bool:
    """True when title is still a placeholder (not first-user-message bound)."""
    text = (title or "").strip()
    if not text:
        return True
    if text == thread_id:
        return True
    if text.startswith("session "):
        return True
    if text.casefold() in {"session", "new session", "untitled"}:
        return True
    return False


def title_from_user_message(text: str | None, *, max_len: int = 80) -> str | None:
    """Normalize first user message into a session title."""
    if not text:
        return None
    one = " ".join(str(text).strip().split())
    if not one:
        return None
    return one[:max_len]


@dataclass
class SessionInfo:
    thread_id: str
    title: str
    model: str | None
    created_at: str
    updated_at: str
    tags: list[str]
    summary: str | None = None
    active_model: str | None = None
    thinking: str | None = None

    def binding(self) -> ModelBinding:
        return ModelBinding(
            active_model=self.active_model,
            model=self.model,
            thinking=self.thinking,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "title": self.title,
            "model": self.model,
            "active_model": self.active_model,
            "thinking": self.thinking,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tags": self.tags,
            "summary": self.summary,
        }


class SessionStore:
    """SQLite-backed session metadata."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                thread_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                model TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                summary TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prefs (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self._ensure_column("active_model", "TEXT")
        self._ensure_column("thinking", "TEXT")
        self._conn.commit()

    def _ensure_column(self, name: str, decl: str) -> None:
        cols = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if name not in cols:
            self._conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {decl}")

    def close(self) -> None:
        self._conn.close()

    def ensure(
        self,
        thread_id: str,
        *,
        title: str | None = None,
        model: str | None = None,
        active_model: str | None = None,
        thinking: str | None = None,
    ) -> SessionInfo:
        existing = self.get(thread_id)
        if existing is not None:
            return existing
        now = _utcnow()
        created_title = title_from_user_message(title) or (
            title.strip()[:120] if title else f"session {thread_id}"
        )
        info = SessionInfo(
            thread_id=thread_id,
            title=created_title[:120],
            model=model,
            created_at=now,
            updated_at=now,
            tags=[],
            summary=None,
            active_model=active_model,
            thinking=thinking,
        )
        self._conn.execute(
            """
            INSERT INTO sessions(
                thread_id, title, model, created_at, updated_at,
                tags_json, summary, active_model, thinking
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                info.thread_id,
                info.title,
                info.model,
                info.created_at,
                info.updated_at,
                "[]",
                None,
                info.active_model,
                info.thinking,
            ),
        )
        self._conn.commit()
        return info

    def touch(
        self,
        thread_id: str,
        *,
        title_hint: str | None = None,
        model: str | None = None,
        active_model: str | None = None,
        thinking: str | None = None,
    ) -> SessionInfo:
        info = self.ensure(
            thread_id,
            title=title_hint,
            model=model,
            active_model=active_model,
            thinking=thinking,
        )
        now = _utcnow()
        title = info.title
        # Bind first user message as title while still placeholder.
        hint = title_from_user_message(title_hint)
        if hint and is_default_session_title(title, thread_id):
            title = hint
        self._conn.execute(
            """
            UPDATE sessions
            SET updated_at = ?,
                title = ?,
                model = COALESCE(?, model),
                active_model = COALESCE(?, active_model),
                thinking = COALESCE(?, thinking)
            WHERE thread_id = ?
            """,
            (now, title, model, active_model, thinking, thread_id),
        )
        self._conn.commit()
        return self.get(thread_id)  # type: ignore[return-value]

    def resolve_session_ref(self, token: str, *, limit: int = 100) -> SessionInfo | None:
        """Resolve thread_id / unique id-prefix / title to a session.

        Matching order:
        1. exact thread_id
        2. unique thread_id prefix
        3. exact title (case-insensitive)
        4. unique title prefix
        5. unique title substring
        """
        query = " ".join((token or "").strip().split())
        if not query:
            return None
        exact = self.get(query)
        if exact is not None:
            return exact

        items = self.list(limit=max(1, limit))
        q = query.casefold()

        id_hits = [s for s in items if s.thread_id.casefold().startswith(q)]
        if len(id_hits) == 1:
            return id_hits[0]
        if len(id_hits) > 1:
            # Prefer exact-casefold id if present among prefixes (already handled).
            return None

        exact_title = [
            s
            for s in items
            if (s.title or "").strip().casefold() == q
            and not is_default_session_title(s.title, s.thread_id)
        ]
        if len(exact_title) == 1:
            return exact_title[0]

        prefix_title = [
            s
            for s in items
            if (s.title or "").strip().casefold().startswith(q)
            and not is_default_session_title(s.title, s.thread_id)
        ]
        if len(prefix_title) == 1:
            return prefix_title[0]

        contains_title = [
            s
            for s in items
            if q in (s.title or "").strip().casefold()
            and not is_default_session_title(s.title, s.thread_id)
        ]
        if len(contains_title) == 1:
            return contains_title[0]
        return None

    def match_sessions(self, partial: str = "", *, limit: int = 50) -> list[SessionInfo]:
        """Filter sessions by thread_id / title prefix or substring."""
        items = self.list(limit=max(limit, 50))
        query = " ".join((partial or "").strip().split())
        if not query:
            return items[:limit]
        q = query.casefold()
        out: list[SessionInfo] = []
        for s in items:
            tid = s.thread_id.casefold()
            title = (s.title or "").strip().casefold()
            if tid.startswith(q) or title.startswith(q) or (q in title):
                out.append(s)
            if len(out) >= limit:
                break
        return out

    def save_model_binding(
        self,
        thread_id: str | None,
        binding: ModelBinding,
        *,
        also_last: bool = True,
    ) -> None:
        """Persist model/thinking for a session and optionally as last-used prefs."""
        if not binding.has_data():
            return
        if thread_id:
            self.ensure(thread_id, model=binding.model)
            self._conn.execute(
                """
                UPDATE sessions
                SET updated_at = ?,
                    model = COALESCE(?, model),
                    active_model = COALESCE(?, active_model),
                    thinking = COALESCE(?, thinking)
                WHERE thread_id = ?
                """,
                (
                    _utcnow(),
                    binding.model,
                    binding.active_model,
                    binding.thinking,
                    thread_id,
                ),
            )
            self._conn.commit()
        if also_last:
            self.set_last_model_binding(binding)

    def get_model_binding(self, thread_id: str) -> ModelBinding:
        info = self.get(thread_id)
        if info is None:
            return ModelBinding()
        return info.binding()

    def set_last_model_binding(self, binding: ModelBinding) -> None:
        if not binding.has_data():
            return
        self._set_pref("last_model_binding", json.dumps(binding.to_dict(), ensure_ascii=False))

    def get_last_model_binding(self) -> ModelBinding:
        raw = self._get_pref("last_model_binding")
        if not raw:
            return ModelBinding()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return ModelBinding()
        if not isinstance(data, dict):
            return ModelBinding()
        return ModelBinding.from_dict(data)

    def _set_pref(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO prefs(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self._conn.commit()

    def _get_pref(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM prefs WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return str(row["value"])

    def get(self, thread_id: str) -> SessionInfo | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        return self._row_to_info(row) if row else None

    def list(self, *, limit: int = 50) -> list[SessionInfo]:
        rows = self._conn.execute(
            """
            SELECT * FROM sessions
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()
        return [self._row_to_info(r) for r in rows]

    def rename(self, thread_id: str, title: str) -> SessionInfo | None:
        if self.get(thread_id) is None:
            return None
        self._conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE thread_id = ?",
            (title.strip()[:120] or thread_id, _utcnow(), thread_id),
        )
        self._conn.commit()
        return self.get(thread_id)

    def delete(self, thread_id: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM sessions WHERE thread_id = ?",
            (thread_id,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def is_placeholder(self, info: SessionInfo) -> bool:
        """True when the session never got a real first-message title."""
        return is_default_session_title(info.title, info.thread_id)

    def list_nonempty(self, *, limit: int = 50) -> list[SessionInfo]:
        """Sessions that look used (title bound from a real user message)."""
        out: list[SessionInfo] = []
        for item in self.list(limit=max(limit * 3, 50)):
            if self.is_placeholder(item):
                continue
            out.append(item)
            if len(out) >= limit:
                break
        return out

    def latest_nonempty(self) -> SessionInfo | None:
        items = self.list_nonempty(limit=1)
        return items[0] if items else None

    def prune_empty(
        self,
        *,
        except_ids: Iterable[str] | None = None,
        limit: int = 500,
    ) -> list[str]:
        """Delete placeholder sessions that never received a real title.

        Returns deleted thread_ids. Does not touch LangGraph checkpoint files.
        """
        keep = {str(x) for x in (except_ids or []) if x}
        deleted: list[str] = []
        for item in self.list(limit=max(1, limit)):
            if item.thread_id in keep:
                continue
            if not self.is_placeholder(item):
                continue
            if self.delete(item.thread_id):
                deleted.append(item.thread_id)
        return deleted

    def search(self, query: str, *, limit: int = 50) -> list[SessionInfo]:
        q = f"%{query.strip()}%"
        rows = self._conn.execute(
            """
            SELECT * FROM sessions
            WHERE title LIKE ? OR IFNULL(summary, '') LIKE ? OR thread_id LIKE ?
               OR IFNULL(model, '') LIKE ? OR IFNULL(active_model, '') LIKE ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (q, q, q, q, q, max(1, limit)),
        ).fetchall()
        return [self._row_to_info(r) for r in rows]

    def export_json(self, thread_id: str) -> dict[str, Any] | None:
        info = self.get(thread_id)
        return info.to_dict() if info else None

    def export_markdown(self, thread_id: str) -> str | None:
        info = self.get(thread_id)
        if info is None:
            return None
        tags = ", ".join(info.tags) if info.tags else "-"
        lines = [
            f"# {info.title}",
            "",
            f"- thread_id: `{info.thread_id}`",
            f"- model: `{info.model or '-'}`",
            f"- active_model: `{info.active_model or '-'}`",
            f"- thinking: `{info.thinking or '-'}`",
            f"- created_at: {info.created_at}",
            f"- updated_at: {info.updated_at}",
            f"- tags: {tags}",
            "",
        ]
        if info.summary:
            lines.extend(["## Summary", "", info.summary, ""])
        lines.extend(
            [
                "## Notes",
                "",
                "Conversation messages live in the LangGraph checkpointer "
                f"(thread_id=`{info.thread_id}`).",
                "",
            ]
        )
        return "\n".join(lines)

    def _row_to_info(self, row: sqlite3.Row) -> SessionInfo:
        try:
            tags = json.loads(row["tags_json"] or "[]")
        except json.JSONDecodeError:
            tags = []
        if not isinstance(tags, list):
            tags = []
        keys = set(row.keys())
        return SessionInfo(
            thread_id=row["thread_id"],
            title=row["title"],
            model=row["model"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            tags=[str(t) for t in tags],
            summary=row["summary"],
            active_model=row["active_model"] if "active_model" in keys else None,
            thinking=row["thinking"] if "thinking" in keys else None,
        )


def default_sessions_path(checkpoint_path: Path | str | None = None) -> Path:
    if checkpoint_path is not None:
        return Path(checkpoint_path).expanduser().resolve().parent / "sessions.sqlite"
    return Path(".coding-agent/sessions.sqlite").resolve()


def format_session_table(items: Iterable[SessionInfo]) -> str:
    rows = list(items)
    if not rows:
        return "(no sessions)"
    lines = ["thread_id     updated_at                 model                  title"]
    for s in rows:
        model = s.binding().display()[:20]
        lines.append(
            f"{s.thread_id:<12} {s.updated_at:<24} {model:<20} {s.title[:48]}"
        )
    return "\n".join(lines)


def allocate_thread_id() -> str:
    """Short id for a not-yet-persisted chat session."""
    import uuid

    return uuid.uuid4().hex[:12]


def pick_startup_thread_id(
    store: SessionStore,
    thread_id: str | None = None,
    *,
    resume_last: bool = True,
) -> tuple[str, bool]:
    """Choose thread id for app/chat start.

    - Explicit ``thread_id``: use as-is.
    - Else if ``resume_last``: resume most recently updated non-empty session.
    - Else / nothing to resume: allocate a fresh id (not persisted until first msg).

    Returns ``(thread_id, resumed_existing)``.
    """
    if thread_id:
        return str(thread_id).strip(), True
    if resume_last:
        latest = store.latest_nonempty()
        if latest is not None:
            return latest.thread_id, True
    return allocate_thread_id(), False


def binding_from_settings(settings: Any) -> ModelBinding:
    """Capture current settings as a session model binding."""
    from synapse.models_registry import settings_thinking_label

    active = _opt_str(getattr(settings, "active_model", None))
    model = _opt_str(getattr(settings, "model", None))
    thinking = settings_thinking_label(settings)
    return ModelBinding(active_model=active, model=model, thinking=thinking)


def apply_binding_to_settings(settings: Any, binding: ModelBinding) -> bool:
    """Apply a stored binding onto settings. Returns True if settings changed.

    Preference order:
    1. profile alias (active_model) when known in registry
    2. concrete model id
    3. thinking label always applied when present
    """
    if not binding.has_data():
        return False

    from synapse.models_registry import (
        apply_profile_to_settings,
        apply_thinking_to_settings,
        registry_from_settings,
    )

    before = (
        getattr(settings, "active_model", None),
        getattr(settings, "model", None),
        getattr(settings, "enable_thinking", True),
        getattr(settings, "reasoning_effort", None),
        getattr(settings, "openai_api_key", None),
        getattr(settings, "anthropic_api_key", None),
        getattr(settings, "openai_base_url", None),
    )
    reg = registry_from_settings(settings)

    applied_profile = False
    if binding.active_model:
        try:
            profile = reg.get(binding.active_model)
        except KeyError:
            profile = None
        if profile is not None:
            apply_profile_to_settings(settings, profile, seed_thinking=True)
            applied_profile = True

    if not applied_profile and binding.model:
        # Legacy / ad-hoc: stored concrete model string.
        try:
            # If it's a known profile name or concrete model id, prefer that path.
            profile = reg.get(binding.model)
            apply_profile_to_settings(settings, profile, seed_thinking=True)
            applied_profile = True
        except KeyError:
            settings.model = binding.model
            # Keep active_model if it already matches, else clear to model string.
            if not getattr(settings, "active_model", None):
                settings.active_model = binding.model

    if binding.thinking:
        try:
            apply_thinking_to_settings(settings, binding.thinking)
        except ValueError:
            pass

    after = (
        getattr(settings, "active_model", None),
        getattr(settings, "model", None),
        getattr(settings, "enable_thinking", True),
        getattr(settings, "reasoning_effort", None),
        getattr(settings, "openai_api_key", None),
        getattr(settings, "anthropic_api_key", None),
        getattr(settings, "openai_base_url", None),
    )
    return before != after


def resolve_startup_binding(
    store: SessionStore,
    *,
    thread_id: str | None,
    cli_model: str | None = None,
) -> ModelBinding | None:
    """Pick which binding to restore on startup.

    Priority:
    1. Explicit CLI --model (caller should skip restore)
    2. Session binding when resuming thread_id
    3. Last-used global preference
    """
    if cli_model:
        return None
    if thread_id:
        bound = store.get_model_binding(thread_id)
        if bound.has_data():
            return bound
    last = store.get_last_model_binding()
    return last if last.has_data() else None
