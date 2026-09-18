import { todoPreviewFromArgs } from './todoView.ts';
/**
 * Pure mappers between the Agent Runtime RPC DTOs (`runtime.session.list`,
 * `runtime.session.history`) and the console transcript/session UI model.
 *
 * These functions are deliberately free of zustand / React / WebSocket
 * dependencies so they can be exercised directly with the Node built-in test
 * runner (`node --test`) and reused from `useConsoleStore`.
 *
 * The transcript projection is a light-weight per-turn projection, NOT a full
 * checkpoint snapshot: it carries no LangChain messages or exact event
 * sequences. Runtime identity/timing are optional on older projections.
 */

import { HISTORY_PAGE_SIZE } from '../client/types.ts';
import type { HistoryEvent, SessionMetadataItem } from '../client/types.ts';
import { mapHistoryAttachments, type TranscriptAttachment } from './historyAttachments.ts';
import { displaySessionTitle } from './sessionList.ts';

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
  /**
   * The call's own arguments, as the projection stores them (already truncated
   * by the runtime).  `intent` is lifted into `label` and a path argument into
   * `path`, so those two read as the row's own fields; the rest is what a reader
   * opens the row to see.  Absent for live rows (the wire item payload carries no
   * arguments) and for legacy projections.
   */
  args?: Record<string, unknown> | null;
  /** Bounded live-stream argument preview from the batch-start event. */
  argsPreview?: string | null;
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

/**
 * One file a turn changed, as the console paints it.
 *
 * The counts are that turn's own contribution (`runtime/workspace_changes`), so the
 * same file may appear in several turns' lists, each with its own numbers.
 */
export interface TurnChangeView {
  path: string;
  /** `added` | `modified` | `deleted` | `renamed`. */
  status: string;
  insertions: number;
  deletions: number;
  /** Changed, but with no line counts to report (binary, or too large to count). */
  binary: boolean;
  /**
   * True once this file's part in the turn was undone.  The turn did change it -- the
   * card keeps saying so -- but the file no longer holds that change, so its counts no
   * longer describe the workspace.
   */
  reverted: boolean;
}

/**
 * Map the wire's change list into the row's view model, dropping malformed entries.
 *
 * `revertedPaths` are the paths the runtime reports as already undone for this turn (the
 * history read carries them), so a reload paints the card the way the workspace is now.
 */
export function turnChangeViews(raw: unknown, revertedPaths?: unknown): TurnChangeView[] {
  if (!Array.isArray(raw)) return [];
  const reverted = new Set<string>();
  if (Array.isArray(revertedPaths)) {
    for (const entry of revertedPaths) {
      if (typeof entry === 'string' && entry !== '') reverted.add(entry);
    }
  }
  const views: TurnChangeView[] = [];
  for (const entry of raw) {
    if (entry === null || typeof entry !== 'object') continue;
    const record = entry as Record<string, unknown>;
    const path = typeof record.path === 'string' ? record.path : '';
    if (path === '') continue;
    views.push({
      path,
      status: typeof record.status === 'string' && record.status !== '' ? record.status : 'modified',
      insertions: countOf(record.insertions),
      deletions: countOf(record.deletions),
      binary: record.binary === true,
      reverted: reverted.has(path),
    });
  }
  return views;
}

/** A line count as the wire may report it: a non-negative integer, or nothing. */
function countOf(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
}

/**
 * Mark one file of one turn as reverted, right after the runtime did it.
 *
 * The same shape the history read produces, so a card painted from a live revert and one
 * painted from a reload cannot disagree.  A turn with no change row -- or one that does
 * not report this path -- is returned unchanged.
 */
export function markRevertedPath(
  messages: TranscriptMessage[],
  turnId: string,
  path: string,
): TranscriptMessage[] {
  let changed = false;
  const next = messages.map((message) => {
    const changes = message.changes;
    if (message.type !== 'changes' || message.turnId !== turnId || !changes) return message;
    if (!changes.some((change) => change.path === path && !change.reverted)) return message;
    changed = true;
    return {
      ...message,
      changes: changes.map((change) =>
        change.path === path ? { ...change, reverted: true } : change,
      ),
    };
  });
  return changed ? next : messages;
}

