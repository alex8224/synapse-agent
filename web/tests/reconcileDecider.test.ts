/**
 * Offline decision tests for the phase-4A formal recovery contract.
 *
 * `decideResumeAfterDrop` maps a `runtime.session.reconcile` snapshot +
 * pre-drop bookkeeping (baseline epoch, last delivered cursor) to one recovery
 * action. Every rule mirrors the Python domain tests:
 *
 * - epoch change (broker stream replaced) => full resync, never cursor resume;
 * - cursor outside the retention window => explicit cursor gap => resync;
 * - a running turn whose live prefix was evicted => `incomplete`, never a
 *   fake-lossless claim;
 * - persist-vs-reconnect races surface as `covered` probes (detectable through
 *   `isCoveredTurn`) without changing a still-valid cursor resume;
 * - an old peer without reconcile support degrades to the legacy bounded
 *   cursor behavior (`legacy_resume`), never a silent `after=0`.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  decideResumeAfterDrop,
  type ReconcileResumeInput,
} from '../src/stores/recoveryDecider.ts';
import { isCoveredTurn } from '../src/client/recoverability.ts';
import type { SessionRecoverabilityResult } from '../src/client/types.ts';

function snapshot(overrides: Partial<SessionRecoverabilityResult> = {}): SessionRecoverabilityResult {
  return {
    project_id: 'p',
    thread_id: 't',
    history_available: true,
    history_total_turns: 0,
    live_epoch: 'epoch-1',
    live_latest_sequence: 10,
    live_oldest_sequence: 1,
    live_dropped_through: 0,
    active_turn_id: null,
    latest_turn_id: null,
    latest_turn_first_sequence: null,
    latest_turn_retained_from: null,
    latest_turn_intact: true,
    probe: [],
    ...overrides,
  };
}

function input(overrides: Partial<ReconcileResumeInput> = {}): ReconcileResumeInput {
  return {
    snapshot: snapshot(),
    baselineEpoch: 'epoch-1',
    cursor: 8,
    fallbackAfter: 0,
    hadEvents: true,
    ...overrides,
  };
}

test('same epoch and in-window cursor resumes from the exact cursor', () => {
  const action = decideResumeAfterDrop(input());
  assert.equal(action.action, 'resume');
  if (action.action === 'resume') assert.equal(action.after, 8);
});

test('epoch change forces a full resync, never cursor resume', () => {
  const action = decideResumeAfterDrop(
    input({ snapshot: snapshot({ live_epoch: 'epoch-2' }) }),
  );
  assert.equal(action.action, 'resync');
  if (action.action === 'resync') {
    assert.equal(action.reason, 'epoch_changed');
  }
});

test('cursor below the eviction watermark is an explicit gap', () => {
  const action = decideResumeAfterDrop(
    input({ cursor: 4, snapshot: snapshot({ live_dropped_through: 6 }) }),
  );
  assert.equal(action.action, 'resync');
  if (action.action === 'resync') assert.equal(action.reason, 'cursor_gap');
});

test('cursor ahead of the stream latest is an explicit gap', () => {
  const action = decideResumeAfterDrop(input({ cursor: 99 }));
  assert.equal(action.action, 'resync');
  if (action.action === 'resync') assert.equal(action.reason, 'cursor_gap');
});

test('cursor at the retention boundary is continuable', () => {
  const action = decideResumeAfterDrop(
    input({ cursor: 6, snapshot: snapshot({ live_dropped_through: 6 }) }),
  );
  assert.equal(action.action, 'resume');
});

test('missing baseline epoch with a cursor cannot prove ownership -> resync', () => {
  const action = decideResumeAfterDrop(input({ baselineEpoch: null }));
  assert.equal(action.action, 'resync');
  if (action.action === 'resync') assert.equal(action.reason, 'epoch_unknown');
});

test('an active turn with an evicted prefix is incomplete, not lossless', () => {
  const action = decideResumeAfterDrop(
    input({
      snapshot: snapshot({
        active_turn_id: 'turn-9',
        latest_turn_id: 'turn-9',
        latest_turn_first_sequence: 1,
        latest_turn_retained_from: 5,
        latest_turn_intact: false,
      }),
    }),
  );
  assert.equal(action.action, 'incomplete');
});

test('no snapshot (old peer) degrades to legacy bounded cursor resume', () => {
  const withCursor = decideResumeAfterDrop(input({ snapshot: null, cursor: 8 }));
  assert.equal(withCursor.action, 'legacy_resume');
  if (withCursor.action === 'legacy_resume') assert.equal(withCursor.after, 8);

  const fresh = decideResumeAfterDrop(
    input({ snapshot: null, cursor: null, fallbackAfter: 13, hadEvents: false }),
  );
  assert.equal(fresh.action, 'legacy_resume');
  if (fresh.action === 'legacy_resume') assert.equal(fresh.after, 13);
});

test('nothing delivered before the drop resyncs from history', () => {
  const action = decideResumeAfterDrop(
    input({ cursor: null, hadEvents: false }),
  );
  assert.equal(action.action, 'resync');
  if (action.action === 'resync') assert.equal(action.reason, 'no_events');
});

test('a watch with no recorded cursor resyncs instead of inventing 0', () => {
  const action = decideResumeAfterDrop(input({ cursor: null, hadEvents: true }));
  assert.equal(action.action, 'resync');
  if (action.action === 'resync') assert.equal(action.reason, 'no_cursor');
});

test('persist-vs-reconnect race: a covered probe is detectable without corrupting resume', () => {
  const snap = snapshot({
    history_total_turns: 1,
    probe: [{ turn_id: 'turn-a', covered: true }],
  });
  // The client was away while turn-a settled: it is durable, so replaying its
  // events would duplicate content the history page will render.
  assert.equal(isCoveredTurn(snap, 'turn-a'), true);
  assert.equal(isCoveredTurn(snap, 'turn-b'), false);
  assert.equal(isCoveredTurn(null, 'turn-a'), false);
  // The decision stays a cursor resume: turn-a's events lie strictly after the
  // last delivered cursor and the broker window still holds them.
  const action = decideResumeAfterDrop(input({ snapshot: snap }));
  assert.equal(action.action, 'resume');
});

test('covered duplicate suppression drops only durable turns', () => {
  // Duplicate replay of an already-durable turn is filtered before rendering
  // (attach flush path): probe membership is the exact guard used.
  const events = [
    { event: { turn_id: 'turn-a' } },
    { event: { turn_id: 'turn-b' } },
  ] as Array<{ event: { turn_id: string } }>;
  const snap = snapshot({ probe: [{ turn_id: 'turn-a', covered: true }] });
  const kept = events.filter((entry) => !isCoveredTurn(snap, entry.event.turn_id));
  assert.deepEqual(kept.map((entry) => entry.event.turn_id), ['turn-b']);
});
