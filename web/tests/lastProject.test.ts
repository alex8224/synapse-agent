import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readLastProjectId, saveLastProjectId } from '../src/stores/lastProject.ts';

test('last selected project stores only its catalog id', () => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  const values = new Map<string, string>();
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value); },
    },
  });
  try {
    saveLastProjectId('project-1');
    assert.equal(readLastProjectId(), 'project-1');
    assert.deepEqual([...values], [['synapse.console.lastProjectId', 'project-1']]);
  } finally {
    if (previous) Object.defineProperty(globalThis, 'localStorage', previous);
    else Reflect.deleteProperty(globalThis, 'localStorage');
  }
});

test('storage rejection does not prevent switching', () => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    get: () => { throw new Error('storage disabled'); },
  });
  try {
    assert.equal(readLastProjectId(), null);
    assert.doesNotThrow(() => saveLastProjectId('project-2'));
  } finally {
    if (previous) Object.defineProperty(globalThis, 'localStorage', previous);
    else Reflect.deleteProperty(globalThis, 'localStorage');
  }
});
