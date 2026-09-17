/**
 * Store-level tests for background session views (stage 3b).
 *
 * `sessionViews.test.ts` covers the pure fold; this file pins the store
 * behaviour that makes a switch lossless:
 *
 * - the session being left keeps its watch (no `unwatchEvents()` call) and moves
 *   into `backgroundViews` with its transcript, status and running turn;
 * - returning to a session whose watch is still registered restores that view
 *   without a second `openSession`/`watchEvents`/history read;
 * - a view whose watch ended (`subscriptionId === null`) is dropped and the
 *   session takes the full attach;
 * - the LRU cap evicts the oldest view and detaches exactly its subscription.
 *
 * It also pins the review fixes: the resume cursor is the *active* session's,
 * a mid-load session is stored stale and its lease released, the >32-session
 * bound holds, a background watch that ends releases its lease, a deleted
 * session's view is dropped, and a completed resync is never a stuck `resync`.
 *
 * The store runs against a stub client: no WebSocket, no daemon.
 */
import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';

import {
  handleSubscriptionNotice,
  resumeAttachedWatch,
  useConsoleStore,
} from '../src/stores/useConsoleStore.ts';
import { recoveryNotice } from '../src/stores/recoveryNoticeView.ts';
import { MAX_BACKGROUND_VIEWS } from '../src/stores/sessionViews.ts';
import type { SessionRecoverabilityResult, SessionRef } from '../src/client/types.ts';
import type { TranscriptMessage } from '../src/stores/historyMapper.ts';

const A: SessionRef = { project_id: 'p', thread_id: 'a' };
const B: SessionRef = { project_id: 'p', thread_id: 'b' };

interface Stub {
  calls: string[];
  live: Map<string, SessionRef>;
  cursors: Map<string, number>;
  watchAfters: Array<{ thread: string; after: number | undefined }>;
  reconcile: (session: SessionRef) => SessionRecoverabilityResult;
  client: Record<string, unknown>;
}

/** One session's recoverability snapshot, as the daemon would report it. */
function recoverability(session: SessionRef): SessionRecoverabilityResult {
  return {
    project_id: session.project_id,
    thread_id: session.thread_id,
    history_available: true,
    history_total_turns: 0,
    live_epoch: `epoch-${session.thread_id}`,
    live_latest_sequence: 100,
    live_oldest_sequence: 1,
    live_dropped_through: 0,
    active_turn_id: null,
    latest_turn_id: null,
    latest_turn_first_sequence: null,
    latest_turn_retained_from: null,
    latest_turn_intact: true,
    probe: [],
  };
}

/** The per-session fields a background view carries. */
function viewFields(overrides: Record<string, unknown> = {}): Record<string, unknown> {
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
    subscriptionId: null,
    touchedAt: 0,
    ...overrides,
  };
}

