/**
 * Offline recovery-contract tests for `SynapseRuntimeClient` (phase-4 slice).
 *
 * They run under the Node built-in test runner with NO real WebSocket: a fake
 * `SocketLike` is injected through `ClientOptions.socketFactory`.  Every test
 * reproduces one acceptance seam (drop while a request is in flight, late
 * frames from a replaced socket, watch cursor resume after reconnect, bounded
 * backoff budget exhaustion, explicit cancel/manual close, replay_gap typed
 * errors, subscription complete/error notices).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  ConnectionLostError,
  RpcCallError,
  SynapseRuntimeClient,
} from '../src/client/SynapseRuntimeClient.ts';
import type { SocketLike } from '../src/client/SynapseRuntimeClient.ts';

const SESSION = { project_id: 'proj', thread_id: 'thr' };
const VIEW = { project_id: 'x', thread_id: 'y', status: 'idle', active_turn_id: null, latest_sequence: 0 };

class FakeSocket implements SocketLike {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: any }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((ev?: { code?: number; reason?: string }) => void) | null = null;
  sent: string[] = [];
  inbox: string[] = [];

  send(data: string): void {
    this.sent.push(data);
    const parsed = JSON.parse(data);
    if (parsed.method === 'runtime.protocol.negotiate') {
      this.push({
        jsonrpc: '2.0',
        id: parsed.id,
        meta: { wire_version: '1' },
        result: {
          wire_version: '1',
          supported_versions: ['1'],
          capabilities: { legacy_v1: true, raw_cursor: true, watch_resume: true, approval_resume: true },
        },
      });
    }
  }
  close(): void {
    if (this.readyState === 3) return;
    this.readyState = 3;
    const cb = this.onclose;
    this.onclose = null;
    cb?.();
  }
  push(frame: unknown): void {
    this.inbox.push(typeof frame === 'string' ? frame : JSON.stringify(frame));
    if (this.onmessage) {
      const next = this.inbox.shift()!;
      this.onmessage({ data: next });
    }
  }
  serverOpen(): void {
    this.readyState = 1;
    this.onopen?.();
  }
  serverDrop(ev?: { code?: number; reason?: string }): void {
    this.readyState = 3;
    const cb = this.onclose;
    this.onclose = null;
    cb?.(ev);
  }
}

class Factory {
  sockets: FakeSocket[] = [];
  calls = 0;
  make = (): FakeSocket => {
    const s = new FakeSocket();
    this.sockets.push(s);
    this.calls += 1;
    return s;
  };
}

const tick = () => new Promise<void>((r) => setTimeout(r, 0));
const tickN = (n: number) => new Promise<void>((r) => setTimeout(r, n));

/**
 * Let the newest socket finish its handshake (when it is still connecting) and
 * then drop it at once with a clean `1000` close.
 *
 * This is the flapping host the reconnect-budget fix targets: the handshake
 * always succeeds, so a client that refilled its budget on `open` would open a
 * new socket forever instead of ever reaching the terminal `error` state.
 */
async function dropNewestAfterHandshake(factory: Factory): Promise<boolean> {
  await tickN(20);
  const socket = factory.sockets[factory.calls - 1];
  if (!socket || socket.readyState === 3) return false;
  if (socket.readyState !== 1) {
    socket.serverOpen();
    await tick();
  }
  socket.serverDrop({ code: 1000, reason: '' });
  return true;
}

/**
 * Track the live timers so a test can prove a long stability window never
 * outlives its connection (a stray 10s timer would keep the Node process alive
 * after the socket is gone).  The client reads the global `setTimeout` /
 * `clearTimeout` at call time, so patching them covers its timers and this
 * file's own `tick` helpers alike.
 *
 * Each callback is also captured, so a test can replay one after its
 * `clearTimeout` — a task that already fired cannot be cancelled in a browser.
 */
