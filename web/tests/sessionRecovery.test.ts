/**
 * Store-level recovery tests: an unexpected socket drop must keep the *attached*
 * session's resume position (its dead subscription id + last delivered cursor)
 * across the outage, keyed to (session, epoch), so the reconnect continues from
 * the exact last delivered event instead of resyncing from history and replaying
 * the running turn from its start.
 *
 * These drive the production callbacks.  A real `SynapseRuntimeClient` is built
 * with `consoleClientCallbacks()` -- the exact options the store's
 * `startAuthenticatedRuntime` spreads into its own client -- over a fake socket,
 * so a real `serverDrop()` / reconnect runs the store's own `onStateChange` /
 * `onRecovery` / `onEvent`.  A test that just cleared `activeSubscriptionId`
 * would only assert against its own fake; this one exercises the path that runs.
 *
 * The store runs offline: no daemon, no real WebSocket, no HTTP.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { afterEach, beforeEach, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { SynapseRuntimeClient } from '../src/runtime-client/SynapseRuntimeClient.ts';
import type { SocketLike } from '../src/runtime-client/SynapseRuntimeClient.ts';
import {
  consoleClientCallbacks,
  resumeAttachedWatch,
  useConsoleStore,
} from '../src/stores/useConsoleStore.ts';
import { recoveryNotice } from '../src/stores/recoveryNoticeView.ts';
import type { RuntimeEvent, SessionRecoverabilityResult, SessionRef } from '../src/client/types.ts';

const A: SessionRef = { project_id: 'p', thread_id: 'a' };
const B: SessionRef = { project_id: 'p', thread_id: 'b' };
const CAPABILITIES = {
  legacy_v1: true,
  raw_cursor: true,
  watch_resume: true,
  approval_resume: true,
};

interface SentFrame {
  id: number;
  method: string;
  params: any;
}

/** Methods the fake holds instead of answering, to open a handshake window. */
const hold = new Set<string>();

/**
 * Hold `runtime.session.reconcile` for one thread only, so a test can park an
 * old session's resume on its snapshot while the user switches to another
 * session (whose own reconcile must keep answering).
 */
let holdReconcileThread: string | null = null;

/** Turn ids the fake daemon reports as durable (covered) in reconcile probes. */
let coveredTurns = new Set<string>();

interface HistoryRequest {
  session: SessionRef;
  before_turn: number | null;
  limit: number;
}

/** One `runtime.session.history` reply for the given request. */
let historyResponder: (request: HistoryRequest) => unknown = () => historyPage();

/**
 * The session shape one test's daemon reports on `runtime.session.open` /
 * `runtime.session.reconcile`.  `idle` (latestSequence 0, no active turn) makes
 * a cold attach watch from 0 so a test can deliver small cursors; `running`
 * reproduces a long turn with retained events below the latest sequence.
 */
interface Scenario {
  status: 'idle' | 'running';
  activeTurnId: string | null;
  latestSequence: number;
  latestTurnId: string | null;
  latestTurnIntact: boolean;
  latestTurnFirstSequence: number | null;
  /** Broker eviction watermark; a cursor below it is a `cursor_gap` resync. */
  liveDroppedThrough: number;
  usage: { input_tokens: number; output_tokens: number; cache_tokens: number };
}

let scenario: Scenario;

function setScenario(overrides: Partial<Scenario> = {}): void {
  scenario = {
    status: 'idle',
    activeTurnId: null,
    latestSequence: 0,
    latestTurnId: null,
    latestTurnIntact: true,
    latestTurnFirstSequence: null,
    liveDroppedThrough: 0,
    usage: { input_tokens: 0, output_tokens: 0, cache_tokens: 0 },
    ...overrides,
  };
}

/** One legal v1 `runtime.event` envelope at the given session sequence. */
function eventAt(sequence: number, kind: string, text: string): RuntimeEvent {
  return {
    sequence,
    turn_sequence: 1,
    turn_id: 'turn-1',
    kind,
    payload: { text },
    version: 1,
  };
}

/** The recoverability snapshot the fake daemon reports for the requested probes. */
function recoverability(
  session: SessionRef,
  probeTurnIds: readonly string[] = [],
): SessionRecoverabilityResult {
  return {
    project_id: session.project_id,
    thread_id: session.thread_id,
    history_available: true,
    history_total_turns: 1,
    live_epoch: `epoch-${session.thread_id}`,
    live_latest_sequence: 100000,
    live_oldest_sequence: 1,
    live_dropped_through: scenario.liveDroppedThrough,
    active_turn_id: scenario.activeTurnId,
    latest_turn_id: scenario.latestTurnId,
    latest_turn_first_sequence: scenario.latestTurnFirstSequence,
    latest_turn_retained_from: scenario.latestTurnFirstSequence,
    latest_turn_intact: scenario.latestTurnIntact,
    probe: probeTurnIds.map((turn_id) => ({ turn_id, covered: coveredTurns.has(turn_id) })),
  };
}

