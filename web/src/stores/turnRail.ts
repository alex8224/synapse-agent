/**
 * Pure helpers for the console's turn rail (the left minimap).
 *
 * Mirrors the TUI's `ui/turn_rail.py`: a one-line preview per turn, ticks packed
 * and centred while they fit, proportional bucket merging once they do not, and
 * a hover label that names a merged bucket by its turn range.
 *
 * Dependency-free on purpose, so the rules run under the Node test runner the
 * same way the TUI's are covered by its own tests.
 */
import type { TranscriptMessage } from './historyMapper.ts';

/** One-line preview budget, matching the TUI rail's `_RAIL_PREVIEW_MAX`. */
export const TURN_RAIL_PREVIEW_MAX = 28;
/** Budget for the hover text, which has room for an actual excerpt. */
export const TURN_RAIL_HOVER_MAX = 120;

const WHITESPACE = /\s+/g;

/** Single-line, whitespace-collapsed excerpt (`(empty)` for blank input). */
export function formatTurnRailPreview(
  text: string,
  maxLen: number = TURN_RAIL_PREVIEW_MAX,
): string {
  const limit = Math.max(8, Math.trunc(maxLen || TURN_RAIL_PREVIEW_MAX));
  let raw = text ?? '';
  // Cheap cap first: the preview only ever shows ~`limit` characters, so never
  // collapse whitespace across a multi-megabyte paste.
  if (raw.length > limit * 4 + 64) raw = raw.slice(0, limit * 4 + 64);
  const one = raw.trim().replace(WHITESPACE, ' ');
  if (one === '') return '(empty)';
  if (one.length > limit) return `${one.slice(0, limit - 1).trimEnd()}…`;
  return one;
}

/** One turn of the transcript: its user message and its answer so far. */
export interface TurnSummary {
  /** 1-based turn number, in transcript order. */
  index: number;
  /** Id of the turn's user message, used as the scroll anchor. */
  anchorId: string;
  user: string;
  /** The turn's conclusion: its last assistant message, `''` while it has none. */
  conclusion: string;
}

/**
 * Split a transcript into turns.
 *
 * A turn starts at a user message and owns everything up to the next one; its
 * conclusion is the *last* assistant message in that span, which is what the
 * turn ended up saying rather than its first paragraph.
 */
export function transcriptTurns(messages: readonly TranscriptMessage[]): TurnSummary[] {
  const turns: TurnSummary[] = [];
  let current: TurnSummary | null = null;
  for (const message of messages) {
    if (message.type === 'user') {
      current = {
        index: turns.length + 1,
        anchorId: message.id,
        user: message.content ?? '',
        conclusion: '',
      };
      turns.push(current);
      continue;
    }
    const body = message.content ?? '';
    if (current !== null && message.type === 'assistant' && body !== '') {
      current.conclusion = body;
    }
  }
  return turns;
}

/**
 * Map `count` turns onto `height` rail rows.
 *
 * While the turns fit, they are packed and centred vertically so the pointer
 * never has to travel far; beyond that, rows merge proportionally and each row
 * stands for a range of turns.
 */
export function turnRailTickSlots(count: number, height: number): number[][] {
  const h = Math.max(1, Math.trunc(height || 1));
  const n = Math.max(0, Math.trunc(count || 0));
  const slots: number[][] = Array.from({ length: h }, () => []);
  if (n <= 0) return slots;
  if (n <= h) {
    const start = Math.floor((h - n) / 2);
    for (let i = 0; i < n; i += 1) slots[start + i].push(i);
    return slots;
  }
  for (let i = 0; i < n; i += 1) {
    const y = Math.min(h - 1, Math.max(0, Math.floor((i * h) / n)));
    slots[y].push(i);
  }
  return slots;
}

/** Hover label for one rail row: the turn itself, or its range when merged. */
export function turnRailSlotLabel(
  indices: readonly number[],
  previews: readonly string[],
  maxLen: number = TURN_RAIL_PREVIEW_MAX,
): string {
  if (indices.length === 0) return '';
  if (indices.length === 1) {
    return previews[0] ?? `#${indices[0] + 1}`;
  }
  const first = indices[0] + 1;
  const last = indices[indices.length - 1] + 1;
  const head = previews[0] ?? '';
  const prefix = `#${first}-${last} `;
  const room = Math.max(6, Math.trunc(maxLen || TURN_RAIL_PREVIEW_MAX) - prefix.length);
  const clipped =
    head.length > room ? `${head.slice(0, Math.max(0, room - 1)).trimEnd()}…` : head;
  return clipped === '' ? `#${first}-${last}` : `${prefix}${clipped}`;
}

/**
 * Hover text for one rail row: the turn's user message, then its conclusion.
 *
 * Richer than the TUI rail, which shows the user preview only — the point here
 * is to recognise the turn without jumping to it.
 */
export function turnRailHoverText(turn: TurnSummary, maxLen: number = TURN_RAIL_HOVER_MAX): string {
  const lines = [`#${turn.index} ${formatTurnRailPreview(turn.user, maxLen)}`];
  if (turn.conclusion !== '') {
    lines.push(`→ ${formatTurnRailPreview(turn.conclusion, maxLen)}`);
  }
  return lines.join('\n');
}

/**
 * Slack above the scroll port's top edge that still counts as "at the anchor".
 *
 * The rail marks the turn whose content fills the viewport, and a turn scrolled
 * to `block: 'start'` lands exactly on the edge -- without a little slack a
 * sub-pixel scroll position would drop the mark.
 */
export const TURN_RAIL_ACTIVE_SLACK_PX = 12;

/**
 * Index of the turn the transcript is showing, or -1 before the first one.
 *
 * `offsets[i]` is the turn anchor's distance from the top of the scrollable
 * content, ascending.  The active turn is the last anchor at or above
 * `scrollTop + slack`: the one the viewport is inside.  Above the first anchor
 * (the top of the transcript, where the port's own padding sits) the first turn
 * is the one on screen, so it stays active rather than the rail going dark.
 *
 * At the bottom the newest turn wins even when it is short enough for its own
 * anchor to sit *inside* the viewport: the reader is looking at that turn, and the
 * anchor rule alone would mark the second-to-last bar on the last turn.
 *
 * `maxScroll` is `scrollHeight - clientHeight`; the default (no scrolling) keeps
 * the bottom rule from firing on a transcript that fits.
 */
export function activeTurnIndex(
  offsets: readonly number[],
  scrollTop: number,
  slack: number = TURN_RAIL_ACTIVE_SLACK_PX,
  maxScroll = Number.POSITIVE_INFINITY,
): number {
  if (offsets.length === 0) return -1;
  if (scrollTop >= maxScroll - slack) return offsets.length - 1;
  let active = 0;
  for (let i = 0; i < offsets.length; i += 1) {
    if (offsets[i] > scrollTop + slack) break;
    active = i;
  }
  return active;
}

/**
 * Rail row that draws `turnIndex`, or -1 when the rail does not show it.
 *
 * Past `RAIL_ROWS` turns a row stands for a range, so the row to animate is the
 * one whose range contains the turn rather than the turn's own position.
 */
export function turnRailRowFor(
  slots: readonly (readonly number[])[],
  turnIndex: number,
): number {
  if (turnIndex < 0) return -1;
  for (let row = 0; row < slots.length; row += 1) {
    if (slots[row].includes(turnIndex)) return row;
  }
  return -1;
}
