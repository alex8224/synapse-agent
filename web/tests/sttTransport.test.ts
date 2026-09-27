/**
 * Offline tests for the two ends behind one transport interface.
 *
 * The whole point of the interface is that the capture hook cannot tell the ends
 * apart, so what is pinned here is the mapping: each verb reaches exactly one RPC, and
 * a transport that cannot run here says why up front instead of failing after the
 * microphone is already open.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { runtimeSttTransport, tauriSttTransport } from '../src/components/composer/sttTransport.ts';
import { NOT_DESKTOP_REASON } from '../src/client/tauriStt.ts';
import type { SynapseRuntimeClient } from '../src/client/SynapseRuntimeClient.ts';

const SESSION = { project_id: 'p1', thread_id: 't1' };

/** A client that records the calls it receives, in order. */
function recordingClient() {
  const calls: string[] = [];
  const client = {
    async sttBegin() {
      calls.push('begin');
      return { sample_rate: 16000 };
    },
    async sttAppend(_session: unknown, dataBase64: string) {
      calls.push(`append:${dataBase64}`);
      return { partial: '在', finalized: ['好'], error: null };
    },
    async sttFinish() {
      calls.push('finish');
      return { finalized: ['尾'] };
    },
    async sttCancel() {
      calls.push('cancel');
      return { cancelled: true };
    },
  };
  return { calls, client: client as unknown as SynapseRuntimeClient };
}

test('the runtime transport maps each verb onto its own RPC, in order', async () => {
  const { calls, client } = recordingClient();
  const transport = runtimeSttTransport(client, SESSION);
  assert.equal(transport.id, 'runtime');
  assert.equal(transport.unavailable, null);

  const started = await transport.begin();
  assert.equal(started.sampleRate, 16000, 'the announced rate is what the encoder uses');
  assert.deepEqual(await started.session.append('AAAA'), {
    partial: '在',
    finalized: ['好'],
    error: null,
  });
  assert.deepEqual(await started.session.finish(), { finalized: ['尾'] });
  await started.session.cancel();
  assert.deepEqual(calls, ['begin', 'append:AAAA', 'finish', 'cancel']);
});

test('a runtime transport without a thread says so before the microphone opens', () => {
  const { client } = recordingClient();
  const transport = runtimeSttTransport(client, { project_id: 'p1', thread_id: '' });
  assert.equal(transport.unavailable, '尚未连接运行时，语音输入不可用');
});

test('the desktop transport is unusable outside the desktop shell', async () => {
  // `node --test` has no `window`, so this is the plain-browser case: the cloud
  // engine's handshake cannot be authenticated here, and the failure must be the
  // bridge's own sentence rather than a Tauri IPC error.
  const transport = tauriSttTransport();
  assert.equal(transport.id, 'tauri');
  await assert.rejects(() => transport.begin(), new RegExp(NOT_DESKTOP_REASON));
});