function openResult(session: SessionRef): unknown {
  return {
    command_id: 'cmd',
    session,
    created: false,
    view: {
      project_id: session.project_id,
      thread_id: session.thread_id,
      status: scenario.status,
      active_turn_id: scenario.activeTurnId,
      latest_sequence: scenario.latestSequence,
      usage: scenario.usage,
      last_error: null,
      last_activity_at: '2024-01-01T00:00:00Z',
      active_model: 'model-x',
      model: 'model-x',
    },
  };
}

/** One newest-page history reply with a single durable user turn. */
function historyPage(): unknown {
  return {
    available: true,
    events: [
      {
        kind: 'user',
        text: 'HISTORY',
        tool_calls: [],
        tool_results: [],
        attachments: [],
        changes: [],
        changes_total: 0,
        reverted_paths: [],
        turn_id: 't-hist',
        elapsed_s: 1,
      },
    ],
    start_turn: 1,
    end_turn: 1,
    total_turns: 1,
    has_more: false,
  };
}

/** One history page from a list of projection events for a single turn. */
function historyTurnPage(
  turnId: string,
  prompt: string,
  answer: string,
  page: { start: number; end: number; total: number; more: boolean },
): unknown {
  return {
    available: true,
    events: [
      {
        kind: 'user', text: prompt, tool_calls: [], tool_results: [], attachments: [],
        changes: [], changes_total: 0, reverted_paths: [], turn_id: turnId, elapsed_s: 1,
      },
      {
        kind: 'answer', text: answer, tool_calls: [], tool_results: [], attachments: [],
        changes: [], changes_total: 0, reverted_paths: [], turn_id: turnId, elapsed_s: null,
      },
    ],
    start_turn: page.start,
    end_turn: page.end,
    total_turns: page.total,
    has_more: page.more,
  };
}

/**
 * Minimal fake transport: it answers the handshake, hands out a fresh `sub-N`
 * subscription id per watch (unique across reconnects, like a real daemon), and
 * can deliver frames for any id.  `hold` defers a chosen method so a test can
 * drop the socket while that handshake is in flight.
 */
class FakeSocket implements SocketLike {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: any }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((ev?: { code?: number; reason?: string }) => void) | null = null;
  readonly sent: SentFrame[] = [];
  readonly watchAfters: Array<{ thread: string; after: number | undefined }> = [];
  private held: SentFrame[] = [];

  private readonly nextWatchId: () => string;

  constructor(nextWatchId: () => string) {
    this.nextWatchId = nextWatchId;
  }

  send(data: string): void {
    const request = JSON.parse(data) as SentFrame;
    this.sent.push(request);
    if (this.isHeld(request)) {
      this.held.push(request);
      return;
    }
    this.push({
      jsonrpc: '2.0',
      id: request.id,
      meta: { wire_version: '1' },
      result: this.reply(request),
    });
  }

  /** True when this request must wait for `release()` instead of answering. */
  private isHeld(request: SentFrame): boolean {
    if (hold.has(request.method)) return true;
    return (
      holdReconcileThread !== null &&
      request.method === 'runtime.session.reconcile' &&
      (request.params.session as SessionRef).thread_id === holdReconcileThread
    );
  }

  /** Replay every request the fake is still holding on this socket, in order. */
  release(): void {
    const pending = this.held;
    this.held = [];
    for (const request of pending) {
      this.push({
        jsonrpc: '2.0',
        id: request.id,
        meta: { wire_version: '1' },
        result: this.reply(request),
      });
    }
  }

  private reply(request: SentFrame): unknown {
    switch (request.method) {
      case 'runtime.protocol.negotiate':
        return { wire_version: '1', supported_versions: ['1'], capabilities: CAPABILITIES };
      case 'runtime.session.open':
        return openResult(request.params.session as SessionRef);
      case 'runtime.session.reconcile':
        return recoverability(
          request.params.session as SessionRef,
          (request.params.probe_turn_ids as string[] | undefined) ?? [],
        );
      case 'runtime.events.watch': {
        const session = request.params.session as SessionRef;
        const after = request.params.after as number | undefined;
        this.watchAfters.push({ thread: session.thread_id, after });
        return { subscription_id: this.nextWatchId(), cursor: after ?? 0 };
      }
      case 'runtime.events.unwatch':
        return { removed: true };
      case 'runtime.session.history':
        return historyResponder(request.params as HistoryRequest);
      case 'runtime.session.goal':
        return null;
      case 'runtime.config.get':
        return {
          current_model: 'model-x',
          available_models: ['model-x'],
          thinking_level: null,
          thinking_levels: [],
          mcp_servers: [],
          mcp_enabled: false,
          can_set_thinking: false,
          can_toggle_mcp_global: false,
          project_thinking_level: null,
          can_set_project_thinking: false,
          context_window: null,
        };
      default:
        // Best-effort reads (config / git) degrade in the store; a shape that
        // yields nothing beats hanging the RPC.
        return {};
    }
  }

  push(frame: unknown): void {
    this.onmessage?.({ data: typeof frame === 'string' ? frame : JSON.stringify(frame) });
  }

  /** Deliver one live `runtime.event` frame for the given subscription. */
  notify(subscriptionId: string, cursor: number, kind = 'answer_delta', text = 'x'): void {
    this.push({
      jsonrpc: '2.0',
      method: 'runtime.event',
      params: { subscription_id: subscriptionId, cursor, event: eventAt(cursor, kind, text) },
    });
  }

  serverOpen(): void {
    this.readyState = 1;
    this.onopen?.();
  }

  serverDrop(code = 1011, reason = 'runtime daemon unavailable'): void {
    this.readyState = 3;
    const cb = this.onclose;
    this.onclose = null;
    cb?.({ code, reason });
  }

  close(): void {
    if (this.readyState === 3) return;
    this.readyState = 3;
    const cb = this.onclose;
    this.onclose = null;
    cb?.();
  }
}

