/**
 * Terminal & Multi-Agent CLI Cockpit State Management Store.
 *
 * Implements:
 * - Three workspace display modes (`displayMode`):
 *   - `dock`: Bottom drawer below chat
 *   - `split`: Side-by-Side Split Workspace (Left Chat + Right Tiling Terminal Cockpit)
 *   - `stage`: Full workspace stage (100% terminal canvas)
 * - Tiling Window Manager layouts (`auto`, `columns`, `rows`, `master-stack`, `monocle`)
 * - Interactive pane resizing weights (`colWeights`, `rowWeights`, `masterRatio`, `splitWorkspaceRatio`)
 * - Chat ⇄ Terminal bidirectional linkage:
 *   - `@term:<seq>` / `@term:all` / `@term:quote:<id>` context expansion into prompt
 *   - `@spawn:<agent>` and `spawnAndDispatch` (auto-open new Agent pane & inject prompt once ready)
 *   - Live 3-line terminal output preview (`tailLines`) & remote `y/n` approval from chat
 *   - `autoReflow` watch mode (notifies main chat when a running Agent terminal finishes)
 */
import { create } from 'zustand';
import { createTerminal, closeTerminal, writeTerminal } from '../client/tauriTerminal.ts';

/**
 * Latest fitted `(cols, rows)` reported by each mounted `XtermView`, keyed by
 * `session.id`. Kept outside Zustand state so high-frequency resize events do
 * not trigger React re-renders, while allowing `createSession` and
 * `restartSession` to spawn ConPTY at the exact pane dimensions from byte 0.
 */
const measuredTerminalDimensions = new Map<string, { cols: number; rows: number }>();

export function reportTerminalDimensions(sessionId: string, cols: number, rows: number): void {
  if (cols > 0 && rows > 0) {
    measuredTerminalDimensions.set(sessionId, { cols, rows });
  }
}

export function clearTerminalDimensions(sessionId: string): void {
  measuredTerminalDimensions.delete(sessionId);
}

async function resolveInitialPtySize(sessionId: string): Promise<{ cols: number; rows: number }> {
  const existing = measuredTerminalDimensions.get(sessionId);
  if (existing) return existing;

  // Give React + XtermView up to two animation frames to mount and run FitAddon
  // so ConPTY opens at the real pane dimensions instead of 80x24.
  if (typeof requestAnimationFrame === 'function') {
    for (let i = 0; i < 2; i++) {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      const measured = measuredTerminalDimensions.get(sessionId);
      if (measured) return measured;
    }
  }
  return { cols: 80, rows: 24 };
}

export type TerminalDisplayMode = 'dock' | 'split' | 'stage';
export type TerminalGridLayout = 'auto' | 'columns' | 'rows' | 'master-stack' | 'monocle';
export type AgentActivityState = 'working' | 'waiting_input' | 'idle' | 'exited';

export interface AgentCliPreset {
  id: string;
  label: string;
  command: string;
  description: string;
  isAgent: boolean;
}

export const AGENT_CLI_PRESETS: AgentCliPreset[] = [
  {
    id: 'agy',
    label: 'agy CLI',
    command: 'agy',
    description: 'Antigravity 原生交互式 CLI 本体',
    isAgent: true,
  },
  {
    id: 'claude',
    label: 'Claude Code',
    command: 'claude',
    description: 'Anthropic Claude Code 终端 Agent',
    isAgent: true,
  },
  {
    id: 'codex',
    label: 'Codex CLI',
    command: 'codex',
    description: 'OpenAI Codex 终端 Agent',
    isAgent: true,
  },
  {
    id: 'synapse',
    label: 'Synapse TUI',
    command: 'synapse',
    description: 'Synapse 原生终端 TUI 实例',
    isAgent: true,
  },
  {
    id: 'pwsh',
    label: 'PowerShell / Shell',
    command: '',
    description: '标准系统交互式命令行终端',
    isAgent: false,
  },
];

