import test from 'node:test';
import assert from 'node:assert/strict';
import {
  getOrCreateDeviceId,
  detectDeviceName,
} from '../src/client/remoteAccess.ts';

test('getOrCreateDeviceId returns a stable non-empty string in mock env', () => {
  // Mock localStorage if in node test env
  const storage = new Map<string, string>();
  globalThis.localStorage = {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
    removeItem: (key: string) => storage.delete(key),
    clear: () => storage.clear(),
    key: () => null,
    length: storage.size,
  } as Storage;

  const id1 = getOrCreateDeviceId();
  assert.ok(id1.startsWith('dev_'));
  const id2 = getOrCreateDeviceId();
  assert.equal(id1, id2);
});

test('detectDeviceName identifies default or UA', () => {
  const name = detectDeviceName();
  assert.ok(typeof name === 'string' && name.length > 0);
});