class Factory {
  sockets: FakeSocket[] = [];
  private seq = 0;
  private readonly nextWatchId = (): string => `sub-${++this.seq}`;
  make = (): FakeSocket => {
    const socket = new FakeSocket(this.nextWatchId);
    this.sockets.push(socket);
    return socket;
  };
  get last(): FakeSocket {
    return this.sockets[this.sockets.length - 1];
  }
  get calls(): number {
    return this.sockets.length;
  }
}

const tick = (ms = 0) => new Promise<void>((resolve) => setTimeout(resolve, ms));

async function waitFor(predicate: () => boolean, label: string, timeoutMs = 3000): Promise<void> {
  const started = Date.now();
  while (!predicate()) {
    if (Date.now() - started > timeoutMs) throw new Error(`timed out waiting for ${label}`);
    await tick(2);
  }
}

let factory: Factory;
let client: SynapseRuntimeClient;

/** Build a real client over the fake socket, wired exactly as the store wires its own. */
async function connectStore(): Promise<void> {
  factory = new Factory();
  client = new SynapseRuntimeClient({
    url: 'ws://core',
    socketFactory: factory.make,
    reconnect: { maxAttempts: 5, baseDelayMs: 1, maxDelayMs: 2 },
    ...consoleClientCallbacks(),
  });
  useConsoleStore.setState({
    client,
    pairingState: 'paired',
    connectionState: 'disconnected',
  });
  const connecting = client.connect();
  factory.last.serverOpen();
  await connecting;
}

/** Silence the store's best-effort warnings while a helper drives it. */
async function muted(fn: () => Promise<void>): Promise<void> {
  const warn = console.warn;
  const error = console.error;
  console.warn = () => {};
  console.error = () => {};
  try {
    await fn();
    await tick();
  } finally {
    console.warn = warn;
    console.error = error;
  }
}

async function attach(session: SessionRef): Promise<void> {
  await muted(() => useConsoleStore.getState().loadSessionHistory(session));
}

async function switchTo(threadId: string): Promise<void> {
  await muted(() => useConsoleStore.getState().switchSession(threadId));
}

beforeEach(() => {
  hold.clear();
  holdReconcileThread = null;
  coveredTurns = new Set();
  historyResponder = () => historyPage();
  setScenario();
  useConsoleStore.setState({
    client: null,
    pairingState: 'paired',
    connectionState: 'disconnected',
    currentSession: { project_id: 'p', thread_id: '' },
    sessionTitle: '',
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
    historyHasMore: false,
    historyAvailable: null,
    historyError: null,
    historyStartTurn: 0,
    historyEndTurn: 0,
    historyTotalTurns: 0,
    recoveryState: 'idle',
    recoveryDetail: null,
    goal: null,
    mcpEnabled: false,
    mcpServers: [],
    mcpRuntime: {},
    mcpConnecting: false,
    mcpRuntimeKnown: false,
    projects: [],
    projectSessions: {},
    attachments: [],
    attachmentError: null,
  });
});

afterEach(() => {
  // Cancel any pending reconnect budget so one test's timer cannot fire into the
  // next (the store's own reset would leave it armed).
  useConsoleStore.getState().closeRuntime();
});

test('the store wires its runtime client through the extracted callbacks', () => {
  // Binding guard: the production client must spread the same callbacks this
  // file builds its client from, or these tests would be exercising a copy.
  const source = readFileSync(
    fileURLToPath(new URL('../src/stores/useConsoleStore.ts', import.meta.url)),
    'utf8',
  );
  assert.ok(
    source.includes('...consoleClientCallbacks()'),
    'startAuthenticatedRuntime must build its client with consoleClientCallbacks()',
  );
});

