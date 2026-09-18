/**
 * Presentation model for the console's recovery / degradation state.
 *
 * Pure module: no React, no zustand, no DOM.  The store already computes the
 * three inputs (`recoveryState`, `recoveryDetail`, `liveBufferDroppedCount`)
 * but nothing used to read them, so a truncated live replay -- the running
 * turn's earlier events dropped or evicted -- was completely silent to the
 * reader.  This module is the single place that decides *whether* the console
 * must say something, and it returns `null` when it must stay quiet.
 *
 * Decision table (the whole policy, one row per input state):
 *
 * | `recoveryState`              | notice      | why                                        |
 * | ---------------------------- | ----------- | ------------------------------------------ |
 * | `'idle'`                     | `null`      | nothing was ever interrupted                |
 * | `'resumed'`                  | `null`      | the resume *succeeded*; there is nothing left to report |
 * | `'failed'`                   | `blocked`   | the reconnect budget ran out: the reader has to act |
 * | `'incomplete'` / `'resync'`  | `degraded`  | replay from the history snapshot is partial / was re-anchored |
 * | `'reconnecting'`/`'resuming'`| `transient` | self-healing in flight; informational only  |
 * | anything else (e.g. `'unknown'`, or a value a newer daemon added) | `null` | forward compatibility: an older console must not paint a notice it cannot explain |
 * | any state, when `liveBufferDroppedCount > 0` | `degraded` (or the state's own kind, with the count merged in) | the live events never reached the transcript, so that replay is truncated |
 *
 * Wording discipline: the title is the headline, `recoveryDetail` is the
 * server's own sentence.  The title must never restate the detail verbatim --
 * see `noticeDetail`.
 */

export type RecoveryNoticeKind = 'blocked' | 'degraded' | 'transient';

export interface RecoveryNotice {
  kind: RecoveryNoticeKind;
  title: string;
  detail: string | null;
  /** Live events dropped by the bounded buffer; `0` when nothing was lost. */
  droppedEvents: number;
}

/**
 * User-facing headlines, phrased like the copy the store already publishes for
 * the same situations (`useConsoleStore` sets
 * `'运行轮次的早期步骤不可完整恢复；已保存历史不受影响。'` for an incomplete
 * replay), so the strip and the transcript cannot describe one incident two
 * different ways.
 */
const TITLES: Record<RecoveryNoticeKind, string> = {
  blocked: '运行时连接恢复失败',
  degraded: '运行轮次的早期步骤不可完整恢复',
  transient: '正在恢复运行时连接',
};

/**
 * The kind one recovery state maps to, or `null` when the state is quiet.
 *
 * A `switch` rather than a lookup table on purpose: a table indexed by an
 * arbitrary string would answer for inherited keys (`'constructor'`), and a
 * newer daemon's unknown state must fall through to silence instead.
 */
function stateKind(recoveryState: string): RecoveryNoticeKind | null {
  switch (recoveryState) {
    case 'failed':
      return 'blocked';
    case 'incomplete':
    case 'resync':
      return 'degraded';
    case 'reconnecting':
    case 'resuming':
      return 'transient';
    // `'idle'` / `'resumed'` are the healthy states and every other value is
    // unknown: both must render nothing.  A `'resumed'` state means the resume
    // succeeded, so reporting it would leave a permanent notice on a healthy
    // console.
    default:
      return null;
  }
}

/** Drops are counted, never estimated: a bad count degrades to `0`. */
function dropCount(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return 0;
  return Math.floor(value);
}

/**
 * The detail line, or `null` when it would add nothing.
 *
 * `recoveryDetail` is echoed as-is, with one exception: when it is empty, or is
 * exactly the headline, it is dropped -- the strip already prints the title, and
 * printing the same sentence twice reads as a rendering bug rather than as
 * information.
 */
function noticeDetail(recoveryDetail: string | null, title: string): string | null {
  if (recoveryDetail === null) return null;
  if (recoveryDetail.trim() === '' || recoveryDetail.trim() === title) return null;
  return recoveryDetail;
}

/**
 * The notice for one snapshot, or `null` when the console must stay silent.
 *
 * Two independent inputs can demand a notice, and they must never produce two
 * strips: when the recovery state already carries one, the dropped count is
 * merged into it (`droppedEvents`) instead of being reported separately.
 */
export function recoveryNotice(
  recoveryState: string,
  recoveryDetail: string | null,
  liveBufferDroppedCount: number,
): RecoveryNotice | null {
  const droppedEvents = dropCount(liveBufferDroppedCount);
  const kind = stateKind(recoveryState);
  if (kind === null && droppedEvents === 0) return null;
  // Dropped live events are always a degradation: the running turn's earlier
  // events were evicted before the transcript could buffer them, so that replay
  // is truncated even while the connection itself is healthy.
  const resolved = kind ?? 'degraded';
  return {
    kind: resolved,
    title: TITLES[resolved],
    detail: noticeDetail(recoveryDetail, TITLES[resolved]),
    droppedEvents,
  };
}