function stubClient(): Stub {
  const calls: string[] = [];
  // The stub registry mirrors the real client: an entry exists only while the
  // watch is live, so `getWatchSession` answers null once it is released.
  const live = new Map<string, SessionRef>([
    ['sub-a', A],
    ['sub-b', B],
  ]);
  const cursors = new Map<string, number>();
  const watchAfters: Array<{ thread: string; after: number | undefined }> = [];
  const lastLive = (): SessionRef | null => {
    let last: SessionRef | null = null;
    for (const session of live.values()) last = session;
    return last;
  };
  let stub: Stub;
  const client = {
    getState: () => 'connected',
    connect: async () => {},
    getWatchSession: (id?: string) =>
      id === undefined ? lastLive() : live.get(id) ?? null,
    getWatchCursor: (id?: string) => {
      if (id === undefined) {
        // The most recently registered watch, mirroring the real client.
        let last: string | null = null;
        for (const key of live.keys()) last = key;
        return last === null ? null : cursors.get(last) ?? null;
      }
      return cursors.get(id) ?? null;
    },
    unwatchEvents: async (id?: string) => {
      if (id === undefined) {
        for (const key of live.keys()) calls.push(`unwatch:${key}`);
        live.clear();
        return;
      }
      calls.push(`unwatch:${id}`);
      live.delete(id);
      cursors.delete(id);
    },
    openSession: async (session: SessionRef) => {
      calls.push(`open:${session.thread_id}`);
      return {
        command_id: 'c',
        session,
        created: false,
        view: {
          project_id: session.project_id,
          thread_id: session.thread_id,
          status: 'idle',
          active_turn_id: null,
          latest_sequence: 0,
          usage: { input_tokens: 0, output_tokens: 0, cache_tokens: 0 },
        },
      };
    },
    watchEvents: async (session: SessionRef, after?: number) => {
      calls.push(`watch:${session.thread_id}`);
      watchAfters.push({ thread: session.thread_id, after });
      const id = `sub-${session.thread_id}`;
      live.set(id, session);
      cursors.set(id, after ?? 0);
      return { subscription_id: id, cursor: after ?? 0 };
    },
    readSessionHistory: async () => ({
      available: false,
      events: [],
      start_turn: 0,
      end_turn: 0,
      total_turns: 0,
      has_more: false,
    }),
    reconcileSession: async ({ session }: { session: SessionRef }) => {
      calls.push(`reconcile:${session.thread_id}`);
      return stub.reconcile(session);
    },
    // The light refresh is best-effort; a peer without these reads degrades to
    // a warning, which keeps this test about the switch itself.
    getRuntimeConfig: async () => {
      throw new Error('unsupported');
    },
    getSessionGoal: async () => {
      throw new Error('unsupported');
    },
    gitStatus: async () => {
      throw new Error('unsupported');
    },
    deleteSession: async ({ session }: { session: SessionRef }) => {
      calls.push(`delete:${session.thread_id}`);
      return { deleted: true, retained_history: true };
    },
  };
  stub = { calls, live, cursors, watchAfters, reconcile: recoverability, client };
  return stub;
}

let stub: Stub;

beforeEach(() => {
  stub = stubClient();
  useConsoleStore.setState({
    pairingState: 'paired',
    client: stub.client as never,
    activeProjectId: 'p',
    currentSession: A,
    sessionTitle: '',
    sessions: [],
    sessionsTotal: 0,
    projects: [],
    projectSessions: {},
    attachments: [],
    attachmentError: null,
    messages: [],
    settledTurnIds: [],
    activeTurnId: null,
    runtimeStatus: 'idle',
    steerQueueCount: 0,
    pendingApproval: null,
    activity: null,
    usage: null,
    sessionUsage: null,
    metricsLabel: '',
    backgroundViews: {},
    activeSubscriptionId: null,
    liveEventBuffer: [],
    liveBufferDroppedCount: 0,
    historyLoading: false,
    goal: null,
    mcpEnabled: false,
    mcpServers: [],
    mcpRuntime: {},
    mcpConnecting: false,
    mcpRuntimeKnown: false,
  });
});

/** Run a switch with the store's best-effort refresh warnings silenced. */
async function mutedSwitch(threadId: string): Promise<void> {
  const warn = console.warn;
  console.warn = () => {};
  try {
    await useConsoleStore.getState().switchSession(threadId);
    // Let the fire-and-forget light refresh settle.
    await new Promise((resolve) => setTimeout(resolve, 0));
  } finally {
    console.warn = warn;
  }
}

/** Run one store action with its best-effort refresh warnings silenced. */
async function muted(fn: () => Promise<void>): Promise<void> {
  const warn = console.warn;
  console.warn = () => {};
  try {
    await fn();
    await new Promise((resolve) => setTimeout(resolve, 0));
  } finally {
    console.warn = warn;
  }
}