test('a running cold attach replays from the turn start, but the resume continues from the delivered cursor', async () => {
  setScenario({
    status: 'running',
    activeTurnId: 'turn-1',
    latestSequence: 15920,
    latestTurnId: 'turn-1',
    latestTurnIntact: true,
    latestTurnFirstSequence: 15051,
  });
  await connectStore();
  await attach(A);

  // The reported incident replays 870 events (15051..15920) after 15050.
  assert.deepEqual(factory.last.watchAfters, [{ thread: 'a', after: 15050 }]);
  const first = useConsoleStore.getState().activeSubscriptionId!;
  assert.equal(first, 'sub-1');

  // The first six events arrive before the connection drops.
  for (let sequence = 15051; sequence <= 15056; sequence += 1) {
    factory.last.notify(first, sequence);
  }
  assert.equal(client.getWatchCursor(first), 15056);

  // The resume must continue after 15056, not repeat the prefix from 15050.
  factory.last.serverDrop();
  await waitFor(() => factory.calls === 2, 'reconnect socket');
  factory.last.serverOpen();
  await waitFor(() => useConsoleStore.getState().activeSubscriptionId === 'sub-2', 'resumed watch');

  assert.deepEqual(
    factory.last.watchAfters,
    [{ thread: 'a', after: 15056 }],
    'the resume continues from the delivered cursor, not the turn start',
  );
  assert.equal(useConsoleStore.getState().recoveryState, 'resumed');
});

test('an A -> B -> A drop resumes A from A\'s cursor, not the most recently registered B', async () => {
  await connectStore();
  await attach(A); // sub-1 (A)
  await switchTo('b'); // sub-2 (B); A is backgrounded but keeps its watch
  await switchTo('a'); // restore A's live view (sub-1) without re-watching
  assert.equal(useConsoleStore.getState().activeSubscriptionId, 'sub-1');
  assert.equal(client.getWatchSession('sub-2')?.thread_id, B.thread_id);

  // A delivered up to 3; B (the most recently *registered* watch) up to 9.  A
  // no-argument `getWatchCursor()` would answer B's 9.
  factory.last.notify('sub-1', 3);
  factory.last.notify('sub-2', 9);
  assert.equal(client.getWatchCursor('sub-1'), 3);
  assert.equal(client.getWatchCursor('sub-2'), 9);

  factory.last.serverDrop();
  await waitFor(() => factory.calls === 2, 'reconnect socket');
  factory.last.serverOpen();
  await waitFor(() => useConsoleStore.getState().activeSubscriptionId === 'sub-3', 'resumed watch');

  assert.deepEqual(factory.last.watchAfters, [{ thread: 'a', after: 3 }]);
  assert.equal(
    factory.last.watchAfters.some((watch) => watch.after === 9),
    false,
    'B\'s cursor must never be applied to A',
  );
});

test('a second drop resumes from the cursor the resumed watch advanced to', async () => {
  setScenario({
    status: 'running',
    activeTurnId: 'turn-1',
    latestSequence: 15050,
    latestTurnId: 'turn-1',
    latestTurnIntact: true,
    latestTurnFirstSequence: 870,
  });
  await connectStore();
  await attach(A);
  factory.last.notify(useConsoleStore.getState().activeSubscriptionId!, 15050);

  factory.last.serverDrop();
  await waitFor(() => factory.calls === 2, 'first reconnect');
  factory.last.serverOpen();
  await waitFor(() => useConsoleStore.getState().activeSubscriptionId === 'sub-2', 'first resume');
  assert.deepEqual(factory.last.watchAfters, [{ thread: 'a', after: 15050 }]);

  // The resumed watch advances; the next drop must carry that cursor forward.
  factory.last.notify('sub-2', 16000);
  assert.equal(client.getWatchCursor('sub-2'), 16000);

  factory.last.serverDrop();
  await waitFor(() => factory.calls === 3, 'second reconnect');
  factory.last.serverOpen();
  await waitFor(() => useConsoleStore.getState().activeSubscriptionId === 'sub-3', 'second resume');

  assert.deepEqual(factory.last.watchAfters, [{ thread: 'a', after: 16000 }]);
});

