"""Workflow definitions, call identity and the run/call state machines.

Import-light on purpose: the worker, the store and the SDK all import this module, so
it must not pull in LangGraph, the agent stack or a UI package.  It owns three things.

**Definition identity.**  A draft carries a ``revision`` and the hash of its script.
Approval names one exact revision *and* hash, and a later revision invalidates it, so an
approval always refers to the program that actually runs.

**Call identity.**  A :class:`CallRequest` is the frozen description of one agent call.
Its fingerprint is what makes a recorded result reusable: the same key with a different
fingerprint is a :class:`~synapse.workflows.errors.ReplayMismatchError`, never a silent
cache hit.

**Status vocabulary.**  The run and call state machines, including the *result
uncertain* state a crash window produces.  That state is not a synonym for "failed" and
never auto-advances.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from synapse.workflows.errors import InvalidDraftError, WorkflowStateError

__all__ = [
    "DEFAULT_LIMITS",
    "MAX_LIMIT_CALLS",
    "MAX_LIMIT_ACTORS",
    "MAX_LIMIT_PARALLEL",
    "ALLOWED_RUN_TRANSITIONS",
    "ALLOWED_CALL_TRANSITIONS",
    "CallRequest",
    "CallStatus",
    "DraftStatus",
    "WorkflowDraft",
    "WorkflowLimits",
    "WorkflowStatus",
    "can_transition",
    "canonical_json",
    "fingerprint_of",
    "script_hash",
    "validate_limits",
]


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """Return a stable JSON encoding, or raise for a value that has none.

    Used for fingerprints, so key order and float formatting must not depend on how the
    caller happened to build the object.  ``allow_nan=False`` rejects the values JSON
    cannot round-trip; the caller turns that into a workflow error.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def script_hash(source: str) -> str:
    """Digest the exact script text a user approves."""
    if not isinstance(source, str):
        raise InvalidDraftError("workflow source must be a string")
    return _digest(source)


def fingerprint_of(request: CallRequest) -> str:
    """Digest the request identity a recorded result is only valid for.

    Deliberately excludes anything that changes between attempts of the *same* call
    (attempt counters, timestamps): those belong to the record, not to the identity.
    """
    return _digest(
        canonical_json(
            {
                "actor_key": request.actor_key,
                "call_key": request.call_key,
                "role": request.role,
                "prompt": request.prompt,
                "input": request.input,
                "schema": request.schema,
                "readonly": request.readonly,
            }
        )
    )


# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------


class DraftStatus(StrEnum):
    """Lifecycle of a generated program, before any run exists.

    A draft and a run are different objects with different vocabularies: a draft is
    approved or discarded, a run executes.  Keeping the two apart is what stops
    "approved" from ever being reported as an execution state.
    """

    DRAFT = "draft"
    APPROVED = "approved"
    DISCARDED = "discarded"