export interface TranscriptMessage {
  id: string;
  type: 'user' | 'thought' | 'tool_group' | 'assistant' | 'info' | 'changes';
  timestamp: string;
  /** Runtime turn identity, or a stable history cursor for legacy projections. */
  turnId?: string;
  /** Turn-level clock lives on its anchor, never on a component instance. */
  work?: { startedAt?: number; elapsed?: number; ended: boolean };
  workExpanded?: boolean;
  /** A steer message belongs to the existing runtime turn, not a new clock. */
  steer?: boolean;
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
  /**
   * Live only: an assistant row still being written.
   *
   * A multi-step turn prints text before each further tool batch, so those
   * segments are separate rows; this flag marks the one the next delta belongs
   * to (the durable projection never sets it).
   */
  streaming?: boolean;
  /** Severity of an `info` row. */
  infoLevel?: 'info' | 'warning';
  /**
   * Image attachments of a `user` turn (durable metadata only, never bytes).
   * Absent when the turn carried none, so an older server that omits the field
   * simply renders no thumbnails.
   */
  attachments?: TranscriptAttachment[];
  /** The files this turn changed, on a `changes` row (see `TurnChangeView`). */
  changes?: TurnChangeView[];
  /** How many files the turn changed in total; `changes` is the bounded list. */
  changesTotal?: number;
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
 * The runtime caps one history page at 896 KiB and answers `history_too_large`;
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
    // An unnamed row shows the console's own placeholder label, never the raw
    // `session <thread_id>` the server stores until the first user message.
    title: displaySessionTitle(item.title, item.thread_id),
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
 * Keys use the durable turn identity/cursor, so pagination and refresh preserve
 * fold identity. `pageTag` remains accepted for existing callers.
 */
export function mapHistoryEvents(
  events: HistoryEvent[],
  opts: { startTurn: number; pageTag: string },
): TranscriptMessage[] {
  const { startTurn } = opts;
  const out: TranscriptMessage[] = [];
  // The first `user` event of the page opens `startTurn`; every later `user`
  // starts the next turn, and non-user events keep the latest turn label.
  let turn = Math.max(0, startTurn - 1);
  let turnId = `history:${turn}`;
  let ordinal = 0;
  for (const ev of events) {
    if (ev.kind === 'user') {
      turn += 1;
      ordinal = 0;
      turnId = ev.turn_id || `history:${turn}`;
    }
    const tag = `${turnId}-${ordinal++}`;
    if (ev.kind === 'user') {
      const content = (ev.text || '').trim();
      // An attachment-only turn persists with empty text, so the row is kept
      // whenever there is either text or at least one attachment to show.
      const attachments = mapHistoryAttachments(ev.attachments);
      if (content || attachments.length > 0) {
        out.push({
          id: `hist-u-${tag}`,
          type: 'user',
          timestamp: `Turn ${turn}`,
          turnId,
          work: {
            ended: true,
            ...(typeof ev.elapsed_s === 'number' && Number.isFinite(ev.elapsed_s) && ev.elapsed_s >= 0
              ? { elapsed: ev.elapsed_s } : {}),
          },
          content,
          ...(attachments.length > 0 ? { attachments } : {}),
        });
      }
    } else if (ev.kind === 'thought') {
      const content = ev.text || '';
      if (content.trim() && turn >= startTurn) {
        out.push({
          id: `hist-t-${tag}`,
          type: 'thought',
          timestamp: `Turn ${turn}`,
          turnId,
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
          turnId,
          content,
        });
      }
    } else if (ev.kind === 'tools') {
      const names: string[] = [];
      // The projected tool calls keep their raw args, so a checklist written by
      // `write_todos` can be rebuilt here the same way the runtime builds it for
      // live events; without that the todo panel would only ever see this
      // session's live turns.
      const previews: (string | null)[] = [];
      for (const call of ev.tool_calls) {
        const name = call?.name;
        const resolved = typeof name === 'string' && name !== '' ? name : 'tool';
        names.push(resolved);
        previews.push(todoPreviewFromArgs(resolved, call?.args));
      }
      if (names.length === 0) {
        // Projections may carry results without calls (e.g. very old rows).
        for (const result of ev.tool_results) {
          const name = result?.name;
          names.push(typeof name === 'string' && name !== '' ? name : 'tool');
          previews.push(null);
        }
      }
      if (names.length > 0 && turn >= startTurn) {
        out.push({
          id: `hist-x-${tag}`,
          type: 'tool_group',
          timestamp: `Turn ${turn}`,
          turnId,
          tools: names.map((name, i) => {
            const item = historyToolItem(`hist-x-${tag}-${i}`, name);
            const call = ev.tool_calls[i];
            const callId = typeof call?.id === 'string' && call.id ? call.id : null;
            const result = callId !== null
              ? ev.tool_results.find((r) => r.id === callId)
              : ev.tool_results[i];
            const args = call?.args !== null && typeof call?.args === 'object' && !Array.isArray(call.args)
              ? call.args : {};
            const status = typeof result?.status === 'string' ? result.status : 'completed';
            const error = status === 'error' || status === 'failed';
            return {
              ...item, callId,
              label: typeof args.intent === 'string' ? args.intent : name,
              path: typeof args.file_path === 'string' ? args.file_path : null,
              ...(Object.keys(args).length > 0 ? { args } : {}),
              preview: previews[i] ?? (typeof result?.content === 'string'
                ? result.content.slice(0, 4000) : null),
              status: error ? 'failed' : status === 'ok' || status === 'success' ? 'completed' : status,
              error,
              subagentName: typeof args.subagent_type === 'string' ? args.subagent_type : null,
            };
          }),
          finished: true,
        });
      }
    } else if (ev.kind === 'changes') {
      // What the turn did to the workspace, as its own row: a turn that changed
      // nothing carries no list, and a row that predates change tracking carries
      // none either -- neither paints an empty card block.
      // The runtime also reports which of these files have since been undone, so a
      // reload paints a reverted card as reverted instead of as a standing edit.
      const changes = turnChangeViews(ev.changes, ev.reverted_paths);
      if (changes.length > 0 && turn >= startTurn) {
        out.push({
          id: `hist-c-${tag}`,
          type: 'changes',
          timestamp: `Turn ${turn}`,
          turnId,
          changes,
          changesTotal: typeof ev.changes_total === 'number' && ev.changes_total > 0
            ? ev.changes_total : changes.length,
        });
      }
    }
    // `meta` and unknown kinds have no visible transcript representation.
  }
  return out;
}
