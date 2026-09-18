"""Query and result DTOs for session listing and history reading.

These DTOs expose only safe, structured data: no LangChain objects, no full
checkpoint deserialization, no unbounded reads.  Sessions without a transcript
projection are reported with ``available=False``; they never pretend to have
an empty history.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from synapse.runtime.service.event_types import TurnChange
from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "HistoryAttachment",
    "HistoryEvent",
    "HISTORY_LIMIT_DEFAULT",
    "HISTORY_LIMIT_MAX",
    "HISTORY_LIMIT_MIN",
    "ListSessionsQuery",
    "ReadSessionHistoryQuery",
    "SESSION_LIST_LIMIT_DEFAULT",
    "SESSION_LIST_LIMIT_MAX",
    "SESSION_LIST_LIMIT_MIN",
    "SESSION_LIST_OFFSET_MAX",
    "SessionHistoryPage",
    "SessionListPage",
    "SessionMetadataItem",
]

SESSION_LIST_LIMIT_MIN = 1
SESSION_LIST_LIMIT_MAX = 100
SESSION_LIST_LIMIT_DEFAULT = 50
SESSION_LIST_OFFSET_MAX = 100_000

HISTORY_LIMIT_MIN = 1
HISTORY_LIMIT_MAX = 100
HISTORY_LIMIT_DEFAULT = 20


def _validate_int(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer, got {type(value).__name__!r}")
    if not (minimum <= value <= maximum):
        raise ValueError(
            f"{name} must be between {minimum} and {maximum}, got {value}"
        )
    return value


@dataclass(frozen=True, slots=True)
class ListSessionsQuery:
    """List session metadata for one project with bounded pagination."""

    project_id: str
    limit: int = SESSION_LIST_LIMIT_DEFAULT
    offset: int = 0

    def __post_init__(self) -> None:
        if type(self.project_id) is not str or not self.project_id.strip():
            raise ValueError("project_id must be a non-empty string")
        object.__setattr__(
            self,
            "limit",
            _validate_int(
                self.limit,
                name="limit",
                minimum=SESSION_LIST_LIMIT_MIN,
                maximum=SESSION_LIST_LIMIT_MAX,
            ),
        )
        object.__setattr__(
            self,
            "offset",
            _validate_int(
                self.offset,
                name="offset",
                minimum=0,
                maximum=SESSION_LIST_OFFSET_MAX,
            ),
        )


@dataclass(frozen=True, slots=True)
class SessionMetadataItem:
    """JSON-safe projection of one session's metadata."""

    thread_id: str
    title: str
    model: str | None
    active_model: str | None
    created_at: str
    updated_at: str
    summary: str | None


@dataclass(frozen=True, slots=True)
class SessionListPage:
    """One page of session metadata with optional continuation offset."""

    items: tuple[SessionMetadataItem, ...]
    next_offset: int | None
    total: int


@dataclass(frozen=True, slots=True)
class HistoryAttachment:
    """Durable metadata for one image attached to a persisted user turn.

    ``attachment_id`` is the opaque server id the runtime persisted into the
    transcript projection; a client loads the bytes through
    ``runtime.attachments.read`` with an ``AttachmentRef`` built from this
    session and that id.  ``image_id`` is the per-turn ``[image#N]`` placeholder
    the user text refers to.  No image bytes (base64) are ever carried here.
    """

    attachment_id: str
    image_id: int
    name: str
    mime: str
    size: int
    revision: str | None = None


@dataclass(frozen=True, slots=True)
class HistoryEvent:
    """One structured, transport-safe transcript event.

    ``kind`` is one of ``user``, ``answer``, ``thought``, ``tools``, ``changes``,
    ``meta``.
    ``tool_calls`` and ``tool_results`` are plain dicts safe for JSON encoding.
    ``attachments`` is empty for every non-user event and for a user event that
    carried no durable attachment references (legacy rows default to empty).
    ``changes`` is the files the turn touched (see ``TurnChange``), empty for every
    other kind and for turns that changed nothing -- a row written before change
    tracking existed simply carries none.
    ``reverted_paths`` are the files of that turn whose change has since been undone, so a
    change card can say so after a reload instead of still claiming the edit stands.  It is
    empty for every other kind, for a turn nothing was reverted from, and for a runtime
    that keeps no revert records.
    No LangChain message objects are ever exposed.
    """

    kind: str
    text: str
    tool_calls: tuple[dict[str, Any], ...]
    tool_results: tuple[dict[str, Any], ...]
    attachments: tuple[HistoryAttachment, ...] = ()
    #: The files one turn created, modified or deleted, with that turn's own line counts.
    changes: tuple[TurnChange, ...] = ()
    #: How many files changed in total; `changes` is the bounded list of them.
    changes_total: int = 0
    #: Paths of this turn's changes that have since been reverted, in report order.
    reverted_paths: tuple[str, ...] = ()
    # Additive metadata: older/checkpoint-rebuilt history reports unknown, not zero.
    turn_id: str | None = None
    elapsed_s: float | None = None


@dataclass(frozen=True, slots=True)
class ReadSessionHistoryQuery:
    """Read paginated transcript history for one session."""

    session: SessionRef
    before_turn: int | None = None
    limit: int = HISTORY_LIMIT_DEFAULT

    def __post_init__(self) -> None:
        if type(self.session) is not SessionRef:
            raise ValueError("session must be a SessionRef")
        if not self.session.project_id or not self.session.thread_id:
            raise ValueError("session must have non-empty project_id and thread_id")
        object.__setattr__(
            self,
            "limit",
            _validate_int(
                self.limit,
                name="limit",
                minimum=HISTORY_LIMIT_MIN,
                maximum=HISTORY_LIMIT_MAX,
            ),
        )
        if self.before_turn is not None:
            if not isinstance(self.before_turn, int) or isinstance(self.before_turn, bool):
                raise ValueError("before_turn must be an integer or None")
            if self.before_turn < 1:
                raise ValueError("before_turn must be >= 1")


@dataclass(frozen=True, slots=True)
class SessionHistoryPage:
    """One page of transcript history with pagination metadata.

    ``available=False`` means the transcript projection does not exist for
    this session (e.g. legacy data, never opened in a projection-aware
    runtime).  The caller must not interpret this as an empty history.
    """

    events: tuple[HistoryEvent, ...]
    start_turn: int
    end_turn: int
    total_turns: int
    has_more: bool
    available: bool = True