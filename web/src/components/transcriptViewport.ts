/**
 * The transcript's virtual viewport, shared between `Transcript` and `TurnRail`.
 *
 * The transcript mounts only the rows near the viewport (see `Transcript.tsx`), so
 * an off-screen turn has no DOM anchor to measure or scroll to: a
 * `querySelector('[data-turn-id="..."]')` lookup finds a row only while that row
 * happens to be mounted.  The rail therefore asks the transcript for turn offsets
 * and for a jump, through this registry, instead of measuring the DOM itself.
 *
 * The handle is passed down as a prop rather than published through a module
 * global: `Transcript` renders the rail *before* its own scrollport, so a
 * registry written from a layout effect would not exist yet when the rail first
 * measures.  The pure rules live here so they stay testable with `node --test`,
 * without a DOM and without importing React.
 */

/**
 * Row height assumed before a row has been measured, in pixels.
 *
 * Only the first paint of a row uses it: `measureElement` reports the real height
 * straight after, and the virtualizer keeps the measured value per row key, so a
 * scroll back to a visited row is exact.
 */
export const ESTIMATED_ROW_PX = 140;

/**
 * Rows mounted beyond each edge of the viewport.
 *
 * The wheel and the keyboard need a row or two of slack to feel seamless; the
 * rest is headroom for a fast flick and for the height correction that lands
 * after a row is first measured.
 */
export const OVERSCAN_ROWS = 8;

/**
 * The `scrollTop` that holds a message's content offset at its viewport offset.
 *
 * Prepending changes the message's content offset; asynchronous parsing can
 * also resize rows below it. Anchoring the message, not the document bottom,
 * keeps those unrelated measurements from shifting what the reader sees.
 */
export function anchoredScrollTop(contentOffset: number, viewportOffset: number): number {
  return Math.max(0, contentOffset - viewportOffset);
}

/** What `Transcript` hands to `TurnRail`. */
export interface TranscriptViewport {
  /** The transcript's scrollport, or `null` before it has mounted. */
  scroller(): HTMLElement | null;
  /**
   * Content offsets of the given message ids, in the same coordinate space as
   * `scroller().scrollTop`.  A message that is not in the transcript reports
   * `Number.POSITIVE_INFINITY`, which is what the rail's rules already treat as
   * "past the end".
   */
  offsetsOf(ids: readonly string[]): number[];
  /** Scroll so the given message sits at the top of the reading area. */
  scrollToMessage(id: string): void;
}

/**
 * The submitted prompt the transcript has already been followed to.
 *
 * Submitting a prompt is the reader's explicit "show me the newest content", but
 * the store rebuilds the `messages` array on every live event (it re-applies the
 * cached fold views), so "the newest row is a user row" is true for the whole
 * window between the submit and the model's first row.  Keying the follow off the
 * row *type* re-armed it on every activity tick of that window: a wheel-up right
 * after the submit was undone by the next tick, and the transcript could not be
 * scrolled at all until the answer started.  The prompt's own id is the one-shot
 * signal; the session key keeps two sessions' ids from colliding.
 */
export interface FollowedPrompt {
  session: string;
  promptId: string | null;
}

/** The two fields of a transcript row the follow rule reads. */
export interface NewestRow {
  id: string;
  type: string;
}

/**
 * Whether this list carries a prompt the reader has not been followed to yet.
 *
 * True only for the first message list that carries the prompt, so a reader who
 * scrolled away from a submitted prompt keeps their position through the pending
 * window (activity ticks, subagent events, usage updates) until the model starts.
 */
export function followsFreshPrompt(
  followed: FollowedPrompt,
  session: string,
  newest: NewestRow | undefined,
): boolean {
  if (newest === undefined || newest.type !== 'user') return false;
  return followed.session !== session || followed.promptId !== newest.id;
}