function trackTimers() {
  const realSet = globalThis.setTimeout;
  const realClear = globalThis.clearTimeout;
  const live = new Set<unknown>();
  const captured = new Map<unknown, { ms?: number; run: () => void }>();
  (globalThis as any).setTimeout = (fn: (...a: any[]) => void, ms?: number, ...rest: any[]) => {
    const handle = realSet(() => {
      live.delete(handle);
      fn(...rest);
    }, ms);
    live.add(handle);
    captured.set(handle, { ms, run: () => fn(...rest) });
    return handle;
  };
  (globalThis as any).clearTimeout = (handle: any) => {
    live.delete(handle);
    realClear(handle);
  };
  return {
    live,
    /** Handle of the single live timer scheduled with `ms`. */
    handleFor(ms: number): unknown {
      for (const handle of live) {
        if (captured.get(handle)?.ms === ms) return handle;
      }
      throw new Error(`no live timer scheduled with ${ms}ms`);
    },
    /** Replay a captured callback as a task that outlived its `clearTimeout`. */
    fireStale(handle: unknown) {
      captured.get(handle)?.run();
    },
    restore() {
      (globalThis as any).setTimeout = realSet;
      (globalThis as any).clearTimeout = realClear;
    },
  };
}

async function openClient(opts?: {
  maxAttempts?: number;
  onRecovery?: (i: any) => void;
  onNotice?: (n: any) => void;
}) {
  const factory = new Factory();
  const client = new SynapseRuntimeClient({
    url: 'ws://loopback',
    socketFactory: factory.make,
    reconnect: { maxAttempts: opts?.maxAttempts ?? 3, baseDelayMs: 1, maxDelayMs: 5 },
    onRecovery: opts?.onRecovery,
    onSubscriptionNotice: opts?.onNotice,
  });
  const connectPromise = client.connect();
  await tick();
  factory.sockets[0].serverOpen();
  await connectPromise;
  return { client, socket: factory.sockets[0], factory };
}

function responseFor(socket: FakeSocket): { jsonrpc: '2.0'; id: number } {
  const req = JSON.parse(socket.sent[socket.sent.length - 1]);
  return req;
}

/**
 * One live `runtime.event` frame.  The daemon pushes the event's own session
 * sequence as the cursor (`cursor = stream.cursor.sequence`), so the helper
 * keeps them equal; `version` is overridable to build an unconsumable frame.
 */
function liveEvent(cursor: number, subscriptionId: string, version = 1) {
  return {
    jsonrpc: '2.0',
    meta: { wire_version: '1' },
    method: 'runtime.event',
    params: {
      subscription_id: subscriptionId,
      cursor,
      event: {
        sequence: cursor,
        turn_id: 'turn-1',
        turn_sequence: 1,
        kind: 'activity_started',
        payload: {},
        version,
      },
    },
  };
}

test('late response from a replaced socket never resolves a new-generation request', async () => {
  const { client, socket, factory } = await openClient();
  const probe = client.getSession(SESSION).catch(() => undefined);
  await tick();

  socket.serverDrop();
  await probe; // the pre-drop request is rejected by the drop; swallow it
  await tickN(30);
  assert.equal(factory.calls, 2, 'client should have reconnected once');
  const socket2 = factory.sockets[1];
  socket2.serverOpen();
  await tickN(40);

  // Request on generation 2.
  const p2 = client.getSession(SESSION);
  await tick();
  const req2 = responseFor(socket2);

  // A late response on the OLD socket for the new request id must be fenced.
  socket.push({ jsonrpc: '2.0', id: req2.id, meta: { wire_version: '1' }, result: VIEW });
  await tickN(5);
  let settled = false;
  p2.then(() => { settled = true; }).catch(() => { settled = true; });
  await tickN(5);
  assert.equal(settled, false, 'old-socket late frame must not settle the new request');

  // The correct response on the current socket resolves it.
  socket2.push({ jsonrpc: '2.0', id: req2.id, meta: { wire_version: '1' }, result: VIEW });
  const view = await p2;
  assert.equal(view.thread_id, 'y');
  client.disconnect();
});

