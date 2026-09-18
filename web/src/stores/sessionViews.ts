/**
 * Per-session live views for the multi-watch console (stage 3b).
 *
 * The console used to hold exactly one session's live state in a flat set of
 * store fields and to drop the old watch on every switch, so returning replayed
 * from history and the head of a running turn could be lost.  With concurrent
 * watches (stage 3a) several sessions can stream at once, so every session the
 * console is *not* showing keeps its own view here, keyed by
 * `project_id:thread_id`.
 *
 * This module is deliberately pure and free of zustand / React / DOM, so the
 * background fold is exercised directly with the Node test runner and -- more
 * importantly -- is the *same* fold the active session uses: `foldLiveEvents`
 * plus the terminal-turn usage bookkeeping, never a second reducer.
 */
import { foldLiveEvents, type LiveEventEntry } from './liveDeltaBatch.ts';
import { isTurnTerminalKind, type LiveReducibleState } from './liveEventReducer.ts';
import type { SessionUsage } from './usageView.ts';

/**
 * Stable identity of one session: `project_id:thread_id`.
 *
 * Mirrors the daemon's `SessionRef.global_id` (`src/synapse/runtime/sessions/ref.py`),
 * so a key the console computes and a key the daemon computes name the same
 * session even though the browser never sees the daemon's own string.
 */
export function sessionKey(session: { project_id: string; thread_id: string }): string {
  return `${session.project_id}:${session.thread_id}`;
}

/**
 * The store's recovery-state union, mirrored here so a view can carry it.
 *
 * Duplicating the literals keeps this module free of the store (which would drag
 * in zustand); the two unions are structurally identical, so a view's state
 * round-trips into the store without a cast.  `'resync'` stays a member for
 * forward compatibility even though the store no longer publishes it: a newer
 * store might.
 */
export type RecoveryState =
  | 'idle'
  | 'reconnecting'
  | 'resuming'
  | 'resumed'
  | 'resync'
  | 'incomplete'
  | 'unknown'
  | 'failed';

/**
 * The per-session live state a background view carries.
 *
 * `subscriptionId === null` means the watch ended (a `complete`/`error` notice,
 * a dropped socket, a fence).  The transcript is kept so the reader still sees
 * something, but returning to that session must do a FULL attach: a transcript
 * whose stream stopped is not proof it is complete.
 *
 * A view also carries the state a live restore must not manufacture: the model
 * label, the history page (so "load earlier" survives the round-trip), and the
 * recovery/degradation state (a truncation belongs to *this* session, never to
 * the one on screen).  `liveEpoch` is the broker-epoch baseline at the moment of
 * backgrounding, so a later resume compares against the session it shows.
 */
export interface BackgroundSessionView extends LiveReducibleState {
  sessionUsage: SessionUsage | null;
  /** Model label resolved for this session (never the previous session's). */
  modelName: string;
  /** History availability/pagination, restored verbatim (no manufactured values). */
  historyAvailable: boolean | null;
  historyHasMore: boolean;
  historyStartTurn: number;
  historyEndTurn: number;
  historyTotalTurns: number;
  historyError: string | null;
  /** Recovery/degradation state travels with the session that produced it. */
  recoveryState: RecoveryState;
  recoveryDetail: string | null;
  liveBufferDroppedCount: number;
  /** Broker-epoch baseline captured when this session was backgrounded. */
  liveEpoch: string | null;
  subscriptionId: string | null;
  touchedAt: number;
}

/**
 * The store fields a view mirrors: exactly the per-session fields the console
 * moves out of view and back, no more.  Declared structurally so the store state
 * satisfies it without this module importing the store (which would drag in
 * zustand).
 *
 * `liveEpoch` is excluded because it is not a store field: the store owns it as
 * a module-level baseline, so it is passed in/out like the watch bookkeeping.
 */
export type LiveViewSource = Omit<
  BackgroundSessionView,
  'subscriptionId' | 'touchedAt' | 'liveEpoch'
>;

