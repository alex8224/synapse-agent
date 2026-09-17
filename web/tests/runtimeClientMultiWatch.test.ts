/**
 * Offline tests for concurrent `runtime.events.watch` subscriptions (stage 3a).
 *
 * They run under the Node built-in test runner with NO real WebSocket: a fake
 * `SocketLike` is injected through `ClientOptions.socketFactory` and hands out a
 * distinct subscription id per watch, so several watches can be held at once.
 *
 * They pin the multi-watch contract the single-watch client could not express:
 *
 * - two sessions watch concurrently and each keeps its own cursor;
 * - a frame for one watch never moves another watch's cursor nor reaches it;
 * - an unknown subscription id is dropped, and an unattributed frame is only
 *   routed while exactly one watch is live;
 * - a fence is per subscription: fencing one watch leaves the others delivering;
 * - `unwatchEvents()` detaches every watch, `unwatchEvents(id)` detaches one;
 * - the watch count is bounded at the daemon's own per-connection cap (32);
 * - watches and their cursors survive an unexpected drop, and a manual
 *   disconnect clears them.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  RpcCallError,
  SynapseRuntimeClient,
} from '../src/runtime-client/SynapseRuntimeClient.ts';
import type { SocketLike } from '../src/runtime-client/SynapseRuntimeClient.ts';
import type { EventNotificationMeta, RuntimeEvent } from '../src/runtime-client/types.ts';

const SESSION_A = { project_id: 'proj', thread_id: 'a' };
const SESSION_B = { project_id: 'proj', thread_id: 'b' };
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

/** One legal v1 `runtime.event` envelope at the given session sequence. */
function eventAt(sequence: number, kind = 'answer_delta'): RuntimeEvent {
  return {
    sequence,
    turn_sequence: 1,
    turn_id: 'turn-1',
    kind,
    payload: { text: 'x' },
    version: 1,
  };
}

/**
 * Minimal fake transport: it answers the handshake and hands out a fresh
 * `sub-N` subscription id per watch, and can deliver frames for any id.
 */