test('unexpected drop rejects an in-flight request with unknownOutcome when it was sent', async () => {
  const { client, socket } = await openClient();
  const p = client.getSession(SESSION);
  await tick();
  socket.serverDrop();
  await assert.rejects(p, (err: any) => {
    assert.ok(err instanceof ConnectionLostError);
    assert.equal(err.unknownOutcome, true, 'sent-but-unanswered must be marked unknown');
    return true;
  });
  client.disconnect();
});

test('watch reconnects from the last cursor: no silent after=0', async () => {
  const factory = new Factory();
  const notices: any[] = [];
  const client = new SynapseRuntimeClient({
    url: 'ws://loopback',
    socketFactory: factory.make,
    reconnect: { maxAttempts: 3, baseDelayMs: 1, maxDelayMs: 5 },
    onSubscriptionNotice: (n) => notices.push(n),
  });
  const cp = client.connect();
  await tick();
  const s1 = factory.sockets[0];
  s1.serverOpen();
  await cp;

  const watchPromise = client.watchEvents(SESSION, 5);
  await tick();
  const watchReq = responseFor(s1);
  assert.equal(watchReq.method, 'runtime.events.watch');
  assert.equal(watchReq.params.after, 5);
  s1.push({ jsonrpc: '2.0', id: watchReq.id, meta: { wire_version: '1' }, result: { subscription_id: 'sub1', cursor: 5 } });
  const watch = await watchPromise;
  assert.equal(watch.cursor, 5);
  assert.equal(client.getWatchCursor(), 5);

  s1.push({
    jsonrpc: '2.0',
    meta: { wire_version: '1' },
    method: 'runtime.event',
    params: { subscription_id: 'sub1', event: { sequence: 6, turn_id: 't', turn_sequence: 1, kind: 'activity_started', timestamp: '', payload: {}, version: 1 }, cursor: 6 },
  });
  await tick();
  assert.equal(client.getWatchCursor(), 6);

  s1.serverDrop();
  await tickN(30);
  assert.equal(factory.calls, 2);
  const s2 = factory.sockets[1];
  s2.serverOpen();
  await tickN(40);

  const rewatchPromise = client.watchEvents(SESSION, client.getWatchCursor() ?? 0);
  await tick();
  const rewatchReq = responseFor(s2);
  assert.equal(rewatchReq.params.after, 6, 'resume must use the exact last cursor, never after=0');
  s2.push({ jsonrpc: '2.0', id: rewatchReq.id, meta: { wire_version: '1' }, result: { subscription_id: 'sub2', cursor: 6 } });
  await rewatchPromise;
  client.disconnect();
});

test('replay_gap watch error surfaces as a typed RpcCallError with service_code', async () => {
  const { client, socket } = await openClient();
  const p = client.readEvents(SESSION, 500);
  await tick();
  const req = responseFor(socket);
  socket.push({
    jsonrpc: '2.0',
    id: req.id,
    meta: { wire_version: '1' },
    error: { code: -32000, message: 'stale', data: { service_code: 'replay_gap' } },
  });
  await assert.rejects(p, (err: any) => {
    assert.ok(err instanceof RpcCallError);
    assert.equal(err.service_code, 'replay_gap');
    return true;
  });
  client.disconnect();
});

test('bounded backoff budget reaches an explicit failure (never infinite reconnect)', async () => {
  const factory = new Factory();
  const recoveries: any[] = [];
  const client = new SynapseRuntimeClient({
    url: 'ws://loopback',
    socketFactory: factory.make,
    reconnect: { maxAttempts: 3, baseDelayMs: 1, maxDelayMs: 5 },
    onRecovery: (i) => recoveries.push(i),
  });
  const cp = client.connect();
  await tick();
  factory.sockets[0].serverOpen();
  await cp;
  assert.equal(client.getState(), 'connected');

  // Drop the first socket; then keep failing every reconnect socket the moment
  // it is created (still CONNECTING). Each failure advances the bounded budget
  // until maxAttempts (3) is exhausted and the state becomes a terminal error.
  for (let drop = 0; drop < 8 && factory.calls <= 4; drop += 1) {
    const newest = factory.sockets[factory.calls - 1];
    if (newest && newest.readyState !== 3) {
      newest.serverDrop();
    }
    await tickN(60);
  }
  assert.equal(client.getState(), 'error');
  const phases = recoveries.map((i) => i.phase);
  assert.ok(phases.includes('reconnecting'));
  assert.ok(phases.includes('failed'));
  assert.ok(factory.calls <= 1 + 3, `bounded attempts: got ${factory.calls}`);
  client.disconnect();
});

