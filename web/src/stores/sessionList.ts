/**
 * Pure helpers for the sidebar session list: relative-time bucketing and search.
 *
 * Kept free of React / zustand so they are exercised directly with the Node
 * built-in test runner, the same way `historyMapper` / `recoveryDecider` are.
 */
import type { SessionItem } from './historyMapper.ts';

export type SessionGroupKey = 'today' | 'yesterday' | 'last7' | 'last30' | 'older';

export interface SessionGroup {
  key: SessionGroupKey;
  label: string;
  items: SessionItem[];
}

/** Display order of the buckets (newest first). */
export const SESSION_GROUP_ORDER: readonly SessionGroupKey[] = [
  'today',
  'yesterday',
  'last7',
  'last30',
  'older',
];

export const SESSION_GROUP_LABELS: Record<SessionGroupKey, string> = {
  today: '今天',
  yesterday: '昨天',
  last7: '过去 7 天',
  last30: '过去 30 天',
  older: '更早',
};

const DAY_MS = 24 * 60 * 60 * 1000;

function startOfLocalDay(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/**
 * Bucket key for one timestamp, relative to `now` in local time.
 *
 * An unparseable timestamp is reported as `older` rather than dropped, so a
 * malformed row stays visible instead of silently disappearing.
 */
export function sessionGroupKey(updatedAt: string, now: Date = new Date()): SessionGroupKey {
  const at = new Date(updatedAt).getTime();
  if (!Number.isFinite(at)) return 'older';
  const today = startOfLocalDay(now);
  if (at >= today) return 'today';
  if (at >= today - DAY_MS) return 'yesterday';
  if (at >= today - 7 * DAY_MS) return 'last7';
  if (at >= today - 30 * DAY_MS) return 'last30';
  return 'older';
}

/**
 * Group sessions by relative update time, newest bucket first.
 *
 * Empty buckets are omitted so the sidebar never renders a bare heading, and
 * the input order is preserved inside each bucket.
 */
export function groupSessionsByTime(items: SessionItem[], now: Date = new Date()): SessionGroup[] {
  const buckets = new Map<SessionGroupKey, SessionItem[]>();
  for (const item of items) {
    const key = sessionGroupKey(item.updated_at, now);
    const bucket = buckets.get(key);
    if (bucket === undefined) buckets.set(key, [item]);
    else bucket.push(item);
  }
  return SESSION_GROUP_ORDER.filter((key) => buckets.has(key)).map((key) => ({
    key,
    label: SESSION_GROUP_LABELS[key],
    items: buckets.get(key) ?? [],
  }));
}

/**
 * Case-insensitive substring search over the sessions loaded so far.
 *
 * An empty query returns the input untouched.  Matching covers the visible
 * title and the thread id, so a pasted session id still finds its row.
 */
export function filterSessions(items: SessionItem[], query: string): SessionItem[] {
  const needle = query.trim().toLowerCase();
  if (needle === '') return items;
  return items.filter(
    (item) =>
      item.title.toLowerCase().includes(needle) || item.thread_id.toLowerCase().includes(needle),
  );
}

/** Minimal project shape the sidebar tree needs (kept structural for tests). */
export interface ProjectLabelSource {
  workspace_path: string;
  workspace_name: string | null;
}

/**
 * Level-1 label of the tree: the last path segment of the workspace, mirroring
 * the TUI drawer's `_dir_label`.  Falls back to the registered name, then to the
 * raw path, so a row is never blank.
 */
export function projectLabel(entry: ProjectLabelSource): string {
  const trimmed = entry.workspace_path.replace(/[\\/]+$/, '');
  const last = trimmed.split(/[\\/]/).pop() ?? '';
  return last || entry.workspace_name || entry.workspace_path;
}

/** True when the project itself matches the search text (name or path). */
export function matchesProject(entry: ProjectLabelSource, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (needle === '') return true;
  return (
    entry.workspace_path.toLowerCase().includes(needle) ||
    (entry.workspace_name ?? '').toLowerCase().includes(needle) ||
    projectLabel(entry).toLowerCase().includes(needle)
  );
}