export interface CreateTerminalSessionOptions {
  agentId?: string;
  label?: string;
  command?: string;
  isAgent?: boolean;
  initialPrompt?: string;
  autoReflow?: boolean;
}

export interface TerminalQuoteEntry {
  quoteId: string;
  sessionId: string;
  seq: number;
  title: string;
  text: string;
  lineCount: number;
}

export interface TerminalTabSession {
  id: string;
  seq: number;
  ptyId: number | null;
  title: string;
  /** The numbered launch title, retained while a display alias is set. */
  originalTitle?: string;
  alias?: string;
  /** agy-generated conversation title; separate from the pane's own name. */
  agentConversationTitle?: string;
  shell: string;
  agentId: string;
  command?: string;
  isAgent: boolean;
  workspace?: string;
  status: 'connecting' | 'running' | 'exited' | 'error';
  activity: AgentActivityState;
  activityDetail?: string;
  broadcastEnabled: boolean;
  /** Whether to automatically reflow the terminal result back to the main chat session when a task completes. */
  autoReflow: boolean;
  /** True only after a prompt/command was actually submitted to this terminal and before it completes. */
  hasActiveTask: boolean;
  /** Epoch ms when the current active task was submitted. */
  taskStartedAt?: number;
  /** Last 3 non-empty lines of terminal output for the live orchestration card in chat. */
  tailLines: string[];
  /** Clean Markdown / output captured when the last task completed, awaiting user adoption. */
  reflowResult?: string;
  errorMessage?: string;
}

/** Custom DOM event fired to insert a terminal mention/quote pill into RichComposer. */
export const INSERT_TERMINAL_PILL_EVENT = 'synapse:insert-terminal-pill';

export interface InsertTerminalPillDetail {
  token: string;
  label: string;
  detail: string;
  followUpText?: string;
}

export function requestInsertTerminalPill(payload: InsertTerminalPillDetail): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent<InsertTerminalPillDetail>(INSERT_TERMINAL_PILL_EVENT, { detail: payload }));
}

interface TerminalStoreState {
  open: boolean;
  height: number;
  isMaximized: boolean;
  displayMode: TerminalDisplayMode;
  /** Left chat width ratio in `split` workspace mode (0.25..0.75, default 0.48). */
  splitWorkspaceRatio: number;
  gridLayout: TerminalGridLayout;
  /** When set, temporarily zooms a single pane to 100% while preserving the tiling layout. */
  zoomedSessionId: string | null;
  /** Horizontal width weights per session ID (default 1.0). */
  colWeights: Record<string, number>;
  /** Vertical height weights per row key or session ID (default 1.0). */
  rowWeights: Record<string, number>;
  /** Left master column width ratio in `master-stack` layout (0.18..0.82, default 0.5). */
  masterRatio: number;
  activeSessionId: string | null;
  sessions: TerminalTabSession[];
  /** Stored terminal selection quotes referenced by `@term:quote:<id>` pills. */
  terminalQuotes: Record<string, TerminalQuoteEntry>;
  /** Prompts queued to be injected once a newly spawned Agent session outputs its initial prompt. */
  pendingDispatches: Record<string, string>;
  /** Session IDs that recently finished a task while `autoReflow` was enabled. */
  reflowReadySessionIds: string[];