test('manual disconnect cancels the reconnect budget (no background reconnect)', async () => {
  const factory = new Factory();
  const client = new SynapseRuntimeClient({
    url: 'ws://loopback',
    socketFactory: factory.make,
    reconnect: { maxAttempts: 3, baseDelayMs: 2, maxDelayMs: 10 },
  });
  const cp = client.connect();
  await tick();
  factory.sockets[0].serverOpen();
  await cp;
  factory.sockets[0].serverDrop();
  await tick();
  client.disconnect(); // user-initiated close must cancel the scheduled retry
  await tickN(80);
  assert.equal(factory.calls, 1, 'manual close must cancel the reconnect budget');
  assert.equal(client.getState(), 'disconnected');
});

test('subscription complete/error notices are routed with service_code', async () => {
  const notices: any[] = [];
  const { client, socket } = await openClient({ onNotice: (n) => notices.push(n) });
  const watchPromise = client.watchEvents(SESSION, 0);
  await tick();
  const req = responseFor(socket);
  socket.push({ jsonrpc: '2.0', id: req.id, meta: { wire_version: '1' }, result: { subscription_id: 'subX', cursor: 0 } });
  await watchPromise;
  socket.push({
    jsonrpc: '2.0',
    meta: { wire_version: '1' },
    method: 'runtime.subscription.error',
    params: { subscription_id: 'subX', error: { code: -32000, message: 'overflow', data: { service_code: 'event_overflow' } } },
  });
  socket.push({
    jsonrpc: '2.0',
    meta: { wire_version: '1' },
    method: 'runtime.subscription.complete',
    params: { subscription_id: 'subX', cursor: 3 },
  });
  await tick();
  assert.deepEqual(
    notices.map((n) => [n.type, n.service_code ?? null, n.cursor ?? null]),
    [
      ['error', 'event_overflow', null],
      ['complete', null, 3],
    ],
  );
  client.disconnect();
});

test('a fenced watch survives a reconnect and resumes from the last good cursor', async () => {
  const factory = new Factory();
  const notices: any[] = [];
  const client = new SynapseRuntimeClient({
    url: 'ws://loopback',
    socketFactory: factory.make,
    reconnect: { maxAttempts: 3, baseDelayMs: 1, maxDelayMs: 5 },
    onSubscriptionNotice: (n) => notices.push(n),
  });
  const cp = client.connect();
  await tick();
  const s1 = factory.sockets[0];
  s1.serverOpen();
  await cp;

  const watchPromise = client.watchEvents(SESSION, 0);
  await tick();
  const watchReq = responseFor(s1);
  s1.push({ jsonrpc: '2.0', id: watchReq.id, meta: { wire_version: '1' }, result: { subscription_id: 'sub1', cursor: 0 } });
  await watchPromise;

  s1.push(liveEvent(4, 'sub1'));
  await tick();
  assert.equal(client.getWatchCursor(), 4);

  // A frame this client cannot consume fences the subscription: the last good
  // cursor (4) is kept and the unreplayed sequence (5) is never jumped over.
  s1.push(liveEvent(5, 'sub1', 2));
  s1.push(liveEvent(6, 'sub1'));
  await tick();
  assert.equal(client.getWatchCursor(), 4, 'the fenced watch may not cross the gap');
  assert.deepEqual(
    notices.map((n) => [n.type, n.service_code]),
    [['error', 'unsupported_event_version']],
  );

  // The drop changes nothing: the gap is still open, and a late frame from the
  // replaced socket cannot revive the dead subscription.
  s1.serverDrop();
  await tickN(30);
  assert.equal(factory.calls, 2);
  const s2 = factory.sockets[1];
  s2.serverOpen();
  await tickN(40);
  assert.equal(client.getWatchCursor(), 4, 'the fence keeps the last good cursor across the drop');
  s1.push(liveEvent(6, 'sub1'));
  await tick();
  assert.equal(client.getWatchCursor(), 4, 'a late frame from the dead socket stays fenced');

  // Re-attaching resumes from the last good cursor, so the daemon replays the
  // gap instead of skipping it, and the fresh subscription delivers again.
  const rewatch = client.watchEvents(SESSION, client.getWatchCursor() ?? 0);
  await tick();
  const rewatchReq = responseFor(s2);
  assert.equal(rewatchReq.params.after, 4, 'the re-attach must resume from the last good cursor');
  s2.push({ jsonrpc: '2.0', id: rewatchReq.id, meta: { wire_version: '1' }, result: { subscription_id: 'sub2', cursor: 4 } });
  await rewatch;

  // The fresh watch owns the cursor: neither the dead subscription id nor a
  // frame from the replaced socket generation may touch it.
  s2.push(liveEvent(6, 'sub1'));
  s1.push(liveEvent(6, 'sub2'));
  await tick();
  assert.equal(client.getWatchCursor(), 4, 'only the fresh subscription may advance the cursor');

  s2.push(liveEvent(5, 'sub2'));
  s2.push(liveEvent(6, 'sub2'));
  await tick();
  assert.equal(client.getWatchCursor(), 6, 'the replayed gap advances the cursor again');
  client.disconnect();
});

