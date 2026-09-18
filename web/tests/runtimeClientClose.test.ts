/**
 * Offline tests for the Web client's `runtime.session.close` wrapper.
 *
 * They run under the Node built-in test runner with NO real WebSocket (fake
 * `SocketLike` injected) and no daemon, and they pin the four properties the
 * wrapper promises:
 *
 * - a default close never cancels: `cancel_active` is *omitted* from the frame
 *   (the wire default is `false`), so a session that still owns an active turn
 *   is refused with the typed `conflict` error instead of being cancelled;
 * - an explicit `cancel_active: true` is the only way to request the
 *   cancellation, and the reply reports the captured turn;
 * - the result is the typed `CloseSessionResult` (including the idempotent
 *   `closed: false` of a missing session) and server errors propagate as typed
 *   `RpcCallError`s / `ConnectionLostError`s;
 * - detach is not a close: disconnecting (or dropping the watch) sends no
 *   `runtime.session.close` and no `runtime.turn.cancel` frame.
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
const CAPS = { legacy_v1: true, raw_cursor: true, watch_resume: true, approval_resume: true };

interface SentFrame {
  id: number;
  method: string;
  params: any;
}

/** The wire shape of a successful close (all fields the daemon always sends). */
function closeResult(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    command_id: 'cmd-1',
    session: SESSION,
    closed: true,
    active_turn_id: null,
    cancellation_requested: false,
    ...overrides,
  };
}

class FakeSocket implements SocketLike {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: any }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  sent: SentFrame[] = [];
  /** Responder for every non-negotiate method; receives the parsed request. */
  respond: (request: SentFrame) => unknown = () => closeResult();

  send(data: string): void {
    const parsed = JSON.parse(data) as SentFrame;
    this.sent.push(parsed);
    if (parsed.method === 'runtime.protocol.negotiate') {
      this.push({
        jsonrpc: '2.0',
        id: parsed.id,
        meta: { wire_version: '1' },
        result: { wire_version: '1', supported_versions: ['1'], capabilities: CAPS },
      });
      return;
    }
    const payload = this.respond(parsed);
    if (payload && typeof payload === 'object' && (payload as any).__error) {
      this.push({ jsonrpc: '2.0', id: parsed.id, error: (payload as any).__error });
      return;
    }
    this.push({ jsonrpc: '2.0', id: parsed.id, meta: { wire_version: '1' }, result: payload });
  }

  close(): void {
    if (this.readyState === 3) return;
    this.readyState = 3;
    const cb = this.onclose;
    this.onclose = null;
    cb?.();
  }

  push(frame: unknown): void {
    if (this.onmessage) this.onmessage({ data: JSON.stringify(frame) });
  }

  serverOpen(): void {
    this.readyState = 1;
    this.onopen?.();
  }

  /** Every frame sent after the handshake, i.e. the business calls. */
  businessFrames(): SentFrame[] {
    return this.sent.filter((frame) => frame.method !== 'runtime.protocol.negotiate');
  }
}

class Factory {
  sockets: FakeSocket[] = [];
  make = (): FakeSocket => {
    const socket = new FakeSocket();
    this.sockets.push(socket);
    return socket;
  };
}

const tick = () => new Promise<void>((r) => setTimeout(r, 0));

async function openClient() {
  const factory = new Factory();
  const client = new SynapseRuntimeClient({
    url: 'ws://loopback',
    socketFactory: factory.make,
    reconnect: { maxAttempts: 2, baseDelayMs: 1, maxDelayMs: 5 },
  });
  const promise = client.connect();
  await tick();
  const socket = factory.sockets[0];
  socket.serverOpen();
  await promise;
  return { client, socket, factory };
}

test('closeSession defaults to a non-cancelling close and returns the typed result', async () => {
  const { client, socket } = await openClient();
  socket.respond = () => closeResult();

  const result = await client.closeSession({ session: SESSION });

  assert.equal(result.closed, true);
  assert.equal(result.active_turn_id, null);
  assert.equal(result.cancellation_requested, false);
  assert.equal(result.command_id, 'cmd-1');
  assert.deepEqual(result.session, SESSION);

  const frames = socket.businessFrames();
  assert.equal(frames.length, 1);
  assert.equal(frames[0].method, 'runtime.session.close');
  // The default is expressed by absence: no `cancel_active` key at all, so the
  // daemon applies its own `false` default and never sees an explicit null.
  assert.deepEqual(frames[0].params, { session: SESSION });
  assert.equal('cancel_active' in frames[0].params, false);
});