test('a drop while the history page is loading keeps the buffered events and merges them once after the resume', async () => {
  await connectStore();
  hold.add('runtime.session.history');
  let attaching: Promise<void>;
  await muted(async () => {
    attaching = useConsoleStore.getState().loadSessionHistory(A);
    // The watch is created before the history page lands.
    await waitFor(() => useConsoleStore.getState().activeSubscriptionId !== null, 'attach watch');
  });
  const droppedSub = useConsoleStore.getState().activeSubscriptionId!;
  assert.equal(useConsoleStore.getState().historyLoading, true, 'the history page is still in flight');

  // A live event arrives while the page is pending: it must be buffered, not
  // rendered over the not-yet-applied history.
  factory.last.notify(droppedSub, 900, 'answer_delta', 'LIVE');
  assert.equal(useConsoleStore.getState().liveEventBuffer.length, 1);

  // Drop while the history read is in flight.  Its rejection must be fenced:
  // no error banner, and the buffered event must be kept for the resume.
  factory.last.serverDrop();
  assert.equal(useConsoleStore.getState().historyError, null, 'a fenced rejection paints no banner');
  assert.equal(useConsoleStore.getState().liveEventBuffer.length, 1, 'the buffer is kept for the resume');

  // Let the resumed watch's history read answer normally.
  hold.delete('runtime.session.history');
  await waitFor(() => factory.calls === 2, 'reconnect socket');
  factory.last.serverOpen();
  await waitFor(
    () => useConsoleStore.getState().activeSubscriptionId === 'sub-2' && !useConsoleStore.getState().historyLoading,
    'resumed watch with the re-read history',
  );
  await attaching!;

  const state = useConsoleStore.getState();
  assert.equal(state.historyError, null, 'the resume must not leave a stale error banner');
  assert.equal(state.recoveryState, 'resumed');
  assert.equal(state.liveEventBuffer.length, 0, 'the buffer was merged, not left pending');
  assert.equal(
    recoveryNotice(state.recoveryState, state.recoveryDetail, state.liveBufferDroppedCount),
    null,
    'a clean resume renders no notice',
  );
  const contents = state.messages.map((message) => message.content).filter(Boolean);
  assert.equal(contents.filter((content) => content === 'HISTORY').length, 1, 'the history page is applied');
  assert.equal(contents.filter((content) => content === 'LIVE').length, 1, 'the buffered event is merged exactly once');
});

test('a drop during the resume handshake keeps its cursor and does not fail the fresh connection', async () => {
  setScenario({
    status: 'running',
    activeTurnId: 'turn-1',
    latestSequence: 15050,
    latestTurnId: 'turn-1',
    latestTurnIntact: true,
    latestTurnFirstSequence: 870,
  });
  await connectStore();
  await attach(A);
  factory.last.notify(useConsoleStore.getState().activeSubscriptionId!, 15050);

  factory.last.serverDrop();
  await waitFor(() => factory.calls === 2, 'first reconnect socket');
  // Hold the resume's `open` so the second drop lands mid-handshake.
  hold.add('runtime.session.open');
  factory.last.serverOpen();
  await waitFor(() => useConsoleStore.getState().recoveryState === 'resuming', 'resume handshake');

  factory.last.serverDrop();
  await tick();
  assert.notEqual(
    useConsoleStore.getState().recoveryState,
    'failed',
    'a mid-handshake transport loss must not fail the fresh connection',
  );

  hold.delete('runtime.session.open');
  await waitFor(() => factory.calls === 3, 'second reconnect socket');
  factory.last.serverOpen();
  await waitFor(() => useConsoleStore.getState().activeSubscriptionId === 'sub-2', 'resumed watch');
  assert.deepEqual(factory.last.watchAfters, [{ thread: 'a', after: 15050 }]);
});

test('a switch while offline discards the old resume point', async () => {
  await connectStore();
  await attach(A);
  const droppedSub = useConsoleStore.getState().activeSubscriptionId!;
  factory.last.notify(droppedSub, 5);
  assert.equal(client.getWatchCursor(droppedSub), 5);

  factory.last.serverDrop();
  // The user switches before the socket is back: the switch owns the new
  // session and A's resume point must not survive it.
  await switchTo('b');
  assert.equal(useConsoleStore.getState().currentSession.thread_id, 'b');

  await waitFor(() => factory.calls === 2, 'reconnect socket');
  factory.last.serverOpen();
  await waitFor(() => factory.last.watchAfters.length > 0, 'B re-attach watch');
  assert.equal(
    factory.last.watchAfters.some((watch) => watch.thread === 'a' && watch.after === 5),
    false,
    'A\'s cursor must not be resumed after the switch',
  );
  assert.ok(
    factory.last.watchAfters.some((watch) => watch.thread === 'b'),
    'the reconnect attaches the session the user switched to',
  );
});

test('closing the runtime discards the resume point', async () => {
  await connectStore();
  await attach(A);
  factory.last.notify(useConsoleStore.getState().activeSubscriptionId!, 5);
  factory.last.serverDrop();

  useConsoleStore.getState().closeRuntime();
  assert.equal(useConsoleStore.getState().activeSubscriptionId, null);

  // A fresh connection comes back; a deliberate close leaves nothing to resume.
  const reconnecting = client.connect();
  factory.last.serverOpen();
  await reconnecting;
  await muted(() => resumeAttachedWatch());
  assert.equal(factory.last.watchAfters.length, 0, 'a closed runtime has no resume point');
});

