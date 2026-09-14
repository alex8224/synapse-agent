/**
 * Console appearance: which theme the document carries, and the reader's choice.
 *
 * The look itself is CSS (`src/index.css`): this module only decides *which*
 * `data-theme` value is applied, and keeps following the operating system while
 * the reader asked for "system".
 *
 * Nothing is persisted, on purpose: the console's C-12 invariant is that the
 * frontend keeps no browser storage at all (`tests/sourceGuard.test.ts` enforces
 * it), because the only state this app is allowed to hold is the host's HttpOnly
 * session cookie.  A reload therefore starts from "follow the system" again.  If
 * that invariant is ever relaxed for UI-only preferences, the seam is
 * `useAppearanceStore.setAppearance` / `initAppearance` and nothing else.
 */
import { create } from 'zustand';

export type Appearance = 'system' | 'light' | 'dark';

/** The theme applied for a dark appearance (`null` = the palette shipped with). */
export const DARK_THEME = 'fluent-dark';

export const APPEARANCE_OPTIONS: readonly { value: Appearance; label: string }[] = [
  { value: 'system', label: '跟随系统' },
  { value: 'light', label: '浅色' },
  { value: 'dark', label: '深色' },
];

/**
 * Theme id for one preference: `null` means the shipped palette, so a reader who
 * never touches the control sees exactly what the console always looked like.
 */
export function themeFor(appearance: Appearance, prefersDark: boolean): string | null {
  if (appearance === 'dark') return DARK_THEME;
  if (appearance === 'light') return null;
  return prefersDark ? DARK_THEME : null;
}

/** Apply one theme id to the document root (`null` restores the shipped palette). */
export function applyTheme(root: { dataset: DOMStringMap } | null, theme: string | null): void {
  if (root === null) return;
  if (theme === null) delete root.dataset.theme;
  else root.dataset.theme = theme;
}

interface AppearanceStore {
  appearance: Appearance;
  setAppearance: (appearance: Appearance) => void;
}

function root(): { dataset: DOMStringMap } | null {
  return typeof document === 'undefined' ? null : document.documentElement;
}

function prefersDark(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false;
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

export const useAppearanceStore = create<AppearanceStore>((set) => ({
  appearance: 'system',
  setAppearance: (appearance) => {
    applyTheme(root(), themeFor(appearance, prefersDark()));
    set({ appearance });
  },
}));

/**
 * Apply the reader's default (follow the system) and keep following it.
 *
 * Called from `main.tsx` *before* the first render, so the console never paints
 * the light palette for a frame and then swaps.  The listener only acts while the
 * preference is still "system": an explicit light/dark choice wins.
 */
export function initAppearance(): void {
  applyTheme(root(), themeFor('system', prefersDark()));
  useAppearanceStore.setState({ appearance: 'system' });
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    const { appearance } = useAppearanceStore.getState();
    if (appearance !== 'system') return;
    applyTheme(root(), themeFor(appearance, prefersDark()));
  });
}