class WorkflowStatus(StrEnum):
    """Lifecycle of one workflow run."""

    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class CallStatus(StrEnum):
    """Lifecycle of one recorded agent call."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


#: Terminal run statuses; nothing transitions out of them.
TERMINAL_RUN_STATUSES = frozenset(
    {
        WorkflowStatus.CANCELLED,
        WorkflowStatus.COMPLETED,
        WorkflowStatus.FAILED,
    }
)

#: Statuses that keep a project's single active-workflow slot occupied.
ACTIVE_RUN_STATUSES = frozenset(
    {
        WorkflowStatus.RUNNING,
        WorkflowStatus.WAITING_APPROVAL,
        WorkflowStatus.CANCELLING,
        WorkflowStatus.UNCERTAIN,
    }
)

ALLOWED_RUN_TRANSITIONS: Mapping[WorkflowStatus, frozenset[WorkflowStatus]] = {
    WorkflowStatus.RUNNING: frozenset(
        {
            WorkflowStatus.WAITING_APPROVAL,
            WorkflowStatus.CANCELLING,
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.UNCERTAIN,
        }
    ),
    WorkflowStatus.WAITING_APPROVAL: frozenset(
        {
            WorkflowStatus.RUNNING,
            WorkflowStatus.CANCELLING,
            WorkflowStatus.FAILED,
            WorkflowStatus.UNCERTAIN,
        }
    ),
    WorkflowStatus.CANCELLING: frozenset({WorkflowStatus.CANCELLED, WorkflowStatus.FAILED}),
    # An uncertain run is a stopped run: the console may show it and cancel it, but
    # resuming it requires a verified outcome, which is a separate explicit action.
    WorkflowStatus.UNCERTAIN: frozenset(
        {WorkflowStatus.CANCELLING, WorkflowStatus.FAILED}
    ),
    WorkflowStatus.CANCELLED: frozenset(),
    WorkflowStatus.COMPLETED: frozenset(),
    WorkflowStatus.FAILED: frozenset(),
}

#: A recorded call only moves forward: ``COMPLETED`` is never revisited, and a call left
#: in flight by a crash becomes ``UNCERTAIN`` rather than being retried implicitly.
ALLOWED_CALL_TRANSITIONS: Mapping[CallStatus, frozenset[CallStatus]] = {
    CallStatus.RUNNING: frozenset(
        {CallStatus.COMPLETED, CallStatus.FAILED, CallStatus.UNCERTAIN}
    ),
    CallStatus.COMPLETED: frozenset(),
    CallStatus.FAILED: frozenset(),
    CallStatus.UNCERTAIN: frozenset(),
}


def can_transition(
    current: WorkflowStatus,
    target: WorkflowStatus,
) -> bool:
    """Whether the run state machine allows ``current -> target``."""
    return target in ALLOWED_RUN_TRANSITIONS.get(current, frozenset())


def _require_transition(current: WorkflowStatus, target: WorkflowStatus) -> None:
    if not can_transition(current, target):
        raise WorkflowStateError(f"cannot move workflow from {current} to {target}")


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

#: Ceilings a draft may not exceed, so a generated script cannot declare itself
#: unbounded.  The scheduler enforces the *draft's* numbers; these only bound them.
MAX_LIMIT_CALLS = 200
MAX_LIMIT_ACTORS = 32
MAX_LIMIT_PARALLEL = 8


@dataclass(frozen=True, slots=True)
class WorkflowLimits:
    """Execution limits one approved draft runs under.

    ``max_calls`` and ``max_actors`` are hard: they are checked before a call is
    dispatched, so they cannot be exceeded by construction.  ``token_budget`` is a
    threshold, not a guarantee: usage is only known after a call returns, so in-flight
    calls may push the total past it.  The UI must not present them as one kind of
    "limit".
    """

    max_calls: int = 40
    max_actors: int = 8
    max_parallel: int = 4
    max_seconds: float = 3600.0
    token_budget: int | None = None

    def __post_init__(self) -> None:
        _require_int_in_range("max_calls", self.max_calls, 1, MAX_LIMIT_CALLS)
        _require_int_in_range("max_actors", self.max_actors, 1, MAX_LIMIT_ACTORS)
        _require_int_in_range("max_parallel", self.max_parallel, 1, MAX_LIMIT_PARALLEL)
        if not isinstance(self.max_seconds, (int, float)) or isinstance(self.max_seconds, bool):
            raise InvalidDraftError("max_seconds must be a number")
        if not 0 < float(self.max_seconds) <= 24 * 3600:
            raise InvalidDraftError("max_seconds must be within (0, 86400]")
        if self.token_budget is not None:
            if isinstance(self.token_budget, bool) or not isinstance(self.token_budget, int):
                raise InvalidDraftError("token_budget must be an int or None")
            if self.token_budget <= 0:
                raise InvalidDraftError("token_budget must be positive")

    def to_json(self) -> dict[str, Any]:
        return {
            "max_calls": self.max_calls,
            "max_actors": self.max_actors,
            "max_parallel": self.max_parallel,
            "max_seconds": float(self.max_seconds),
            "token_budget": self.token_budget,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> WorkflowLimits:
        if not data:
            return DEFAULT_LIMITS
        return cls(
            max_calls=int(data.get("max_calls", DEFAULT_LIMITS.max_calls)),
            max_actors=int(data.get("max_actors", DEFAULT_LIMITS.max_actors)),
            max_parallel=int(data.get("max_parallel", DEFAULT_LIMITS.max_parallel)),
            max_seconds=float(data.get("max_seconds", DEFAULT_LIMITS.max_seconds)),
            token_budget=(
                None if data.get("token_budget") is None else int(data["token_budget"])
            ),
        )


def _require_int_in_range(name: str, value: Any, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidDraftError(f"{name} must be an int")
    if not low <= value <= high:
        raise InvalidDraftError(f"{name} must be within [{low}, {high}]")


DEFAULT_LIMITS = WorkflowLimits()


def validate_limits(limits: WorkflowLimits) -> WorkflowLimits:
    """Return ``limits`` unchanged; exists so callers can validate a parsed value."""
    if not isinstance(limits, WorkflowLimits):
        raise InvalidDraftError("limits must be a WorkflowLimits instance")
    return limits


# ---------------------------------------------------------------------------
# Call request
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CallRequest:
    """One frozen agent call, before it is dispatched.

    ``key`` values are the script's own identity for a call and must be derived from
    stable inputs (a file path, an item id) rather than from completion order, because
    they are what a resumed run matches against its recorded calls.
    """

    role: str
    actor_key: str
    call_key: str
    prompt: str
    input: Any = None
    schema: Mapping[str, Any] | None = None
    readonly: bool = False

    def __post_init__(self) -> None:
        for name in ("role", "actor_key", "call_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise InvalidDraftError(f"{name} must be a non-empty string")
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise InvalidDraftError("prompt must be a non-empty string")
        if self.schema is not None and not isinstance(self.schema, Mapping):
            raise InvalidDraftError("schema must be a mapping or None")
        if not isinstance(self.readonly, bool):
            raise InvalidDraftError("readonly must be a bool")

    def fingerprint(self) -> str:
        """Identity of this request; see :func:`fingerprint_of`."""
        return fingerprint_of(self)


# ---------------------------------------------------------------------------
# Draft
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkflowDraft:
    """One generated workflow program, awaiting (or holding) an approval.

    ``roles`` is *advisory*: the generator declares which roles it intends to use so the
    approval screen can preview them, but a script is opaque Python and may reference
    another role at runtime.  Runtime resolution still validates every ``actor()`` call,
    so this list is never treated as a permission boundary.
    """

    workflow_id: str
    project_id: str
    source: str
    #: The session that asked for this draft, when a session did.  Empty means the draft is
    #: project-scoped, which is the normal case for a console request; it is display-only and
    #: never an authorization input.
    thread_id: str = ""
    limits: WorkflowLimits = DEFAULT_LIMITS
    revision: int = 1
    title: str = ""
    goal: str = ""
    roles: tuple[str, ...] = ()
    approved_hash: str | None = None
    status: DraftStatus = DraftStatus.DRAFT
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        for name in ("workflow_id", "project_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise InvalidDraftError(f"{name} must be a non-empty string")
        # ``thread_id`` records the session that asked for this draft when there is one.
        # A project-scoped request (the console's own draft) has no session, and an empty
        # value says exactly that instead of inventing one.
        if not isinstance(self.thread_id, str):
            raise InvalidDraftError("thread_id must be a string")
        if not isinstance(self.source, str) or not self.source.strip():
            raise InvalidDraftError("workflow source must not be empty")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise InvalidDraftError("revision must be an int")
        if self.revision < 1:
            raise InvalidDraftError("revision must be >= 1")
        validate_limits(self.limits)
        if not isinstance(self.roles, tuple):
            object.__setattr__(self, "roles", tuple(self.roles))

    @property
    def script_hash(self) -> str:
        return script_hash(self.source)

    @property
    def approved(self) -> bool:
        """True when the current revision and its exact script were approved."""
        return (
            self.status is DraftStatus.APPROVED
            and self.approved_hash == self.script_hash
        )

    def approve(self, *, approved_hash: str, at: str) -> WorkflowDraft:
        """Return an approved copy, refusing a hash that is not this revision's."""
        if approved_hash != self.script_hash:
            raise InvalidDraftError(
                "approval does not name the current revision's script"
            )
        return replace(
            self,
            approved_hash=approved_hash,
            status=DraftStatus.APPROVED,
            updated_at=at,
        )

    def revised(
        self,
        *,
        source: str,
        at: str,
        title: str | None = None,
        goal: str | None = None,
        roles: Sequence[str] | None = None,
        limits: WorkflowLimits | None = None,
    ) -> WorkflowDraft:
        """Return a new revision; the previous approval never carries over."""
        return replace(
            self,
            source=source,
            revision=self.revision + 1,
            title=self.title if title is None else title,
            goal=self.goal if goal is None else goal,
            roles=self.roles if roles is None else tuple(roles),
            limits=self.limits if limits is None else validate_limits(limits),
            approved_hash=None,
            status=DraftStatus.DRAFT,
            updated_at=at,
        )

    def discard(self, *, at: str) -> WorkflowDraft:
        return replace(
            self,
            status=DraftStatus.DISCARDED,
            approved_hash=None,
            updated_at=at,
        )

    def require_approved(self) -> None:
        """Raise unless this draft is exactly the approved program."""
        if self.status is DraftStatus.DISCARDED:
            raise InvalidDraftError("workflow draft was discarded")
        if not self.approved:
            raise InvalidDraftError("workflow draft is not approved at its current revision")


