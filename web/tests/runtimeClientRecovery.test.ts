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
  onclose: (() => void) | null = null;
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
  serverDrop(): void {
    this.readyState = 3;
    const cb = this.onclose;
    this.onclose = null;
    cb?.();
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