test('a historyPending resume after the turn settled during the outage renders the final turn once', async () => {
  setScenario({
    status: 'running',
    activeTurnId: 'turn-1',
    latestSequence: 15050,
    latestTurnId: 'turn-1',
    latestTurnIntact: true,
    latestTurnFirstSequence: 870,
  });
  await connectStore();
  // Hold the initial history page so the drop lands while it is still in flight.
  hold.add('runtime.session.history');
  let attaching: Promise<void>;
  await muted(async () => {
    attaching = useConsoleStore.getState().loadSessionHistory(A);
    await waitFor(() => useConsoleStore.getState().activeSubscriptionId !== null, 'attach watch');
  });
  const droppedSub = useConsoleStore.getState().activeSubscriptionId!;
  assert.equal(useConsoleStore.getState().historyLoading, true);

  // The running turn streams a partial answer while the page is pending: it is
  // buffered, never rendered over the not-yet-applied history.
  factory.last.notify(droppedSub, 870, 'answer_delta', 'PARTIAL');
  assert.equal(useConsoleStore.getState().liveEventBuffer.length, 1);

  // The turn settles during the outage: the broker epoch is unchanged, the turn
  // is now durable, and the retried history page carries its final answer.
  setScenario({
    status: 'idle',
    activeTurnId: null,
    latestSequence: 15060,
    latestTurnId: 'turn-1',
    latestTurnIntact: true,
    latestTurnFirstSequence: 870,
  });
  coveredTurns = new Set(['turn-1']);
  historyResponder = () =>
    historyTurnPage('turn-1', 'PROMPT', 'DONE', { start: 1, end: 1, total: 1, more: false });

  factory.last.serverDrop();

  hold.delete('runtime.session.history');
  await waitFor(() => factory.calls === 2, 'reconnect socket');
  factory.last.serverOpen();
  await waitFor(
    () =>
      useConsoleStore.getState().activeSubscriptionId === 'sub-2' &&
      !useConsoleStore.getState().historyLoading,
    'resumed watch with the retried history',
  );
  await attaching!;

  const state = useConsoleStore.getState();
  assert.equal(state.runtimeStatus, 'idle', 'the settled view must not stay "running"');
  assert.equal(state.activeTurnId, null, 'no active turn survives the settle');
  const contents = state.messages.map((message) => message.content).filter(Boolean);
  assert.equal(contents.filter((content) => content === 'DONE').length, 1, 'the durable answer renders once');
  assert.equal(
    contents.filter((content) => content === 'PARTIAL').length,
    0,
    'the buffered partial of a durable turn is suppressed, not replayed as a second assistant',
  );
  const user = state.messages.filter((message) => message.type === 'user');
  assert.equal(user.length, 1);
  assert.equal(user[0].turnId, 'turn-1');
  assert.equal(user[0].work?.ended, true, 'the settled turn stays settled in the projection');
  assert.equal(
    recoveryNotice(state.recoveryState, state.recoveryDetail, state.liveBufferDroppedCount),
    null,
    'a clean settle resume renders no notice',
  );
});

test('a drop during an earlier page keeps the rendered page and merges the buffered event once', async () => {
  setScenario();
  const firstPage = () =>
    historyTurnPage('turn-5', 'PART1', 'P1-DONE', { start: 5, end: 5, total: 8, more: true });
  const grownPage = () =>
    historyTurnPage('turn-7', 'LATEST', 'L-DONE', { start: 7, end: 7, total: 8, more: false });
  const earlierPage = () =>
    historyTurnPage('turn-2', 'EARLIER', 'E-DONE', { start: 2, end: 2, total: 8, more: false });
  historyResponder = () => firstPage();
  await connectStore();
  await attach(A);
  assert.equal(useConsoleStore.getState().historyHasMore, true);
  assert.equal(useConsoleStore.getState().historyStartTurn, 5);
  assert.ok(useConsoleStore.getState().messages.some((message) => message.content === 'PART1'));

  // Begin an earlier-page read and hold it, so the drop lands mid-pagination.
  hold.add('runtime.session.history');
  let earlier: Promise<void>;
  await muted(async () => {
    earlier = useConsoleStore.getState().loadEarlierHistory();
    await waitFor(() => useConsoleStore.getState().historyLoading, 'earlier read in flight');
  });

  // A live event arrives while the earlier page is pending: buffered.
  const sub = useConsoleStore.getState().activeSubscriptionId!;
  factory.last.notify(sub, 900, 'answer_delta', 'PART2');
  assert.equal(useConsoleStore.getState().liveEventBuffer.length, 1);

  // The session advanced during the outage, so a naive re-read of the newest
  // page would replace the already-rendered PART1.
  historyResponder = (request) => (request.before_turn === null ? grownPage() : earlierPage());
  factory.last.serverDrop();

  // Pagination is cancelled, not restarted: the page stays and the buffer lands.
  assert.equal(useConsoleStore.getState().historyLoading, false, 'the earlier read is cancelled');
  assert.ok(
    useConsoleStore.getState().messages.some((message) => message.content === 'PART2'),
    'the buffered live event is applied now',
  );

  hold.delete('runtime.session.history');
  await waitFor(() => factory.calls === 2, 'reconnect socket');
  factory.last.serverOpen();
  await waitFor(() => useConsoleStore.getState().activeSubscriptionId === 'sub-2', 'resumed watch');
  await earlier!;

  const state = useConsoleStore.getState();
  const contents = state.messages.map((message) => message.content).filter(Boolean);
  assert.equal(contents.filter((content) => content === 'PART1').length, 1, 'the rendered page survives the resume');
  assert.equal(contents.filter((content) => content === 'LATEST').length, 0, 'the first page is not re-read');
  assert.equal(contents.filter((content) => content === 'PART2').length, 1, 'the buffered event is merged exactly once');
  assert.equal(state.historyHasMore, true, 'the earlier-page cursor is preserved for a user retry');
  assert.equal(state.historyStartTurn, 5);
});

