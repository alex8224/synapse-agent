"""Persisted workflow records.

Plain frozen dataclasses: the store returns them and the service layer projects them
into wire views, so neither side owns a second shape.  Large payloads (a call result, a
run result) stay in the database as JSON text and are read through dedicated accessors,
which is why listing runs never deserializes them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from synapse.workflows.contract import (
    ACTIVE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    CallStatus,
    WorkflowLimits,
    WorkflowStatus,
)

__all__ = ["CallRecord", "WorkflowEvent", "WorkflowRun"]


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    """One execution of an approved draft."""

    run_id: str
    workflow_id: str
    project_id: str
    thread_id: str
    revision: int
    script_hash: str
    status: WorkflowStatus
    limits: WorkflowLimits
    #: How many calls have been dispatched (hard-limited by ``limits.max_calls``).
    calls: int = 0
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    approved_at: str | None = None
    finished_at: str | None = None

    @property
    def active(self) -> bool:
        """Whether this run still occupies its project's single workflow slot."""
        return self.status in ACTIVE_RUN_STATUSES

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One recorded agent call, keyed by the script's own call key."""

    run_id: str
    call_key: str
    actor_key: str
    role: str
    #: Request identity this record's result is only valid for (see ``contract``).
    fingerprint: str
    status: CallStatus
    #: Dispatch order inside the run; stable across a resume.
    sequence: int
    #: How many model attempts this call needed (1, or 2 after a format correction).
    attempts: int = 0
    #: Tokens this call cost.  Known as soon as the call ran, including a failed one:
    #: a run's budget is about what was spent, not about what succeeded.
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
    started_at: str = ""
    finished_at: str | None = None

    @property
    def reusable(self) -> bool:
        """True only for a call whose result was committed.

        A ``FAILED`` or ``UNCERTAIN`` record is deliberately not reusable: the caller
        has to decide what to do instead of receiving a partial value.
        """
        return self.status is CallStatus.COMPLETED


@dataclass(frozen=True, slots=True)
class WorkflowEvent:
    """One ordered, durable workflow event."""

    run_id: str
    sequence: int
    kind: str
    payload: Any
    created_at: str
