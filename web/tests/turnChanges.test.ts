/**
 * Offline tests for the turn's change cards.
 *
 * The event is emitted as the turn settles, so it always arrives *after* the turn's
 * terminal one -- the case these tests pin down, together with the reload path that
 * reads the same list out of the projection.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import type { RuntimeEvent } from '../src/client/types.ts';
import { mapHistoryEvents, turnChangeViews } from '../src/stores/historyMapper.ts';
import { reduceRuntimeEvent, type LiveReducibleState } from '../src/stores/liveEventReducer.ts';

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

function event(kind: string, payload: unknown, turnId = 't1', sequence = 1): RuntimeEvent {
  return {
    sequence,
    turn_id: turnId,
    turn_sequence: sequence,
    version: 1,
    kind,
    payload,
  } as RuntimeEvent;
}

function apply(state: LiveReducibleState, next: RuntimeEvent): LiveReducibleState {
  return { ...state, ...reduceRuntimeEvent(state, next, now) };
}

const CHANGES = {
  changes: [
    { path: 'a.py', status: 'modified', insertions: 3, deletions: 1, binary: false },
    { path: 'b.py', status: 'added', insertions: 7, deletions: 0, binary: false },
  ],
  total: 2,
};

test('a settled turn gains its change cards from the late event', () => {
  // The whole point: `turn_changes` is emitted as the turn settles, so it arrives after
  // `turn_completed` -- and it must still land on that turn, without reviving it.
  let state = apply(baseState(), event('user', { text: 'do it' }));
  state = apply(state, event('answer_completed', { text: 'done' }, 't1', 2));
  state = apply(state, event('turn_completed', { status: 'completed' }, 't1', 3));
  assert.equal(state.activeTurnId, null, 'the terminal event ends the turn');

  state = apply(state, event('turn_changes', CHANGES, 't1', 4));

  const row = state.messages.find((m) => m.type === 'changes');
  assert.ok(row, 'the turn must gain a changes row');
  assert.equal(row?.turnId, 't1');
  assert.equal(row?.changesTotal, 2);
  assert.deepEqual(row?.changes?.map((c) => c.path), ['a.py', 'b.py']);
  assert.equal(state.activeTurnId, null, 'a settled turn must stay settled');
  assert.equal(state.runtimeStatus, 'idle', 'and must not seize the runtime status');
});

test('the cards land on their own turn, not on the newest one', () => {
  let state = apply(baseState(), event('user', { text: 'first' }, 't1'));
  state = apply(state, event('answer_completed', { text: 'one' }, 't1', 2));
  state = apply(state, event('turn_completed', { status: 'completed' }, 't1', 3));
  // A newer turn is already under way when the settled turn's list arrives.
  state = apply(state, event('user', { text: 'second' }, 't2', 4));
  state = apply(state, event('answer_completed', { text: 'two' }, 't2', 5));

  state = apply(state, event('turn_changes', CHANGES, 't1', 6));

  const rows = state.messages.filter((m) => m.type === 'changes');
  assert.equal(rows.length, 1);
  assert.equal(rows[0].turnId, 't1');
  const index = state.messages.findIndex((m) => m.type === 'changes');
  const firstOfNewer = state.messages.findIndex((m) => m.turnId === 't2');
  assert.ok(
    index !== -1 && firstOfNewer !== -1 && index < firstOfNewer,
    'the row belongs at the end of its own turn, before the newer one',
  );
});

test('applying the same list twice rewrites the row instead of stacking it', () => {
  let state = apply(baseState(), event('user', { text: 'do it' }));
  state = apply(state, event('turn_completed', { status: 'completed' }, 't1', 2));
  state = apply(state, event('turn_changes', CHANGES, 't1', 3));
  state = apply(state, event('turn_changes', CHANGES, 't1', 4));
  assert.equal(state.messages.filter((m) => m.type === 'changes').length, 1);
});

test('a turn that changed nothing adds no row', () => {
  let state = apply(baseState(), event('user', { text: 'do it' }));
  state = apply(state, event('turn_completed', { status: 'completed' }, 't1', 2));
  state = apply(state, event('turn_changes', { changes: [], total: 0 }, 't1', 3));
  assert.equal(state.messages.some((m) => m.type === 'changes'), false);
});

test('a malformed change list is ignored rather than painted', () => {
  assert.deepEqual(turnChangeViews(null), []);
  assert.deepEqual(turnChangeViews([{ path: '' }, 'nope', { status: 'modified' }]), []);
  assert.deepEqual(turnChangeViews([{ path: 'a.py' }]), [
    {
      path: 'a.py',
      status: 'modified',
      insertions: 0,
      deletions: 0,
      binary: false,
      reverted: false,
    },
  ]);
  // A negative or non-numeric count is nothing, not a fabricated zero with a sign.
  assert.equal(turnChangeViews([{ path: 'a.py', insertions: -4 }])[0].insertions, 0);
  assert.equal(turnChangeViews([{ path: 'a.py', deletions: 'x' }])[0].deletions, 0);
  assert.equal(turnChangeViews([{ path: 'a.py', binary: true }])[0].binary, true);
});

test('a reload paints the same cards from the projection', () => {
  const rows = mapHistoryEvents(
    [
      { kind: 'user', text: 'do it', tool_calls: [], tool_results: [], changes: [], changes_total: 0 },
      {
        kind: 'changes',
        text: '',
        tool_calls: [],
        tool_results: [],
        changes: CHANGES.changes,
        changes_total: 2,
      },
    ],
    { startTurn: 1, pageTag: 't' },
  );
  const row = rows.find((m) => m.type === 'changes');
  assert.ok(row, 'the projection must paint a changes row');
  assert.equal(row?.changesTotal, 2);
  assert.deepEqual(row?.changes?.map((c) => [c.path, c.status]), [
    ['a.py', 'modified'],
    ['b.py', 'added'],
  ]);
  assert.equal(
    rows.some((m) => m.type === 'changes' && (m.changes ?? []).length === 0),
    false,
    'a turn that changed nothing must not paint an empty card block',
  );
});