test('a switch during the resume reconcile is not stolen back by the stale session', async () => {
  setScenario({
    status: 'running',
    activeTurnId: 'turn-1',
    latestSequence: 15050,
    latestTurnId: 'turn-1',
    latestTurnIntact: true,
    latestTurnFirstSequence: 870,
  });
  await connectStore();
  await attach(A);
  factory.last.notify(useConsoleStore.getState().activeSubscriptionId!, 15050);

  // The running turn's prefix is evicted during the outage: the resume decides
  // to resync, the branch that used to attach without a post-reconcile fence.
  setScenario({
    status: 'running',
    activeTurnId: 'turn-1',
    latestSequence: 15060,
    latestTurnId: 'turn-1',
    latestTurnIntact: false,
    latestTurnFirstSequence: 870,
  });
  factory.last.serverDrop();
  // Park A's resume on its reconcile; B's own attach must keep answering.
  holdReconcileThread = 'a';
  await waitFor(() => factory.calls === 2, 'reconnect socket');
  factory.last.serverOpen();
  await waitFor(() => useConsoleStore.getState().recoveryState === 'resuming', 'resume handshake');

  // The user switches while the stale resume is still awaiting its snapshot.
  await switchTo('b');
  assert.equal(useConsoleStore.getState().currentSession.thread_id, 'b');

  // Release the held snapshot: the superseded resume must abandon silently.
  holdReconcileThread = null;
  factory.last.release();
  await tick(20);
  assert.equal(
    useConsoleStore.getState().currentSession.thread_id,
    'b',
    'the stale resume must not attach A back over the session the user switched to',
  );
});

test('closing the runtime fences a pending history read so no error banner is painted', async () => {
  setScenario();
  await connectStore();
  hold.add('runtime.session.history');
  let attaching: Promise<void>;
  await muted(async () => {
    attaching = useConsoleStore.getState().loadSessionHistory(A);
    await waitFor(() => useConsoleStore.getState().historyLoading, 'history read in flight');
  });
  assert.equal(useConsoleStore.getState().historyLoading, true);

  useConsoleStore.getState().closeRuntime();
  // The teardown rejects the held read; the deliberate close must fence it.
  await tick(20);
  await attaching!;

  const state = useConsoleStore.getState();
  assert.equal(state.historyError, null, 'a closed runtime paints no history banner');
  assert.equal(state.historyLoading, false, 'a closed runtime is not left loading');
});

test('a resync whose stored cursor fell out of the window keeps its incomplete notice', async () => {
  setScenario({
    status: 'running',
    activeTurnId: 'turn-1',
    latestSequence: 15050,
    latestTurnId: 'turn-1',
    latestTurnIntact: true,
    latestTurnFirstSequence: 870,
  });
  await connectStore();
  await attach(A);
  factory.last.notify(useConsoleStore.getState().activeSubscriptionId!, 15050);

  // The broker evicted past the delivered cursor during the outage: the resume
  // resyncs from history and must keep warning that a replay gap was lost.
  setScenario({
    status: 'running',
    activeTurnId: 'turn-1',
    latestSequence: 15050,
    latestTurnId: 'turn-1',
    latestTurnIntact: true,
    latestTurnFirstSequence: 870,
    liveDroppedThrough: 15060,
  });
  factory.last.serverDrop();
  await waitFor(() => factory.calls === 2, 'reconnect socket');
  factory.last.serverOpen();
  await waitFor(
    () => useConsoleStore.getState().activeSubscriptionId === 'sub-2',
    'resynced watch',
  );
  await waitFor(
    () => useConsoleStore.getState().recoveryState === 'incomplete',
    'the resync keeps its incomplete notice',
  );

  const state = useConsoleStore.getState();
  assert.equal(state.currentSession.thread_id, 'a');
  assert.notEqual(
    recoveryNotice(state.recoveryState, state.recoveryDetail, state.liveBufferDroppedCount),
    null,
    'a resync that lost events must not silently drop its degraded notice',
  );
});

const TURN_USAGE = { input_tokens: 100, output_tokens: 40, cache_tokens: 10 };
const EXPECTED_USAGE = { input: 100, output: 40, cache: 10 };

function terminal(socket: FakeSocket, subscriptionId: string, sequence: number): void {
  socket.push({
    jsonrpc: '2.0',
    method: 'runtime.event',
    params: {
      subscription_id: subscriptionId,
      cursor: sequence,
      event: { ...eventAt(sequence, 'turn_completed', ''), payload: TURN_USAGE },
    },
  });
}

