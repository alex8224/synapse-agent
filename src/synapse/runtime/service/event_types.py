"""Pure, UI-independent turn event contract (v1).

This module owns the event *shape* only: the ``TurnEventKind`` enum, the
ordered ``TurnEvent`` envelope, and every payload dataclass the producers emit.
It deliberately imports no runtime implementation package (no ``streaming``,
``sessions.runtime``, ``tool_ignore``, or agent execution), so a contract
consumer can import the event vocabulary without paying for the execution
stack.  ``synapse.runtime.streaming.events`` re-exports these names unchanged
(same objects) and keeps the producer-side snapshot helpers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "EVENT_VERSION",
    "ActivityPayload",
    "ApprovalActionPayload",
    "ApprovalPayload",
    "DiffPayload",
    "PlanEntryPayload",
    "PlanPayload",
    "PlanRemovedPayload",
    "SubagentStatusPayload",
    "TextPayload",
    "ToolBatchFinishedPayload",
    "ToolBatchPayload",
    "ToolCallPayload",
    "ToolFinishedPayload",
    "ToolItemPayload",
    "ToolResultPayload",
    "TurnEvent",
    "TurnEventKind",
    "TurnChange",
    "TurnChangesPayload",
    "TurnTerminalPayload",
    "UsagePayload",
]

EVENT_VERSION = 1


class TurnEventKind(StrEnum):
    """Stable event names consumed by CLI/TUI adapters."""

    ACTIVITY_STARTED = "activity_started"
    ACTIVITY_UPDATED = "activity_updated"
    ACTIVITY_STOPPED = "activity_stopped"
    REASONING_DELTA = "reasoning_delta"
    REASONING_COMPLETED = "reasoning_completed"
    ANSWER_DELTA = "answer_delta"
    ANSWER_COMPLETED = "answer_completed"
    TOOL_BATCH_STARTED = "tool_batch_started"
    TOOL_STARTED = "tool_started"
    TOOL_UPDATED = "tool_updated"
    TOOL_FINISHED = "tool_finished"
    TOOL_RESULT = "tool_result"
    TOOL_BATCH_FINISHED = "tool_batch_finished"
    SUBAGENT_STATUS_CHANGED = "subagent_status_changed"
    PLAN_UPDATED = "plan_updated"
    PLAN_REMOVED = "plan_removed"
    DIFF_UPDATED = "diff_updated"
    USAGE_UPDATED = "usage_updated"
    APPROVAL_REQUIRED = "approval_required"
    INFO = "info"
    TURN_CHANGES = "turn_changes"
    TURN_COMPLETED = "turn_completed"
    TURN_CANCELLED = "turn_cancelled"
    TURN_WAITING_APPROVAL = "turn_waiting_approval"
    TURN_FAILED = "turn_failed"


@dataclass(frozen=True, slots=True)
class TurnEvent:
    """One ordered event from a turn-local event stream."""

    version: int
    thread_id: str
    turn_id: str
    sequence: int
    kind: TurnEventKind
    payload: object

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible event envelope."""
        payload = asdict(self.payload) if is_dataclass(self.payload) else self.payload
        return {
            "version": self.version,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "sequence": self.sequence,
            "kind": self.kind.value,
            "payload": payload,
        }


@dataclass(frozen=True, slots=True)
class ActivityPayload:
    phase: str
    detail: str = ""
    reset_timer: bool = False


@dataclass(frozen=True, slots=True)
class TextPayload:
    text: str
    message_id: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalActionPayload:
    """One HITL action waiting for a human decision (broker-safe copy)."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    allowed_decisions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApprovalPayload:
    """Structured HITL interrupt for interactive approval UIs (replay-safe)."""

    actions: tuple[ApprovalActionPayload, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolCallPayload:
    call_id: str
    name: str
    args_preview: str


@dataclass(frozen=True, slots=True)
class ToolBatchPayload:
    calls: tuple[ToolCallPayload, ...]
    parallel: bool
    group_id: str | None = None
    items: tuple[ToolItemPayload, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolBatchFinishedPayload:
    group_id: str


@dataclass(frozen=True, slots=True)
class SubagentStatusPayload:
    """Transient stage of one running subagent row.

    ``status`` is one of ``calling_tools`` / ``reasoning`` / ``answering``,
    or ``None`` to clear the stage (parent finished/errored/cancelled).
    """

    parent_id: str
    status: str | None = None


@dataclass(frozen=True, slots=True)
class ToolFinishedPayload:
    item_id: str
    status: str
    preview: str | None = None
    error: bool = False


@dataclass(frozen=True, slots=True)
class ToolResultPayload:
    """Legacy tool completion used when per-item events are unavailable."""

    name: str
    status: str
    sub: bool = False
    call_id: str | None = None


@dataclass(frozen=True, slots=True)
class PlanEntryPayload:
    content: str
    priority: str = "medium"
    status: str = "pending"


@dataclass(frozen=True, slots=True)
class PlanPayload:
    plan_id: str
    entries: tuple[PlanEntryPayload, ...]


@dataclass(frozen=True, slots=True)
class PlanRemovedPayload:
    plan_id: str


@dataclass(frozen=True, slots=True)
class DiffPayload:
    call_id: str
    path: str
    new_text: str
    old_text: str | None = None


@dataclass(frozen=True, slots=True)
class ToolItemPayload:
    item_id: str
    call_id: str | None
    name: str
    category: str
    label: str
    path: str | None
    status: str
    preview: str | None
    error: bool
    sub: bool
    parent_id: str | None
    workspace_changed: bool = False
    # Subagent metadata (top-level ``task`` items only). Optional with
    # defaults so older events without the fields keep deserializing.
    subagent_name: str | None = None
    subagent_model: str | None = None
    subagent_reasoning_effort: str | None = None
    subagent_model_inherited: bool = False
    subagent_reasoning_inherited: bool = False


@dataclass(frozen=True, slots=True)
class UsagePayload:
    turn_input: int = 0
    turn_output: int = 0
    turn_cache: int = 0
    last_input: int = 0
    last_output: int = 0
    last_cache: int = 0
    output_tokens_per_second: float | None = None
    ttft_s: float | None = None
    rate_basis: str = "end_to_end"
    rate_estimated: bool = False
    context_size: int | None = None
    # Completed model calls in the turn so far (the bottombar "N steps").
    # Carried on the usage event because the step count is only known to the
    # streaming parser, while the chrome that renders it is fed by usage events.
    model_calls: int = 0


@dataclass(frozen=True, slots=True)
class TurnChange:
    """One file a turn created, modified or deleted, with its own line counts.

    Counts are this *turn's* contribution, not the workspace's standing delta against
    ``HEAD``: a file edited by three turns carries three deltas, and reverting one turn
    means undoing its own numbers.  ``binary`` means the file changed but has no line
    counts to report.
    """

    path: str
    #: ``added`` | ``modified`` | ``deleted`` | ``renamed``
    status: str
    insertions: int = 0
    deletions: int = 0
    binary: bool = False


@dataclass(frozen=True, slots=True)
class TurnChangesPayload:
    """The files one turn touched, emitted once as the turn settles.

    ``total`` is how many files changed and ``changes`` is the bounded list of them, so
    a turn that rewrote a whole tree says so without sending it.
    """

    changes: tuple[TurnChange, ...] = ()
    total: int = 0


@dataclass(frozen=True, slots=True)
class TurnTerminalPayload:
    """Bounded summary emitted exactly once when a turn terminates."""

    status: str
    final_text: str = ""
    error: str | None = None
    interrupted: bool = False
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    compact_events: int = 0
    elapsed_s: float = 0.0