  // Actions
  setOpen: (open: boolean) => void;
  toggleOpen: () => void;
  setHeight: (height: number) => void;
  toggleMaximize: () => void;
  setDisplayMode: (mode: TerminalDisplayMode) => void;
  toggleStageMode: () => void;
  toggleSplitWorkspaceMode: () => void;
  setSplitWorkspaceRatio: (ratio: number) => void;
  setGridLayout: (layout: TerminalGridLayout) => void;
  toggleZoomSession: (id: string) => void;
  promoteToMaster: (id: string) => void;
  setMasterRatio: (ratio: number) => void;
  setColPairWeights: (
    leftId: string,
    rightId: string,
    leftWeight: number,
    rightWeight: number,
  ) => void;
  setRowPairWeights: (
    topKey: string,
    bottomKey: string,
    topWeight: number,
    bottomWeight: number,
  ) => void;
  resetTilingWeights: () => void;
  setActiveSession: (id: string) => void;
  createSession: (
    workspace?: string,
    shell?: string,
    options?: CreateTerminalSessionOptions,
  ) => Promise<string>;
  spawnAndDispatch: (
    workspace: string | undefined,
    agentId: string,
    promptText: string,
  ) => Promise<string>;
  dispatchToSession: (id: string, promptText: string) => Promise<void>;
  consumePendingDispatch: (id: string) => Promise<void>;
  renameSession: (id: string, alias: string) => void;
  restartSession: (id: string) => Promise<void>;
  closeSession: (id: string) => Promise<void>;
  setAgentConversationTitle: (id: string, ptyId: number, title: string | null) => void;
  updateSessionPty: (
    id: string,
    ptyId: number,
    status: 'running' | 'exited' | 'error',
    errorMessage?: string,
  ) => void;
  markTaskStarted: (id: string) => void;
  completeActiveTask: (
    id: string,
    tailLines?: string[],
    reflowResult?: string,
    forceReflow?: boolean,
  ) => boolean;
  setReflowResult: (id: string, reflowResult: string) => void;
  setSessionActivity: (
    id: string,
    activity: AgentActivityState,
    detail?: string,
    tailLines?: string[],
  ) => void;
  updateActivityFromOsc: (id: string, rawPayload: string) => void;
  toggleBroadcastTarget: (id: string) => void;
  setAllBroadcastTargets: (enabled: boolean) => void;
  toggleAutoReflow: (id: string) => void;
  dismissReflowNotice: (id: string) => void;
  registerSelectionQuote: (sessionId: string, text: string) => TerminalQuoteEntry | null;
  broadcastInput: (text: string, explicitTargetIds?: string[]) => Promise<number>;
  sendQuickResponse: (id: string, data: string) => Promise<void>;
  handleSessionExit: (ptyId: number) => void;
}

const DEFAULT_HEIGHT = 300;
const MIN_HEIGHT = 140;

function nextAvailableSeq(sessions: TerminalTabSession[]): number {
  const used = new Set(sessions.map((s) => s.seq));
  let seq = 1;
  while (used.has(seq)) {
    seq++;
  }
  return seq;
}

/**
 * Format text for PTY injection:
 * - For interactive CLI Agent TUIs (`isAgent === true`, e.g. `agy`, `claude`, `codex`
 *   running under Windows ConPTY), raw newlines inside a single write are translated
 *   by ConPTY into premature Enter keystrokes on the first line, and unnegotiated
 *   `\x1b[200~` bracketed-paste sequences can be dropped by raw-mode TUI parsers.
 *   Flattening multi-line context into a clean single-line printable string and
 *   sending `\r` in a separate delayed packet guarantees the TUI input box receives
 *   the full text and then submits it cleanly.
 */
export function formatPtySubmission(rawText: string, isAgent = false): string {
  const normalized = rawText.replace(/\r\n/g, '\n').trim();
  if (isAgent) {
    const singleLine = normalized
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean)
      .join(' | ');
    return `${singleLine}\r`;
  }
  if (normalized.includes('\n')) {
    return `\x1b[200~${normalized}\x1b[201~\r`;
  }
  return `${normalized}\r`;
}

/**
 * Two-phase PTY prompt injection for CLI Agent TUIs (`agy`, `claude`, `codex`):
 * Phase 1 writes pure printable characters (no `\r`/`\n` control chars) into the
 * focused TUI input box; Phase 2 waits 240ms for the TUI framework (Ink/BubbleTea)
 * to commit the input buffer state, then sends an isolated `\r` (Enter) keystroke.
 */
export async function writePtyPromptAndSubmit(
  ptyId: number,
  rawText: string,
  isAgent: boolean,
): Promise<void> {
  const normalized = rawText.replace(/\r\n/g, '\n').trim();
  if (!normalized) return;

  if (isAgent) {
    const printableLine = normalized
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean)
      .join(' | ');
    await writeTerminal(ptyId, printableLine);
    await new Promise((resolve) => setTimeout(resolve, 240));
    await writeTerminal(ptyId, '\r');
  } else {
    await writeTerminal(ptyId, formatPtySubmission(normalized, false));
  }
}

