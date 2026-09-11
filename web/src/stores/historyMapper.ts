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

/**
 * One tool invocation as rendered inside a transcript tool group.
 *
 * Mirrors the runtime `ToolItemPayload` (plus the transient subagent stage from
 * `subagent_status_changed`).  `preview` is the bounded preview the runtime
 * sends; full tool output is not part of the event stream.
 */
export interface ToolItemView {
  /** Stable per-turn item id (`ToolItemPayload.item_id`). */
  id: string;
  /** Provider tool-call id, used to correlate legacy `tool_result` events. */
  callId: string | null;
  name: string;
  label: string;
  category: string;
  path: string | null;
  /** Runtime-defined lifecycle status (`running` / `completed` / `failed` / ...). */
  status: string;
  preview: string | null;
  error: boolean;
  /** True for nested subagent tool items. */
  sub: boolean;
  parentId: string | null;
  /** Transient subagent stage; cleared when the parent finishes. */
  subagentStatus: string | null;
  subagentName: string | null;
  /** Material icon name (presentation only). */
  icon: string;
  duration?: string;
}

export interface TranscriptMessage {
  id: string;
  type: 'user' | 'thought' | 'tool_group' | 'assistant' | 'info';
  timestamp: string;
  content?: string;
  duration?: string;
  tools?: ToolItemView[];
  expanded?: boolean;
  /** Tool group metadata (live events only; the history projection has no group). */
  groupId?: string | null;
  parallel?: boolean;
  /** True once `tool_batch_finished` closed the group. */
  finished?: boolean;
  /** Epoch ms a streaming thought started (live only, used for its duration). */
  startedAt?: number;
  /** Severity of an `info` row. */
  infoLevel?: 'info' | 'warning';
}

/** Minimal tool item for a projected history row (no live status/preview). */
export function historyToolItem(id: string, name: string): ToolItemView {
  return {
    id,
    callId: null,
    name,
    label: name,
    category: 'other',
    path: null,
    status: 'completed',
    preview: null,
    error: false,
    sub: false,
    parentId: null,
    subagentStatus: null,
    subagentName: null,
    icon: 'build',
    duration: 'done',
  };
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

/**
 * Page sizes tried newest-first when the daemon rejects a page as too large.
 *
 * The runtime caps one history page at 256 KiB and answers `history_too_large`;
 * content-rich sessions exceed that even at the default size, so the console
 * walks down to a single turn instead of rendering nothing.
 */
export const HISTORY_PAGE_SIZES: readonly number[] = [HISTORY_PAGE_SIZE, 10, 5, 2, 1];

/** Latest-page history request used on attach / refresh. */
export function latestHistoryParams(
  session: {
    project_id: string;
    thread_id: string;
  },
  limit: number = HISTORY_PAGE_SIZE,
): { session: { project_id: string; thread_id: string }; before_turn: null; limit: number } {
  return { session, before_turn: null, limit };
}

/** Earlier-page history request; `beforeTurn` is the oldest loaded turn. */
export function earlierHistoryParams(
  session: { project_id: string; thread_id: string },
  beforeTurn: number,
  limit: number = HISTORY_PAGE_SIZE,
): { session: { project_id: string; thread_id: string }; before_turn: number; limit: number } {
  return { session, before_turn: beforeTurn, limit };
}

/** The `service_code` a runtime error carries, when it has one. */
export function serviceCodeOf(error: unknown): string | null {
  if (typeof error !== 'object' || error === null) return null;
  const code = (error as { service_code?: unknown }).service_code;
  return typeof code === 'string' ? code : null;
}

/** True when the runtime refused a page because it is too large to deliver. */
export function isHistoryTooLarge(error: unknown): boolean {
  return serviceCodeOf(error) === 'history_too_large';
}

/**
 * Read one history page, shrinking the page size while the runtime reports
 * `history_too_large`.
 *
 * Any other failure propagates immediately (it is not a size problem), and the
 * last size rejection is re-thrown when even the smallest page is refused.
 */
export async function readHistoryPage<T>(
  read: (limit: number) => Promise<T>,
  sizes: readonly number[] = HISTORY_PAGE_SIZES,
): Promise<T> {
  let lastError: unknown = new Error('no history page size configured');
  for (const limit of sizes) {
    try {
      return await read(limit);
    } catch (error) {
      if (!isHistoryTooLarge(error)) throw error;
      lastError = error;
    }
  }
  throw lastError;
}

/** User-facing copy for a failed history read (never a stack or a credential). */
export function describeHistoryFailure(error: unknown): string {
  const code = serviceCodeOf(error);
  if (code === 'history_too_large') {
    return '该会话的历史页超过服务端单页上限（history_too_large），按最小页重试仍失败。';
  }
  if (code !== null) return `历史加载失败（${code}）。`;
  const message = error instanceof Error ? error.message : String(error);
  return `历史加载失败：${message}`;
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
      const names: string[] = [];
      for (const call of ev.tool_calls) {
        names.push((call && call.name) || 'tool');
      }
      if (names.length === 0) {
        // Projections may carry results without calls (e.g. very old rows).
        for (const result of ev.tool_results) {
          names.push((result && result.name) || 'tool');
        }
      }
      if (names.length > 0 && turn >= startTurn) {
        out.push({
          id: `hist-x-${tag}`,
          type: 'tool_group',
          timestamp: `Turn ${turn}`,
          tools: names.map((name, i) => historyToolItem(`hist-x-${tag}-${i}`, name)),
          finished: true,
        });
      }
    }
    // `meta` and unknown kinds have no visible transcript representation.
  }
  return out;
}