test('a fenced subscription reports no second failure and no late completion', async () => {
  const notices: any[] = [];
  const { client, socket } = await openClient({ onNotice: (n) => notices.push(n) });
  const watchPromise = client.watchEvents(SESSION, 0);
  await tick();
  const req = responseFor(socket);
  socket.push({ jsonrpc: '2.0', id: req.id, meta: { wire_version: '1' }, result: { subscription_id: 'subX', cursor: 0 } });
  await watchPromise;

  socket.push({
    jsonrpc: '2.0',
    meta: { wire_version: '1' },
    method: 'runtime.event',
    params: { subscription_id: 'subX', cursor: 5, event: { sequence: 5, turn_sequence: 1, kind: 'activity_started', payload: {}, version: 1 } },
  });
  await tick();
  assert.deepEqual(
    notices.map((n) => [n.type, n.service_code]),
    [['error', 'malformed_runtime_event']],
  );

  // The dead subscription may not speak again: neither a server-side error nor
  // a completion may re-arm the console while the unreplayed gap is open.
  socket.push({
    jsonrpc: '2.0',
    meta: { wire_version: '1' },
    method: 'runtime.subscription.error',
    params: { subscription_id: 'subX', error: { code: -32000, message: 'overflow', data: { service_code: 'event_overflow' } } },
  });
  socket.push({
    jsonrpc: '2.0',
    meta: { wire_version: '1' },
    method: 'runtime.subscription.complete',
    params: { subscription_id: 'subX', cursor: 9 },
  });
  await tick();
  assert.deepEqual(
    notices.map((n) => [n.type, n.service_code]),
    [['error', 'malformed_runtime_event']],
    'a fenced subscription reports its failure exactly once',
  );
  assert.equal(client.getWatchCursor(), 0, 'a late completion may not advance the fenced watch');
  client.disconnect();
});

// --- reconnect-budget stability window (flapping-host fix) -------------------