export const useTerminalStore = create<TerminalStoreState>((set, get) => ({
  open: false,
  height: DEFAULT_HEIGHT,
  isMaximized: false,
  displayMode: 'dock',
  splitWorkspaceRatio: 0.48,
  gridLayout: 'auto',
  zoomedSessionId: null,
  colWeights: {},
  rowWeights: {},
  masterRatio: 0.5,
  activeSessionId: null,
  sessions: [],
  terminalQuotes: {},
  pendingDispatches: {},
  reflowReadySessionIds: [],

  setOpen: (open) =>
    set((state) => ({
      open,
      displayMode: open ? state.displayMode : 'dock',
    })),

  toggleOpen: () =>
    set((state) => {
      const nextOpen = !state.open;
      return {
        open: nextOpen,
        displayMode: nextOpen ? state.displayMode : 'dock',
      };
    }),

  setHeight: (rawHeight) => {
    const maxHeight = typeof window !== 'undefined' ? window.innerHeight - 100 : 800;
    const clamped = Math.max(MIN_HEIGHT, Math.min(rawHeight, maxHeight));
    set({ height: clamped, isMaximized: false });
  },

  toggleMaximize: () =>
    set((state) => {
      if (!state.open) {
        return { open: true, isMaximized: true };
      }
      return { isMaximized: !state.isMaximized };
    }),

  setDisplayMode: (mode) =>
    set(() => ({
      open: true,
      displayMode: mode,
      isMaximized: false,
    })),

  toggleStageMode: () =>
    set((state) => {
      if (!state.open || state.displayMode !== 'stage') {
        return { open: true, displayMode: 'stage', isMaximized: false };
      }
      return { displayMode: 'dock' };
    }),

  toggleSplitWorkspaceMode: () =>
    set((state) => {
      if (!state.open || state.displayMode !== 'split') {
        return { open: true, displayMode: 'split', isMaximized: false };
      }
      return { displayMode: 'dock' };
    }),

  setSplitWorkspaceRatio: (ratio) =>
    set(() => ({
      splitWorkspaceRatio: Math.max(0.25, Math.min(0.75, ratio)),
    })),

  setGridLayout: (layout) =>
    set(() => ({
      gridLayout: layout,
      zoomedSessionId: null,
    })),

  toggleZoomSession: (id) =>
    set((state) => ({
      activeSessionId: id,
      zoomedSessionId: state.zoomedSessionId === id ? null : id,
    })),

  promoteToMaster: (id) =>
    set((state) => {
      const target = state.sessions.find((s) => s.id === id);
      if (!target) return state;
      const rest = state.sessions.filter((s) => s.id !== id);
      return {
        activeSessionId: id,
        sessions: [target, ...rest],
      };
    }),

  setMasterRatio: (ratio) =>
    set(() => ({
      masterRatio: Math.max(0.18, Math.min(0.82, ratio)),
    })),

  setColPairWeights: (leftId, rightId, leftWeight, rightWeight) =>
    set((state) => ({
      colWeights: {
        ...state.colWeights,
        [leftId]: Math.max(0.15, leftWeight),
        [rightId]: Math.max(0.15, rightWeight),
      },
    })),

  setRowPairWeights: (topKey, bottomKey, topWeight, bottomWeight) =>
    set((state) => ({
      rowWeights: {
        ...state.rowWeights,
        [topKey]: Math.max(0.15, topWeight),
        [bottomKey]: Math.max(0.15, bottomWeight),
      },
    })),

  resetTilingWeights: () =>
    set(() => ({
      colWeights: {},
      rowWeights: {},
      masterRatio: 0.5,
    })),

  setActiveSession: (id) =>
    set((state) => ({
      activeSessionId: id,
      zoomedSessionId: state.zoomedSessionId ? id : null,
    })),

  createSession: async (workspace, shell, options) => {
    const id = `term-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
    const seq = nextAvailableSeq(get().sessions);
    const preset = options?.agentId
      ? AGENT_CLI_PRESETS.find((p) => p.id === options.agentId)
      : undefined;
    const agentId = options?.agentId || preset?.id || shell || 'pwsh';
    const command = options?.command !== undefined ? options.command : preset?.command;
    const isAgent =
      options?.isAgent !== undefined
        ? options.isAgent
        : Boolean(preset?.isAgent || (command && command.trim().length > 0));
    const displayLabel = options?.label || preset?.label || shell || 'pwsh';
    const title = `${seq}: ${displayLabel}`;

    const initialPrompt = options?.initialPrompt?.trim();

    const newSession: TerminalTabSession = {
      id,
      seq,
      ptyId: null,
      title,
      shell: shell || 'pwsh',
      agentId,
      command,
      isAgent,
      workspace,
      status: 'connecting',
      activity: initialPrompt ? 'working' : 'idle',
      broadcastEnabled: isAgent,
      autoReflow: options?.autoReflow ?? isAgent,
      hasActiveTask: Boolean(initialPrompt),
      taskStartedAt: initialPrompt ? Date.now() : undefined,
      tailLines: [],
    };

    set((state) => ({
      open: true,
      sessions: [...state.sessions, newSession],
      activeSessionId: id,
      zoomedSessionId: null,
    }));

    try {
      const { cols, rows } = await resolveInitialPtySize(id);
      // Pass `initialPrompt` directly to `createTerminal` so `terminal.rs` sets
      // `SYNAPSE_AGENT_PROMPT` and invokes `agy -i "$env:SYNAPSE_AGENT_PROMPT"` natively!
      const ptyId = await createTerminal(
        workspace,
        shell,
        cols,
        rows,
        command,
        initialPrompt,
      );
      get().updateSessionPty(id, ptyId, 'running');
      return id;
    } catch (err) {
      get().updateSessionPty(id, 0, 'error', String(err));
      return id;
    }
  },

  spawnAndDispatch: async (workspace, agentId, promptText) => {
    const preset = AGENT_CLI_PRESETS.find((p) => p.id.toLowerCase() === agentId.toLowerCase());
    if (!get().open || get().displayMode === 'dock') {
      set({ open: true, displayMode: 'split', isMaximized: false });
    }
    return await get().createSession(workspace, undefined, {
      agentId: preset?.id ?? agentId,
      label: preset?.label ?? agentId,
      command: preset ? preset.command : agentId === 'pwsh' ? '' : agentId,
      isAgent: preset ? preset.isAgent : agentId !== 'pwsh',
      initialPrompt: promptText,
      autoReflow: true,
    });
  },

  dispatchToSession: async (id, promptText) => {
    const trimmed = promptText.trim();
    if (!trimmed) return;
    const session = get().sessions.find((s) => s.id === id);
    if (!session) return;
    if (!get().open) {
      set({ open: true });
    }
    set({ activeSessionId: id });
    // If the session is already running AND idle (ready at its prompt), inject immediately.
    // If it is still connecting or currently busy running a turn (`working`), queue in
    // `pendingDispatches` so it flushes as soon as the `SessionStart` or `Stop` hook marks it idle.
    if (session.ptyId && session.status === 'running' && session.activity !== 'working') {
      get().markTaskStarted(id);
      await writePtyPromptAndSubmit(session.ptyId, trimmed, session.isAgent);
    } else {
      set((state) => ({
        pendingDispatches: { ...state.pendingDispatches, [id]: trimmed },
      }));
    }
  },

  consumePendingDispatch: async (id) => {
    const prompt = get().pendingDispatches[id];
    if (!prompt) return;
    const session = get().sessions.find((s) => s.id === id);
    if (!session?.ptyId || session.status !== 'running') return;

    set((state) => {
      const next = { ...state.pendingDispatches };
      delete next[id];
      return { pendingDispatches: next };
    });

    get().markTaskStarted(id);
    await writePtyPromptAndSubmit(session.ptyId, prompt, session.isAgent);
  },

  renameSession: (id, alias) => {
    const session = get().sessions.find((s) => s.id === id);
    const nextAlias = alias.trim().replace(/\s+/g, ' ') || undefined;
    if (!session || session.alias === nextAlias) return;
    const originalTitle = session.originalTitle ?? session.title;
    set((state) => ({
      sessions: state.sessions.map((s) => s.id === id ? {
        ...s,
        title: nextAlias ? `${originalTitle} (${nextAlias})` : originalTitle,
        originalTitle: nextAlias ? originalTitle : undefined,
        alias: nextAlias,
      } : s),
    }));
  },

  restartSession: async (id) => {
    const session = get().sessions.find((s) => s.id === id);
    if (!session) return;
    if (session.ptyId) {
      try {
        await closeTerminal(session.ptyId);
      } catch {}
    }
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.id === id
          ? {
              ...s,
              ptyId: null,
              agentConversationTitle: undefined,
              status: 'connecting',
              activity: s.isAgent ? 'working' : 'idle',
              activityDetail: undefined,
              errorMessage: undefined,
            }
          : s,
      ),
    }));
    try {
      const { cols, rows } = await resolveInitialPtySize(id);
      const ptyId = await createTerminal(
        session.workspace,
        session.shell,
        cols,
        rows,
        session.command,
      );
      get().updateSessionPty(id, ptyId, 'running');
    } catch (err) {
      get().updateSessionPty(id, 0, 'error', String(err));
    }
  },

  closeSession: async (id) => {
    const { sessions, activeSessionId, zoomedSessionId, colWeights, rowWeights } = get();
    const idx = sessions.findIndex((s) => s.id === id);
    const session = idx >= 0 ? sessions[idx] : undefined;
    if (session?.ptyId) {
      try {
        await closeTerminal(session.ptyId);
      } catch {}
    }
    clearTerminalDimensions(id);

    const nextSessions = sessions.filter((s) => s.id !== id);
    const fallbackNeighbor =
      nextSessions[Math.min(idx, nextSessions.length - 1)]?.id ?? nextSessions[0]?.id ?? null;
    const nextActive = activeSessionId === id ? fallbackNeighbor : activeSessionId;
    const nextZoomed = zoomedSessionId === id ? null : zoomedSessionId;

    const nextColWeights = { ...colWeights };
    delete nextColWeights[id];
    const nextRowWeights = { ...rowWeights };
    delete nextRowWeights[id];

    if (nextSessions.length === 0) {
      set({
        sessions: [],
        activeSessionId: null,
        zoomedSessionId: null,
        colWeights: {},
        rowWeights: {},
        masterRatio: 0.5,
        reflowReadySessionIds: [],
        open: false,
        displayMode: 'dock',
      });
    } else {
      set((state) => ({
        sessions: nextSessions,
        activeSessionId: nextActive,
        zoomedSessionId: nextZoomed,
        colWeights: nextColWeights,
        rowWeights: nextRowWeights,
        reflowReadySessionIds: state.reflowReadySessionIds.filter((sid) => sid !== id),
      }));
    }
  },

  setAgentConversationTitle: (id, ptyId, title) => {
    const next = title?.replace(/\s+/g, ' ').trim().slice(0, 160) || undefined;
    set((state) => {
      const current = state.sessions.find((session) => session.id === id);
      if (!current || current.ptyId !== ptyId || current.agentConversationTitle === next) return state;
      return { sessions: state.sessions.map((session) =>
        session === current ? { ...session, agentConversationTitle: next } : session,
      ) };
    });
  },

  updateSessionPty: (id, ptyId, status, errorMessage) => {
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.id === id
          ? {
              ...s,
              ptyId,
              agentConversationTitle: s.ptyId === ptyId ? s.agentConversationTitle : undefined,
              status,
              activity: status === 'exited' || status === 'error' ? 'exited' : s.activity,
              errorMessage,
            }
          : s,
      ),
    }));
  },

  markTaskStarted: (id) => {
    const now = Date.now();
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.id === id
          ? {
              ...s,
              activity: 'working',
              activityDetail: undefined,
              hasActiveTask: true,
              taskStartedAt: now,
              reflowResult: undefined,
            }
          : s,
      ),
      reflowReadySessionIds: state.reflowReadySessionIds.filter((sid) => sid !== id),
    }));
  },

  completeActiveTask: (id, tailLines, reflowResult, forceReflow = false) => {
    const current = get().sessions.find((s) => s.id === id);
    if (!current) return false;
    const shouldAutoReflow = current.autoReflow && (forceReflow || current.hasActiveTask);
    const nextTail = tailLines ?? current.tailLines;

    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.id === id
          ? {
              ...s,
              activity: 'idle',
              activityDetail: undefined,
              hasActiveTask: false,
              tailLines: nextTail,
              reflowResult: reflowResult ?? s.reflowResult,
            }
          : s,
      ),
      reflowReadySessionIds:
        shouldAutoReflow && !state.reflowReadySessionIds.includes(id)
          ? [...state.reflowReadySessionIds, id]
          : state.reflowReadySessionIds,
    }));

    return shouldAutoReflow;
  },

  setReflowResult: (id, reflowResult) => {
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.id === id ? { ...s, reflowResult } : s,
      ),
    }));
  },

  setSessionActivity: (id, activity, detail, tailLines) => {
    const current = get().sessions.find((s) => s.id === id);
    if (!current) return;
    const nextTail = tailLines ?? current.tailLines;
    const tailUnchanged =
      nextTail.length === current.tailLines.length &&
      nextTail.every((line, i) => line === current.tailLines[i]);

    if (
      current.activity === activity &&
      current.activityDetail === detail &&
      tailUnchanged
    ) {
      return;
    }

    const completedWork =
      current.autoReflow &&
      current.hasActiveTask &&
      current.activity === 'working' &&
      (activity === 'idle' || activity === 'waiting_input') &&
      nextTail.length > 0;

    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.id === id
          ? {
              ...s,
              activity,
              activityDetail: detail,
              tailLines: nextTail,
            }
          : s,
      ),
      reflowReadySessionIds:
        completedWork && !state.reflowReadySessionIds.includes(id)
          ? [...state.reflowReadySessionIds, id]
          : activity === 'working'
            ? state.reflowReadySessionIds.filter((sid) => sid !== id)
            : state.reflowReadySessionIds,
    }));
  },

  /**
   * Native compatibility with Rex's OSC 7501 agent status reporting sequence
   * (`\x1b]7501;<state>[;<detail>]\x07` or JSON payload).
   */
  updateActivityFromOsc: (id, rawPayload) => {
    const trimmed = rawPayload.trim();
    if (!trimmed) return;
    let stateToken = trimmed.toLowerCase();
    let detail: string | undefined;

    if (trimmed.startsWith('{')) {
      try {
        const parsed = JSON.parse(trimmed) as { state?: string; status?: string; message?: string };
        stateToken = (parsed.state || parsed.status || '').toLowerCase();
        detail = parsed.message;
      } catch {}
    } else if (trimmed.includes(';')) {
      const [first, ...rest] = trimmed.split(';');
      stateToken = first.trim().toLowerCase();
      detail = rest.join(';').trim() || undefined;
    } else if (trimmed.includes('=')) {
      const parts = Object.fromEntries(
        trimmed.split(';').map((kv) => {
          const [k, v] = kv.split('=');
          return [k?.trim().toLowerCase(), v?.trim()];
        }),
      );
      stateToken = (parts.state || parts.status || stateToken).toLowerCase();
      detail = parts.message || parts.detail;
    }

    let mapped: AgentActivityState = 'idle';
    if (stateToken.includes('work') || stateToken.includes('run') || stateToken.includes('busy')) {
      mapped = 'working';
    } else if (
      stateToken.includes('block') ||
      stateToken.includes('wait') ||
      stateToken.includes('input') ||
      stateToken.includes('approv')
    ) {
      mapped = 'waiting_input';
    } else if (stateToken.includes('exit') || stateToken.includes('err')) {
      mapped = 'exited';
    } else {
      mapped = 'idle';
    }

    get().setSessionActivity(id, mapped, detail);
  },

  toggleBroadcastTarget: (id) =>
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.id === id ? { ...s, broadcastEnabled: !s.broadcastEnabled } : s,
      ),
    })),

  setAllBroadcastTargets: (enabled) =>
    set((state) => ({
      sessions: state.sessions.map((s) => ({ ...s, broadcastEnabled: enabled })),
    })),

  toggleAutoReflow: (id) =>
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.id === id ? { ...s, autoReflow: !s.autoReflow } : s,
      ),
    })),

  dismissReflowNotice: (id) =>
    set((state) => ({
      reflowReadySessionIds: state.reflowReadySessionIds.filter((sid) => sid !== id),
    })),

  registerSelectionQuote: (sessionId, text) => {
    const trimmed = text.trim();
    if (!trimmed) return null;
    const session = get().sessions.find((s) => s.id === sessionId);
    if (!session) return null;
    const lineCount = trimmed.split(/\r?\n/).length;
    const quoteId = `q-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 5)}`;
    const entry: TerminalQuoteEntry = {
      quoteId,
      sessionId,
      seq: session.seq,
      title: session.title,
      text: trimmed,
      lineCount,
    };
    set((state) => ({
      terminalQuotes: { ...state.terminalQuotes, [quoteId]: entry },
    }));
    return entry;
  },

  broadcastInput: async (text, explicitTargetIds) => {
    const trimmed = text.trim();
    if (!trimmed) return 0;

    const { sessions, activeSessionId } = get();
    let payloadText = trimmed;
    let targets: TerminalTabSession[] = [];

    // Support `@agent` or `@1` prefix routing in the unified broadcast input
    const mentionMatch = /^@([a-zA-Z0-9_-]+)\s+([\s\S]+)$/.exec(trimmed);
    if (!explicitTargetIds && mentionMatch) {
      const tag = mentionMatch[1].toLowerCase();
      payloadText = mentionMatch[2];
      const byNumber = /^\d+$/.test(tag);
      targets = sessions.filter((s) => {
        if (s.status !== 'running' || !s.ptyId) return false;
        if (byNumber) return String(s.seq) === tag;
        // An alias is presentation only; it must not add broadcast recipients.
        const launchTitle = (s.originalTitle ?? s.title).toLowerCase();
        return s.agentId.toLowerCase() === tag || launchTitle.includes(tag);
      });
    } else if (explicitTargetIds && explicitTargetIds.length > 0) {
      const idSet = new Set(explicitTargetIds);
      targets = sessions.filter((s) => idSet.has(s.id) && s.status === 'running' && s.ptyId);
    } else {
      targets = sessions.filter((s) => s.broadcastEnabled && s.status === 'running' && s.ptyId);
      if (targets.length === 0 && activeSessionId) {
        targets = sessions.filter(
          (s) => s.id === activeSessionId && s.status === 'running' && s.ptyId,
        );
      }
    }

    if (targets.length === 0) return 0;

    await Promise.all(
      targets.map(async (target) => {
        if (!target.ptyId) return;
        get().markTaskStarted(target.id);
        await writePtyPromptAndSubmit(target.ptyId, payloadText, target.isAgent);
      }),
    );

    return targets.length;
  },

  sendQuickResponse: async (id, data) => {
    const session = get().sessions.find((s) => s.id === id);
    if (!session?.ptyId || session.status !== 'running') return;
    get().setSessionActivity(id, 'working');
    await writeTerminal(session.ptyId, data);
  },

  handleSessionExit: (ptyId: number) => {
    const target = get().sessions.find((s) => s.ptyId === ptyId);
    if (!target) return;
    if (target.isAgent) {
      get().updateSessionPty(target.id, ptyId, 'exited');
    } else {
      void get().closeSession(target.id);
    }
  },
}));
