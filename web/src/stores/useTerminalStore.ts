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
  activeSessionId: string | null;
  sessions: TerminalTabSession[];

  // Actions
  setOpen: (open: boolean) => void;
  toggleOpen: () => void;
  setHeight: (height: number) => void;
  toggleMaximize: () => void;
  setActiveSession: (id: string) => void;
  createSession: (workspace?: string, shell?: string) => Promise<string>;
  closeSession: (id: string) => Promise<void>;
  updateSessionPty: (id: string, ptyId: number, status: 'running' | 'exited' | 'error', errorMessage?: string) => void;
  handleSessionExit: (ptyId: number) => void;
}

const DEFAULT_HEIGHT = 300;
const MIN_HEIGHT = 140;

export const useTerminalStore = create<TerminalStoreState>((set, get) => ({
  open: false,
  height: DEFAULT_HEIGHT,
  isMaximized: false,
  activeSessionId: null,
  sessions: [],

  setOpen: (open) => set({ open }),

  toggleOpen: () => set((state) => ({ open: !state.open })),

  setHeight: (rawHeight) => {
    const maxHeight = typeof window !== 'undefined' ? window.innerHeight - 100 : 800;
    const clamped = Math.max(MIN_HEIGHT, Math.min(rawHeight, maxHeight));
    set({ height: clamped, isMaximized: false });
  },

  toggleMaximize: () => set((state) => ({ isMaximized: !state.isMaximized })),

  setActiveSession: (id) => set({ activeSessionId: id }),
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
      return {
        sessions: nextSessions,
        activeSessionId: id,
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
    const { sessions, activeSessionId } = get();
    const session = sessions.find((s) => s.id === id);
    if (session?.ptyId) {
      try {
        await closeTerminal(session.ptyId);
      } catch {}
    }

    const nextSessions = sessions.filter((s) => s.id !== id);
    let nextActive = activeSessionId === id ? (nextSessions[0]?.id ?? null) : activeSessionId;

    if (nextSessions.length === 0) {
      set({
        sessions: [],
        activeSessionId: null,
        open: false,
      });
    } else {
      set({
        sessions: nextSessions,
        activeSessionId: nextActive,
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

  handleSessionExit: (ptyId: number) => {
    const target = get().sessions.find((s) => s.ptyId === ptyId);
    if (target) {
      void get().closeSession(target.id);
    }
  },
}));
