/**
 * Pure, DOM-free decision for the live-replay cursor used when the console
 * (re-)attaches to a session.
 *
 * Switching away and back while a turn is still running means the browser must
 * ask the daemon's broker to replay that turn's events.  The broker retains a
 * bounded window and reports, for its newest turn, both the sequence of the
 * turn's first event (`latest_turn_first_sequence`) and the earliest sequence it
 * *still* holds (`latest_turn_retained_from`).  The console used to fall back to
 * the tail (`latest_sequence`) whenever the prefix was not fully intact, which
 * silently dropped the head of the running turn even though the daemon still
 * held it.  This module decides the cursor from that snapshot, never inventing
 * `after = 0` and never letting a malformed field drive the decision.
 *
 * Two invariants keep a chosen cursor continuable:
 *
 * - broker sequences start at 1, so a turn boundary is only usable when it is a
 *   *positive* safe integer; a `0` is a malformed field, not a real position;
 * - the daemon evicts the first PREVIEW event anywhere in its buffer, so a
 *   retained LOSSLESS event can precede an evicted one and
 *   `latest_turn_retained_from` can sit *below* `live_dropped_through`.  The
 *   watch answers `replay_gap` (`gap = cursor < dropped_through`) for a cursor
 *   below the watermark, so the continuable start is the watermark, not
 *   `retained_from - 1`.
 */
import type { SessionRecoverabilityResult } from '../client/types.ts';

export interface AttachCursor {
  /** Exclusive broker sequence to watch from (`after`). Never negative. */
  after: number;
  /**
   * Whether the whole running turn was replayed losslessly.  `false` means the
   * caller must surface `incomplete` (the head of the turn was evicted or is
   * unknown), even though the retained remainder may still have been replayed.
   */
  complete: boolean;
}

/** Coerce one wire numeric field to a non-negative safe integer, else null. */
function nonNegativeIntOrNull(value: number | null | undefined): number | null {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) return null;
  return value;
}

/**
 * Coerce a turn boundary to a positive safe integer, else null.
 *
 * Broker sequences start at 1, so a `0` (or a fraction / `NaN` / a negative) is
 * malformed.  Accepting it would fabricate a replay boundary the stream never
 * had; treating it as unusable degrades the decision to the next honest branch.
 */
function positiveIntOrNull(value: number | null | undefined): number | null {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1) return null;
  return value;
}

/**
 * Choose the `after` cursor (and whether the replay is lossless) for one attach.
 *
 * @param view the opened session view (`active_turn_id` + `latest_sequence`)
 * @param coverage the attach snapshot, or null when reconcile is unavailable
 */
export function chooseAttachCursor(
  view: { activeTurnId: string | null; latestSequence: number },
  coverage: SessionRecoverabilityResult | null,
): AttachCursor {
  const latestSequence = nonNegativeIntOrNull(view.latestSequence);

  if (view.activeTurnId === null) {
    // Nothing is running: the just-finished turn is already durable in the
    // transcript projection, so the history page renders it. Watching from the
    // tail avoids replaying settled events a second time. A malformed
    // `latest_sequence` is an *unknown* position -- `after = 0, complete = true`
    // would fabricate a full replay -- so it degrades to an incomplete tail.
    if (latestSequence === null) return { after: 0, complete: false };
    return { after: latestSequence, complete: true };
  }

  if (coverage !== null && coverage.latest_turn_id === view.activeTurnId) {
    const firstSequence = positiveIntOrNull(coverage.latest_turn_first_sequence);
    if (coverage.latest_turn_intact && firstSequence !== null) {
      // The broker still holds the whole turn: replay from its first event for a
      // lossless reconstruction of the running turn.
      return { after: Math.max(firstSequence - 1, 0), complete: true };
    }
    const retainedFrom = positiveIntOrNull(coverage.latest_turn_retained_from);
    if (retainedFrom !== null) {
      // The prefix was evicted but the broker still holds the remainder: replay
      // everything it retains (not just the tail) and report the loss through
      // `complete: false`. `live_dropped_through` is the continuable start: a
      // cursor below it is refused as `replay_gap`, so the retained remainder
      // would never actually replay. Clamped to a non-negative integer.
      const droppedThrough = nonNegativeIntOrNull(coverage.live_dropped_through) ?? 0;
      return { after: Math.max(retainedFrom - 1, droppedThrough), complete: false };
    }
    // The turn is running but the broker reports no usable retained boundary:
    // fall back to the tail, still incomplete because the head is unrecoverable.
    return { after: latestSequence ?? 0, complete: false };
  }

  // A different (or unknown) latest turn, or no snapshot at all: this is not the
  // running turn we can replay from its boundary, so watch from the tail and
  // flag the running turn as incomplete.
  return { after: latestSequence ?? 0, complete: false };
}
