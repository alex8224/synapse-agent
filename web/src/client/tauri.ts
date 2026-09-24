/** Helper for detecting and communicating with the Tauri GUI wrapper. */

export function isTauri(): boolean {
  return (
    typeof window !== 'undefined' &&
    Boolean((window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__)
  );
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
