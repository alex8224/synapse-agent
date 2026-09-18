"""Command DTOs for the Agent Runtime Service (submit + session lifecycle)."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from synapse.runtime.service.queries import SessionView
from synapse.runtime.service.runtime_config import RuntimeConfigView
from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "ApprovalDecision",
    "CancelTurnCommand",
    "CancelTurnResult",
    "CloseSessionCommand",
    "CloseSessionResult",
    "CommandReceipt",
    "McpServerStateView",
    "OpenSessionCommand",
    "OpenSessionResult",
    "ReloadMcpCommand",
    "ReloadMcpResult",
    "RebindSessionCommand",
    "RebindSessionResult",
    "SetProjectThinkingLevelCommand",
    "SetProjectThinkingLevelResult",
    "SetThinkingLevelCommand",
    "SetThinkingLevelResult",
    "SteerTurnCommand",
    "SteerTurnResult",
    "SubmitTurnCommand",
    "ResumeTurnCommand",
    "ResumeTurnResult",
]

_EMPTY_OVERRIDES: Mapping[str, Any] = MappingProxyType({})

_APPROVAL_KINDS = frozenset({"allow_once", "allow_always", "reject_once", "reject_always"})


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """Pure-data decision accepted by the runtime HITL port."""

    kind: str
    message: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in _APPROVAL_KINDS:
            raise ValueError("invalid approval decision")
        if self.message is not None and type(self.message) is not str:
            raise ValueError("approval message must be a string or null")


@dataclass(frozen=True, slots=True)
class ResumeTurnCommand:
    session: SessionRef
    expected_turn_id: str
    decisions: tuple[ApprovalDecision, ...]
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if type(self.expected_turn_id) is not str or not self.expected_turn_id:
            raise ValueError("expected_turn_id must not be empty")
        decisions = tuple(self.decisions)
        if not decisions or not all(type(item) is ApprovalDecision for item in decisions):
            raise ValueError("decisions must contain ApprovalDecision values")
        object.__setattr__(self, "decisions", decisions)


@dataclass(frozen=True, slots=True)
class ResumeTurnResult:
    command_id: str
    session: SessionRef
    turn_id: str
    accepted: bool = True


@dataclass(frozen=True, slots=True)
class SubmitTurnCommand:
    """Start one turn on a session.

    Explicitly an in-process contract only: the dataclass fields are frozen,
    ``config_overrides`` is copy-isolated at construction and read-only at the
    top level, but ``attachments`` and some nested override values remain
    in-process objects.  No full-DTO remote/transport encoding is promised.

    ``attachment_refs`` is the transport-safe alternative to ``attachments``: a
    tuple of opaque, server-generated attachment ids already finalized for this
    session.  The two sources are mutually exclusive (a caller must not mix
    in-process objects with durable refs), and the service resolves the ids from
    the trusted session workspace.  At least one of ``text`` / ``attachments`` /
    ``attachment_refs`` must be present.

    The optional ``command_id`` defaults to a stable unique string generated
    once at construction so callers can correlate receipts without exposing
    any runtime handle.
    """

    session: SessionRef
    text: str
    attachments: tuple[Any, ...] = ()
    attachment_refs: tuple[str, ...] = ()
    config_overrides: Mapping[str, Any] = field(
        default_factory=lambda: _EMPTY_OVERRIDES
    )
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        # Deep-copy the caller's mapping so later mutation of the source dict
        # (including nested containers) cannot change this command after
        # construction; the stored copy is then frozen read-only at the top
        # level.  Nested values are not recursively frozen — the contract
        # guarantees copy isolation, not deep immutability.
        object.__setattr__(
            self,
            "config_overrides",
            MappingProxyType(copy.deepcopy(dict(self.config_overrides))),
        )
        # Normalize the opaque-id tuple so a caller passing a list cannot mutate
        # the command after construction.
        object.__setattr__(self, "attachment_refs", tuple(self.attachment_refs))


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    """Confirmation that a turn was accepted and started.

    Backpressure semantics: the receipt is returned only after the runtime
    manager acquired its per-session submit lock and global concurrency quota
    and the session actually started the turn — it is not a pre-queued
    acknowledgment and no separate command queue exists.  A receipt therefore
    implies the turn is running; the caller tracks progress through session
    queries and events.

    Deliberately never exposes a TurnHandle/Future/Task or any runtime
    object; the caller tracks progress through session queries and events.
    """

    command_id: str
    session: SessionRef
    turn_id: str
    accepted: bool = True


@dataclass(frozen=True, slots=True)
class OpenSessionCommand:
    """Open (idempotently) the runtime for one session.

    Idempotency is keyed on the ``SessionRef`` itself: re-opening the same
    ref returns the existing runtime with ``created=False``.  The optional
    ``command_id`` only correlates the call — it is never used for
    deduplication.
    """

    session: SessionRef
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True, slots=True)
class OpenSessionResult:
    """Pure-data result of an open; never carries a runtime object."""

    command_id: str
    session: SessionRef
    created: bool
    view: SessionView


@dataclass(frozen=True, slots=True)
class ReloadMcpCommand:
    """Apply one MCP session action and rebuild the current session binding.

    Three shapes, mirroring the TUI MCP panel:

    - ``server=None`` → attach/reload every enabled server (no config write);
    - ``server`` + ``enabled`` → persist the on/off flag, then reconnect;
    - ``server`` + ``include_tools`` → persist the tool whitelist, then reconnect.
    """

    session: SessionRef
    server: str | None = None
    enabled: bool | None = None
    include_tools: tuple[str, ...] | None = None
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if self.server is not None and (type(self.server) is not str or not self.server.strip()):
            raise ValueError("server must not be empty")
        if self.enabled is not None and type(self.enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        if self.include_tools is not None:
            if self.server is None:
                raise ValueError("include_tools requires a server")
            if not all(type(tool) is str and tool for tool in self.include_tools):
                raise ValueError("include_tools must be non-empty strings")
        if self.server is None and self.enabled is not None:
            raise ValueError("enabled requires a server")


@dataclass(frozen=True, slots=True)
class McpServerStateView:
    """One MCP server: the configured selection plus the live attach result.

    ``discovered`` is what the server advertised (live connection only, empty
    while nothing is attached) and ``loaded`` is the subset that ended up in
    the agent's tool list after the include/exclude filter.
    """

    name: str
    enabled: bool
    attached: bool
    include_tools: tuple[str, ...] = ()
    discovered: tuple[str, ...] = ()
    loaded: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReloadMcpResult:
    """Configured and actual runtime state after an MCP session rebuild."""

    command_id: str
    session: SessionRef
    server: str | None
    enabled: bool | None
    attached: bool
    active_servers: tuple[str, ...]
    tool_count: int
    warnings: tuple[str, ...]
    tool_names: tuple[str, ...] = ()
    servers: tuple[McpServerStateView, ...] = ()


@dataclass(frozen=True, slots=True)
class RebindSessionCommand:
    """Rebuild one session binding for future turns with a selected model."""

    session: SessionRef
    model: str
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if type(self.model) is not str or not self.model.strip():
            raise ValueError("model must not be empty")


@dataclass(frozen=True, slots=True)
class RebindSessionResult:
    """Confirmation that future turns use the replacement agent binding."""

    command_id: str
    session: SessionRef
    model: str
    view: SessionView


@dataclass(frozen=True, slots=True)
class SetThinkingLevelCommand:
    """Set one session's reasoning level for future turns.

    Session-scoped, exactly like :class:`RebindSessionCommand`: the level is
    validated against the *session's* thinking-level whitelist by the service
    (never against a global catalog), and the replacement agent/settings binding
    is persisted for that thread only.  Project defaults are never mutated, so
    the write never leaks into another session.
    """

    session: SessionRef
    level: str
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if type(self.level) is not str or not self.level.strip():
            raise ValueError("level must not be empty")


@dataclass(frozen=True, slots=True)
class SetThinkingLevelResult:
    """Confirmation that future turns use the replacement reasoning level.

    ``level`` is the canonical applied label (``off`` when thinking was turned
    off).  ``view`` is the refreshed read-only config projection, so a client
    can render the new state without a second round trip.
    """

    command_id: str
    session: SessionRef
    level: str
    view: RuntimeConfigView


@dataclass(frozen=True, slots=True)
class SetProjectThinkingLevelCommand:
    """Set one project's default reasoning level for *future* sessions.

    Project-scoped, so it carries a ``project_id`` instead of a ``SessionRef``:
    the write targets the project's settings layer, never a thread, and it never
    rebinds a running session.  Sessions already open keep the level they were
    built with — only sessions opened afterwards inherit the new default.
    """

    project_id: str
    level: str
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if type(self.project_id) is not str or not self.project_id.strip():
            raise ValueError("project_id must not be empty")
        if type(self.level) is not str or not self.level.strip():
            raise ValueError("level must not be empty")


@dataclass(frozen=True, slots=True)
class SetProjectThinkingLevelResult:
    """Confirmation that the project default was persisted.

    ``level`` is the canonical label that was written (``off`` when thinking was
    disabled for the project).  The target file is deliberately *not* reported:
    the read surface never hands out workspace-absolute paths, and the console
    only needs to say that the project default changed, not where it lives.
    """

    command_id: str
    project_id: str
    level: str


@dataclass(frozen=True, slots=True)
class CancelTurnCommand:
    """Cancel a turn only when it matches ``expected_turn_id``.

    A stale id can never cancel a newer turn: the runtime raises
    ``turn_mismatch`` instead.  ``reason`` is propagated to the cancel token.
    """

    session: SessionRef
    expected_turn_id: str
    reason: str = "user"
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True, slots=True)
class CancelTurnResult:
    """Confirmation of a (possibly repeated) cancellation request.

    ``cancellation_requested`` is True only for the call that first committed
    the cancellation at its linearization point; ordinary repeats of the same
    still-live turn succeed with ``cancellation_requested=False``.
    """

    command_id: str
    session: SessionRef
    turn_id: str
    cancellation_requested: bool


@dataclass(frozen=True, slots=True)
class SteerTurnCommand:
    """Deliver mid-run guidance only to the turn matching ``expected_turn_id``."""

    session: SessionRef
    expected_turn_id: str
    text: str
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True, slots=True)
class SteerTurnResult:
    """Confirmation that guidance was (or was not) enqueued for one turn.

    ``accepted`` is False when the text was empty.  ``pending_count`` is the
    actual steer queue depth after the call.
    """

    command_id: str
    session: SessionRef
    turn_id: str
    accepted: bool
    pending_count: int


@dataclass(frozen=True, slots=True)
class CloseSessionCommand:
    """Close one session.

    ``cancel_active=False`` rejects a session that still owns a turn/
    reservation/settlement with ``conflict`` without changing state;
    ``cancel_active=True`` cancels the active turn and waits for settlement
    before the close returns.  Closing a missing session is idempotent:
    ``closed=False`` in the result.
    """

    session: SessionRef
    cancel_active: bool = False
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True, slots=True)
class CloseSessionResult:
    """Pure-data close outcome; ``active_turn_id`` is the turn captured at the
    atomic close claim and ``cancellation_requested`` whether this close
    actually requested its cancellation."""

    command_id: str
    session: SessionRef
    closed: bool
    active_turn_id: str | None
    cancellation_requested: bool
