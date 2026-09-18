/**
 * Offline tests for the delta batching that sits between the live event stream
 * and the store.
 *
 * Batching must be invisible in what it produces: the same text, in the same
 * order, with every non-delta event still folded exactly where it arrived.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import type { RuntimeEvent } from '../src/client/types.ts';
import {
  coalesceLiveEvents,
  DELTA_COALESCE_MS,
  foldLiveEvents,
  isCoalescibleDeltaKind,
  type LiveEventEntry,
} from '../src/stores/liveDeltaBatch.ts';
import {
  reduceRuntimeEvent,
  type LiveReducibleState,
} from '../src/stores/liveEventReducer.ts';

const AT = new Date(0);
const now = () => AT;

function baseState(): LiveReducibleState {
  return {
    messages: [],
    activeTurnId: null,
    runtimeStatus: 'idle',
    steerQueueCount: 0,
    pendingApproval: null,
    activity: null,
    usage: null,
    metricsLabel: '',
  };
}

function event(
  kind: string,
  payload: unknown,
  turnId = 't1',
  sequence = 1,
  messageId?: string,
): RuntimeEvent {
  return {
    sequence,
    turn_sequence: sequence,
    turn_id: turnId,
    kind,
    payload: { ...(payload as Record<string, unknown>), ...(messageId ? { message_id: messageId } : {}) },
    version: 1,
  };
}

function entry(ev: RuntimeEvent): LiveEventEntry {
  return { event: ev, subscription_id: 's1' };
}

function texts(entries: readonly LiveEventEntry[]): string[] {
  return entries.map((item) => String((item.event.payload as Record<string, unknown>).text ?? ''));
}

test('the display window is a frame-scale delay, not a batch of seconds', () => {
  assert.ok(DELTA_COALESCE_MS >= 16 && DELTA_COALESCE_MS <= 50, `window is ${DELTA_COALESCE_MS}ms`);
});

test('only append-only text deltas are coalescible', () => {
  assert.equal(isCoalescibleDeltaKind('reasoning_delta'), true);
  assert.equal(isCoalescibleDeltaKind('answer_delta'), true);
  for (const kind of ['reasoning_completed', 'answer_completed', 'tool_started', 'activity_updated']) {
    assert.equal(isCoalescibleDeltaKind(kind), false, `${kind} must not be merged`);
  }
});

test('consecutive deltas of one row merge into a single event', () => {
  const merged = coalesceLiveEvents([
    entry(event('reasoning_delta', { text: 'a' }, 't1', 1)),
    entry(event('reasoning_delta', { text: 'b' }, 't1', 2)),
    entry(event('reasoning_delta', { text: 'c' }, 't1', 3)),
  ]);
  assert.equal(merged.length, 1);
  assert.deepEqual(texts(merged), ['abc']);
  // The newest envelope survives, so the cursor the store advances stays monotonic.
  assert.equal(merged[0].event.sequence, 3);
});

test('a non-delta event between deltas keeps the order', () => {
  const merged = coalesceLiveEvents([
    entry(event('reasoning_delta', { text: 'a' }, 't1', 1)),
    entry(event('reasoning_completed', { text: 'ab' }, 't1', 2)),
    entry(event('answer_delta', { text: 'x' }, 't1', 3)),
    entry(event('answer_delta', { text: 'y' }, 't1', 4)),
  ]);
  assert.deepEqual(
    merged.map((item) => item.event.kind),
    ['reasoning_delta', 'reasoning_completed', 'answer_delta'],
  );
  assert.deepEqual(texts(merged), ['a', 'ab', 'xy']);
});

test('deltas of different rows or turns never merge', () => {
  const other = coalesceLiveEvents([
    entry(event('reasoning_delta', { text: 'a' }, 't1', 1)),
    entry(event('reasoning_delta', { text: 'b' }, 't2', 2)),
    entry(event('reasoning_delta', { text: 'c' }, 't1', 3, 'm1')),
    entry(event('reasoning_delta', { text: 'd' }, 't1', 4, 'm2')),
  ]);
  assert.equal(other.length, 4);
  assert.deepEqual(texts(other), ['a', 'b', 'c', 'd']);
});

test('an answer delta for another message id is not folded into the previous row', () => {
  const merged = coalesceLiveEvents([
    entry(event('answer_delta', { text: 'a' }, 't1', 1, 'm1')),
    entry(event('answer_delta', { text: 'b' }, 't1', 2, 'm1')),
    entry(event('answer_delta', { text: 'c' }, 't1', 3, 'm2')),
  ]);
  assert.deepEqual(texts(merged), ['ab', 'c']);
});

test('a malformed payload passes through instead of merging', () => {
  const merged = coalesceLiveEvents([
    entry(event('reasoning_delta', { text: 42 }, 't1', 1)),
    entry(event('reasoning_delta', { text: 'b' }, 't1', 2)),
  ]);
  assert.equal(merged.length, 2);
});

test('a merged run folds to exactly the transcript the per-event fold produced', () => {
  const events = [
    event('activity_updated', { phase: 'thinking' }, 't1', 1),
    event('reasoning_delta', { text: 'one ' }, 't1', 2),
    event('reasoning_delta', { text: 'two ' }, 't1', 3),
    event('reasoning_delta', { text: 'three' }, 't1', 4),
    event('reasoning_completed', { text: 'one two three' }, 't1', 5),
    event('answer_delta', { text: 'a' }, 't1', 6),
    event('answer_delta', { text: 'b' }, 't1', 7),
    event('answer_completed', { text: 'ab' }, 't1', 8),
  ];
  const perEvent = events.reduce<LiveReducibleState>(
    (state, ev) => ({ ...state, ...reduceRuntimeEvent(state, ev, now) }),
    baseState(),
  );
  const batched = foldLiveEvents(
    baseState(),
    coalesceLiveEvents(events.map((ev) => entry(ev))),
    now,
  );
  assert.deepEqual(batched.messages, perEvent.messages);
  assert.equal(batched.runtimeStatus, perEvent.runtimeStatus);
  assert.equal(batched.activeTurnId, perEvent.activeTurnId);
});

test('folding a run of one event is the per-event fold', () => {
  const ev = event('reasoning_delta', { text: 'only' }, 't1', 1);
  const state = baseState();
  assert.deepEqual(
    foldLiveEvents(state, [entry(ev)], now).messages,
    { ...state, ...reduceRuntimeEvent(state, ev, now) }.messages,
  );
});

test('folding never mutates the state it was given', () => {
  const state = baseState();
  const snapshot = JSON.stringify(state);
  foldLiveEvents(state, [entry(event('reasoning_delta', { text: 'x' }, 't1', 1))]);
  assert.equal(JSON.stringify(state), snapshot);
});