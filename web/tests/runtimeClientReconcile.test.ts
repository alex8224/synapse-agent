/**
 * Offline tests for the Web client's `runtime.session.reconcile` method.
 *
 * They run under the Node built-in test runner with NO real WebSocket (fake
 * `SocketLike` injected).  They verify:
 *
 * - the request is only sent after a successful negotiate (all business frames
 *   require the handshake; the wire shape stays the legacy one);
 * - the result is strictly parsed into the epoch / retention / probe DTO and a
 *   malformed or truncated result rejects instead of driving a fake recovery;
 * - an old wire server (`method_not_found`) and an old delegate
 *   (`invalid_request`) surface typed `RpcCallError`s that callers can turn
 *   into the explicit "recovery unavailable" degradation.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  RpcCallError,
  SynapseRuntimeClient,
} from '../src/client/SynapseRuntimeClient.ts';
import type { SocketLike } from '../src/client/SynapseRuntimeClient.ts';

const SESSION = { project_id: 'proj', thread_id: 'thr' };
const CAPS = { legacy_v1: true, raw_cursor: true, watch_resume: true, approval_resume: true };

function validSnapshot(): Record<string, unknown> {
  return {
    project_id: 'proj',
    thread_id: 'thr',
    history_available: false,
    history_total_turns: 0,
    live_epoch: 'epoch-1',
    live_latest_sequence: 10,
    live_oldest_sequence: 1,
    live_dropped_through: 0,
    active_turn_id: null,
    latest_turn_id: 'turn-1',
    latest_turn_first_sequence: 1,
    latest_turn_retained_from: 1,
    latest_turn_intact: true,
    probe: [{ turn_id: 'turn-1', covered: false }],
  };
}

class FakeSocket implements SocketLike {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: any }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  sent: string[] = [];
  inbox: string[] = [];
  /** Responder for non-negotiate methods; receives the parsed request. */
  respond: (request: { method: string; params: any; id: number }) => unknown = () =>
    validSnapshot();

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
          capabilities: CAPS,
        },
      });
      return;
    }
    const payload = this.respond({ method: parsed.method, params: parsed.params, id: parsed.id });
    if (payload && typeof payload === 'object' && (payload as any).__error) {
      const error = (payload as any).__error;
      this.push({
        jsonrpc: '2.0',
        id: parsed.id,
        meta: { wire_version: '1' },
        error,
      });
      return;
    }
    this.push({
      jsonrpc: '2.0',
      id: parsed.id,
      meta: { wire_version: '1' },
      result: payload,
    });
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
}

class Factory {
  sockets: FakeSocket[] = [];
  make = (): FakeSocket => {
    const s = new FakeSocket();
    this.sockets.push(s);
    return s;
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
  return { client, socket };
}

test('reconcileSession sends the exact frame only after negotiation', async () => {
  const { client, socket } = await openClient();
  const result = await client.reconcileSession({
    session: SESSION,
    probe_turn_ids: ['turn-1', 'turn-2'],
  });
  assert.equal(result.live_epoch, 'epoch-1');
  assert.equal(result.live_latest_sequence, 10);
  assert.equal(result.live_dropped_through, 0);
  assert.deepEqual(result.probe, [{ turn_id: 'turn-1', covered: false }]);
  // The handshake frame arrives first; the reconcile frame carries the probe.
  assert.equal(socket.sent[0].includes('runtime.protocol.negotiate'), true);
  const frame = JSON.parse(socket.sent[socket.sent.length - 1]);
  assert.equal(frame.method, 'runtime.session.reconcile');
  assert.deepEqual(frame.params.session, SESSION);
  assert.deepEqual(frame.params.probe_turn_ids, ['turn-1', 'turn-2']);
});

test('old wire server method_not_found stays a typed RpcCallError', async () => {
  const { client, socket } = await openClient();
  socket.respond = () => ({
    __error: {
      code: -32601,
      message: 'method not found',
      data: { service_code: 'method_not_found' },
    },
  });
  await assert.rejects(
    client.reconcileSession({ session: SESSION }),
    (err: unknown) => {
      assert.ok(err instanceof RpcCallError);
      assert.equal((err as RpcCallError).code, -32601);
      assert.equal((err as RpcCallError).service_code, 'method_not_found');
      return true;
    },
  );
});

test('old delegate invalid_request stays a typed RpcCallError', async () => {
  const { client, socket } = await openClient();
  socket.respond = () => ({
    __error: {
      code: -32000,
      message: 'runtime service error',
      data: { service_code: 'invalid_request' },
    },
  });
  await assert.rejects(
    client.reconcileSession({ session: SESSION }),
    (err: unknown) => {
      assert.equal((err as RpcCallError).service_code, 'invalid_request');
      return true;
    },
  );
});

test('malformed reconcile results reject instead of driving a fake recovery', async () => {
  const cases: Array<Record<string, unknown>> = [
    { ...validSnapshot(), extra_field: 1 },
    { ...validSnapshot(), live_epoch: 42 },
    { ...validSnapshot(), live_latest_sequence: -1 },
    { ...validSnapshot(), latest_turn_retained_from: 'x' },
    { ...validSnapshot(), active_turn_id: 1 },
    { ...validSnapshot(), history_available: 'yes' },
    { ...validSnapshot(), probe: 'nope' },
    { ...validSnapshot(), probe: Array.from({ length: 33 }, (_, i) => ({ turn_id: `t${i}`, covered: false })) },
    { ...validSnapshot(), probe: [{ turn_id: '', covered: false }] },
  ];
  for (const payload of cases) {
    const { client, socket } = await openClient();
    socket.respond = () => payload;
    await assert.rejects(
      client.reconcileSession({ session: SESSION }),
      (err: unknown) => {
        assert.ok(err instanceof RpcCallError, `expected RpcCallError for ${JSON.stringify(payload).slice(0, 80)}`);
        assert.equal((err as RpcCallError).service_code, 'malformed_reconcile_result');
        return true;
      },
    );
  }
});
