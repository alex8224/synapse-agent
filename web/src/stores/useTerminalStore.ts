/**
 * Terminal State Management Store.
 *
 * Implements:
 * - Central bottom terminal dock visibility, height, maximization, and split state
 * - Multi-session tab lifecycle (create, switch, split, close)
 * - Automatic binding with active project workspace
 */
import { create } from 'zustand';
import { createTerminal, closeTerminal } from '../client/tauriTerminal.ts';

export interface TerminalTabSession {
  id: string;
  ptyId: number | null;
  title: string;
  shell: string;
  workspace?: string;
  status: 'connecting' | 'running' | 'exited' | 'error';
  errorMessage?: string;
}

interface TerminalStoreState {
  open: boolean;
  height: number;
  isMaximized: boolean;
  isSplit: boolean;
  activeSessionId: string | null;
  splitSessionId: string | null;
  sessions: TerminalTabSession[];

  // Actions
  setOpen: (open: boolean) => void;
  toggleOpen: () => void;
  setHeight: (height: number) => void;
  toggleMaximize: () => void;
  toggleSplit: () => void;
  setActiveSession: (id: string) => void;
  setSplitSession: (id: string | null) => void;
  createSession: (workspace?: string, shell?: string) => Promise<string>;
  closeSession: (id: string) => Promise<void>;
  updateSessionPty: (id: string, ptyId: number, status: 'running' | 'exited' | 'error', errorMessage?: string) => void;
}

const DEFAULT_HEIGHT = 300;
const MIN_HEIGHT = 140;

export const useTerminalStore = create<TerminalStoreState>((set, get) => ({
  open: false,
  height: DEFAULT_HEIGHT,
  isMaximized: false,
  isSplit: false,
  activeSessionId: null,
  splitSessionId: null,
  sessions: [],

  setOpen: (open) => set({ open }),

  toggleOpen: () => set((state) => ({ open: !state.open })),

  setHeight: (rawHeight) => {
    const maxHeight = typeof window !== 'undefined' ? window.innerHeight - 100 : 800;
    const clamped = Math.max(MIN_HEIGHT, Math.min(rawHeight, maxHeight));
    set({ height: clamped, isMaximized: false });
  },

  toggleMaximize: () => set((state) => ({ isMaximized: !state.isMaximized })),

  toggleSplit: () => {
    const { isSplit, sessions, activeSessionId } = get();
    if (!isSplit) {
      // If opening split and we have another session, select it for split, otherwise create one
      const remaining = sessions.filter((s) => s.id !== activeSessionId);
      if (remaining.length > 0) {
        set({ isSplit: true, splitSessionId: remaining[0].id });
      } else {
        set({ isSplit: true, splitSessionId: null });
        void get().createSession();
      }
    } else {
      set({ isSplit: false, splitSessionId: null });
    }
  },

  setActiveSession: (id) => set({ activeSessionId: id }),

  setSplitSession: (id) => set({ splitSessionId: id }),

  createSession: async (workspace, shell) => {
    const id = `term-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
    const count = get().sessions.length + 1;
    const title = `${count}: ${shell || 'pwsh'}`;

    const newSession: TerminalTabSession = {
      id,
      ptyId: null,
      title,
      shell: shell || 'pwsh',
      workspace,
      status: 'connecting',
    };

    set((state) => {
      const nextSessions = [...state.sessions, newSession];
      const activeId = state.activeSessionId ? state.activeSessionId : id;
      const splitId = state.isSplit && !state.splitSessionId ? id : state.splitSessionId;
      return {
        sessions: nextSessions,
        activeSessionId: activeId,
        splitSessionId: splitId,
      };
    });

    try {
      const ptyId = await createTerminal(workspace, shell);
      get().updateSessionPty(id, ptyId, 'running');
      return id;
    } catch (err) {
      get().updateSessionPty(id, 0, 'error', String(err));
      return id;
    }
  },

  closeSession: async (id) => {
    const { sessions, activeSessionId, splitSessionId } = get();
    const session = sessions.find((s) => s.id === id);
    if (session?.ptyId) {
      try {
        await closeTerminal(session.ptyId);
      } catch {}
    }

    const nextSessions = sessions.filter((s) => s.id !== id);
    let nextActive = activeSessionId === id ? (nextSessions[0]?.id ?? null) : activeSessionId;
    let nextSplit = splitSessionId === id ? null : splitSessionId;

    if (nextSessions.length === 0) {
      set({
        sessions: [],
        activeSessionId: null,
        splitSessionId: null,
        open: false,
      });
    } else {
      set({
        sessions: nextSessions,
        activeSessionId: nextActive,
        splitSessionId: nextSplit,
        isSplit: nextSplit !== null,
      });
    }
  },

  updateSessionPty: (id, ptyId, status, errorMessage) => {
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.id === id ? { ...s, ptyId, status, errorMessage } : s,
      ),
    }));
  },
}));
