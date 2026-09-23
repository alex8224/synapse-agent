"""Deciding whether a stopped run may continue, from its own records.

A call is durable in two places: the workflow store records it, and the LangGraph
orchestration checkpoint holds the task result.  The SDK commits the store record
*before* the checkpoint, which is what makes the dangerous direction impossible — a call
whose side effect happened but which left no record — but it still leaves windows a resume
must reason about:

- **Recorded as running, no result.**  The process died while the call was in flight.  Its
  outcome is genuinely unknown: retrying may repeat a file modification, and skipping it
  may drop work.  This module records that as ``UNCERTAIN`` (the evidence), settles the run
  as uncertain, and refuses to continue.
- **Recorded as completed, checkpoint missing.**  The store record is the authority here:
  the SDK reuses the recorded result without re-running the agent, and the task's result is
  committed on the way through.  Nothing to repair.
- **A run that is not executing.**  A terminal run is not resumed at all.

The point of the split is that **repairing means recording an unknown outcome, never
inventing a result**.  Nothing here fabricates a value for a call, so a resume can only be
refused or allowed — never silently patched.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from synapse.workflows.contract import ACTIVE_RUN_STATUSES, CallStatus, WorkflowStatus
from synapse.workflows.errors import ResumeBlockedError
from synapse.workflows.store import WorkflowStore, utcnow

__all__ = [
    "ReconcileReport",
    "ResumeBlocker",
    "ResumePlan",
    "plan_resume",
    "reconcile_run",
]


class ResumeBlocker(StrEnum):
    """Why a run may not continue."""

    NO_RUN = "no_run"
    FINISHED = "finished"
    CANCELLING = "cancelling"
    UNCERTAIN_CALL = "uncertain_call"
    FAILED_CALL = "failed_call"
    IN_FLIGHT_CALL = "in_flight_call"
    BROKEN_COUNTER = "broken_counter"


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """What the store's own records say about one run's calls."""

    run_id: str
    calls: int
    completed: tuple[str, ...]
    uncertain: tuple[str, ...]
    failed: tuple[str, ...]
    in_flight: tuple[str, ...]
    #: In-flight calls this pass recorded as uncertain (the only write it performs).
    recorded_uncertain: tuple[str, ...]
    #: Whether the run's dispatch counter agrees with the number of call records.
    counter_matches: bool

    @property
    def consistent(self) -> bool:
        return self.counter_matches and not self.in_flight


@dataclass(frozen=True, slots=True)
class ResumePlan:
    """Whether one run may continue, and if not, why."""

    run_id: str
    status: WorkflowStatus | None
    resumable: bool
    blockers: tuple[ResumeBlocker, ...]
    blocked_calls: tuple[str, ...]
    detail: str

    def require_resumable(self) -> None:
        """Raise :class:`ResumeBlockedError` when this plan forbids continuing."""
        if not self.resumable:
            raise ResumeBlockedError(self.detail)


def reconcile_run(
    store: WorkflowStore, run_id: str, *, reason: str, at: str | None = None
) -> ReconcileReport:
    """Read one run's call records and record every unknown outcome as uncertain.

    The single write it performs is ``RUNNING -> UNCERTAIN`` for calls whose dispatching
    process is gone.  That is evidence, not a guess: it says "this call's outcome was never
    established", which is exactly what a later reader needs in order not to retry it.
    """
    stamp = at or utcnow()
    run = store.get_run(run_id)
    calls = store.list_calls(run_id)
    completed = tuple(c.call_key for c in calls if c.status is CallStatus.COMPLETED)
    uncertain = [c.call_key for c in calls if c.status is CallStatus.UNCERTAIN]
    failed = tuple(c.call_key for c in calls if c.status is CallStatus.FAILED)
    in_flight = [c.call_key for c in calls if c.status is CallStatus.RUNNING]

    recorded: list[str] = []
    for call_key in in_flight:
        try:
            store.mark_call_uncertain(run_id, call_key, reason=reason, at=stamp)
        except Exception:  # noqa: BLE001 - a record we cannot move is still reported
            continue
        recorded.append(call_key)
        uncertain.append(call_key)

    if recorded and run is not None and run.status is WorkflowStatus.RUNNING:
        # A run with an unknown outcome is not "still running": say so before anything
        # else reads the status, so a reader cannot mistake it for work in progress.
        store.set_run_status(
            run_id,
            WorkflowStatus.UNCERTAIN,
            error=f"{len(recorded)} call(s) have no established outcome: {reason}"[:2000],
            at=stamp,
        )

    return ReconcileReport(
        run_id=run_id,
        calls=len(calls),
        completed=completed,
        uncertain=tuple(uncertain),
        failed=failed,
        in_flight=tuple(in_flight),
        recorded_uncertain=tuple(recorded),
        counter_matches=run is None or run.calls == len(calls),
    )


def plan_resume(store: WorkflowStore, run_id: str) -> ResumePlan:
    """Classify one run as continuable or blocked, from its records.

    Read-only: call :func:`reconcile_run` first so in-flight calls have already been
    recorded as uncertain.
    """
    run = store.get_run(run_id)
    if run is None:
        return ResumePlan(
            run_id=run_id,
            status=None,
            resumable=False,
            blockers=(ResumeBlocker.NO_RUN,),
            blocked_calls=(),
            detail=f"unknown workflow run {run_id!r}",
        )

    calls = store.list_calls(run_id)
    uncertain = tuple(c.call_key for c in calls if c.status is CallStatus.UNCERTAIN)
    failed = tuple(c.call_key for c in calls if c.status is CallStatus.FAILED)
    in_flight = tuple(c.call_key for c in calls if c.status is CallStatus.RUNNING)
    counter_matches = run.calls == len(calls)

    blockers: list[ResumeBlocker] = []
    blocked_calls: list[str] = []
    if run.status not in ACTIVE_RUN_STATUSES:
        blockers.append(ResumeBlocker.FINISHED)
    elif run.status is WorkflowStatus.CANCELLING:
        blockers.append(ResumeBlocker.CANCELLING)
    if uncertain:
        blockers.append(ResumeBlocker.UNCERTAIN_CALL)
        blocked_calls.extend(uncertain)
    if in_flight:
        blockers.append(ResumeBlocker.IN_FLIGHT_CALL)
        blocked_calls.extend(in_flight)
    if failed:
        blockers.append(ResumeBlocker.FAILED_CALL)
        blocked_calls.extend(failed)
    if not counter_matches:
        blockers.append(ResumeBlocker.BROKEN_COUNTER)

    resumable = not blockers
    return ResumePlan(
        run_id=run_id,
        status=run.status,
        resumable=resumable,
        blockers=tuple(blockers),
        blocked_calls=tuple(dict.fromkeys(blocked_calls)),
        detail=_detail(run.status, blockers, uncertain, failed, counter_matches),
    )


def _detail(
    status: WorkflowStatus,
    blockers: list[ResumeBlocker],
    uncertain: tuple[str, ...],
    failed: tuple[str, ...],
    counter_matches: bool,
) -> str:
    if not blockers:
        return f"run {status} has no unresolved calls and may continue"
    parts: list[str] = []
    if ResumeBlocker.FINISHED in blockers:
        parts.append(f"the run is {status}")
    if ResumeBlocker.CANCELLING in blockers:
        parts.append("the run is being cancelled")
    if uncertain:
        parts.append(
            "call(s) with no established outcome: "
            + ", ".join(uncertain)
            + " (their results cannot be reused and retrying them may repeat a side effect)"
        )
    if failed:
        parts.append("call(s) that already failed: " + ", ".join(failed))
    if not counter_matches:
        parts.append("the run's call counter does not match its records")
    return "; ".join(parts)
