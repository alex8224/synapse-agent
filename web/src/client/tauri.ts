/** Helper for detecting and communicating with the Tauri GUI wrapper. */

export function isTauri(): boolean {
  if (typeof window === 'undefined') return false;
  const w = window as unknown as { __TAURI_INTERNALS__?: unknown; __TAURI__?: unknown };
  const detected = Boolean(
    w.__TAURI_INTERNALS__ ||
      w.__TAURI__ ||
      (typeof window.location !== 'undefined' && window.location.search.includes('tauri=1'))
  );
  if (detected && typeof document !== 'undefined') {
    document.documentElement.dataset.tauri = 'true';
  }
  return detected;
}

export async function tauriMinimize(): Promise<void> {
  const internals = (window as unknown as {
    __TAURI_INTERNALS__?: { invoke?: (cmd: string, args?: unknown) => Promise<void> };
  }).__TAURI_INTERNALS__;
  if (internals?.invoke) {
    try {
      await internals.invoke('plugin:window|minimize');
    } catch {
      await internals.invoke('minimize_window');
    }
  }
}

export async function tauriOpenPath(path: string): Promise<boolean> {
  const internals = (window as unknown as {
    __TAURI_INTERNALS__?: { invoke?: (cmd: string, args?: unknown) => Promise<void> };
  }).__TAURI_INTERNALS__;
  if (internals?.invoke) {
    try {
      await internals.invoke('open_path', { path });
      return true;
    } catch {}
  }
  return false;
}

export async function tauriToggleMaximize(): Promise<void> {
  const internals = (window as unknown as {
    __TAURI_INTERNALS__?: { invoke?: (cmd: string, args?: unknown) => Promise<void> };
  }).__TAURI_INTERNALS__;
  if (internals?.invoke) {
    try {
      await internals.invoke('plugin:window|toggle_maximize');
    } catch {
      await internals.invoke('toggle_maximize_window');
    }
  }
}

export async function tauriSetWindowTheme(appearance: 'system' | 'light' | 'dark'): Promise<void> {
  const internals = (window as unknown as {
    __TAURI_INTERNALS__?: { invoke?: (cmd: string, args?: unknown) => Promise<void> };
  }).__TAURI_INTERNALS__;
  if (internals?.invoke) {
    try {
      const dark = appearance === 'system' ? null : appearance === 'dark';
      await internals.invoke('tauri_set_window_theme', { dark });
    } catch {}
  }
}

export async function tauriClose(): Promise<void> {
  const internals = (window as unknown as {
    __TAURI_INTERNALS__?: { invoke?: (cmd: string, args?: unknown) => Promise<void> };
  }).__TAURI_INTERNALS__;
  if (internals?.invoke) {
    try {
      await internals.invoke('close_window');
    } catch {
      await internals.invoke('plugin:window|hide');
    }
  }
}
