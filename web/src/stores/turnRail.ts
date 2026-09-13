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
