/**
 * Offline decision tests for `chooseAttachCursor`.
 *
 * They pin the attach-time replay cursor: a running turn whose prefix is still
 * retained must replay from the broker's earliest retained sequence (never only
 * the tail), a fully intact turn replays from its first event, and every branch
 * reports the correct `complete` flag. Malformed numeric fields must never leak
 * a bogus cursor (no negative `after`, no `NaN`).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { chooseAttachCursor } from '../src/stores/attachCursor.ts';
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

test('nothing running watches from the tail and is complete', () => {
  const cursor = chooseAttachCursor({ activeTurnId: null, latestSequence: 42 }, snapshot());
  assert.deepEqual(cursor, { after: 42, complete: true });
});

test('nothing running ignores coverage and never invents after=0', () => {
  const cursor = chooseAttachCursor(
    { activeTurnId: null, latestSequence: 7 },
    snapshot({ latest_turn_id: 'turn-1', latest_turn_first_sequence: 1, latest_turn_intact: true }),
  );
  assert.deepEqual(cursor, { after: 7, complete: true });
});

test('an intact running turn replays from its first event losslessly', () => {
  const cursor = chooseAttachCursor(
    { activeTurnId: 'turn-1', latestSequence: 12 },
    snapshot({
      active_turn_id: 'turn-1',
      latest_turn_id: 'turn-1',
      latest_turn_first_sequence: 5,
      latest_turn_retained_from: 5,
      latest_turn_intact: true,
    }),
  );
  assert.deepEqual(cursor, { after: 4, complete: true });
});

test('an evicted prefix replays the retained remainder and is incomplete', () => {
  const cursor = chooseAttachCursor(
    { activeTurnId: 'turn-1', latestSequence: 12 },
    snapshot({
      active_turn_id: 'turn-1',
      latest_turn_id: 'turn-1',
      latest_turn_first_sequence: 2,
      latest_turn_retained_from: 8,
      latest_turn_intact: false,
    }),
  );
  // NEW behaviour: replay from the earliest retained sequence, not the tail.
  assert.deepEqual(cursor, { after: 7, complete: false });
});

test('an interior eviction makes the cursor the dropped-through watermark', () => {
  // The daemon evicts the first PREVIEW event anywhere in its buffer, so a
  // retained LOSSLESS event can precede an evicted one: `retained_from` (8) can
  // sit *below* `live_dropped_through` (10). Watching from `retained_from - 1`
  // (7) is below the watermark, so the watch answers `replay_gap`; the
  // continuable start is the watermark, not `retained_from - 1`.
  const cursor = chooseAttachCursor(
    { activeTurnId: 'turn-1', latestSequence: 20 },
    snapshot({
      active_turn_id: 'turn-1',
      latest_turn_id: 'turn-1',
      latest_turn_first_sequence: 2,
      latest_turn_retained_from: 8,
      latest_turn_intact: false,
      live_dropped_through: 10,
    }),
  );
  assert.deepEqual(cursor, { after: 10, complete: false });
});

test('a watermark below retained_from keeps the retained boundary', () => {
  // The common shape: nothing before the retained prefix was evicted, so the
  // watermark is below it and `retained_from - 1` is already continuable.
  const cursor = chooseAttachCursor(
    { activeTurnId: 'turn-1', latestSequence: 20 },
    snapshot({
      active_turn_id: 'turn-1',
      latest_turn_id: 'turn-1',
      latest_turn_first_sequence: 2,
      latest_turn_retained_from: 8,
      latest_turn_intact: false,
      live_dropped_through: 4,
    }),
  );
  assert.deepEqual(cursor, { after: 7, complete: false });
});

test('an evicted prefix with retained_from=1 replays from sequence 0', () => {
  const cursor = chooseAttachCursor(
    { activeTurnId: 'turn-1', latestSequence: 9 },
    snapshot({
      latest_turn_id: 'turn-1',
      latest_turn_first_sequence: 1,
      latest_turn_retained_from: 1,
      latest_turn_intact: false,
    }),
  );
  assert.deepEqual(cursor, { after: 0, complete: false });
});

test('an intact flag without a first sequence falls back to the tail, incomplete', () => {
  const cursor = chooseAttachCursor(
    { activeTurnId: 'turn-1', latestSequence: 12 },
    snapshot({
      latest_turn_id: 'turn-1',
      latest_turn_first_sequence: null,
      latest_turn_retained_from: null,
      latest_turn_intact: true,
    }),
  );
  assert.deepEqual(cursor, { after: 12, complete: false });
});

test('an evicted prefix without retained_from falls back to the tail, incomplete', () => {
  const cursor = chooseAttachCursor(
    { activeTurnId: 'turn-1', latestSequence: 12 },
    snapshot({
      latest_turn_id: 'turn-1',
      latest_turn_first_sequence: 2,
      latest_turn_retained_from: null,
      latest_turn_intact: false,
    }),
  );
  assert.deepEqual(cursor, { after: 12, complete: false });
});

test('a different latest turn falls back to the tail, incomplete', () => {
  const cursor = chooseAttachCursor(
    { activeTurnId: 'turn-9', latestSequence: 12 },
    snapshot({
      active_turn_id: 'turn-9',
      latest_turn_id: 'turn-1',
      latest_turn_first_sequence: 1,
      latest_turn_retained_from: 1,
      latest_turn_intact: true,
    }),
  );
  assert.deepEqual(cursor, { after: 12, complete: false });
});

test('null coverage falls back to the tail, incomplete', () => {
  const cursor = chooseAttachCursor({ activeTurnId: 'turn-1', latestSequence: 12 }, null);
  assert.deepEqual(cursor, { after: 12, complete: false });
});

test('malformed numeric fields are guarded (NaN / negative)', () => {
  const nanFirst = chooseAttachCursor(
    { activeTurnId: 'turn-1', latestSequence: 12 },
    snapshot({
      latest_turn_id: 'turn-1',
      latest_turn_first_sequence: Number.NaN,
      latest_turn_retained_from: Number.NaN,
      latest_turn_intact: true,
    }),
  );
  // Both boundaries are unusable: tail fallback, still incomplete.
  assert.deepEqual(nanFirst, { after: 12, complete: false });

  const negativeBoundaries = chooseAttachCursor(
    { activeTurnId: 'turn-1', latestSequence: 12 },
    snapshot({
      latest_turn_id: 'turn-1',
      latest_turn_first_sequence: -1,
      latest_turn_retained_from: -3,
      latest_turn_intact: true,
    }),
  );
  assert.deepEqual(negativeBoundaries, { after: 12, complete: false });
});

test('a malformed latest sequence is an unknown position, never a full replay', () => {
  // A `latest_sequence` that is not a non-negative safe integer must not become
  // `after = 0, complete = true`: that would fabricate a replay from a position
  // the snapshot never proved.  It degrades to an incomplete tail instead.
  assert.deepEqual(
    chooseAttachCursor({ activeTurnId: null, latestSequence: Number.NaN }, null),
    { after: 0, complete: false },
  );
  assert.deepEqual(
    chooseAttachCursor({ activeTurnId: 'turn-1', latestSequence: -5 }, null),
    { after: 0, complete: false },
  );
  assert.deepEqual(
    chooseAttachCursor({ activeTurnId: null, latestSequence: 2.5 }, null),
    { after: 0, complete: false },
  );
});

test('a zero turn boundary is malformed (sequences start at 1), so it is unused', () => {
  // Broker sequences start at 1: a `0` boundary is not a real position.  The
  // intact branch is skipped (no first sequence) and the retained branch too (no
  // retained boundary), so the decision degrades to the tail, still incomplete.
  const cursor = chooseAttachCursor(
    { activeTurnId: 'turn-1', latestSequence: 3 },
    snapshot({
      latest_turn_id: 'turn-1',
      latest_turn_first_sequence: 0,
      latest_turn_retained_from: 0,
      latest_turn_intact: true,
    }),
  );
  assert.deepEqual(cursor, { after: 3, complete: false });
});
