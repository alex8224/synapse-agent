/**
 * Wire tests for the five session-scoped local speech-to-text methods
 * (`runtime.stt.status/begin/append/finish/cancel`).
 *
 * No daemon and no browser: the real protocol client runs over an injected
 * `SocketLike`, so what is pinned is the exact frame each thin wrapper sends
 * (method name and params) and the result it returns unchanged.  The local
 * engine's own audio path is exercised offline by `localSpeechAudio.test.ts`.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { SynapseRuntimeClient } from '../src/runtime-client/SynapseRuntimeClient.ts';
import type { SocketLike } from '../src/runtime-client/SynapseRuntimeClient.ts';

const SESSION = { project_id: 'proj', thread_id: 'thr' };

interface SentFrame {
  id: number;
  method: string;
  params: any;
}

/** Fake transport: answers the handshake and each STT method. */
class SttSocket implements SocketLike {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: any }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((ev?: { code?: number; reason?: string }) => void) | null = null;
  sent: SentFrame[] = [];

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

  reply(request: SentFrame): unknown {
    switch (request.method) {
      case 'runtime.protocol.negotiate':
        return {
          wire_version: '1',
          supported_versions: ['1'],
          capabilities: {
            legacy_v1: true,
            raw_cursor: true,
            watch_resume: true,
            approval_resume: true,
          },
        };
      case 'runtime.stt.status':
        return {
          available: true,
          engine: 'local',
          reason: null,
          loaded: false,
          model_dir: '/models/ear',
          sample_rate: 16000,
        };
      case 'runtime.stt.begin':
        return { sample_rate: 16000 };
      case 'runtime.stt.append':
        return { partial: '你好', finalized: ['上一句。'] };
      case 'runtime.stt.finish':
        return { finalized: ['最后一句。'] };
      case 'runtime.stt.cancel':
        return { cancelled: true };
      default:
        return {};
    }
  }

  push(frame: unknown): void {
    this.onmessage?.({ data: typeof frame === 'string' ? frame : JSON.stringify(frame) });
  }

  serverOpen(): void {
    this.readyState = 1;
    this.onopen?.();
  }

  close(): void {
    if (this.readyState === 3) return;
    this.readyState = 3;
    const cb = this.onclose;
    this.onclose = null;
    cb?.();
  }
}

async function connected(): Promise<{ client: SynapseRuntimeClient; socket: SttSocket }> {
  const socket = new SttSocket();
  const client = new SynapseRuntimeClient({
    url: 'ws://core',
    socketFactory: () => socket,
    onEvent: () => {},
  });
  const connecting = client.connect();
  socket.serverOpen();
  await connecting;
  return { client, socket };
}

/** The last frame the client sent for `method`. */
function lastFrame(socket: SttSocket, method: string): SentFrame | undefined {
  return [...socket.sent].reverse().find((frame) => frame.method === method);
}

test('the STT wrappers send the exact wire frames and return the results', async () => {
  const { client, socket } = await connected();

  const status = await client.sttStatus(SESSION);
  assert.deepEqual(lastFrame(socket, 'runtime.stt.status')?.params, { session: SESSION });
  assert.equal(status.engine, 'local');
  assert.equal(status.sample_rate, 16000);

  const begin = await client.sttBegin(SESSION);
  assert.deepEqual(lastFrame(socket, 'runtime.stt.begin')?.params, { session: SESSION });
  assert.equal(begin.sample_rate, 16000);

  const appended = await client.sttAppend(SESSION, 'AAEC');
  assert.deepEqual(lastFrame(socket, 'runtime.stt.append')?.params, {
    session: SESSION,
    data_base64: 'AAEC',
  });
  assert.equal(appended.partial, '你好');
  assert.deepEqual(appended.finalized, ['上一句。']);

  const finished = await client.sttFinish(SESSION);
  assert.deepEqual(lastFrame(socket, 'runtime.stt.finish')?.params, { session: SESSION });
  assert.deepEqual(finished.finalized, ['最后一句。']);

  const cancelled = await client.sttCancel(SESSION);
  assert.deepEqual(lastFrame(socket, 'runtime.stt.cancel')?.params, { session: SESSION });
  assert.equal(cancelled.cancelled, true);

  client.disconnect();
});