test('a handshake that drops immediately still exhausts the bounded budget (real 1000 close)', async () => {
  const factory = new Factory();
  const recoveries: any[] = [];
  const client = new SynapseRuntimeClient({
    url: 'ws://loopback',
    socketFactory: factory.make,
    reconnect: { maxAttempts: 5, baseDelayMs: 1, maxDelayMs: 3, stableConnectionMs: 10_000 },
    onRecovery: (i) => recoveries.push(i),
  });
  const cp = client.connect();
  await tick();
  factory.sockets[0].serverOpen();
  await cp;
  assert.equal(client.getState(), 'connected');

  // The host accepts every socket and drops it at once with a clean 1000 close
  // (masking the real 1013 "try again later"). A completed handshake must not
  // refill the budget, or this loops forever opening a new socket each time.
  for (let i = 0; i < 10 && client.getState() !== 'error'; i += 1) {
    assert.ok(await dropNewestAfterHandshake(factory), `reconnect socket #${i + 1} should exist`);
  }

  assert.equal(client.getState(), 'error', 'the flapping endpoint must hit the terminal budget');
  assert.equal(factory.calls, 1 + 5, 'exactly the initial connect plus maxAttempts reconnects');
  assert.deepEqual(
    recoveries.map((i) => `${i.phase}:${i.attempt}`),
    [
      'reconnecting:1',
      'reconnected:1',
      'reconnecting:2',
      'reconnected:2',
      'reconnecting:3',
      'reconnected:3',
      'reconnecting:4',
      'reconnected:4',
      'reconnecting:5',
      'reconnected:5',
      'failed:5',
    ],
    'the budget advances on every drop and never rolls back on a handshake',
  );
  client.disconnect();
});

test('a generation that stays connected past the window restores the full budget', async () => {
  const factory = new Factory();
  const recoveries: any[] = [];
  const client = new SynapseRuntimeClient({
    url: 'ws://loopback',
    socketFactory: factory.make,
    reconnect: { maxAttempts: 4, baseDelayMs: 1, maxDelayMs: 3, stableConnectionMs: 25 },
    onRecovery: (i) => recoveries.push(i),
  });
  const cp = client.connect();
  await tick();
  factory.sockets[0].serverOpen();
  await cp;

  const reconnecting = () =>
    recoveries.filter((r) => r.phase === 'reconnecting').map((r) => r.attempt);

  // Spend two attempts: each reconnect handshakes and is dropped at once, so
  // the budget keeps counting (a handshake alone must not refill it).
  factory.sockets[0].serverDrop({ code: 1000, reason: '' });
  await tickN(15);
  factory.sockets[1].serverOpen();
  await tick();
  factory.sockets[1].serverDrop({ code: 1000, reason: '' });
  await tickN(15);
  const gen3 = factory.sockets[2];
  gen3.serverOpen();
  await tick();
  assert.equal(client.getState(), 'connected');
  assert.deepEqual(reconnecting(), [1, 2], 'two flapping reconnects spend attempts 1 and 2');

  // Let the third generation stay connected past its window: the budget refills.
  await tickN(40);

  // The next failure is attempt 1 again, not attempt 3.
  gen3.serverDrop({ code: 1000, reason: '' });
  await tick();
  assert.deepEqual(reconnecting(), [1, 2, 1], 'a stable generation refills the budget');
  client.disconnect();
});

test('a manual disconnect then reconnect starts a fresh budget', async () => {
  const factory = new Factory();
  const recoveries: any[] = [];
  const client = new SynapseRuntimeClient({
    url: 'ws://loopback',
    socketFactory: factory.make,
    reconnect: { maxAttempts: 2, baseDelayMs: 1, maxDelayMs: 3, stableConnectionMs: 10_000 },
    onRecovery: (i) => recoveries.push(i),
  });
  const cp = client.connect();
  await tick();
  factory.sockets[0].serverOpen();
  await cp;

  // Flap until the budget is exhausted.
  for (let i = 0; i < 8 && client.getState() !== 'error'; i += 1) {
    await dropNewestAfterHandshake(factory);
  }
  assert.equal(client.getState(), 'error');
  const callsAtExhaustion = factory.calls;

  // A manual close ends the recovery lifecycle...
  client.disconnect();
  assert.equal(client.getState(), 'disconnected');
  recoveries.length = 0;

  // ...and the next user connect gets a full budget: its first drop is attempt 1.
  const cp2 = client.connect();
  await tick();
  const fresh = factory.sockets[factory.calls - 1];
  assert.ok(factory.calls > callsAtExhaustion, 'a manual reconnect opens a new socket');
  fresh.serverOpen();
  await cp2;
  assert.equal(client.getState(), 'connected');
  fresh.serverDrop({ code: 1000, reason: '' });
  await tick();
  const reconnecting = recoveries.filter((r) => r.phase === 'reconnecting');
  assert.equal(reconnecting.length, 1, 'the fresh connection reconnects once');
  assert.equal(reconnecting[0].attempt, 1, 'a manual reconnect starts from a full budget');
  assert.equal(reconnecting[0].maxAttempts, 2);
  client.disconnect();
});

