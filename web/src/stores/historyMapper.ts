/**
 * Pure mappers between the Agent Runtime RPC DTOs (`runtime.session.list`,
 * `runtime.session.history`) and the console transcript/session UI model.
 *
 * These functions are deliberately free of zustand / React / WebSocket
 * dependencies so they can be exercised directly with the Node built-in test
 * runner (`node --test`) and reused from `useConsoleStore`.
 *
 * The transcript projection is a light-weight per-turn projection, NOT a full
 * checkpoint snapshot: it carries no LangChain messages, no exact event
 * sequences, and no turn ids. Mapping here must never pretend otherwise.
 */

import { HISTORY_PAGE_SIZE } from '../client/types.ts';
import type { HistoryEvent, SessionMetadataItem } from '../client/types.ts';

export interface TranscriptMessage {
  id: string;
  type: 'user' | 'thought' | 'tool_group' | 'assistant';
  timestamp: string;
  content?: string;
  duration?: string;
  tools?: Array<{ name: string; icon: string; duration?: string }>;
  expanded?: boolean;
}

export interface SessionItem {
  thread_id: string;
  title: string;
  updated_at: string;
  time_label: string;
}

export interface SessionListView {
  items: SessionItem[];
  next_offset: number | null;
  total: number;
}

/** Latest-page history request used on attach / refresh. */
export function latestHistoryParams(session: {
  project_id: string;
  thread_id: string;
}): { session: { project_id: string; thread_id: string }; before_turn: null; limit: number } {
  return { session, before_turn: null, limit: HISTORY_PAGE_SIZE };
}

/** Earlier-page history request; `beforeTurn` is the oldest loaded turn. */
export function earlierHistoryParams(
  session: { project_id: string; thread_id: string },
  beforeTurn: number,
): { session: { project_id: string; thread_id: string }; before_turn: number; limit: number } {
  return { session, before_turn: beforeTurn, limit: HISTORY_PAGE_SIZE };
}

/** Compact timestamp label (`MM-DD HH:MM`), matching the legacy list mapping. */
export function timeLabelFromIso(iso?: string | null): string {
  return iso ? iso.slice(5, 16).replace('T', ' ') : '';
}

export function toSessionItem(item: SessionMetadataItem): SessionItem {
  return {
    thread_id: item.thread_id,
    title: item.title || item.thread_id,
    updated_at: item.updated_at,
    time_label: timeLabelFromIso(item.updated_at),
  };
}

export function toSessionListView(page: {
  items: SessionMetadataItem[];
  next_offset: number | null;
  total: number;
}): SessionListView {
  return {
    items: page.items.map(toSessionItem),
    next_offset: page.next_offset,
    total: page.total,
  };
}

/**
 * Map one page of projection `HistoryEvent`s to transcript messages.
 *
 * Events are already ordered oldest -> newest within the page and each turn
 * starts with a `user` event, so a running per-turn counter can render the
 * same `Turn N` labels the previous REST transcript UI used. `user` /
 * `answer` / `thought` map to text messages, `tools` to a tool chip group;
 * `meta` and empty payloads are skipped (they have no visible representation).
 *
 * `pageTag` must be unique per loaded page (e.g. derived from `start_turn`)
 * so earlier pages prepended to the transcript never collide on React keys.
 */
export function mapHistoryEvents(
  events: HistoryEvent[],
  opts: { startTurn: number; pageTag: string },
): TranscriptMessage[] {
  const { startTurn, pageTag } = opts;
  const out: TranscriptMessage[] = [];
  // The first `user` event of the page opens `startTurn`; every later `user`
  // starts the next turn, and non-user events keep the latest turn label.
  let turn = Math.max(0, startTurn - 1);
  let ordinal = 0;
  for (const ev of events) {
    const tag = `${pageTag}-${ordinal}`;
    ordinal += 1;
    if (ev.kind === 'user') {
      turn += 1;
      const content = (ev.text || '').trim();
      if (content) {
        out.push({ id: `hist-u-${tag}`, type: 'user', timestamp: `Turn ${turn}`, content });
      }
    } else if (ev.kind === 'thought') {
      const content = ev.text || '';
      if (content.trim() && turn >= startTurn) {
        out.push({
          id: `hist-t-${tag}`,
          type: 'thought',
          timestamp: `Turn ${turn}`,
          content,
          expanded: false,
        });
      }
    } else if (ev.kind === 'answer') {
      const content = ev.text || '';
      if (content.trim() && turn >= startTurn) {
        out.push({
          id: `hist-a-${tag}`,
          type: 'assistant',
          timestamp: `Turn ${turn}`,
          content,
        });
      }
    } else if (ev.kind === 'tools') {
      const tools: Array<{ name: string; icon: string; duration?: string }> = [];
      for (const call of ev.tool_calls) {
        tools.push({ name: (call && call.name) || 'tool', icon: 'build', duration: 'done' });
      }
      if (tools.length === 0) {
        // Projections may carry results without calls (e.g. very old rows).
        for (const result of ev.tool_results) {
          tools.push({ name: (result && result.name) || 'tool', icon: 'build', duration: 'done' });
        }
      }
      if (tools.length > 0 && turn >= startTurn) {
        out.push({ id: `hist-x-${tag}`, type: 'tool_group', timestamp: `Turn ${turn}`, tools });
      }
    }
    // `meta` and unknown kinds have no visible transcript representation.
  }
  return out;
}
