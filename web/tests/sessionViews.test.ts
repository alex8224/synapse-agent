/**
 * Offline tests for the per-session live views (stage 3b).
 *
 * A background view is the store state of a session the console is watching but
 * not showing.  The properties pinned here are the ones that keep a switch
 * lossless:
 *
 * - a view mirrors exactly the per-session fields and round-trips through
 *   snapshot/restore;
 * - folding into a view is the *same* fold the active session uses -- the shared
 *   `foldLiveEvents` plus the terminal-turn usage bookkeeping -- so a background
 *   turn reaches the same status/approval/usage a foreground one would, and a
 *   foreign or already-settled event is ignored identically;
 * - the LRU evicts the least-recently-touched view and never the protected one;
 * - a view whose watch ended (`subscriptionId === null`) is recognisable as
 *   stale, because returning to it must re-attach rather than trust it.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { RuntimeEvent } from '../src/client/types.ts';
import {
  MAX_BACKGROUND_VIEWS,
  addTurnUsage,
  foldBackgroundEvents,
  pruneBackgroundViews,
  restoreLiveView,
  sessionKey,
  sessionStatusMarkers,
  snapshotLiveView,
  type BackgroundSessionView,
  type LiveViewSource,
} from '../src/stores/sessionViews.ts';
import { EMPTY_USAGE } from '../src/stores/usageView.ts';
import type { LiveEventEntry } from '../src/stores/liveDeltaBatch.ts';

/** Deterministic clock: `touchedAt` must be observable, not wall-clock. */
const AT = 1_700_000_000_000;
const now = () => new Date(AT);

function event(
  kind: string,
  payload: unknown,
  turnId = 't1',
  sequence = 1,
): RuntimeEvent {
  return {
    sequence,
    turn_sequence: sequence,
    turn_id: turnId,
    kind,
    payload: payload as RuntimeEvent['payload'],
    version: 1,
  };
}

function entry(ev: RuntimeEvent): LiveEventEntry {
  return { event: ev, subscription_id: 'sub-a' };
}

/** A minimal live view, the way the store's own snapshot would build one. */
function view(overrides: Partial<BackgroundSessionView> = {}): BackgroundSessionView {
  return {
    messages: [],
    settledTurnIds: [],
    activeTurnId: null,
    runtimeStatus: 'idle',
    steerQueueCount: 0,
    pendingApproval: null,
    activity: null,
    usage: null,
    metricsLabel: '',
    sessionUsage: null,
    modelName: '',
    historyAvailable: null,
    historyHasMore: false,
    historyStartTurn: 0,
    historyEndTurn: 0,
    historyTotalTurns: 0,
    historyError: null,
    recoveryState: 'idle',
    recoveryDetail: null,
    liveBufferDroppedCount: 0,
    liveEpoch: null,
    subscriptionId: 'sub-a',
    touchedAt: 0,
    ...overrides,
  };
}

test('sessionKey mirrors the daemon global id', () => {
  assert.equal(sessionKey({ project_id: 'p1', thread_id: 'thr' }), 'p1:thr');
  assert.equal(sessionKey({ project_id: '', thread_id: '' }), ':');
});

test('snapshot then restore round-trips exactly the per-session fields', () => {
  const source: LiveViewSource = {
    messages: [{ id: 'm1', type: 'assistant', timestamp: '10:00', content: 'hi', streaming: true }],
    settledTurnIds: ['t0'],
    activeTurnId: 't1',
    runtimeStatus: 'running',
    steerQueueCount: 2,
    pendingApproval: {
      turn_id: 't1',
      actions: [
        { index: 0, name: 'shell', args: { cmd: 'rm' }, description: 'danger', allowed_decisions: ['allow_once'] },
      ],
    },
    activity: { phase: 'thinking', detail: 'model', startedAt: 5, active: true },
    usage: { ...EMPTY_USAGE, turnInput: 1234 },
    metricsLabel: '1.2k/s',
    sessionUsage: { input: 10, output: 20, cache: 30 },
    modelName: 'gpt-5',
    historyAvailable: true,
    historyHasMore: true,
    historyStartTurn: 3,
    historyEndTurn: 5,
    historyTotalTurns: 9,
    historyError: null,
    recoveryState: 'incomplete',
    recoveryDetail: '运行轮次最早的步骤已被清理',
    liveBufferDroppedCount: 4,
  };

  const snap = snapshotLiveView(source, 'sub-7', AT, 'epoch-a');

  assert.equal(snap.subscriptionId, 'sub-7');
  assert.equal(snap.touchedAt, AT);
  assert.equal(snap.liveEpoch, 'epoch-a', 'the broker-epoch baseline travels with the view');
  assert.deepEqual(restoreLiveView(snap), {
    messages: source.messages,
    settledTurnIds: ['t0'],
    activeTurnId: 't1',
    runtimeStatus: 'running',
    steerQueueCount: 2,
    pendingApproval: source.pendingApproval,
    activity: source.activity,
    usage: source.usage,
    metricsLabel: '1.2k/s',
    sessionUsage: { input: 10, output: 20, cache: 30 },
    modelName: 'gpt-5',
    historyAvailable: true,
    historyHasMore: true,
    historyStartTurn: 3,
    historyEndTurn: 5,
    historyTotalTurns: 9,
    historyError: null,
    recoveryState: 'incomplete',
    recoveryDetail: '运行轮次最早的步骤已被清理',
    liveBufferDroppedCount: 4,
  });
});

