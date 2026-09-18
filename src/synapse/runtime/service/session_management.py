"""Command/query DTOs and the pure metadata-store port for session management.

The session-management surface (create / rename / delete / search) is defined
here as transport-neutral, JSON-safe value objects plus one dependency-inversion
port.  The :class:`SessionMetadataStore` port is the only persistence
dependency and is injected by the composition root, so this module never
imports the concrete SQLite session store and never carries a filesystem path.

Deletion removes only the human-facing metadata row (and the thread goal stored
in the same database); LangGraph checkpoints and the transcript projection are
retained, which :class:`DeleteSessionResult` states explicitly so a UI cannot
claim the conversation was erased.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from synapse.runtime.service.history import SessionMetadataItem
from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "CreateSessionCommand",
    "CreateSessionResult",
    "DeleteSessionCommand",
    "DeleteSessionResult",
    "RenameSessionCommand",
    "RenameSessionResult",
    "SearchSessionsQuery",
    "SESSION_SEARCH_LIMIT_DEFAULT",
    "SESSION_SEARCH_LIMIT_MAX",
    "SESSION_SEARCH_LIMIT_MIN",
    "SESSION_SEARCH_OFFSET_MAX",
    "SESSION_SEARCH_TEXT_MAX",
    "SESSION_TITLE_MAX",
    "SessionMetadataStore",
    "SessionProjectContext",
    "SessionSearchPage",
]

#: A stored session title is non-empty and at most this many characters.
SESSION_TITLE_MAX = 120

SESSION_SEARCH_LIMIT_MIN = 1
SESSION_SEARCH_LIMIT_MAX = 100
SESSION_SEARCH_LIMIT_DEFAULT = 50
SESSION_SEARCH_OFFSET_MAX = 100_000
SESSION_SEARCH_TEXT_MAX = 200


def _validate_int(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer, got {type(value).__name__!r}")
    if not (minimum <= value <= maximum):
        raise ValueError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _validate_ref(ref: object) -> SessionRef:
    if type(ref) is not SessionRef:
        raise ValueError("session must be a SessionRef")
    if not ref.project_id or not ref.thread_id:
        raise ValueError("session must have non-empty project_id and thread_id")
    return ref


def _validate_title(title: object, *, name: str = "title") -> str:
    """Return the stripped title, rejecting an empty or over-long value."""
    if type(title) is not str:
        raise ValueError(f"{name} must be a string, got {type(title).__name__!r}")
    text = title.strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    if len(text) > SESSION_TITLE_MAX:
        raise ValueError(f"{name} must be at most {SESSION_TITLE_MAX} characters")
    return text


@dataclass(frozen=True, slots=True)
class SessionProjectContext:
    """One project's resolved settings and optional live manager.

    ``settings`` carries the project's session-metadata path resolver (the only
    source of the write path).  ``manager`` is the live ``RuntimeManager`` for
    the project when one exists, used to make deletion atomic against a running
    turn; it is ``None`` when the project has no in-process runtime.
    """

    project_id: str
    settings: Any
    manager: Any | None = None


@dataclass(frozen=True, slots=True)
class RenameSessionCommand:
    """Rename one session's title (non-empty, at most ``SESSION_TITLE_MAX``)."""

    session: SessionRef
    title: str
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        _validate_ref(self.session)
        object.__setattr__(self, "title", _validate_title(self.title))


@dataclass(frozen=True, slots=True)
class RenameSessionResult:
    command_id: str
    session: SessionRef
    title: str
    renamed: bool = True


@dataclass(frozen=True, slots=True)
class CreateSessionCommand:
    """Create (idempotently) one persisted session metadata row.

    Idempotency follows the existing open-session convention: when the caller
    supplies ``thread_id`` the ref itself is the key (``ensure`` returns the
    existing row with ``created=False``); when it does not, the server allocates
    a fresh id.  ``command_id`` only correlates the call.
    """

    project_id: str
    title: str | None = None
    thread_id: str | None = None
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if type(self.project_id) is not str or not self.project_id.strip():
            raise ValueError("project_id must be a non-empty string")
        object.__setattr__(self, "project_id", self.project_id.strip())
        if self.thread_id is not None:
            if type(self.thread_id) is not str or not self.thread_id.strip():
                raise ValueError("thread_id must be a non-empty string when provided")
            object.__setattr__(self, "thread_id", self.thread_id.strip())
        if self.title is not None:
            object.__setattr__(self, "title", _validate_title(self.title))


@dataclass(frozen=True, slots=True)
class CreateSessionResult:
    command_id: str
    session: SessionRef
    created: bool
    title: str


@dataclass(frozen=True, slots=True)
class DeleteSessionCommand:
    session: SessionRef
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        _validate_ref(self.session)


@dataclass(frozen=True, slots=True)
class DeleteSessionResult:
    """Result of deleting one session, its conversation included.

    ``retained_history`` is ``False`` when every local store that held the thread
    was purged: the metadata row and thread goal, the LangGraph checkpoints
    (subagent ``tools:*`` namespaces included), the transcript projection, the
    full-text search index and the thread's turn snapshots.  It is ``True`` when
    something survived, and ``purge_failures`` then names the stores that could
    not be purged (never a path), so a caller can report which part is still on
    disk instead of claiming the conversation was erased.
    """

    command_id: str
    session: SessionRef
    deleted: bool
    retained_history: bool = False
    purge_failures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchSessionsQuery:
    """Search one project's persisted session metadata with bounded paging."""

    project_id: str
    text: str = ""
    limit: int = SESSION_SEARCH_LIMIT_DEFAULT
    offset: int = 0

    def __post_init__(self) -> None:
        if type(self.project_id) is not str or not self.project_id.strip():
            raise ValueError("project_id must be a non-empty string")
        if type(self.text) is not str:
            raise ValueError("text must be a string")
        if len(self.text) > SESSION_SEARCH_TEXT_MAX:
            raise ValueError(
                f"text must be at most {SESSION_SEARCH_TEXT_MAX} characters"
            )
        object.__setattr__(
            self,
            "limit",
            _validate_int(
                self.limit,
                name="limit",
                minimum=SESSION_SEARCH_LIMIT_MIN,
                maximum=SESSION_SEARCH_LIMIT_MAX,
            ),
        )
        object.__setattr__(
            self,
            "offset",
            _validate_int(
                self.offset,
                name="offset",
                minimum=0,
                maximum=SESSION_SEARCH_OFFSET_MAX,
            ),
        )


@dataclass(frozen=True, slots=True)
class SessionSearchPage:
    """One bounded page of search hits ordered newest-first."""

    items: tuple[SessionMetadataItem, ...]
    next_offset: int | None
    total: int


class SessionMetadataStore(Protocol):
    """Pure operations over one project's persisted session metadata.

    Implementations are injected by the composition root and resolve their
    database path from the project resolver; the port never carries a path.
    ``ensure`` returns ``(item, created)``.  ``rename`` returns ``None`` and
    ``delete`` returns ``False`` for an unknown session, and neither may create
    a database file or schema for a session that does not exist.
    """

    def ensure(
        self, thread_id: str, *, title: str | None = None
    ) -> tuple[SessionMetadataItem, bool]: ...

    def rename(self, thread_id: str, title: str) -> SessionMetadataItem | None: ...

    def delete(self, thread_id: str) -> bool: ...