test('a stability window is cleared with its generation and never outlives the connection', async () => {
  const tracker = trackTimers();
  try {
    const factory = new Factory();
    const client = new SynapseRuntimeClient({
      url: 'ws://loopback',
      socketFactory: factory.make,
      reconnect: { maxAttempts: 3, baseDelayMs: 1, maxDelayMs: 3, stableConnectionMs: 10_000 },
    });
    const cp = client.connect();
    await tick();
    factory.sockets[0].serverOpen();
    await cp;
    // A live generation arms exactly one long window.
    assert.equal(tracker.live.size, 1, 'a live generation arms exactly one window');

    // Drop it: the window is cleared with the generation, so the reconnect arms
    // exactly one *new* window — a stale one can never refill the new budget.
    factory.sockets[0].serverDrop({ code: 1000, reason: '' });
    await tickN(20);
    const s1 = factory.sockets[1];
    s1.serverOpen();
    await tick();
    assert.equal(client.getState(), 'connected');
    assert.equal(tracker.live.size, 1, 'a replaced generation leaves no stale window behind');

    // A manual close clears the last one, so nothing keeps the host alive.
    client.disconnect();
    assert.equal(tracker.live.size, 0, 'a manual close clears the pending window');
  } finally {
    tracker.restore();
  }
});

test('a stale stability callback cannot refill the budget nor drop the live handle', async () => {
  const tracker = trackTimers();
  try {
    const factory = new Factory();
    const recoveries: any[] = [];
    const client = new SynapseRuntimeClient({
      url: 'ws://loopback',
      socketFactory: factory.make,
      reconnect: { maxAttempts: 3, baseDelayMs: 1, maxDelayMs: 3, stableConnectionMs: 10_000 },
      onRecovery: (i) => recoveries.push(i),
    });
    const cp = client.connect();
    await tick();
    factory.sockets[0].serverOpen();
    await cp;

    // gen 1 is healthy and owns exactly one long window; capture its callback.
    const staleHandle = tracker.handleFor(10_000);

    // The socket drops and the window is cleared, but a task that already fired
    // cannot be cancelled. Replay it while the generation has not advanced yet:
    // the worst case for a naive fence, since `gen` still matches.
    factory.sockets[0].serverDrop({ code: 1000, reason: '' });
    tracker.fireStale(staleHandle);
    await tickN(20);

    const reconnecting = () =>
      recoveries.filter((r) => r.phase === 'reconnecting').map((r) => r.attempt);
    assert.deepEqual(reconnecting(), [1], 'the replayed task must not refill the budget');

    // gen 2 handshakes and arms a fresh window of its own.
    const s2 = factory.sockets[1];
    s2.serverOpen();
    await tick();
    assert.equal(client.getState(), 'connected');
    const liveHandle = tracker.handleFor(10_000);
    assert.notEqual(liveHandle, staleHandle, 'the new generation arms a new window');

    // Replaying the dead generation's task must not wipe the live handle either.
    tracker.fireStale(staleHandle);
    assert.ok(tracker.live.has(liveHandle), 'the live window must survive a stale fire');

    // The next drop is still attempt 2 and clears the live window: a lost handle
    // would leak the timer, a phantom refill would restart the count at 1.
    s2.serverDrop({ code: 1000, reason: '' });
    await tickN(20);
    assert.deepEqual(reconnecting(), [1, 2], 'the budget keeps advancing across stale fires');
    assert.equal(tracker.live.has(liveHandle), false, 'the drop still clears the live window');
    client.disconnect();
  } finally {
    tracker.restore();
  }
});