test('leaving a session keeps its watch and stores a background view', async () => {
  const message: TranscriptMessage = { id: 'm1', type: 'assistant', timestamp: '', content: 'hi' };
  useConsoleStore.setState({
    currentSession: A,
    activeSubscriptionId: 'sub-a',
    messages: [message],
    runtimeStatus: 'running',
    activeTurnId: 't1',
    steerQueueCount: 2,
  });

  await mutedSwitch('b');

  const state = useConsoleStore.getState();
  const view = state.backgroundViews['p:a'];
  assert.ok(view, 'the leaving session must be moved into a background view');
  assert.equal(view.subscriptionId, 'sub-a');
  assert.equal(view.runtimeStatus, 'running');
  assert.equal(view.activeTurnId, 't1');
  assert.equal(view.steerQueueCount, 2);
  assert.deepEqual(view.messages, [message]);
  assert.equal(state.currentSession.thread_id, 'b');
  assert.equal(state.activeSubscriptionId, 'sub-b');
  assert.equal(
    stub.calls.includes('unwatch:sub-a'),
    false,
    'the leaving watch must stay live -- that is what keeps the session streaming',
  );
});

test('returning to a live background view restores it without re-attaching', async () => {
  const message: TranscriptMessage = { id: 'm2', type: 'assistant', timestamp: '', content: 'bg' };
  useConsoleStore.setState({
    currentSession: B,
    activeSubscriptionId: 'sub-b',
    messages: [],
    backgroundViews: {
      'p:a': viewFields({
        messages: [message],
        runtimeStatus: 'running',
        activeTurnId: 't9',
        sessionUsage: { input: 1, output: 2, cache: 3 },
        subscriptionId: 'sub-a',
        touchedAt: 1,
      }) as never,
    },
  });

  await mutedSwitch('a');

  const state = useConsoleStore.getState();
  assert.equal(state.currentSession.thread_id, 'a');
  assert.equal(state.activeSubscriptionId, 'sub-a', 'the live subscription becomes the active one');
  assert.deepEqual(state.messages, [message]);
  assert.equal(state.runtimeStatus, 'running');
  assert.equal(state.activeTurnId, 't9');
  assert.deepEqual(state.sessionUsage, { input: 1, output: 2, cache: 3 });
  assert.equal(state.backgroundViews['p:a'], undefined, 'the reused view leaves backgroundViews');
  assert.equal(stub.calls.some((call) => call.startsWith('open:')), false, 'reuse must not open a session');
  assert.equal(stub.calls.some((call) => call.startsWith('watch:')), false, 'reuse must not re-watch');
  // The session being left is backgrounded in the same switch.
  assert.equal(state.backgroundViews['p:b']?.subscriptionId, 'sub-b');
});

test('returning to a live view restores its model, history page and recovery state', async () => {
  useConsoleStore.setState({
    currentSession: B,
    activeSubscriptionId: 'sub-b',
    // The previous session's values must not survive under the restored one.
    modelName: 'gpt-5',
    historyAvailable: true,
    historyHasMore: true,
    historyStartTurn: 3,
    historyEndTurn: 5,
    historyTotalTurns: 9,
    backgroundViews: {
      'p:a': viewFields({
        subscriptionId: 'sub-a',
        touchedAt: 1,
        modelName: 'o3',
        historyAvailable: false,
        historyHasMore: false,
        historyStartTurn: 0,
        historyEndTurn: 0,
        historyTotalTurns: 0,
        historyError: '读取失败',
        recoveryState: 'incomplete',
        recoveryDetail: '部分步骤丢失',
        liveBufferDroppedCount: 5,
      }) as never,
    },
  });

  await mutedSwitch('a');

  const state = useConsoleStore.getState();
  assert.equal(state.modelName, 'o3', 'the restored session keeps its own model label');
  assert.equal(state.historyAvailable, false, 'an unavailable history must not come back available');
  assert.equal(state.historyError, '读取失败', 'and its failure reason travels with it');
  assert.equal(state.recoveryState, 'incomplete', 'a real truncation is neither hidden nor invented');
  assert.equal(state.liveBufferDroppedCount, 5);
});