/**
 * Capture the active session's live fields into a view.
 *
 * `subscriptionId` and `touchedAt` are bookkeeping the store owns (which watch
 * delivers the view, and when it was last updated for the LRU), and `liveEpoch`
 * is the store's broker-epoch baseline, so all three are passed in rather than
 * read from the live state.
 */
export function snapshotLiveView(
  state: LiveViewSource,
  subscriptionId: string | null,
  touchedAt: number,
  liveEpoch: string | null,
): BackgroundSessionView {
  return {
    messages: state.messages,
    // A concrete array, not the optional field: a view must be self-contained,
    // and an `undefined` here would round-trip as "no field" instead of "none".
    settledTurnIds: state.settledTurnIds ?? [],
    activeTurnId: state.activeTurnId,
    runtimeStatus: state.runtimeStatus,
    steerQueueCount: state.steerQueueCount,
    pendingApproval: state.pendingApproval,
    activity: state.activity,
    usage: state.usage,
    metricsLabel: state.metricsLabel,
    sessionUsage: state.sessionUsage,
    modelName: state.modelName,
    historyAvailable: state.historyAvailable,
    historyHasMore: state.historyHasMore,
    historyStartTurn: state.historyStartTurn,
    historyEndTurn: state.historyEndTurn,
    historyTotalTurns: state.historyTotalTurns,
    historyError: state.historyError,
    recoveryState: state.recoveryState,
    recoveryDetail: state.recoveryDetail,
    liveBufferDroppedCount: state.liveBufferDroppedCount,
    liveEpoch,
    subscriptionId,
    touchedAt,
  };
}

/** The store patch that puts a view back in front of the reader. */
export function restoreLiveView(view: BackgroundSessionView): Partial<LiveViewSource> {
  return {
    messages: view.messages,
    settledTurnIds: view.settledTurnIds ?? [],
    activeTurnId: view.activeTurnId,
    runtimeStatus: view.runtimeStatus,
    steerQueueCount: view.steerQueueCount,
    pendingApproval: view.pendingApproval,
    activity: view.activity,
    usage: view.usage,
    metricsLabel: view.metricsLabel,
    sessionUsage: view.sessionUsage,
    modelName: view.modelName,
    historyAvailable: view.historyAvailable,
    historyHasMore: view.historyHasMore,
    historyStartTurn: view.historyStartTurn,
    historyEndTurn: view.historyEndTurn,
    historyTotalTurns: view.historyTotalTurns,
    historyError: view.historyError,
    recoveryState: view.recoveryState,
    recoveryDetail: view.recoveryDetail,
    liveBufferDroppedCount: view.liveBufferDroppedCount,
  };
}

/**
 * What a session row's live marker reports.
 *
 * `approval` wins over `running` because a turn blocked on a human is still
 * "running" but is the thing the reader must act on; `undefined` means the
 * session has no marker at all.
 */
export type SessionStatusKind = 'running' | 'approval';

/** The slice of one background view the sidebar marker needs. */
export interface SessionStatusView {
  /** `null` means the watch ended: the status is unknown, so no marker. */
  subscriptionId: string | null;
  runtimeStatus: 'idle' | 'running';
  pendingApproval: unknown;
}

/** The active session's own status (it is always live -- it is on screen). */
export interface ActiveSessionStatus {
  /** `sessionKey` of the session on screen, or null when none is open. */
  key: string | null;
  runtimeStatus: 'idle' | 'running';
  pendingApproval: unknown;
}

/**
 * The live status of every session the console holds a view for, keyed by
 * `sessionKey`.
 *
 * A stale view (`subscriptionId === null`) means "unknown", not "running": the
 * watch stopped, so keeping its last status would leave a pulsing dot (or an
 * approval) alive forever.  The active session's own state counts too, so its row
 * carries the marker while its transcript is on screen.  Returned as a small
 * string map so a shallow comparison can skip a re-render for an unchanged
 * status (a background delta rewrites its view on every frame).
 */
