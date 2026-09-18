/**
 * The console's persisted, non-secret preferences.
 *
 * Two of them live here: the appearance (which theme the document carries, and the
 * reader's choice) and the "open with" memory (which host application the reader
 * picked for an extension).  The look itself is CSS (`src/index.css`): the theme
 * half of this module only decides *which* `data-theme` value is applied, and keeps
 * following the operating system while the reader asked for "system".
 *
 * Both are persisted in `localStorage`.  That is the deliberate exception to the
 * console's C-12 invariant, which forbids *credential* persistence: the only secret
 * this app may hold is the host's HttpOnly session cookie, and neither a theme name
 * nor an application id is one -- an application id is a label the host itself
 * published, never a path or a command.  `tests/sourceGuard.test.ts` keeps the rule
 * intact by allowing a storage API in this file and in
 * `stores/transcriptCache.ts` only.  Storage that is unavailable (private window,
 * "block all cookies") degrades to the default instead of raising -- see
 * {@link readStoredAppearance} and {@link readStoredOpenWith}.
 */
import { create } from 'zustand';

export type Appearance = 'system' | 'light' | 'dark';

/** Where the reader's choice is persisted (non-secret, per browser origin). */
export const APPEARANCE_STORAGE_KEY = 'synapse.console.appearance';

/** Where the remembered "open with" application per extension is persisted. */
export const OPEN_WITH_STORAGE_KEY = 'synapse.console.openWith';

/** How many extensions the "open with" memory keeps before the oldest is dropped. */
export const OPEN_WITH_MEMORY_LIMIT = 32;

/** Remembered application id per extension key (`'.tsx'`, or `''` for none). */
export type OpenWithMemory = Readonly<Record<string, string>>;

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

/**
 * The appearance one click of the sidebar's theme toggle switches to.
 *
 * It flips what is *on screen*, not what is stored: while the preference is still
 * "system" the click resolves the operating system first and then stores the
 * opposite as an explicit choice, so the button always changes the palette the
 * reader is looking at (and stops following the system from then on).  "Follow the
 * system" stays reachable from the settings dialog's three-way control.
 */
export function oppositeAppearance(appearance: Appearance, systemPrefersDark: boolean): Appearance {
  return themeFor(appearance, systemPrefersDark) === DARK_THEME ? 'light' : 'dark';
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
  openWith: OpenWithMemory;
  setAppearance: (appearance: Appearance) => void;
  /** Flip to the explicit opposite of the palette on screen (button / Ctrl+Shift+L). */
  toggleAppearance: () => void;
  /** Remember (or, with `null`, forget) the application for one extension. */
  rememberOpenWith: (extension: string, appId: string | null) => void;
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

/** An application id the host published: a bounded label, never a path. */
function isAppId(value: unknown): value is string {
  return typeof value === 'string' && /^[a-z0-9][a-z0-9._-]{0,63}$/.test(value);
}

/**
 * The persisted "open with" memory, or `{}`.
 *
 * Only canonical extension keys and published-looking application ids survive: a
 * stored value that does not look like either is dropped, so a hand-edited entry
 * cannot make the menu claim an application the host never offered.
 */
export function readStoredOpenWith(): OpenWithMemory {
  const store = storage();
  if (store === null) return {};
  try {
    const raw = store.getItem(OPEN_WITH_STORAGE_KEY);
    if (raw === null) return {};
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    const memory: Record<string, string> = {};
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (!/^(\.[a-z0-9]{1,16})?$/.test(key) || !isAppId(value)) continue;
      memory[key] = value;
      if (Object.keys(memory).length >= OPEN_WITH_MEMORY_LIMIT) break;
    }
    return memory;
  } catch {
    return {};
  }
}

/** Best-effort write: blocked storage must not break opening a file. */
function persistOpenWith(memory: OpenWithMemory): void {
  const store = storage();
  if (store === null) return;
  try {
    store.setItem(OPEN_WITH_STORAGE_KEY, JSON.stringify(memory));
  } catch {
    /* Quota or blocked storage: the choice still applies to this session. */
  }
}

/**
 * Whether the operating system asks for the dark palette.
 *
 * Exported because the sidebar's one-click toggle has to know which way the next
 * click goes while the preference is still "system".
 */
export function prefersDark(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false;
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

export const useAppearanceStore = create<AppearanceStore>((set, get) => ({
  appearance: 'system',
  openWith: {},
  setAppearance: (appearance) => {
    applyAppearance(themeFor(appearance, prefersDark()));
    persistAppearance(appearance);
    set({ appearance });
  },
  toggleAppearance: () =>
    get().setAppearance(oppositeAppearance(get().appearance, prefersDark())),
  rememberOpenWith: (extension, appId) => {
    set((state) => {
      const next: Record<string, string> = { ...state.openWith };
      if (appId === null) delete next[extension];
      else next[extension] = appId;
      const bounded = Object.fromEntries(
        Object.entries(next).slice(-OPEN_WITH_MEMORY_LIMIT),
      );
      persistOpenWith(bounded);
      return { openWith: bounded };
    });
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
  useAppearanceStore.setState({ appearance: stored, openWith: readStoredOpenWith() });
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    const { appearance } = useAppearanceStore.getState();
    if (appearance !== 'system') return;
    applyAppearance(themeFor(appearance, prefersDark()));
  });
}