test('a background view whose watch ended takes the full attach path', async () => {
  useConsoleStore.setState({
    currentSession: B,
    activeSubscriptionId: 'sub-b',
    backgroundViews: {
      'p:a': viewFields({ subscriptionId: null, touchedAt: 1 }) as never,
    },
  });

  await mutedSwitch('a');

  const state = useConsoleStore.getState();
  assert.equal(state.currentSession.thread_id, 'a');
  assert.ok(stub.calls.includes('open:a'), 'a stale view must be re-attached from scratch');
  assert.ok(stub.calls.includes('watch:a'));
  assert.equal(state.backgroundViews['p:a'], undefined, 'the stale view is dropped');
});

test('the background view cap evicts the oldest view and detaches its watch', async () => {
  const views: Record<string, unknown> = {};
  for (let index = 0; index < 8; index += 1) {
    views[`p:k${index}`] = viewFields({ subscriptionId: `sub-k${index}`, touchedAt: index });
  }
  useConsoleStore.setState({
    currentSession: A,
    activeSubscriptionId: 'sub-a',
    backgroundViews: views as never,
  });

  await mutedSwitch('b');

  const state = useConsoleStore.getState();
  assert.equal(Object.keys(state.backgroundViews).length, 8, 'the cap holds after the switch');
  assert.equal(state.backgroundViews['p:k0'], undefined, 'the least-recently-touched view goes');
  assert.ok(stub.calls.includes('unwatch:sub-k0'), 'the evicted view detaches exactly its subscription');
  assert.ok(state.backgroundViews['p:a'], 'the just-backgrounded session survives');
});

// --- FIX 2: a mid-load session is stored stale and its lease released --------

test('a session backgrounded while its history loads is stored stale and released', async () => {
  useConsoleStore.setState({
    currentSession: A,
    activeSubscriptionId: 'sub-a',
    historyLoading: true,
    liveEventBuffer: [
      { event: { sequence: 1, turn_sequence: 1, turn_id: 't1', kind: 'answer_delta', payload: {}, version: 1 } },
    ],
  });

  await mutedSwitch('b');

  const state = useConsoleStore.getState();
  const view = state.backgroundViews['p:a'];
  assert.ok(view, 'the leaving session still keeps its transcript');
  assert.equal(
    view.subscriptionId,
    null,
    'a view whose buffer/history merge is in flight must be stale, never reusable',
  );
  assert.ok(stub.calls.includes('unwatch:sub-a'), 'and its lease must be released');

  // Returning takes the full attach path: no reuse of the incomplete transcript.
  stub.calls.length = 0;
  await mutedSwitch('a');
  assert.ok(stub.calls.includes('open:a'), 'returning must re-attach');
  assert.ok(stub.calls.includes('watch:a'), 'and re-watch');
});

// --- FIX 4: leases are released, so the >32-session bound holds --------------

test('visiting more than the watch cap while backgrounding stays bounded and releases each lease', async () => {
  let maxLive = 0;
  for (let index = 0; index < 40; index += 1) {
    const current = `t${index}`;
    // Pretend the current session is mid-load: backgrounding it must store it
    // stale and release its lease immediately.
    stub.live.set(`sub-${current}`, { project_id: 'p', thread_id: current });
    stub.cursors.set(`sub-${current}`, 0);
    useConsoleStore.setState({
      currentSession: { project_id: 'p', thread_id: current },
      activeSubscriptionId: `sub-${current}`,
      historyLoading: true,
      liveEventBuffer: [
        { event: { sequence: 1, turn_sequence: 1, turn_id: 'x', kind: 'answer_delta', payload: {}, version: 1 } },
      ],
    });

    await mutedSwitch(`t${index + 1}`);

    assert.equal(
      stub.live.has(`sub-${current}`),
      false,
      `the lease of ${current} must be released when it is backgrounded`,
    );
    maxLive = Math.max(maxLive, stub.live.size);
  }
  assert.ok(
    maxLive <= MAX_BACKGROUND_VIEWS + 1,
    `live watches stay bounded (saw ${maxLive}, cap ${MAX_BACKGROUND_VIEWS + 1})`,
  );
});

