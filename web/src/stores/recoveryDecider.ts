/**
 * Pure recovery decision logic for the Web console's phase-4A contract.
 *
 * The store asks this module what to do after an unexpected drop / watch
 * termination, given (a) the formal `runtime.session.reconcile` snapshot the
 * server returned, (b) the broker epoch recorded when the pre-drop watch was
 * attached, and (c) the last delivered live cursor.  The module never touches
 * the socket, never fakes a lossless resume, and never invents `after=0`.
 *
 * Rules (all mirror the Python domain tests in
 * `tests/test_runtime_recovery_s4_a.py`):
 *
 * - no snapshot (reconcile unsupported / old peer): keep the legacy bounded
 *   behavior - resume from the exact last cursor, or from the freshly opened
 *   latest sequence when nothing was ever delivered;
 * - epoch changed since the baseline => the stored cursor belongs to a
 *   different broker stream (session reopened / daemon restarted): full resync
 *   from the history snapshot (`resync`);
 * - cursor below the eviction watermark or ahead of the stream => explicit
 *   cursor gap: resync, never a silent skip;
 * - a running turn whose live prefix was evicted (`latest_turn_intact=false`)
 *   cannot be replayed losslessly until it settles: `incomplete`;
 * - otherwise the cursor is continuable within the current epoch: resume.
 */
import type { SessionRecoverabilityResult } from '../client/types.ts';

export type RecoveryAction =
  | { action: 'resume'; after: number; detail?: string }
  | { action: 'legacy_resume'; after: number; detail?: string }
  | { action: 'resync'; reason: string; detail: string }
  | { action: 'incomplete'; detail: string };

export interface ReconcileResumeInput {
  /** The formal snapshot, or null when the peer does not support reconcile. */
  snapshot: SessionRecoverabilityResult | null;
  /** Broker epoch recorded when the pre-drop watch was attached (or null). */
  baselineEpoch: string | null;
  /** Last delivered live cursor, or null when no event was ever delivered. */
  cursor: number | null;
  /** `open.view.latest_sequence` of the freshly opened session (fallback). */
  fallbackAfter: number;
  /** Whether a live watch had delivered events before the drop. */
  hadEvents: boolean;
}

export function decideResumeAfterDrop(input: ReconcileResumeInput): RecoveryAction {
  const { snapshot, baselineEpoch, cursor, fallbackAfter } = input;
  if (snapshot === null) {
    // Old peer: reconcile unsupported. Keep the pre-reconcile bounded behavior:
    // resume from the exact last cursor when events were delivered, otherwise
    // start after the freshly opened server snapshot. Never invent 0.
    const after = cursor ?? fallbackAfter;
    return {
      action: 'legacy_resume',
      after,
      detail: cursor === null ? 'recovery snapshot unavailable; resumed after fresh open' : 'recovery snapshot unavailable; resumed from last cursor',
    };
  }

  // A running turn whose live prefix was evicted is not losslessly replayable
  // until settlement persists it; surface incomplete instead of fake-lossless.
  const active = snapshot.active_turn_id;
  if (
    active !== null &&
    snapshot.latest_turn_id === active &&
    snapshot.latest_turn_intact === false
  ) {
    return {
      action: 'incomplete',
      detail: `active turn ${active} live prefix was evicted; replay incomplete until settlement`,
    };
  }

  if (cursor !== null) {
    if (baselineEpoch === null) {
      return {
        action: 'resync',
        reason: 'epoch_unknown',
        detail: 'no baseline epoch recorded; resyncing from history snapshot',
      };
    }
    if (baselineEpoch !== snapshot.live_epoch) {
      return {
        action: 'resync',
        reason: 'epoch_changed',
        detail: `broker epoch changed (${baselineEpoch} -> ${snapshot.live_epoch}); stored cursor is stale`,
      };
    }
    if (cursor < snapshot.live_dropped_through || cursor > snapshot.live_latest_sequence) {
      return {
        action: 'resync',
        reason: 'cursor_gap',
        detail: `stored cursor ${cursor} is outside the retained window [${snapshot.live_dropped_through}..${snapshot.live_latest_sequence}]`,
      };
    }
    return { action: 'resume', after: cursor };
  }

  if (!input.hadEvents) {
    return {
      action: 'resync',
      reason: 'no_events',
      detail: 'no live cursor was ever delivered; resyncing from history snapshot',
    };
  }
  // Fallthrough: no cursor recorded yet although the watch was attached. A full
  // resync from history is the only honest option (never a silent after=0).
  return {
    action: 'resync',
    reason: 'no_cursor',
    detail: 'watch cursor missing after an unexpected drop; resyncing from history snapshot',
  };
}

/** Reason strings used by `resync` actions (observable in recoveryDetail). */
export const RESYNC_REASONS = [
  'epoch_changed',
  'epoch_unknown',
  'cursor_gap',
  'no_events',
  'no_cursor',
] as const;

/** Human label for one resync reason. */
export function resyncReasonLabel(reason: string): string {
  switch (reason) {
    case 'epoch_changed':
      return 'session stream was replaced (epoch changed)';
    case 'epoch_unknown':
      return 'cannot prove the stored cursor belongs to the current stream';
    case 'cursor_gap':
      return 'stored cursor fell outside the broker retention window';
    case 'no_events':
      return 'nothing was delivered before the drop';
    case 'no_cursor':
      return 'no watch cursor was recorded';
    default:
      return 'history/live reconciliation required';
  }
}