#: Field names a caller may pass to :func:`draft_from_payload`.
_DRAFT_FIELDS = (
    "workflow_id",
    "project_id",
    "thread_id",
    "source",
    "limits",
    "revision",
    "title",
    "goal",
    "roles",
    "approved_hash",
    "status",
    "created_at",
    "updated_at",
)


def draft_from_payload(payload: Mapping[str, Any]) -> WorkflowDraft:
    """Build a draft from a request payload, rejecting unknown fields.

    The service layer will call this; keeping it here means the wire shape and the
    domain shape cannot drift into two validators.
    """
    unknown = set(payload) - set(_DRAFT_FIELDS)
    if unknown:
        raise InvalidDraftError(f"unknown draft field(s): {', '.join(sorted(unknown))}")
    data = dict(payload)
    limits = data.get("limits")
    if isinstance(limits, Mapping):
        data["limits"] = WorkflowLimits.from_json(limits)
    roles = data.get("roles")
    if roles is not None and not isinstance(roles, tuple):
        data["roles"] = tuple(roles)
    status = data.get("status")
    if status is not None and not isinstance(status, DraftStatus):
        data["status"] = DraftStatus(str(status))
    return WorkflowDraft(**data)  # type: ignore[arg-type]


def transitions_for(status: WorkflowStatus) -> frozenset[WorkflowStatus]:
    """Allowed next statuses, for a caller that wants to render the choices."""
    return ALLOWED_RUN_TRANSITIONS.get(status, frozenset())
