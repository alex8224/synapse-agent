import type { TranscriptMessage } from './historyMapper.ts';

const PREFIX = 'synapse:transcript-view:v1:';
const MAX_BYTES = 100_000;
interface TurnView {
  work?: TranscriptMessage['work'];
  expanded?: boolean;
  folds: Record<string, boolean>;
}
type Views = Record<string, TurnView>;
const keyOf = (session: { project_id: string; thread_id: string }) =>
  PREFIX + JSON.stringify([session.project_id, session.thread_id]);

/** Per-tab view preferences only: no prompt, tool output, attachments or credentials. */
export function readTranscriptViews(session: { project_id: string; thread_id: string }): Views {
  try {
    const raw = globalThis.sessionStorage?.getItem(keyOf(session));
    if (!raw || raw.length > MAX_BYTES) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    const views: Views = Object.create(null);
    for (const [key, value] of Object.entries(parsed).slice(-100)) {
      if (!value || typeof value !== 'object') continue;
      const v = value as TurnView;
      const w = v.work;
      views[key] = { folds: Object.create(null), expanded: v.expanded === true };
      if (w && typeof w.ended === 'boolean') {
        views[key].work = {
          ended: w.ended,
          ...(typeof w.startedAt === 'number' && Number.isFinite(w.startedAt) && w.startedAt >= 0 ? { startedAt: w.startedAt } : {}),
          ...(typeof w.elapsed === 'number' && Number.isFinite(w.elapsed) && w.elapsed >= 0 ? { elapsed: w.elapsed } : {}),
        };
      }
      const folds = v.folds && typeof v.folds === 'object' ? v.folds : {};
      for (const [fold, expanded] of Object.entries(folds).slice(0, 500)) {
        if (typeof expanded === 'boolean') views[key].folds[fold] = expanded;
      }
    }
    return views;
  } catch {
    // Storage can be disabled or corrupt; server history remains authoritative.
    return {};
  }
}

function walk(messages: TranscriptMessage[], visit: (m: TranscriptMessage, key: string, fold: string) => TranscriptMessage): TranscriptMessage[] {
  const ordinals = new Map<string, number>();
  return messages.map((m) => {
    const key = m.turnId || m.id;
    const prefix = `${key}:${m.type}`;
    const ordinal = ordinals.get(prefix) ?? 0;
    ordinals.set(prefix, ordinal + 1);
    return visit(m, key, `${m.type}:${ordinal}`);
  });
}

export function restoreTranscriptViews(messages: TranscriptMessage[], views: Views): TranscriptMessage[] {
  return walk(messages, (m, key, fold) => {
    const view = views[key];
    if (!view) return m;
    const toolFolds = m.tools?.map((t) => t.callId ? view.folds[`call:${t.callId}`] : undefined)
      .filter((v) => v !== undefined) ?? [];
    const expanded = toolFolds.length ? toolFolds.some(Boolean) : view.folds[fold] ?? m.expanded;
    const workExpanded = m.work ? view.expanded === true : m.workExpanded;
    // Only restore a missing start; server settlement and live terminal metadata
    // always win. An old cached running clock can never reopen a finished turn.
    const work = m.work && m.work.startedAt === undefined && view.work?.startedAt !== undefined
      ? { ...view.work, ...m.work, startedAt: view.work.startedAt } : m.work;
    if (expanded === m.expanded && workExpanded === m.workExpanded && work === m.work) return m;
    return { ...m, expanded, workExpanded, ...(work ? { work } : {}) };
  });
}

export function saveTranscriptViews(session: { project_id: string; thread_id: string }, messages: TranscriptMessage[]): void {
  if (!session.project_id || !session.thread_id || messages.length === 0) return;
  try {
    const views = readTranscriptViews(session);
    walk(messages, (m, key, fold) => {
      const view = views[key] ?? { folds: Object.create(null) };
      if (m.work) { view.work = m.work; view.expanded = m.workExpanded === true; }
      if (m.expanded !== undefined) {
        view.folds[fold] = m.expanded;
        for (const tool of m.tools ?? []) {
          if (tool.callId) view.folds[`call:${tool.callId}`] = m.expanded;
        }
      }
      view.folds = Object.fromEntries(Object.entries(view.folds).slice(-500));
      views[key] = view;
      return m;
    });
    const entries = Object.entries(views).slice(-100);
    let raw = JSON.stringify(Object.fromEntries(entries));
    while (raw.length > MAX_BYTES && entries.length) {
      entries.shift(); raw = JSON.stringify(Object.fromEntries(entries));
    }
    const storage = globalThis.sessionStorage;
    if (storage && storage.getItem(keyOf(session)) !== raw) {
      storage.setItem(keyOf(session), raw);
    }
  } catch {
    // Quota/private-mode failures must not interrupt a turn or its rendering.
  }
}

export function clearTranscriptViews(): void {
  try {
    const storage = globalThis.sessionStorage;
    if (!storage) return;
    for (let i = storage.length - 1; i >= 0; i--) {
      const key = storage.key(i);
      if (key?.startsWith(PREFIX)) storage.removeItem(key);
    }
  } catch { /* Best-effort logout cleanup when browser storage is unavailable. */ }
}