test('an unavailable-history view round-trips as unavailable, not manufactured', () => {
  // Returning to a session must not invent `historyAvailable: true` (which would
  // claim a page exists) nor drop the error that explains the absence.
  const source: LiveViewSource = {
    messages: [],
    activeTurnId: null,
    runtimeStatus: 'idle',
    steerQueueCount: 0,
    pendingApproval: null,
    activity: null,
    usage: null,
    metricsLabel: '',
    sessionUsage: null,
    modelName: '',
    historyAvailable: false,
    historyHasMore: false,
    historyStartTurn: 0,
    historyEndTurn: 0,
    historyTotalTurns: 0,
    historyError: '读取转录失败',
    recoveryState: 'idle',
    recoveryDetail: null,
    liveBufferDroppedCount: 0,
  };
  const restored = restoreLiveView(snapshotLiveView(source, 'sub-9', AT, 'epoch-b'));
  assert.equal(restored.historyAvailable, false, 'an unavailable history must not come back available');
  assert.equal(restored.historyError, '读取转录失败');
});

test('a view with no settled turns still snapshots a concrete tombstone list', () => {
  const source: LiveViewSource = {
    messages: [],
    activeTurnId: null,
    runtimeStatus: 'idle',
    steerQueueCount: 0,
    pendingApproval: null,
    activity: null,
    usage: null,
    metricsLabel: '',
    sessionUsage: null,
    modelName: '',
    historyAvailable: null,
    historyHasMore: false,
    historyStartTurn: 0,
    historyEndTurn: 0,
    historyTotalTurns: 0,
    historyError: null,
    recoveryState: 'idle',
    recoveryDetail: null,
    liveBufferDroppedCount: 0,
  };
  const snap = snapshotLiveView(source, null, 1, null);
  assert.deepEqual(snap.settledTurnIds, [], 'the optional field must be materialised');
  assert.equal(snap.subscriptionId, null, 'a stale view keeps its null subscription');
  assert.equal(snap.liveEpoch, null, 'a view with no captured epoch keeps null');
  assert.deepEqual(restoreLiveView(snap).settledTurnIds, []);
});

test('folding a running turn fills status, approval, active turn and usage', () => {
  const folded = foldBackgroundEvents(
    view(),
    [
      entry(event('activity_started', { phase: 'thinking', detail: 'model' })),
      entry(event('approval_required', {
        actions: [{ name: 'shell', args: {}, description: 'danger', allowed_decisions: ['allow_once'] }],
      }, 't1', 2)),
      entry(event('usage_updated', { turn_input: 1234, turn_output: 4321, context_size: 45000 }, 't1', 3)),
    ],
    now,
  );

  assert.equal(folded.runtimeStatus, 'running');
  assert.equal(folded.activeTurnId, 't1');
  assert.deepEqual(folded.activity, { phase: 'thinking', detail: 'model', startedAt: AT, active: true });
  assert.equal(folded.pendingApproval?.turn_id, 't1');
  assert.equal(folded.pendingApproval?.actions[0].name, 'shell');
  assert.equal(folded.usage?.turnInput, 1234);
  assert.notEqual(folded.metricsLabel, '', 'the metrics label follows the usage view');
  // The bookkeeping fields survive the fold untouched.
  assert.equal(folded.subscriptionId, 'sub-a');
  assert.equal(folded.touchedAt, AT, 'a folded batch marks the view freshly touched');
});

test('a terminal turn resets the live state and adds the turn tokens to the totals', () => {
  const running = view({ runtimeStatus: 'running', activeTurnId: 't1', steerQueueCount: 2, sessionUsage: { input: 5, output: 5, cache: 5 } });
  const folded = foldBackgroundEvents(
    running,
    [entry(event('turn_completed', { input_tokens: 100, output_tokens: 20, cache_tokens: 7 }, 't1', 9))],
    now,
  );

  assert.equal(folded.runtimeStatus, 'idle');
  assert.equal(folded.activeTurnId, null);
  assert.equal(folded.steerQueueCount, 0);
  assert.deepEqual(folded.sessionUsage, { input: 105, output: 25, cache: 12 });
  assert.deepEqual(folded.settledTurnIds, ['t1']);
});

