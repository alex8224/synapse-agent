import assert from 'node:assert/strict';
import { test } from 'node:test';
import { tauriSetWindowTheme } from '../src/client/tauri.ts';

test('tauriSetWindowTheme passes dark: true for dark appearance', async () => {
  let invokedCmd = '';
  let invokedArgs: unknown = null;

  const mockWindow = {
    __TAURI_INTERNALS__: {
      invoke: async (cmd: string, args?: unknown) => {
        invokedCmd = cmd;
        invokedArgs = args;
      },
    },
  };

  const originalWindow = (globalThis as unknown as { window?: unknown }).window;
  (globalThis as unknown as { window: unknown }).window = mockWindow;

  try {
    await tauriSetWindowTheme('dark');
    assert.equal(invokedCmd, 'tauri_set_window_theme');
    assert.deepEqual(invokedArgs, { dark: true });

    await tauriSetWindowTheme('light');
    assert.deepEqual(invokedArgs, { dark: false });

    await tauriSetWindowTheme('system');
    assert.deepEqual(invokedArgs, { dark: null });
  } finally {
    (globalThis as unknown as { window?: unknown }).window = originalWindow;
  }
});

test('tauriSetWindowTheme degrades gracefully when invoke fails', async () => {
  const mockWindow = {
    __TAURI_INTERNALS__: {
      invoke: async () => {
        throw new Error('IPC failed');
      },
    },
  };

  const originalWindow = (globalThis as unknown as { window?: unknown }).window;
  (globalThis as unknown as { window: unknown }).window = mockWindow;

  try {
    await assert.doesNotReject(async () => {
      await tauriSetWindowTheme('dark');
    });
  } finally {
    (globalThis as unknown as { window?: unknown }).window = originalWindow;
  }
});
