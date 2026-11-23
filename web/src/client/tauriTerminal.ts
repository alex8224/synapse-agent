/**
 * Tauri 2 Native Terminal Bridge.
 *
 * Implements:
 * - Direct bidirectional PTY communication (spawn with native CLI initialPrompt, write, resize, close)
 * - Event-based streaming from native PTY reader to Xterm frontend
 * - Deterministic Agent lifecycle hook event subscription (`terminal-agent-state-{id}`)
 */
import { invoke } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';
import { isTauri } from './tauri.ts';
import { TerminalCommandQueue } from './terminalCommandQueue.ts';

const terminalCommands = new TerminalCommandQueue();

export interface TerminalAgentHookEvent {
  seq: number;
  ptyId: number;
  state: 'idle' | 'working' | 'done';
  event: string;
  detail?: string;
  conversationId?: string | null;
  terminationReason?: string | null;
  fullyIdle?: boolean;
  lastAssistantMarkdown?: string | null;
}

export interface TerminalAgentTitle {
  ptyId: number;
  conversationId: string | null;
  title: string | null;
}

export interface TerminalDataChunk {
  data: string;
  startOffset?: number;
  endOffset?: number;
}

export interface TerminalSessionInfo {
  ptyId: number;
  workspace: string | null;
  shell: string;
  command: string | null;
  cols: number;
  rows: number;
  exited: boolean;
  conversationId: string | null;
  title: string | null;
  lastAgentState?: TerminalAgentHookEvent | null;
}

export interface TerminalListResponse {
  bootId: string;
  sessions: TerminalSessionInfo[];
}

export interface TerminalAttachSnapshot {
  bootId: string;
  ptyId: number;
  scrollback: string;
  endOffset: number;
  cols: number;
  rows: number;
  exited: boolean;
  conversationId: string | null;
  title: string | null;
  lastAgentState?: TerminalAgentHookEvent | null;
}

export async function createTerminal(
  workspace?: string,
  shell?: string,
  cols = 80,
  rows = 24,
  command?: string,
  initialPrompt?: string,
): Promise<number> {
  if (!isTauri()) {
    throw new Error('原生终端仅支持在桌面端环境运行');
  }
  return await invoke<number>('tauri_terminal_create', {
    workspace,
    shell,
    cols,
    rows,
    command,
    initialPrompt,
  });
}

export async function writeTerminal(id: number, data: string): Promise<void> {
  if (!isTauri()) return;
  await terminalCommands.run(id, () => invoke('tauri_terminal_write', { id, data }));
}

export async function resizeTerminal(id: number, cols: number, rows: number): Promise<void> {
  if (!isTauri()) return;
  await terminalCommands.run(id, () => invoke('tauri_terminal_resize', { id, cols, rows }));
}

export async function closeTerminal(id: number): Promise<void> {
  if (!isTauri()) return;
  await terminalCommands.run(id, () => invoke('tauri_terminal_close', { id }));
}

export async function listTerminals(): Promise<TerminalListResponse> {
  if (!isTauri()) {
    return { bootId: '', sessions: [] };
  }
  return await invoke<TerminalListResponse>('tauri_terminal_list');
}

export async function attachTerminal(id: number): Promise<TerminalAttachSnapshot> {
  if (!isTauri()) {
    throw new Error('原生终端仅支持在桌面端环境运行');
  }
  return await invoke<TerminalAttachSnapshot>('tauri_terminal_attach', { id });
}

export async function onTerminalData(
  id: number,
  callback: (data: string, chunk?: TerminalDataChunk) => void,
): Promise<UnlistenFn> {
  if (!isTauri()) {
    return () => {};
  }
  return await listen<string | TerminalDataChunk>(`terminal-data-${id}`, (event) => {
    const payload = event.payload;
    if (typeof payload === 'string') {
      callback(payload);
    } else if (payload && typeof payload.data === 'string') {
      callback(payload.data, payload);
    }
  });
}

export async function onTerminalAgentState(
  id: number,
  callback: (payload: TerminalAgentHookEvent) => void,
): Promise<UnlistenFn> {
  if (!isTauri()) {
    return () => {};
  }
  return await listen<TerminalAgentHookEvent>(`terminal-agent-state-${id}`, (event) => {
    callback(event.payload);
  });
}

export async function onTerminalAgentTitle(
  ptyId: number,
  callback: (payload: TerminalAgentTitle) => void,
): Promise<UnlistenFn> {
  if (!isTauri()) return () => {};
  let receivedEvent = false;
  const unlisten = await listen<TerminalAgentTitle>(`terminal-agent-title-${ptyId}`, ({ payload }) => {
    if (payload.ptyId !== ptyId) return;
    receivedEvent = true;
    callback(payload);
  });
  // Subscribe first to catch changes while the snapshot IPC is in flight.
  void invoke<{ pty_id: number; conversation_id: string | null; title: string | null }>(
    'tauri_terminal_agent_title', { ptyId },
  ).then((snapshot) => {
    if (!receivedEvent && snapshot.pty_id === ptyId) {
      callback({ ptyId, conversationId: snapshot.conversation_id, title: snapshot.title });
    }
  }).catch(() => {}); // Older desktop shells do not expose this command.
  return unlisten;
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
