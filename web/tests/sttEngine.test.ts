/**
 * Offline tests for the speech-engine rules the composer's microphone depends on.
 *
 * The build they gate is expensive (about a minute on a CPU), so the interesting
 * cases are the ones where asking would be wrong: the browser engine is in use,
 * the models are already in memory, or the local engine is selected but unusable.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  needsWarmUp,
  speechEngineErrorMessage,
  usesLocalEngine,
  usesRuntimeEngine,
} from '../src/components/composer/sttEngine.ts';
import type { SttStatusView } from '../src/runtime-client/types.ts';

function status(overrides: Partial<SttStatusView> = {}): SttStatusView {
  return {
    available: true,
    engine: 'local',
    reason: null,
    loaded: false,
    model_dir: '/models/stt',
    sample_rate: 16000,
    ...overrides,
  };
}

test('the local engine is warmed once it is selected, usable and cold', () => {
  assert.equal(needsWarmUp(status()), true);
});

test('nothing is warmed while the browser engine is the one in use', () => {
  assert.equal(needsWarmUp(status({ engine: 'browser' })), false);
});

test('an already warm engine is not asked again', () => {
  assert.equal(needsWarmUp(status({ loaded: true })), false);
});

test('a local engine that cannot run is not asked to build', () => {
  // Asking would fail on the host and paint a wait that never ends; the card shows
  // the reason instead and keeps the browser engine.
  assert.equal(needsWarmUp(status({ available: false, reason: '缺少模型文件' })), false);
});

test('no status yet means no request', () => {
  assert.equal(needsWarmUp(null), false);
});

test('the local engine only runs when it is both selected and available', () => {
  assert.equal(usesLocalEngine(status()), true);
  assert.equal(usesLocalEngine(status({ engine: 'browser' })), false);
  assert.equal(usesLocalEngine(status({ available: false })), false);
  assert.equal(usesLocalEngine(null), false);
});

test('cloud providers use the runtime audio path without loading local models', () => {
  const cloud = status({ engine: 'doubao', providers: [{
    id: 'doubao', label: '豆包流式识别（在线）', kind: 'cloud',
    needs_key: true, key_configured: true, available: true, reason: null, detail: '',
  }] });
  assert.equal(usesRuntimeEngine(cloud), true);
  assert.equal(needsWarmUp(cloud), false);
  assert.equal(usesRuntimeEngine({ ...cloud, available: false }), false);
  assert.equal(usesRuntimeEngine(status()), true);
  assert.equal(usesRuntimeEngine(status({ engine: 'browser' })), false);
  assert.equal(usesRuntimeEngine(status({ engine: 'unknown' })), false);
  assert.equal(usesRuntimeEngine(null), false);
});

test('a daemon that predates the method is named as a restart, not as a broken feature', () => {
  // "method not found" is what a stale daemon answers; the reader can act on that
  // ("restart the console"), and cannot act on the raw JSON-RPC text.
  assert.match(speechEngineErrorMessage({ service_code: 'method_not_found' }), /重启/);
  assert.match(speechEngineErrorMessage({ code: -32601 }), /重启/);
  // The daemon knows the engine list the console offers, so "unknown engine" is a
  // build skew too -- and it used to reach the reader as the wire's opaque
  // "runtime service error", which says nothing about what to do next.
  assert.match(speechEngineErrorMessage({ service_code: 'unknown_stt_engine' }), /重启/);
  assert.equal(speechEngineErrorMessage(new Error('boom')), 'boom');
  assert.equal(speechEngineErrorMessage(null), '设置语音引擎失败');
});
