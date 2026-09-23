// Only a catalog project id is persisted. Never store pairing or runtime credentials.
const LAST_PROJECT_KEY = 'synapse.console.lastProjectId';

export function readLastProjectId(): string | null {
  try {
    return globalThis.localStorage?.getItem(LAST_PROJECT_KEY) ?? null;
  } catch {
    return null; // Storage may be disabled (or absent in Node tests).
  }
}

export function saveLastProjectId(projectId: string): void {
  try {
    globalThis.localStorage?.setItem(LAST_PROJECT_KEY, projectId);
  } catch {
    // Persistence is optional; switching projects must still work without it.
  }
}
