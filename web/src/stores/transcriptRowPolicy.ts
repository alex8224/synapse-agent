import type { TranscriptMessage } from './historyMapper.ts';

/**
 * How a row kind behaves in its turn's fold.
 *
 * The fold hides a turn's steps behind its "已工作 N 秒" header, and the header's
 * status pill is read off those same steps.  Which kinds are steps is a property of
 * the kind, so a new kind declares itself here and the list, the fold and the pill all
 * follow -- there is no branch in the layout to remember to extend.
 */
export interface RowKindPolicy {
  /** The row is a step of its turn: the fold may hide it, and it feeds the pill. */
  step: boolean;
  /**
   * A step with nothing in it is not a step at all: a batch opens its group before its
   * first item lands (and a batch can end up carrying none), so the row paints nothing
   * until it has items -- the TUI never paints an empty "0 tools" placeholder either.
   */
  paints?: (message: TranscriptMessage) => boolean;
}

export const ROW_POLICY: Record<TranscriptMessage['type'], RowKindPolicy> = {
  user: { step: false },
  thought: { step: true },
  tool_group: { step: true, paints: (message) => (message.tools?.length ?? 0) > 0 },
  assistant: { step: false },
  info: { step: false },
  // The turn's outcome, not its process: a folded turn still shows what it changed.
  changes: { step: false },
};

/** True when the row is a step of its turn's fold and has something to show. */
export function isFoldStep(message: TranscriptMessage): boolean {
  const policy = ROW_POLICY[message.type];
  if (!policy.step) return false;
  return policy.paints === undefined || policy.paints(message);
}