test('a background watch that ends releases its lease', async () => {
  useConsoleStore.setState({
    currentSession: B,
    activeSubscriptionId: 'sub-b',
    backgroundViews: {
      'p:a': viewFields({ subscriptionId: 'sub-a', runtimeStatus: 'running', touchedAt: 1 }) as never,
    },
  });

  handleSubscriptionNotice({ type: 'complete', subscription_id: 'sub-a' });

  const state = useConsoleStore.getState();
  assert.equal(state.backgroundViews['p:a']?.subscriptionId, null, 'the ended watch is marked stale');
  assert.ok(stub.calls.includes('unwatch:sub-a'), 'and its lease is released');
});

// --- FIX 7: a deleted session drops its view and releases its lease ----------

test('deleting a session drops and releases its background view', async () => {
  useConsoleStore.setState({
    currentSession: A,
    activeSubscriptionId: 'sub-a',
    backgroundViews: {
      'p:b': viewFields({ subscriptionId: 'sub-b', touchedAt: 1 }) as never,
    },
  });

  await useConsoleStore.getState().deleteSession('b');

  const state = useConsoleStore.getState();
  assert.equal(state.backgroundViews['p:b'], undefined, 'the deleted view is gone');
  assert.ok(stub.calls.includes('unwatch:sub-b'), 'and its lease is released');
});

// --- FIX 1: the resume cursor belongs to the session on screen ---------------

test('resuming after A -> B -> A uses A\'s cursor, not the most recent watch (B)', async () => {
  await mutedSwitch('a'); // attach A (sub-a)
  await mutedSwitch('b'); // A backgrounded (sub-a stays live), attach B
  await mutedSwitch('a'); // restore A (sub-a) without re-registering

  // A's watch delivered up to sequence 3; B's (the most recently *registered*
  // watch) up to 9.  A no-argument `getWatchCursor()` would answer B's.
  stub.cursors.set('sub-a', 3);
  stub.cursors.set('sub-b', 9);
  stub.watchAfters.length = 0;

  await muted(resumeAttachedWatch);

  const state = useConsoleStore.getState();
  assert.equal(state.currentSession.thread_id, 'a');
  assert.ok(
    stub.watchAfters.some((watch) => watch.thread === 'a' && watch.after === 3),
    'A must resume from its own cursor (3), not B\'s (9)',
  );
  assert.equal(
    stub.watchAfters.some((watch) => watch.after === 9),
    false,
    'B\'s cursor must never be applied to A',
  );
});

// --- FIX 8: a completed resync is never a stuck `resync` ---------------------

test('a completed resync publishes the gap as incomplete, not a stuck resync', async () => {
  await mutedSwitch('a');
  // A cursor below the eviction watermark is an explicit gap: events were
  // evicted, so the durable state must be `incomplete`.
  stub.cursors.set('sub-a', 3);
  stub.reconcile = (session) => ({ ...recoverability(session), live_dropped_through: 10 });

  await muted(resumeAttachedWatch);

  const state = useConsoleStore.getState();
  assert.equal(state.recoveryState, 'incomplete');
  const notice = recoveryNotice(state.recoveryState, state.recoveryDetail, state.liveBufferDroppedCount);
  assert.equal(notice?.kind, 'degraded', 'the reader is told the replay was truncated');
  assert.match(notice?.detail ?? '', /retained window/, 'the gap detail names what was lost');
});

test('a successful resync that lost nothing shows no permanently stuck notice', async () => {
  await mutedSwitch('a');
  // No cursor was ever delivered -> `no_events` resync, which lost nothing: the
  // re-anchor succeeded, so no degraded strip may stay up.
  stub.cursors.delete('sub-a');

  await muted(resumeAttachedWatch);

  const state = useConsoleStore.getState();
  assert.equal(state.recoveryState, 'idle', 'a clean re-anchor must not stay degraded');
  assert.equal(
    recoveryNotice(state.recoveryState, state.recoveryDetail, state.liveBufferDroppedCount),
    null,
    'and it must render nothing',
  );
});
