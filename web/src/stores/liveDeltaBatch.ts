/**
 * Batching of streamed text deltas on their way into the console store.
 *
 * One model chunk arrives as one `reasoning_delta` / `answer_delta` event, and
 * folding an event is not free: the reducer rebuilds the transcript array, the
 * store notifies every subscriber, and the row that grew re-parses its whole
 * accumulated Markdown.  A fast reasoning stream delivers hundreds of chunks per
 * second, so paying that cost per chunk is what makes the console burn CPU while
 * the agent thinks.
 *
 * Events are therefore held for a short display window and merged, so the same
 * text reaches the store a few dozen times per second instead of a few hundred.
 * Only *consecutive* deltas of the same kind, turn and message are merged, and
 * the store flushes the queue before folding anything else, so the order the
 * events are applied in never changes.
 */
import { reduceRuntimeEvent, type LiveReducibleState } from './liveEventReducer.ts';
import type { RuntimeEvent } from '../client/types.ts';

/** One live event as the store holds it, with the subscription that delivered it. */
export interface LiveEventEntry {
  event: RuntimeEvent;
  subscription_id?: string;
}

/**
 * How long streamed deltas are held before they are folded into state.
 *
 * 40ms is one frame at 25fps: the text still appears to flow, but a stream that
 * arrives as 250 chunks/s costs 25 store updates/s instead of 250.
 */
export const DELTA_COALESCE_MS = 40;

/** Kinds whose payload is append-only text, so consecutive ones merge losslessly. */
export function isCoalescibleDeltaKind(kind: string): boolean {
  return kind === 'reasoning_delta' || kind === 'answer_delta';
}

/** The payload as a record, or null when it is not a JSON object. */
function payloadRecord(event: RuntimeEvent): Record<string, unknown> | null {
  const payload: unknown = event.payload;
  if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) return null;
  return payload as Record<string, unknown>;
}

/**
 * Whether two deltas may be merged.
 *
 * Same kind, same turn and same message only: a delta belonging to another row
 * would be appended to the wrong one, and merging across a kind boundary (or
 * across a non-delta event) would reorder the transcript.
 */
function mergeable(previous: RuntimeEvent, next: RuntimeEvent): boolean {
  if (previous.kind !== next.kind || previous.turn_id !== next.turn_id) return false;
  const before = payloadRecord(previous);
  const after = payloadRecord(next);
  if (before === null || after === null) return false;
  if (typeof before.text !== 'string' || typeof after.text !== 'string') return false;
  return before.message_id === after.message_id;
}

/**
 * Merge runs of consecutive coalescible deltas.
 *
 * The newest envelope (sequence, turn_sequence) is kept for a merged run, so the
 * cursor the store advances stays monotonic; only `payload.text` accumulates.
 * Every other entry passes through untouched.
 */
export function coalesceLiveEvents(entries: readonly LiveEventEntry[]): LiveEventEntry[] {
  const merged: LiveEventEntry[] = [];
  for (const entry of entries) {
    const previous = merged[merged.length - 1];
    if (
      previous === undefined ||
      !isCoalescibleDeltaKind(entry.event.kind) ||
      !mergeable(previous.event, entry.event)
    ) {
      merged.push(entry);
      continue;
    }
    const before = payloadRecord(previous.event) ?? {};
    const after = payloadRecord(entry.event) ?? {};
    merged[merged.length - 1] = {
      ...entry,
      event: {
        ...entry.event,
        payload: {
          ...after,
          text: `${String(before.text ?? '')}${String(after.text ?? '')}`,
        } as RuntimeEvent['payload'],
      },
    };
  }
  return merged;
}

/**
 * Fold a run of live events into the transcript with a single state transition.
 *
 * One event is the degenerate case and folds exactly like the store's per-event
 * path did, so a run of one entry is not a special case anywhere.
 */
export function foldLiveEvents<S extends LiveReducibleState>(
  state: S,
  entries: readonly LiveEventEntry[],
  now?: () => Date,
): S {
  let next: S = state;
  for (const entry of entries) {
    next = { ...next, ...reduceRuntimeEvent(next, entry.event, now) };
  }
  return next;
}
