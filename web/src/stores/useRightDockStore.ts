/**
 * Store for the right auxiliary dock's runtime state and layout preferences.
 *
 * Implements pure in-memory state conforming strictly to C-12 invariants
 * (no non-exempt browser storage persistence in src/stores).
 */
import { create } from 'zustand';
import {
  clampDockWidth,
  DEFAULT_DOCK_WIDTH,
} from '../components/rightDock/contract.ts';

export interface RightDockStore {
  /** Whether the dock is currently open. */
  open: boolean;
  /** Width in pixels when open. */
  width: number;
  /** ID of the active tab. */
  activeTabId: string;
  /** Whether the files tab filters exclusively to changed files (ZCode feature). */
  onlyChangedFiles: boolean;
  /** Search query in the files tab. */
  fileSearchQuery: string;

  // Actions
  toggleOpen: (force?: boolean) => void;
  setOpen: (open: boolean) => void;
  setWidth: (width: number) => void;
  resetWidth: () => void;
  setActiveTab: (tabId: string) => void;
  setOnlyChangedFiles: (flag: boolean) => void;
  toggleOnlyChangedFiles: () => void;
  setFileSearchQuery: (query: string) => void;
}

export const useRightDockStore = create<RightDockStore>((set, get) => ({
  open: false,
  width: DEFAULT_DOCK_WIDTH,
  activeTabId: 'files',
  onlyChangedFiles: false,
  fileSearchQuery: '',

  toggleOpen: (force?: boolean) => {
    const next = force !== undefined ? force : !get().open;
    set({ open: next });
  },

  setOpen: (open: boolean) => {
    set({ open });
  },

  setWidth: (rawWidth: number) => {
    const bounded = clampDockWidth(rawWidth);
    set({ width: bounded });
  },

  resetWidth: () => {
    set({ width: DEFAULT_DOCK_WIDTH });
  },

  setActiveTab: (activeTabId: string) => {
    set({ activeTabId });
  },

  setOnlyChangedFiles: (onlyChangedFiles: boolean) => {
    set({ onlyChangedFiles });
  },

  toggleOnlyChangedFiles: () => {
    set((state) => ({ onlyChangedFiles: !state.onlyChangedFiles }));
  },

  setFileSearchQuery: (fileSearchQuery: string) => {
    set({ fileSearchQuery });
  },
}));
