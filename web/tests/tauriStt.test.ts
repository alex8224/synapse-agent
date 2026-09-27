/**
 * The cloud bridge refuses to pretend outside the desktop shell.
 *
 * `node --test` runs without a `window`, which is exactly the plain-browser case: the
 * vendor authenticates its WebSocket handshake with headers a browser cannot set, so
 * the bridge must report that plainly rather than surfacing a Tauri IPC error, and a
 * status read must answer "no bridge here" instead of throwing.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  NOT_DESKTOP_REASON,
  sttCloudAppend,
  sttCloudBegin,
  sttCloudCancel,
  sttCloudFinish,
  sttCloudStatus,
} from '../src/client/tauriStt.ts';

test('a status read outside the desktop shell reports no bridge instead of throwing', async () => {
  assert.equal(await sttCloudStatus(), null);
});

test('every cloud call refuses outside the desktop shell', async () => {
  const calls = [
    () => sttCloudBegin(),
    () => sttCloudAppend(1, 'AAAA'),
    () => sttCloudFinish(1),
    () => sttCloudCancel(1),
  ];
  for (const call of calls) {
    await assert.rejects(call, new RegExp(NOT_DESKTOP_REASON));
  }
});