export function sessionStatusMarkers(
  views: Record<string, SessionStatusView>,
  active: ActiveSessionStatus,
): Record<string, SessionStatusKind> {
  const statuses: Record<string, SessionStatusKind> = {};
  for (const [key, view] of Object.entries(views)) {
    if (view.subscriptionId === null) continue;
    if (view.pendingApproval !== null) statuses[key] = 'approval';
    else if (view.runtimeStatus === 'running') statuses[key] = 'running';
  }
  if (active.key !== null) {
    if (active.pendingApproval !== null) statuses[active.key] = 'approval';
    else if (active.runtimeStatus === 'running') statuses[active.key] = 'running';
  }
  return statuses;
}

/**
 * Add one finished turn's tokens to a session's cumulative totals.
 *
 * The terminal payload carries that turn's own `input/output/cache` counts and
 * the runtime accumulates exactly those in its settle step, so adding them here
 * keeps the bar exact.  Shared with the active fold (`foldTurnUsage` in the
 * store) so the two paths can never drift.
 */
export function addTurnUsage(base: SessionUsage | null, payload: unknown): SessionUsage | null {
  if (payload === null || typeof payload !== 'object') return base;
  const record = payload as Record<string, unknown>;
  const count = (key: string): number => {
    const value = record[key];
    return typeof value === 'number' && Number.isFinite(value) ? Math.trunc(value) : 0;
  };
  const input = count('input_tokens');
  const output = count('output_tokens');
  const cache = count('cache_tokens');
  if (input === 0 && output === 0 && cache === 0) return base;
  const start = base ?? { input: 0, output: 0, cache: 0 };
  return {
    input: start.input + input,
    output: start.output + output,
    cache: start.cache + cache,
  };
}

/**
 * Fold a batch of live events into a background view.
 *
 * One call to the shared `foldLiveEvents` handles the transcript, status,
 * approval and activity exactly as the active fold does (including ignoring a
 * late event from a settled or foreign turn); the only extra work is the
 * terminal-turn usage bookkeeping, which the store does outside its own fold.
 * `touchedAt` moves on every batch so the LRU reflects real activity, not just
 * a switch.
 */
export function foldBackgroundEvents(
  view: BackgroundSessionView,
  entries: readonly LiveEventEntry[],
  now: () => Date = () => new Date(),
): BackgroundSessionView {
  const folded = foldLiveEvents(view, entries, now);
  let sessionUsage = folded.sessionUsage;
  for (const entry of entries) {
    if (isTurnTerminalKind(entry.event.kind)) {
      sessionUsage = addTurnUsage(sessionUsage, entry.event.payload);
    }
  }
  return { ...folded, sessionUsage, touchedAt: now().getTime() };
}

/**
 * How many background views the console keeps.
 *
 * The daemon caps one connection at 32 subscriptions; the console holds one
 * active plus this many background views, so the cap is never approached and a
 * long browsing session cannot grow the map without bound.
 */
export const MAX_BACKGROUND_VIEWS = 8;

/**
 * Evict the least-recently-touched views down to `limit`.
 *
 * `protectedKey` (the session being attached) is never evicted even if it is the
 * oldest, because it is about to become the active view and evicting it would
 * only drop the transcript the console is showing.  Returns the surviving map
 * and the evicted keys, so the caller can detach exactly those watches.
 */
export function pruneBackgroundViews(
  views: Record<string, BackgroundSessionView>,
  limit: number,
  protectedKey: string | null,
): { kept: Record<string, BackgroundSessionView>; evicted: string[] } {
  const keys = Object.keys(views);
  if (keys.length <= limit) return { kept: views, evicted: [] };
  const candidates = keys
    .filter((key) => key !== protectedKey)
    .sort((a, b) => views[a].touchedAt - views[b].touchedAt);
  const evicted = candidates.slice(0, keys.length - limit);
  if (evicted.length === 0) return { kept: views, evicted: [] };
  const dropped = new Set(evicted);
  const kept: Record<string, BackgroundSessionView> = {};
  for (const key of keys) {
    if (!dropped.has(key)) kept[key] = views[key];
  }
  return { kept, evicted };
}
