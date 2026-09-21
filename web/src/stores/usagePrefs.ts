/**
 * The usage dashboard's remembered filter choices.
 *
 * Which project scope, range, model and heat-map metric the reader last picked is
 * a *view* preference, not telemetry: it only decides what the panel asks the host
 * for on the next open. It is persisted in `localStorage`, the deliberate
 * exception to the console's C-12 invariant (which forbids *credential*
 * persistence) -- the only secret this app may hold is the host's HttpOnly session
 * cookie, and none of these values is one. They are labels the host itself
 * published in the stats payload, never a path or a credential.
 * `tests/sourceGuard.test.ts` keeps the rule intact by allowing a storage API in
 * this file, `stores/appearance.ts` and `stores/transcriptCache.ts` only.
 *
 * Storage that is unavailable (private window, "block all cookies") degrades to
 * the defaults instead of raising: a dashboard that forgets its filters is still
 * a working dashboard, and a read must never break the panel it feeds.
 */
import type { UsageRangeKey } from '../client/usageStats.ts';

/** Where the dashboard's filter choices are persisted (non-secret, per origin). */
export const USAGE_PREFS_STORAGE_KEY = 'synapse:usage-dashboard:prefs:v1';

/** Which series the activity heat map paints. */
export type HeatMetric = 'tokens' | 'sessions' | 'loc';

/** Which dimension the breakdown donut splits by. */
export type BreakdownDim = 'project' | 'model' | 'agent';

/** Everything the dashboard remembers between openings; every field is optional. */
export interface StoredUsagePreferences {
  project?: string;
  range?: UsageRangeKey;
  customStart?: string;
  customEnd?: string;
  model?: string;
  heatMetric?: HeatMetric;
  breakdownDim?: BreakdownDim;
}

/** The remembered preferences, or `{}` when nothing usable is stored. */
export function loadStoredUsagePrefs(): StoredUsagePreferences {
  if (typeof window === 'undefined' || !window.localStorage) return {};
  try {
    const raw = localStorage.getItem(USAGE_PREFS_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return typeof parsed === 'object' && parsed !== null ? parsed : {};
  } catch {
    return {};
  }
}

/**
 * Merge `patch` into the stored preferences.
 *
 * Read-modify-write on purpose: every control saves only its own field, so the
 * panel never has to hold the whole preference set in one state object (and a
 * second dashboard opened later still sees the other control's choice). A storage
 * failure is swallowed -- losing a remembered filter is not worth an error.
 */
export function saveStoredUsagePrefs(patch: Partial<StoredUsagePreferences>): void {
  if (typeof window === 'undefined' || !window.localStorage) return;
  try {
    const existing = loadStoredUsagePrefs();
    const updated = { ...existing, ...patch };
    localStorage.setItem(USAGE_PREFS_STORAGE_KEY, JSON.stringify(updated));
  } catch {
    // Quota or security exception: the in-memory state still drives the panel.
  }
}