test('an explicit undefined cancel_active is omitted, never sent as null', async () => {
  const { client, socket } = await openClient();
  socket.respond = () => closeResult();

  await client.closeSession({ session: SESSION, cancel_active: undefined });

  const frame = socket.businessFrames()[0];
  assert.equal('cancel_active' in frame.params, false);
  assert.equal(JSON.stringify(frame.params).includes('cancel_active'), false);
});

test('cancel_active: true is the only way to request the cancellation', async () => {
  const { client, socket } = await openClient();
  socket.respond = () =>
    closeResult({
      command_id: 'cmd-2',
      active_turn_id: 'turn-1',
      cancellation_requested: true,
    });

  const result = await client.closeSession({
    session: SESSION,
    cancel_active: true,
    command_id: 'cmd-2',
  });

  const frame = socket.businessFrames()[0];
  assert.equal(frame.method, 'runtime.session.close');
  assert.deepEqual(frame.params, {
    session: SESSION,
    cancel_active: true,
    command_id: 'cmd-2',
  });
  assert.equal(result.cancellation_requested, true);
  assert.equal(result.active_turn_id, 'turn-1');
  assert.equal(result.command_id, 'cmd-2');
});

test('closing a missing session stays an idempotent closed: false result', async () => {
  const { client, socket } = await openClient();
  socket.respond = () => closeResult({ closed: false });

  const result = await client.closeSession({ session: SESSION });

  assert.equal(result.closed, false);
  assert.equal(result.cancellation_requested, false);
});

test('a busy session surfaces the typed conflict error instead of cancelling', async () => {
  const { client, socket } = await openClient();
  socket.respond = () => ({
    __error: {
      code: -32000,
      message: 'runtime service error',
      data: { service_code: 'conflict' },
    },
  });

  await assert.rejects(
    client.closeSession({ session: SESSION }),
    (err: unknown) => {
      assert.ok(err instanceof RpcCallError);
      assert.equal((err as RpcCallError).code, -32000);
      assert.equal((err as RpcCallError).service_code, 'conflict');
      return true;
    },
  );
  // The refused close is a plain rejected request: no follow-up cancel frame.
  assert.deepEqual(
    socket.businessFrames().map((frame) => frame.method),
    ['runtime.session.close'],
  );
});

test('an older daemon without the method stays a typed RpcCallError', async () => {
  const { client, socket } = await openClient();
  socket.respond = () => ({
    __error: {
      code: -32601,
      message: 'method not found',
      data: { service_code: 'method_not_found' },
    },
  });

  await assert.rejects(
    client.closeSession({ session: SESSION }),
    (err: unknown) => {
      assert.ok(err instanceof RpcCallError);
      assert.equal((err as RpcCallError).code, -32601);
      assert.equal((err as RpcCallError).service_code, 'method_not_found');
      return true;
    },
  );
});

test('a close before connect rejects without opening a socket', async () => {
  const factory = new Factory();
  const client = new SynapseRuntimeClient({ url: 'ws://loopback', socketFactory: factory.make });

  await assert.rejects(
    client.closeSession({ session: SESSION }),
    (err: unknown) => {
      assert.ok(err instanceof ConnectionLostError);
      return true;
    },
  );
  assert.equal(factory.sockets.length, 0);
});

test('disconnect and watch detach never close or cancel the session', async () => {
  const { client, socket } = await openClient();
  socket.respond = () => ({ subscription_id: 'sub-1', cursor: 3 });

  await client.watchEvents(SESSION);
  await client.unwatchEvents();
  client.disconnect();

  const methods = socket.businessFrames().map((frame) => frame.method);
  assert.deepEqual(methods, ['runtime.events.watch', 'runtime.events.unwatch']);
  assert.equal(methods.includes('runtime.session.close'), false);
  assert.equal(methods.includes('runtime.turn.cancel'), false);
  assert.equal(client.getState(), 'disconnected');
  assert.equal(client.getWatchSession(), null);
});
