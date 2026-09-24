/**
 * Tauri 2 Native Terminal Bridge.
 *
 * Implements:
 * - Direct bidirectional PTY communication (spawn, write, resize, close)
 * - Event-based streaming from native PTY reader to Xterm frontend
 */
import { invoke } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';
import { isTauri } from './tauri.ts';

export async function createTerminal(
  workspace?: string,
  shell?: string,
  cols = 80,
  rows = 24,
): Promise<number> {
  if (!isTauri()) {
    throw new Error('原生终端仅支持在桌面端环境运行');
  }
  return await invoke<number>('tauri_terminal_create', {
    workspace,
    shell,
    cols,
    rows,
  });
}

export async function writeTerminal(id: number, data: string): Promise<void> {
  if (!isTauri()) return;
  await invoke('tauri_terminal_write', { id, data });
}

export async function resizeTerminal(id: number, cols: number, rows: number): Promise<void> {
  if (!isTauri()) return;
  await invoke('tauri_terminal_resize', { id, cols, rows });
}

export async function closeTerminal(id: number): Promise<void> {
  if (!isTauri()) return;
  await invoke('tauri_terminal_close', { id });
}

export async function onTerminalData(
  id: number,
  callback: (data: string) => void,
): Promise<UnlistenFn> {
  if (!isTauri()) {
    return () => {};
  }
  return await listen<string>(`terminal-data-${id}`, (event) => {
    callback(event.payload);
  });
}

export async function onTerminalExit(
  id: number,
  callback: () => void,
): Promise<UnlistenFn> {
  if (!isTauri()) {
    return () => {};
  }
  return await listen<number>(`terminal-exit-${id}`, () => {
    callback();
  });
}