class FakeSocket implements SocketLike {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: any }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((ev?: { code?: number; reason?: string }) => void) | null = null;
  sent: SentFrame[] = [];
  private watchSeq = 0;

  send(data: string): void {
    const request = JSON.parse(data) as SentFrame;
    this.sent.push(request);
    this.push({
      jsonrpc: '2.0',
      id: request.id,
      meta: { wire_version: '1' },
      result: this.reply(request),
    });
  }

  private reply(request: SentFrame): unknown {
    switch (request.method) {
      case 'runtime.protocol.negotiate':
        return { wire_version: '1', supported_versions: ['1'], capabilities: CAPABILITIES };
      case 'runtime.events.watch':
        this.watchSeq += 1;
        return { subscription_id: `sub-${this.watchSeq}`, cursor: request.params.after ?? 0 };
      case 'runtime.events.unwatch':
        return { removed: true };
      default:
        return {};
    }
  }

  push(frame: unknown): void {
    this.onmessage?.({ data: typeof frame === 'string' ? frame : JSON.stringify(frame) });
  }

  /** Deliver one live `runtime.event` frame for the given subscription. */
  notify(subscriptionId: string, cursor: number, kind = 'answer_delta'): void {
    this.push({
      jsonrpc: '2.0',
      method: 'runtime.event',
      params: { subscription_id: subscriptionId, cursor, event: eventAt(cursor, kind) },
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

  watchFrames(): SentFrame[] {
    return this.sent.filter((frame) => frame.method === 'runtime.events.watch');
  }

  unwatchFrames(): SentFrame[] {
    return this.sent.filter((frame) => frame.method === 'runtime.events.unwatch');
  }
}

class Factory {
  sockets: FakeSocket[] = [];
  make = (): FakeSocket => {
    const socket = new FakeSocket();
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

async function waitFor(predicate: () => boolean, label: string, timeoutMs = 2000): Promise<void> {
  const started = Date.now();
  while (!predicate()) {
    if (Date.now() - started > timeoutMs) throw new Error('timed out waiting for ' + label);
    await tick(2);
  }
}

type Delivery = { sub?: string; seq: number };

/** Connect one client over a single injected socket and record deliveries. */
async function openClient(delivered: Delivery[] = []) {
  const socket = new FakeSocket();
  const client = new SynapseRuntimeClient({
    url: 'ws://core',
    socketFactory: () => socket,
    onEvent: (event, meta: EventNotificationMeta | undefined) =>
      delivered.push({ sub: meta?.subscription_id, seq: event.sequence }),
  });
  const connecting = client.connect();
  socket.serverOpen();
  await connecting;
  return { client, socket };
}

test('two concurrent watches keep independent cursors and routes', async () => {
  const delivered: Delivery[] = [];
  const { client, socket } = await openClient(delivered);

  const a = await client.watchEvents(SESSION_A); // sub-1
  const b = await client.watchEvents(SESSION_B); // sub-2
  assert.equal(a.subscription_id, 'sub-1');
  assert.equal(b.subscription_id, 'sub-2');
  assert.equal(socket.watchFrames().length, 2, 'both watches are registered, not replaced');
  // The compatibility getters track the most recently registered watch (B).
  assert.equal(client.getWatchSession()?.thread_id, 'b');
  assert.equal(client.getWatchCursor(), 0);

  socket.notify('sub-1', 4);
  socket.notify('sub-2', 7);
  assert.equal(client.getWatchCursor('sub-1'), 4);
  assert.equal(client.getWatchCursor('sub-2'), 7);
  assert.equal(client.getWatchCursor(), 7, 'the no-argument getter is the latest watch');
  assert.equal(client.getWatchSession('sub-1')?.thread_id, 'a');
  assert.deepEqual(delivered, [
    { sub: 'sub-1', seq: 4 },
    { sub: 'sub-2', seq: 7 },
  ]);

  // A frame for A moves only A's cursor and reaches only A's consumer view.
  socket.notify('sub-1', 9);
  assert.equal(client.getWatchCursor('sub-1'), 9);
  assert.equal(client.getWatchCursor('sub-2'), 7, 'a frame for A must not move B');
  assert.deepEqual(delivered, [
    { sub: 'sub-1', seq: 4 },
    { sub: 'sub-2', seq: 7 },
    { sub: 'sub-1', seq: 9 },
  ]);

  client.disconnect();
});

test('a frame naming an unknown subscription is dropped', async () => {
  const delivered: Delivery[] = [];
  const { client, socket } = await openClient(delivered);
  await client.watchEvents(SESSION_A); // sub-1

  socket.notify('ghost', 3);
  assert.equal(delivered.length, 0, 'an unknown subscription id must not be delivered');
  assert.equal(client.getWatchCursor(), 0, 'an unknown subscription id must not move a cursor');

  socket.notify('sub-1', 5);
  assert.deepEqual(delivered, [{ sub: 'sub-1', seq: 5 }]);
  assert.equal(client.getWatchCursor(), 5);
  client.disconnect();
});

test('a fence on one watch leaves the other delivering', async () => {
  const notices: Array<{ type: string; service_code?: string; subscription_id?: string }> = [];
  const delivered: number[] = [];
  const socket = new FakeSocket();
  const client = new SynapseRuntimeClient({
    url: 'ws://core',
    socketFactory: () => socket,
    onEvent: (event) => delivered.push(event.sequence),
    onSubscriptionNotice: (notice) => notices.push(notice),
  });
  const connecting = client.connect();
  socket.serverOpen();
  await connecting;

  await client.watchEvents(SESSION_A); // sub-1
  await client.watchEvents(SESSION_B); // sub-2

  // An unconsumable frame (a future envelope version) fences A only.
  socket.push({
    jsonrpc: '2.0',
    method: 'runtime.event',
    params: { subscription_id: 'sub-1', cursor: 4, event: { ...eventAt(4), version: 2 } },
  });
  assert.deepEqual(
    notices.map((notice) => [notice.type, notice.service_code, notice.subscription_id]),
    [['error', 'unsupported_event_version', 'sub-1']],
  );

  // A later legal frame for the fenced A stays silent and keeps A's cursor...
  socket.notify('sub-1', 5);
  assert.equal(delivered.length, 0, 'a fenced subscription may not deliver');
  assert.equal(client.getWatchCursor('sub-1'), 0, 'a fenced subscription may not advance its cursor');

  // ...but B is a different subscription and keeps streaming.
  socket.notify('sub-2', 6);
  assert.equal(client.getWatchCursor('sub-2'), 6);
  assert.deepEqual(delivered, [6], 'a fence on A must not fence B');

  // An unattributable frame is fenced while any fence is open.
  socket.push({
    jsonrpc: '2.0',
    method: 'runtime.event',
    params: { cursor: 7, event: eventAt(7) },
  });
  assert.deepEqual(delivered, [6], 'an unattributed frame is fenced while any fence is open');

  client.disconnect();
});

test('a frame with no subscription id is routed only while exactly one watch is live', async () => {
  const delivered: Delivery[] = [];
  const { client, socket } = await openClient(delivered);

  // One live watch: a legacy unattributed frame is attributed to it.
  await client.watchEvents(SESSION_A); // sub-1
  socket.push({
    jsonrpc: '2.0',
    method: 'runtime.event',
    params: { cursor: 5, event: eventAt(5) },
  });
  // The *resolved* id travels with the event, so the store routes it to the sole
  // live watch's view instead of whichever session happens to be active.
  assert.deepEqual(delivered, [{ sub: 'sub-1', seq: 5 }]);
  assert.equal(client.getWatchCursor('sub-1'), 5);

  // Two live watches: the frame can no longer be attributed, so it is ignored.
  await client.watchEvents(SESSION_B); // sub-2
  socket.push({
    jsonrpc: '2.0',
    method: 'runtime.event',
    params: { cursor: 6, event: eventAt(6) },
  });
  assert.equal(delivered.length, 1, 'an ambiguous unattributed frame is ignored');
  assert.equal(client.getWatchCursor('sub-1'), 5, 'the unattributed frame moved no cursor');
  assert.equal(client.getWatchCursor('sub-2'), 0);
  client.disconnect();
});

test('an unattributed completion is attributed to the sole live watch', async () => {
  const notices: Array<{ type: string; subscription_id?: string; cursor?: number }> = [];
  const socket = new FakeSocket();
  const client = new SynapseRuntimeClient({
    url: 'ws://core',
    socketFactory: () => socket,
    onSubscriptionNotice: (notice) => notices.push(notice),
  });
  const connecting = client.connect();
  socket.serverOpen();
  await connecting;

  await client.watchEvents(SESSION_A); // sub-1
  socket.push({
    jsonrpc: '2.0',
    method: 'runtime.subscription.complete',
    params: { cursor: 9 },
  });
  await tick();
  // The resolved id travels with the notice too, so the store marks *this*
  // watch's view stale instead of treating it as the active session's completion.
  assert.deepEqual(notices, [{ type: 'complete', subscription_id: 'sub-1', cursor: 9 }]);
  client.disconnect();
});

test("an unattributed frame lands in the sole live watch's view, not the active session", async () => {
  // Mirror the store's routing: events are attributed by `meta.subscription_id`,
  // and the active session is a *different* watch (during a switch, the one being
  // left).  Before the fix the client forwarded `undefined`, which the store would
  // apply to the active session.
  const views = new Map<string, number[]>([['active', []]]);
  const activeSubscriptionId = 'sub-active';
  const socket = new FakeSocket();
  const client = new SynapseRuntimeClient({
    url: 'ws://core',
    socketFactory: () => socket,
    onEvent: (event, meta) => {
      const key = meta?.subscription_id ?? activeSubscriptionId;
      views.set(key, [...(views.get(key) ?? []), event.sequence]);
    },
  });
  const connecting = client.connect();
  socket.serverOpen();
  await connecting;

  await client.watchEvents(SESSION_A); // sub-1, NOT the active session
  socket.push({
    jsonrpc: '2.0',
    method: 'runtime.event',
    params: { cursor: 5, event: eventAt(5) },
  });
  await tick();

  assert.deepEqual(views.get('sub-1'), [5], "the frame lands in the sole watch's view");
  assert.deepEqual(views.get('active'), [], "and never in the active session's view");
  client.disconnect();
});

test('re-watching a fenced session releases the replaced lease on the live socket', async () => {
  const { client, socket } = await openClient();
  await client.watchEvents(SESSION_A); // sub-1

  // An unconsumable frame fences sub-1 (its last good cursor stays at 0).
  socket.push({
    jsonrpc: '2.0',
    method: 'runtime.event',
    params: { subscription_id: 'sub-1', cursor: 4, event: { ...eventAt(4), version: 2 } },
  });
  await tick();

  await client.watchEvents(SESSION_A, 0); // sub-2 replaces the fenced lease
  await tick();
  // A client-side fence does not kill the daemon lease, so the replaced id is
  // released best-effort while the same socket that raised the fence is live.
  assert.deepEqual(
    socket.unwatchFrames().map((frame) => frame.params.subscription_id),
    ['sub-1'],
  );
  client.disconnect();
});

test('re-watching after a drop does not unwatch a dead socket generation lease', async () => {
  const factory = new Factory();
  const client = new SynapseRuntimeClient({
    url: 'ws://core',
    socketFactory: factory.make,
    reconnect: { maxAttempts: 3, baseDelayMs: 1, maxDelayMs: 2 },
  });
  const connecting = client.connect();
  const first = factory.last;
  first.serverOpen();
  await connecting;

  await client.watchEvents(SESSION_A); // sub-1
  first.push({
    jsonrpc: '2.0',
    method: 'runtime.event',
    params: { subscription_id: 'sub-1', cursor: 4, event: { ...eventAt(4), version: 2 } },
  });
  await tick();

  first.serverDrop();
  await waitFor(() => factory.calls === 2, 'the reconnect socket');
  factory.last.serverOpen();
  await waitFor(() => client.getState() === 'connected', 'reconnected');

  await client.watchEvents(SESSION_A, 0); // replaces the fenced watch on the new socket
  await tick();
  // The fence was raised in the *old* generation, so the daemon already closed
  // that subscription with the socket: no unwatch is sent.
  assert.deepEqual(factory.last.unwatchFrames(), []);
  client.disconnect();
});

test('unwatchEvents() with no argument detaches every watch', async () => {
  const { client, socket } = await openClient();
  await client.watchEvents(SESSION_A); // sub-1
  await client.watchEvents(SESSION_B); // sub-2

  await client.unwatchEvents();

  assert.equal(client.getWatchCursor(), null);
  assert.equal(client.getWatchSession(), null);
  assert.deepEqual(
    socket.unwatchFrames().map((frame) => frame.params.subscription_id).sort(),
    ['sub-1', 'sub-2'],
  );
  client.disconnect();
});

test('unwatchEvents(id) detaches just that watch', async () => {
  const delivered: Delivery[] = [];
  const { client, socket } = await openClient(delivered);
  await client.watchEvents(SESSION_A); // sub-1
  await client.watchEvents(SESSION_B); // sub-2

  await client.unwatchEvents('sub-1');

  assert.equal(client.getWatchCursor('sub-1'), null);
  assert.equal(client.getWatchSession('sub-1'), null);
  assert.deepEqual(
    socket.unwatchFrames().map((frame) => frame.params.subscription_id),
    ['sub-1'],
  );
  // The surviving watch still owns the latest slot and keeps its cursor.
  assert.equal(client.getWatchSession()?.thread_id, 'b');
  assert.equal(client.getWatchCursor('sub-2'), 0);

  // The detached id is unknown again, so a late frame for it is dropped.
  socket.notify('sub-1', 4);
  assert.equal(delivered.length, 0, 'a detached watch may not be delivered');
  socket.notify('sub-2', 4);
  assert.deepEqual(delivered, [{ sub: 'sub-2', seq: 4 }]);
  client.disconnect();
});

test('a 33rd concurrent watch is refused with the transport_busy convention', async () => {
  const { client, socket } = await openClient();
  for (let i = 0; i < 32; i += 1) {
    await client.watchEvents({ project_id: 'proj', thread_id: `t${i}` });
  }
  assert.equal(socket.watchFrames().length, 32, 'the daemon cap is 32 watches per connection');

  await assert.rejects(
    client.watchEvents({ project_id: 'proj', thread_id: 'overflow' }),
    (err: unknown) => {
      assert.ok(err instanceof RpcCallError);
      assert.equal((err as RpcCallError).code, -32001);
      assert.equal((err as RpcCallError).service_code, 'transport_busy');
      return true;
    },
  );
  assert.equal(socket.watchFrames().length, 32, 'the refused watch sends no frame');
  client.disconnect();
});

test('concurrent watches survive an unexpected drop but not a manual disconnect', async () => {
  const factory = new Factory();
  const client = new SynapseRuntimeClient({
    url: 'ws://core',
    socketFactory: factory.make,
    reconnect: { maxAttempts: 3, baseDelayMs: 1, maxDelayMs: 2 },
  });
  const connecting = client.connect();
  const first = factory.last;
  first.serverOpen();
  await connecting;

  await client.watchEvents(SESSION_A); // sub-1
  await client.watchEvents(SESSION_B); // sub-2
  first.notify('sub-1', 4);
  first.notify('sub-2', 9);

  first.serverDrop();
  await waitFor(() => factory.calls === 2, 'the reconnect socket');
  factory.last.serverOpen();
  await waitFor(() => client.getState() === 'connected', 'reconnected');

  assert.equal(client.getWatchCursor('sub-1'), 4, 'watch A survives the drop');
  assert.equal(client.getWatchCursor('sub-2'), 9, 'watch B survives the drop');

  client.disconnect();
  assert.equal(client.getWatchCursor(), null, 'a manual close clears every watch');
  assert.equal(client.getWatchSession(), null);
});