for (const page of ['none', 'initial', 'earlier'] as const) {
  test(`a ${page} history resume counts an outage settlement once even after the page lands`, async () => {
    setScenario({
      status: 'running', activeTurnId: 'turn-1', latestSequence: 1,
      latestTurnId: 'turn-1', latestTurnFirstSequence: 1,
    });
    await connectStore();
    let pending: Promise<void> | undefined;
    if (page === 'initial') {
      hold.add('runtime.session.history');
      pending = useConsoleStore.getState().loadSessionHistory(A);
      await waitFor(() => useConsoleStore.getState().activeSubscriptionId !== null, 'initial watch');
    } else {
      await attach(A);
    }
    const sub = useConsoleStore.getState().activeSubscriptionId!;
    factory.last.notify(sub, 1, 'answer_delta', 'PREFIX');
    if (page === 'earlier') {
      useConsoleStore.setState({ historyHasMore: true, historyStartTurn: 5 });
      hold.add('runtime.session.history');
      pending = useConsoleStore.getState().loadEarlierHistory();
      await waitFor(() => useConsoleStore.getState().historyLoading, 'earlier page');
    }
    factory.last.serverDrop();
    setScenario({ latestSequence: 3, latestTurnId: 'turn-1', usage: TURN_USAGE });
    coveredTurns = new Set(['turn-1']);
    historyResponder = () =>
      historyTurnPage('turn-1', 'PROMPT', 'DONE', { start: 1, end: 1, total: 1, more: false });
    hold.delete('runtime.session.history');
    await waitFor(() => factory.calls === 2, 'reconnect');
    factory.last.serverOpen();
    await waitFor(
      () => useConsoleStore.getState().activeSubscriptionId === 'sub-2' &&
        !useConsoleStore.getState().historyLoading,
      'resumed history',
    );
    await pending;
    // The snapshot already includes this terminal, but the cursor has not seen
    // it. A resume must use either the snapshot or the replay, never both.
    terminal(factory.last, 'sub-2', 3);
    assert.deepEqual(useConsoleStore.getState().sessionUsage, EXPECTED_USAGE);
  });
}

for (const wasRunning of [false, true]) {
  test(`history dedupe keeps terminal usage when the opening view was running=${wasRunning}`, async () => {
    if (wasRunning) {
      setScenario({
        status: 'running', activeTurnId: 'turn-1', latestSequence: 1,
        latestTurnId: 'turn-1', latestTurnFirstSequence: 1,
      });
    }
    await connectStore();
    hold.add('runtime.session.history');
    const attaching = useConsoleStore.getState().loadSessionHistory(A);
    await waitFor(() => useConsoleStore.getState().activeSubscriptionId !== null, 'watch');
    const sub = useConsoleStore.getState().activeSubscriptionId!;
    factory.last.notify(sub, 1, 'answer_delta', 'PARTIAL');
    terminal(factory.last, sub, 2);
    historyResponder = () =>
      historyTurnPage('turn-1', 'PROMPT', 'DONE', { start: 1, end: 1, total: 1, more: false });
    hold.delete('runtime.session.history');
    factory.last.release();
    await attaching;
    const state = useConsoleStore.getState();
    assert.deepEqual(state.sessionUsage, EXPECTED_USAGE);
    assert.equal(state.messages.some((row) => row.content === 'PARTIAL'), false);
    assert.equal(state.runtimeStatus, 'idle');
  });
}

test('a normal resume replays the old turn tail before adopting a newer running turn', async () => {
  setScenario({
    status: 'running', activeTurnId: 'turn-1', latestSequence: 1,
    latestTurnId: 'turn-1', latestTurnFirstSequence: 1,
  });
  await connectStore();
  await attach(A);
  factory.last.notify('sub-1', 1, 'answer_delta', 'PREFIX');
  factory.last.serverDrop();
  setScenario({
    status: 'running', activeTurnId: 'turn-2', latestSequence: 4,
    latestTurnId: 'turn-2', latestTurnFirstSequence: 4, usage: TURN_USAGE,
  });
  await waitFor(() => factory.calls === 2, 'reconnect');
  factory.last.serverOpen();
  await waitFor(() => useConsoleStore.getState().activeSubscriptionId === 'sub-2', 'watch');
  factory.last.notify('sub-2', 2, 'answer_delta', '-TAIL');
  terminal(factory.last, 'sub-2', 3); // also flush the coalesced answer
  assert.ok(useConsoleStore.getState().messages.some((row) => row.content === 'PREFIX-TAIL'));
  assert.deepEqual(useConsoleStore.getState().sessionUsage, EXPECTED_USAGE);
  factory.last.push({
    jsonrpc: '2.0', method: 'runtime.event',
    params: {
      subscription_id: 'sub-2', cursor: 4,
      event: {
        ...eventAt(4, 'activity_started', ''),
        turn_id: 'turn-2', payload: { phase: 'thinking', detail: 'working' },
      },
    },
  });
  assert.equal(useConsoleStore.getState().runtimeStatus, 'running');
  assert.equal(useConsoleStore.getState().activeTurnId, 'turn-2');
});
