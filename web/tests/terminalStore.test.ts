/**
 * Unit tests for the integrated terminal store and shortcut integration.
 *
 * Verifies:
 *  - toggleOpen toggles open/closed state
 *  - toggleMaximize when closed automatically opens and maximizes
 *  - toggleMaximize when open flips the isMaximized boolean
 *  - setHeight clears isMaximized and clamps within bounds
 *  - TERMINAL_MAXIMIZE_CHORD is advertised in CONSOLE_SHORTCUTS
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { useTerminalStore } from '../src/stores/useTerminalStore.ts';
import {
  CONSOLE_SHORTCUTS,
  TERMINAL_MAXIMIZE_CHORD,
} from '../src/components/consoleShortcuts.ts';

test('toggleOpen toggles open state', () => {
  useTerminalStore.setState({ open: false, isMaximized: false });
  assert.equal(useTerminalStore.getState().open, false);

  useTerminalStore.getState().toggleOpen();
  assert.equal(useTerminalStore.getState().open, true);

  useTerminalStore.getState().toggleOpen();
  assert.equal(useTerminalStore.getState().open, false);
});

test('toggleMaximize when closed opens the terminal and marks it maximized', () => {
  useTerminalStore.setState({ open: false, isMaximized: false });

  useTerminalStore.getState().toggleMaximize();
  assert.equal(useTerminalStore.getState().open, true);
  assert.equal(useTerminalStore.getState().isMaximized, true);
});

test('toggleMaximize when already open flips isMaximized between true and false', () => {
  useTerminalStore.setState({ open: true, isMaximized: true });

  useTerminalStore.getState().toggleMaximize();
  assert.equal(useTerminalStore.getState().open, true);
  assert.equal(useTerminalStore.getState().isMaximized, false);

  useTerminalStore.getState().toggleMaximize();
  assert.equal(useTerminalStore.getState().open, true);
  assert.equal(useTerminalStore.getState().isMaximized, true);
});

test('setHeight resets isMaximized and respects minimum height', () => {
  useTerminalStore.setState({ open: true, isMaximized: true, height: 300 });

  useTerminalStore.getState().setHeight(400);
  assert.equal(useTerminalStore.getState().isMaximized, false);
  assert.equal(useTerminalStore.getState().height, 400);

  // Clamps to MIN_HEIGHT (140)
  useTerminalStore.getState().setHeight(50);
  assert.equal(useTerminalStore.getState().height, 140);
});

test('TERMINAL_MAXIMIZE_CHORD is declared and exposed in CONSOLE_SHORTCUTS', () => {
  assert.equal(TERMINAL_MAXIMIZE_CHORD, 'Ctrl + Shift + `');
  const shortcut = CONSOLE_SHORTCUTS.find((s) => s.chord === TERMINAL_MAXIMIZE_CHORD);
  assert.ok(shortcut !== undefined, 'TERMINAL_MAXIMIZE_CHORD must be registered in CONSOLE_SHORTCUTS');
  assert.equal(shortcut?.label, '最大化 / 还原集成终端');
});
