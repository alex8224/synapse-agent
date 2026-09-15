/**
 * Console appearance: which theme the document carries, and the reader's choice.
 *
 * The look itself is CSS (`src/index.css`): this module only decides *which*
 * `data-theme` value is applied, and keeps following the operating system while
 * the reader asked for "system".
 *
 * The choice is persisted in `localStorage`.  That is the one deliberate
 * exception to the console's C-12 invariant, which forbids *credential*
 * persistence: the only secret this app may hold is the host's HttpOnly session
 * cookie, and a theme name is not one.  `tests/sourceGuard.test.ts` keeps the
 * rule intact by allowing a storage API in this file and in
 * `stores/transcriptCache.ts` only.  Storage that is unavailable (private
 * window, "block all cookies") degrades to "follow the system" instead of
 * raising -- see {@link readStoredAppearance}.
 */
import { create } from 'zustand';

export type Appearance = 'system' | 'light' | 'dark';

/** Where the reader's choice is persisted (non-secret, per browser origin). */
export const APPEARANCE_STORAGE_KEY = 'synapse.console.appearance';

/** Both appearances use the same Fluent component language. */
export const LIGHT_THEME = 'fluent-light';
export const DARK_THEME = 'fluent-dark';

export const APPEARANCE_OPTIONS: readonly { value: Appearance; label: string }[] = [
  { value: 'system', label: '跟随系统' },
  { value: 'light', label: '浅色' },
  { value: 'dark', label: '深色' },
];

/** Resolve an explicit appearance, or follow the operating system. */
export function themeFor(appearance: Appearance, prefersDark: boolean): string {
  if (appearance === 'dark') return DARK_THEME;
  if (appearance === 'light') return LIGHT_THEME;
  return prefersDark ? DARK_THEME : LIGHT_THEME;
}

/** Apply one theme id to the document root (`null` restores the shipped palette). */
export function applyTheme(root: { dataset: DOMStringMap } | null, theme: string | null): void {
  if (root === null) return;
  if (theme === null) delete root.dataset.theme;
  else root.dataset.theme = theme;
}

/**
 * The window frame color for each theme.
 *
 * Chromium paints the installed window's title bar -- and, under the window
 * controls overlay, the strip carrying the window buttons and the origin chip --
 * with the manifest's `theme_color`.  A manifest is a static file, so that value
 * cannot follow the reader's theme; the `<meta name="theme-color">` tag is what
 * does, and this module rewrites it whenever the theme changes.  The shipped tag
 * (and the manifest) carry the light value for the first paint.
 */
export const FRAME_COLOR_LIGHT = '#F8F9FA';
export const FRAME_COLOR_DARK = '#1F1F1F';

/** The frame color a theme asks for (`null` is the shipped light palette). */
export function frameColorFor(theme: string | null): string {
  return theme === DARK_THEME ? FRAME_COLOR_DARK : FRAME_COLOR_LIGHT;
}

/** The document surface {@link applyFrameColor} needs; a `Document` satisfies it. */
export interface FrameColorTarget {
  querySelector(selector: string): { setAttribute(name: string, value: string): void } | null;
}

function documentTarget(): FrameColorTarget | null {
  return typeof document === 'undefined' ? null : document;
}

/**
 * Point the browser's window chrome at the active theme.
 *
 * A document without the tag is left alone: the console ships the tag in
 * `index.html`, and a document that lost it keeps the manifest's static color
 * instead of raising.
 */
export function applyFrameColor(
  theme: string | null,
  target: FrameColorTarget | null = documentTarget(),
): void {
  const meta = target?.querySelector('meta[name="theme-color"]') ?? null;
  if (meta === null) return;
  meta.setAttribute('content', frameColorFor(theme));
}

/** Apply one theme to the document: the palette *and* the window frame color. */
function applyAppearance(theme: string | null): void {
  applyTheme(root(), theme);
  applyFrameColor(theme);
}

interface AppearanceStore {
  appearance: Appearance;
  setAppearance: (appearance: Appearance) => void;
}

function isAppearance(value: unknown): value is Appearance {
  return value === 'system' || value === 'light' || value === 'dark';
}

/**
 * The browser's storage, or `null` when it is not usable.
 *
 * A private window or a "block all cookies" setting makes the *property access*
 * itself throw, so the lookup is guarded rather than assumed.
 */
function storage(): Pick<Storage, 'getItem' | 'setItem'> | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

/**
 * The persisted choice, or `system`.
 *
 * A missing, unknown or unreadable value is never an error: the console simply
 * starts from "follow the system" as it did before the preference was stored.
 */
export function readStoredAppearance(): Appearance {
  const store = storage();
  if (store === null) return 'system';
  try {
    const raw = store.getItem(APPEARANCE_STORAGE_KEY);
    return isAppearance(raw) ? raw : 'system';
  } catch {
    return 'system';
  }
}

/** Best-effort write: blocked storage must not break the theme switch. */
function persistAppearance(appearance: Appearance): void {
  const store = storage();
  if (store === null) return;
  try {
    store.setItem(APPEARANCE_STORAGE_KEY, appearance);
  } catch {
    /* Quota or blocked storage: the choice still applies to this session. */
  }
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
    applyAppearance(themeFor(appearance, prefersDark()));
    persistAppearance(appearance);
    set({ appearance });
  },
}));

/**
 * Apply the reader's stored choice and keep following the system while it is
 * still `system`.
 *
 * Called from `main.tsx` *before* the first render, so the console never paints
 * one palette for a frame and then swaps -- including on a reload, which now
 * restores the persisted choice instead of falling back to the system.  The
 * listener only acts while the preference is still "system": an explicit
 * light/dark choice wins.
 */
export function initAppearance(): void {
  const stored = readStoredAppearance();
  applyAppearance(themeFor(stored, prefersDark()));
  useAppearanceStore.setState({ appearance: stored });
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    const { appearance } = useAppearanceStore.getState();
    if (appearance !== 'system') return;
    applyAppearance(themeFor(appearance, prefersDark()));
  });
}