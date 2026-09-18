"""Read-only recovery DTOs for the explicit history/live reconcile protocol.

The durable transcript projection and the in-memory event broker are two
independent stores.  They share no sequence watermark: the transcript is
ordered by ``turn_seq``/``event_seq`` and the broker by its own session
sequence, and the only durable identity both understand is ``turn_id``.  This
slice therefore does **not** pretend the two stores are one atomic log.

``reconcile_session`` returns one bounded, read-only snapshot a recovery
client uses as an explicit precondition:

- durable transcript coverage (``history_available`` / ``history_total_turns``
  plus membership of a bounded set of ``probe_turn_ids`` against the durable
  ``transcript_turns`` table), and
- live broker state (``live_epoch`` stream identity, retention bounds, and the
  newest observed turn replay boundary).

The caller decides recovery with these guarantees, never with ``after=0`` or a
history tail page masquerading as a full restore:

- If the stored ``live_epoch`` differs from the snapshot's, the stored broker
  cursor belongs to a different stream instance (session reopened / daemon
  restarted) and must not be resumed: restart from history coverage.
- A cursor within the current epoch is only continuable when it is not below
  the eviction watermark; otherwise the client must mark the stream
  ``resync_required``/``incomplete`` rather than skip silently.
- A running (not-yet-settled) turn is never reported as durable: probing its
  ``turn_id`` returns ``covered=False`` until settlement appends it.  Until
  then its broker replay boundary is ``latest_turn_*``; if
  ``latest_turn_intact`` is False its live prefix may have been evicted, so
  the client must not claim lossless live replay of that turn.

The method never opens a session, creates an agent, or cancels a turn; it
requires an already-open session so the live half of the snapshot exists.
"""
from __future__ import annotations

from dataclasses import dataclass

from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "MAX_RECONCILE_PROBE_TURNS",
    "MAX_RECONCILE_TURN_ID_BYTES",
    "ReconcileSessionQuery",
    "SessionRecoverabilityView",
    "TurnCoverageProbe",
]

#: Bounded number of turn ids a client may probe for durable coverage.
MAX_RECONCILE_PROBE_TURNS = 32
#: Upper bound on one probed turn id (transport turn-id bound is 256 bytes).
MAX_RECONCILE_TURN_ID_BYTES = 256


def _validate_turn_id(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError("probe turn ids must be non-empty strings")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("probe turn ids must be valid UTF-8") from None
    if size > MAX_RECONCILE_TURN_ID_BYTES:
        raise ValueError(
            f"probe turn id exceeds the {MAX_RECONCILE_TURN_ID_BYTES} byte limit"
        )
    return value


@dataclass(frozen=True, slots=True)
class ReconcileSessionQuery:
    """Ask for one recovery snapshot of an open session.

    ``probe_turn_ids`` are turn ids the client already saw on the live stream
    (bounded) whose durable coverage it wants confirmed or refuted at the
    snapshot instant.  Duplicates are collapsed and the probe is capped.
    """

    session: SessionRef
    probe_turn_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.session) is not SessionRef:
            raise ValueError("session must be a SessionRef")
        if not self.session.project_id or not self.session.thread_id:
            raise ValueError("session must have non-empty project_id and thread_id")
        if self.probe_turn_ids is None:
            raise ValueError("probe_turn_ids must be a tuple of strings")
        seen: list[str] = []
        for item in self.probe_turn_ids:
            validated = _validate_turn_id(item)
            if validated not in seen:
                seen.append(validated)
        if len(seen) > MAX_RECONCILE_PROBE_TURNS:
            raise ValueError(
                f"probe_turn_ids must not exceed {MAX_RECONCILE_PROBE_TURNS} entries"
            )
        object.__setattr__(self, "probe_turn_ids", tuple(seen))


@dataclass(frozen=True, slots=True)
class TurnCoverageProbe:
    """Durable transcript membership for one requested turn id."""

    turn_id: str
    covered: bool


@dataclass(frozen=True, slots=True)
class SessionRecoverabilityView:
    """One bounded recovery snapshot (history coverage + live stream state).

    ``history_available=False`` mirrors the history port: the transcript
    projection does not exist for this session and the caller must not treat
    it as empty history.  ``live_*`` fields describe the open session's broker
    instance at snapshot time; see the module docstring for the epoch/replay
    semantics the caller must implement.
    """

    project_id: str
    thread_id: str
    history_available: bool
    history_total_turns: int
    live_epoch: str
    live_latest_sequence: int
    live_oldest_sequence: int
    live_dropped_through: int
    active_turn_id: str | None
    latest_turn_id: str | None
    latest_turn_first_sequence: int | None
    latest_turn_retained_from: int | None
    latest_turn_intact: bool
    probe: tuple[TurnCoverageProbe, ...]