test('a foreign turn is ignored exactly like the active fold ignores it', () => {
  const running = view({ runtimeStatus: 'running', activeTurnId: 't1' });
  const folded = foldBackgroundEvents(
    running,
    [entry(event('answer_delta', { text: 'stray' }, 't2', 4))],
    now,
  );

  assert.deepEqual(folded.messages, [], 'another turn may not write into this view');
  assert.equal(folded.activeTurnId, 't1', 'the running turn keeps its identity');
  assert.equal(folded.runtimeStatus, 'running');
});

test('a late event from a settled turn cannot revive it', () => {
  const settled = view({ settledTurnIds: ['t1'] });
  const folded = foldBackgroundEvents(
    settled,
    [entry(event('answer_delta', { text: 'too late' }, 't1', 10))],
    now,
  );

  assert.deepEqual(folded.messages, [], 'a settled turn stays settled');
  assert.equal(folded.runtimeStatus, 'idle');
});

test('the shared usage accumulation never invents a total from nothing', () => {
  assert.equal(addTurnUsage(null, { input_tokens: 0, output_tokens: 0, cache_tokens: 0 }), null);
  assert.equal(addTurnUsage(null, 'not an object'), null);
  assert.deepEqual(addTurnUsage(null, { input_tokens: 3 }), { input: 3, output: 0, cache: 0 });
  assert.deepEqual(
    addTurnUsage({ input: 1, output: 2, cache: 3 }, { input_tokens: 4, output_tokens: 5, cache_tokens: 6 }),
    { input: 5, output: 7, cache: 9 },
  );
  // Counts are integers on the wire; a fractional one is truncated, not rounded.
  assert.deepEqual(addTurnUsage(null, { input_tokens: 1.9 }), { input: 1, output: 0, cache: 0 });
});

test('pruneBackgroundViews evicts the least-recently-touched views', () => {
  const views: Record<string, BackgroundSessionView> = {
    a: view({ touchedAt: 1 }),
    b: view({ touchedAt: 3 }),
    c: view({ touchedAt: 2 }),
  };

  const { kept, evicted } = pruneBackgroundViews(views, 2, null);

  assert.deepEqual(evicted, ['a'], 'the oldest touch goes first');
  assert.deepEqual(Object.keys(kept).sort(), ['b', 'c']);
});

test('pruneBackgroundViews never evicts the protected active key', () => {
  const views: Record<string, BackgroundSessionView> = {
    a: view({ touchedAt: 1 }),
    b: view({ touchedAt: 2 }),
    c: view({ touchedAt: 3 }),
  };

  const { kept, evicted } = pruneBackgroundViews(views, 2, 'a');

  assert.deepEqual(evicted, ['b'], 'the protected view survives its age');
  assert.deepEqual(Object.keys(kept).sort(), ['a', 'c']);
});

test('pruneBackgroundViews is a no-op below the limit', () => {
  const views: Record<string, BackgroundSessionView> = { a: view({ touchedAt: 1 }) };
  const { kept, evicted } = pruneBackgroundViews(views, 2, null);
  assert.equal(kept, views, 'nothing to drop keeps the same map');
  assert.deepEqual(evicted, []);
});

test('session status markers skip stale views and prefer approval over running', () => {
  const markers = sessionStatusMarkers(
    {
      'p:running': { subscriptionId: 'sub-r', runtimeStatus: 'running', pendingApproval: null },
      'p:approval': { subscriptionId: 'sub-a', runtimeStatus: 'running', pendingApproval: { turn_id: 't' } },
      // A stale view means "unknown": it must not keep a marker alive.
      'p:stale': { subscriptionId: null, runtimeStatus: 'running', pendingApproval: { turn_id: 't' } },
    },
    { key: 'p:active', runtimeStatus: 'idle', pendingApproval: null },
  );
  assert.deepEqual(markers, { 'p:running': 'running', 'p:approval': 'approval' });
});

test('session status markers include the active session and skip an unopened one', () => {
  assert.deepEqual(
    sessionStatusMarkers({}, { key: 'p:active', runtimeStatus: 'running', pendingApproval: null }),
    { 'p:active': 'running' },
  );
  assert.deepEqual(
    sessionStatusMarkers({}, { key: null, runtimeStatus: 'running', pendingApproval: null }),
    {},
    'no open session contributes no marker',
  );
});

test('the background view cap is small enough to leave watch slots free', () => {
  assert.equal(MAX_BACKGROUND_VIEWS, 8);
  // One active watch plus the cap must stay well inside the daemon's 32 slots.
  assert.ok(MAX_BACKGROUND_VIEWS + 1 < 32);
});
