import { create } from 'zustand';
import { SynapseRuntimeClient } from '../client/SynapseRuntimeClient.ts';
import {
  ConsoleAuthRequiredError,
  deriveRuntimeSocketUrl,
  fetchConsoleSession,
  fetchProjects,
  normalizePairingCode,
  pairConsole,
  requestConsoleLogout,
} from '../client/bootstrap.ts';
import type {
  ConsoleProject,
  ConsoleProjectEntry,
  ConsoleSession,
} from '../client/bootstrap.ts';
import {
  ConnectionLostError,
  RpcCallError,
} from '../client/SynapseRuntimeClient.ts';
import type { ConnectionState } from '../client/SynapseRuntimeClient.ts';
import type {
  RuntimeEvent,
  SessionRef,
  PendingApprovalView,
  ApprovalDecision,
  SessionRecoverabilityResult,
} from '../client/types.ts';
import { isCoveredTurn } from '../client/recoverability.ts';
import {
  RuntimeStatusUnavailableError,
  fetchRuntimeStatus,
} from '../client/runtimeStatus.ts';
import {
  RUNTIME_DIAGNOSTICS_IDLE,
} from './runtimeDiagnosticsView.ts';
import type { RuntimeDiagnosticsSnapshot } from './runtimeDiagnosticsView.ts';
import {
  describeHistoryFailure,
  earlierHistoryParams,
  latestHistoryParams,
  mapHistoryEvents,
  readHistoryPage,
  toSessionListView,
} from './historyMapper.ts';
import type { TranscriptMessage, SessionItem } from './historyMapper.ts';
import {
  mapRuntimeConfig,
  mcpStatusLabel,
} from './runtimeConfigMapper.ts';
import { decideResumeAfterDrop } from './recoveryDecider.ts';
import { reduceRuntimeEvent, type ActivityView } from './liveEventReducer.ts';
import type { UsageView } from './usageView.ts';
import { parseSessionGoal } from './goalView.ts';
import type { SessionGoalView } from './goalView.ts';

// Re-exported for callers that imported these from the store in earlier phases.
export type { TranscriptMessage, SessionItem } from './historyMapper';

/**
 * Browser pairing state machine (phase-5 A1):
 * `checking`  - an init attempt is probing `GET /api/session`
 * `unpaired`  - the host answered 401: the pairing code must be entered
 * `pairing`   - a `POST /api/pair` request is in flight
 * `paired`    - a valid console session exists (only then may RPCs be issued)
 * `error`     - the last authentication step failed and the reason is shown
 */
export type PairingState = 'checking' | 'unpaired' | 'pairing' | 'paired' | 'error';

/** Diagnosable error surfaced when a business RPC is attempted too early. */
export const RUNTIME_RPC_NOT_READY =
  'console is not authenticated: complete browser pairing before issuing runtime RPCs';

/**
 * Single-flight guard for `initClient`: concurrent or repeated calls share one
 * attempt, so the console can never fire a duplicate session probe or open a
 * second runtime socket.
 */
let initPromise: Promise<void> | null = null;

/** Bumped on logout so an in-flight authentication attempt cannot re-arm. */
let authEpoch = 0;

/**
 * Single-flight guard for the read-only runtime diagnostics read
 * (`GET /api/runtime-status`, phase-5 C3).
 *
 * A relay failure can trip several triggers at once (state change + connect
 * rejection) and repeated failures must not turn into a request storm, so at
 * most one read is issued per pairing lifetime; only an explicit user refresh
 * (`force`) bypasses the latch.  `runtimeDiagnosticsEpoch` fences a read that
 * was started before a logout/new pairing so its late result can never be
 * written into the next session's state.
 */
let runtimeDiagnosticsPromise: Promise<void> | null = null;
let runtimeDiagnosticsAttempted = false;
let runtimeDiagnosticsEpoch = 0;

/** Options of one diagnostics read. */
export interface RuntimeDiagnosticsRequest {
  /** What asked for the read (`relay_unavailable` / `connect_failed` / `manual`). */
  trigger?: string;
  /** Bounded connection detail to display next to the host's facts. */
  detail?: string | null;
  /** Explicit user refresh: allowed to bypass the once-per-pairing latch. */
  force?: boolean;
}

/**
 * Drop the diagnostics latch (new pairing / logout) so the next failure starts
 * a fresh read instead of being suppressed by the previous session's attempt.
 *
 * Exported because it is the only way to observe the latch from outside the
 * store (the pairing and logout paths above call it, and the offline tests use
 * it to isolate one diagnostics scenario from the next).
 */
export function resetRuntimeDiagnostics(): void {
  runtimeDiagnosticsEpoch += 1;
  runtimeDiagnosticsAttempted = false;
  runtimeDiagnosticsPromise = null;
}

/**
 * One read of `GET /api/runtime-status` into the store.
 *
 * A successful read publishes the host's facts; every failure publishes only a
 * typed reason and leaves the existing copy untouched (silent degradation).
 * A read whose epoch was superseded (logout / new pairing) writes nothing.
 */
async function readRuntimeDiagnostics(
  trigger: string,
  detail: string | null,
  epoch: number,
): Promise<void> {
  const store = useConsoleStore;
  store.setState({
    runtimeDiagnostics: { status: 'loading', view: null, reason: null, trigger, detail },
  });
  try {
    const view = await fetchRuntimeStatus();
    if (epoch !== runtimeDiagnosticsEpoch) return;
    store.setState({ runtimeDiagnostics: { status: 'ready', view, reason: null, trigger, detail } });
  } catch (err) {
    if (epoch !== runtimeDiagnosticsEpoch) return;
    const reason = err instanceof RuntimeStatusUnavailableError ? err.reason : 'unknown';
    store.setState({
      runtimeDiagnostics: { status: 'unavailable', view: null, reason, trigger, detail },
    });
  }
}

function describeError(err: unknown): string {
  if (err instanceof Error && err.message) return err.message;
  return String(err);
}

/**
 * Gate for every business RPC.  Before authentication the console has no
 * runtime client at all, so a call is refused with an observable, diagnosable
 * reason instead of silently doing nothing (C5).
 */
function requireRuntimeClient(): SynapseRuntimeClient | null {
  const { client, pairingState } = useConsoleStore.getState();
  if (!client || pairingState !== 'paired') {
    useConsoleStore.setState({ rpcBlockedReason: RUNTIME_RPC_NOT_READY });
    console.warn('runtime RPC blocked:', RUNTIME_RPC_NOT_READY);
    return null;
  }
  return client;
}

/**
 * Apply the authenticated project context and open the runtime transport.
 *
 * Only reachable after a successful `POST /api/pair` or a 200
 * `GET /api/session`; the socket URL is derived from `window.location` and
 * carries no query string and no credential (C-07).
 */
function startAuthenticatedRuntime(project: ConsoleProject, epoch: number): void {
  if (epoch !== authEpoch) return;
  const store = useConsoleStore;
  // A new authenticated runtime starts from a clean diagnostics slate: the
  // previous pairing's read (and its once-per-pairing latch) must not leak in.
  resetRuntimeDiagnostics();
  const targetUrl = deriveRuntimeSocketUrl(window.location);
  const client = new SynapseRuntimeClient({
    url: targetUrl,
    onStateChange: (state, reason) => {
      const prev = store.getState().connectionState;
      store.setState({ connectionState: state });
      if (state === 'connected') {
        // The recovery lifecycle (reconnecting/resumed/gap/failed) is driven
        // by onRecovery below; a plain transition back to connected with no
        // prior drop stays idle.
        return;
      }
      if (state === 'error' || state === 'disconnected') {
        // Relay unavailable / connection failure (including the host's frozen
        // `1011` + `runtime daemon unavailable` close, whose code and reason are
        // carried in `reason`).  Read the host's read-only diagnostics so the
        // user sees the daemon endpoint / state dir / start hint instead of only
        // the frozen close copy.  The action is gated (paired only) and
        // single-flight, so this can never become a request storm.
        if (reason !== 'closed by user') {
          void store.getState().loadRuntimeDiagnostics({
            trigger: prev === 'connected' ? 'relay_unavailable' : 'connect_failed',
            detail: reason ?? null,
          });
        }
      }
      if (prev === 'connected' && !(reason === 'closed by user')) {
        // Unexpected drop: the old subscription id is dead. Clear it so live
        // events delivered right after the resume watch are buffered and
        // merged once the new subscription is attributed, never dropped.
        store.setState({ activeSubscriptionId: null, recoveryState: 'reconnecting' });
      }
    },
    onRecovery: (info) => {
      if (info.phase === 'reconnecting') {
        store.setState({
          recoveryState: 'reconnecting',
          recoveryDetail: `${info.reason ?? 'connection lost'} (attempt ${info.attempt}/${info.maxAttempts})`,
        });
      } else if (info.phase === 'reconnected') {
        // Transport is back; resume the same session's watch from its last
        // delivered cursor (never a silent after=0).
        const s = store.getState();
        const attached =
          lastAttachedSession !== null &&
          lastAttachedEpoch !== 0 &&
          lastAttachedEpoch === sessionEpoch &&
          s.currentSession.project_id === lastAttachedSession.project_id &&
          s.currentSession.thread_id === lastAttachedSession.thread_id;
        if (attached) {
          void resumeAttachedWatch();
        } else if (!s.historyLoading) {
          // The previous attach was interrupted by the drop (or the user
          // switched while offline): re-attach the current session from the
          // authoritative history snapshot. attachToSession is idempotent and
          // guarded by its own epoch bump, so this can never stack with a
          // concurrent attach started by the user.
          void attachToSession(s.currentSession, s.sessionTitle);
        }
      } else if (info.phase === 'failed') {
        store.setState({
          recoveryState: 'failed',
          recoveryDetail: info.reason ?? 'reconnect budget exhausted',
        });
      }
    },
    onSubscriptionNotice: (notice) => {
      if (notice.type === 'error') {
        const code = notice.service_code;
        if (code === 'replay_gap' || code === 'invalid_cursor' || code === 'event_overflow') {
          // Watch ended server-side with an explicit gap/overflow: full
          // resync from the history snapshot (formal), not silent recovery.
          const { client: c, currentSession, sessionTitle } = store.getState();
          if (!c || c.getState() !== 'connected') return;
          store.setState({
            recoveryState: 'resync',
            recoveryDetail: `watch terminated (${code}); resyncing from history snapshot`,
          });
          void attachToSession(currentSession, sessionTitle).then(() => {
            store.setState((s) =>
              s.currentSession.project_id === currentSession.project_id &&
              s.currentSession.thread_id === currentSession.thread_id
                ? { recoveryState: 'resync', recoveryDetail: `resynced after ${code}` }
                : {},
            );
          });
        } else {
          store.setState({
            recoveryState: 'failed',
            recoveryDetail: `watch error: ${code ?? 'unknown'}`,
          });
        }
      } else if (notice.type === 'complete') {
        // Server closed the subscription (e.g. session closed). Detach the
        // watch bookkeeping without cancelling the session (watch.detach is
        // deliberately not a cancel) and keep the transcript as-is.
        store.setState({ activeSubscriptionId: null, recoveryState: 'idle' });
      }
    },
    onEvent: (event, meta) => {
      const activeId = store.getState().activeSubscriptionId;
      const subId = meta?.subscription_id;
      if (activeId !== null && subId !== undefined && subId !== activeId) {
        // Stale event from a subscription that was replaced while switching sessions.
        return;
      }
      store.setState((s) => {
        if (s.historyLoading || activeId === null) {
          // History page not yet applied (or watch handshake in flight): hold the
          // event and merge it after the snapshot lands so nothing is dropped and
          // history is never over-written by live deltas. The buffer is bounded:
          // once full, the oldest events are dropped and the drop is observable.
          const next = [...s.liveEventBuffer, { event, subscription_id: subId }];
          const dropped = next.length - MAX_LIVE_BUFFER;
          return dropped > 0
            ? {
                liveEventBuffer: next.slice(dropped),
                liveBufferDroppedCount: s.liveBufferDroppedCount + dropped,
              }
            : { liveEventBuffer: next };
        }
        return reduceRuntimeEvent(s, event);
      });
    },
  });

  store.setState({
    client,
    pairingState: 'paired',
    pairingError: null,
    rpcBlockedReason: null,
    connectionState: 'connecting',
    runtimeDiagnostics: RUNTIME_DIAGNOSTICS_IDLE,
    workspacePath: project.workspace_path,
    // No silent fallback to a previously rendered branch: the value is exactly
    // what the authenticated host reported (empty when it reported none).
    gitBranch: project.git_branch ?? '',
    currentSession: { project_id: project.project_id, thread_id: '' },
    sessionTitle: '',
    sessions: [],
    sessionsNextOffset: null,
    sessionsTotal: 0,
    sessionQuery: '',
    projects: [],
    activeProjectId: project.project_id,
    expandedProjectIds: [project.project_id],
    projectSessions: {},
    loadingProjectIds: [],
    messages: [],
    activeTurnId: null,
    runtimeStatus: 'idle',
    steerQueueCount: 0,
    pendingApproval: null,
    activity: null,
    usage: null,
    metricsLabel: '',
    goal: null,
    thinkingLevelError: null,
    projectThinkingLevel: null,
    canSetProjectThinking: false,
    projectThinkingError: null,
  });
  void connectAuthenticatedRuntime(client, project, epoch);
}

/**
 * Connect the authenticated transport and attach the project's most recent
 * session.  When the project has no session yet, a fresh one is created through
 * the runtime instead of attaching to a hardcoded placeholder thread id.
 */
async function connectAuthenticatedRuntime(
  client: SynapseRuntimeClient,
  project: ConsoleProject,
  epoch: number,
): Promise<void> {
  const store = useConsoleStore;
  try {
    await client.connect();
  } catch (err) {
    console.warn('Runtime transport connect note:', err);
    // Safety net for a connect that failed without any state-change callback
    // (e.g. the socket constructor itself threw).  The diagnostics action is
    // single-flight, so the usual path (onStateChange above) is not duplicated.
    void store.getState().loadRuntimeDiagnostics({
      trigger: 'connect_failed',
      detail: describeError(err),
    });
    return;
  }
  if (epoch !== authEpoch || store.getState().client !== client) return;
  // The switchable project list is a read-only host endpoint, not a runtime RPC:
  // it is fetched once per pairing (and re-fetched on demand from the sidebar).
  void store.getState().loadProjects();
  await store.getState().fetchSessions();
  if (epoch !== authEpoch || store.getState().client !== client) return;
  const first = store.getState().sessions[0];
  if (first) {
    await attachToSession(
      { project_id: project.project_id, thread_id: first.thread_id },
      first.title,
    );
  } else {
    await store.getState().createNewSession();
  }
}

/**
 * One authentication attempt: probe the session cookie and, when the host
 * answers 401, stop at the explicit pairing gate.  Every other failure is a
 * visible terminal error state with its reason (C3) — never a silent fallback.
 */
async function runConsoleInit(): Promise<void> {
  const epoch = authEpoch;
  const store = useConsoleStore;
  store.setState({
    pairingState: 'checking',
    pairingError: null,
    rpcBlockedReason: null,
    connectionState: 'connecting',
  });
  let session: ConsoleSession;
  try {
    session = await fetchConsoleSession();
  } catch (err) {
    if (epoch !== authEpoch) return;
    if (err instanceof ConsoleAuthRequiredError) {
      store.setState({
        pairingState: 'unpaired',
        connectionState: 'disconnected',
        pairingError: null,
      });
      return;
    }
    store.setState({
      pairingState: 'error',
      connectionState: 'error',
      pairingError: describeError(err),
    });
    return;
  }
  if (epoch !== authEpoch) return;
  startAuthenticatedRuntime(session.project, epoch);
}

interface ConsoleStore {
  // Connection
  connectionState: ConnectionState;
  client: SynapseRuntimeClient | null;
  initClient: () => void;

  // Browser pairing / session authentication (phase-5 A1 contract).  The
  // console performs no runtime RPC and opens no socket until this reaches
  // 'paired'; every other value is an explicit, visible state.
  pairingState: PairingState;
  pairingError: string | null;
  /** Submit the host pairing code; resolves true only when the session is live. */
  submitPairingCode: (code: string) => Promise<boolean>;
  /** Invalidate the session server-side and return to the pairing gate. */
  logoutConsole: () => Promise<void>;
  /** Diagnosable reason the last business RPC was blocked (null when allowed). */
  rpcBlockedReason: string | null;

  // Recovery (phase-4): observable connection recovery state, never silent.
  recoveryState:
    | 'idle'
    | 'reconnecting'
    | 'resuming'
    | 'resumed'
    | 'resync'
    | 'incomplete'
    | 'unknown'
    | 'failed';
  recoveryDetail: string | null;

  // Read-only runtime diagnostics (phase-5 C3): the facts served by the host's
  // `GET /api/runtime-status`.  Read only on a relay failure path, only after a
  // successful pairing, and at most once per pairing lifetime (see the latch).
  runtimeDiagnostics: RuntimeDiagnosticsSnapshot;
  /** Read the host's read-only runtime diagnostics (gated, single-flight). */
  loadRuntimeDiagnostics: (options?: RuntimeDiagnosticsRequest) => Promise<void>;

  pendingApproval: PendingApprovalView | null;
  resolveApproval: (decision: 'allow_once' | 'reject_once') => Promise<void>;
  submitPrompt: (text: string) => Promise<void>;
  cancelActiveTurn: () => Promise<void>;
  /** Explicit user close: cancel reconnect budget and detach the watch. */
  closeRuntime: () => void;

  // Layout
  isSidebarCollapsed: boolean;
  toggleSidebar: () => void;

  // Workspace & Branch
  workspacePath: string;
  gitBranch: string;
  gitDirty: boolean;

  // Session
  currentSession: SessionRef;
  sessionTitle: string;
  sessions: SessionItem[];
  switchSession: (threadId: string, title?: string) => Promise<void>;
  loadSessionHistory: (session: SessionRef) => Promise<void>;
  loadEarlierHistory: () => Promise<void>;
  createNewSession: () => Promise<void>;
  fetchSessions: () => Promise<void>;
  // Session list pagination (explicit paging; no unbounded auto paging).
  sessionsNextOffset: number | null;
  sessionsTotal: number;
  sessionsLoading: boolean;
  loadMoreSessions: () => Promise<void>;
  /** Sidebar search text; only the sessions loaded so far are searched. */
  sessionQuery: string;
  setSessionQuery: (query: string) => void;
  /** Monotonic signal asking the sidebar to focus its search box (Ctrl+K). */
  searchFocusToken: number;
  requestSessionSearchFocus: () => void;

  // Multi-project sidebar (project -> session tree, mirroring the TUI drawer)
  projects: ConsoleProjectEntry[];
  /** Project the console is currently attached to. */
  activeProjectId: string;
  /** Projects whose session list is expanded in the sidebar. */
  expandedProjectIds: string[];
  /** Lazily fetched session lists for expanded, non-active projects. */
  projectSessions: Record<string, SessionItem[]>;
  loadingProjectIds: string[];
  loadProjects: () => Promise<void>;
  toggleProjectExpanded: (projectId: string) => Promise<void>;
  switchProject: (projectId: string, threadId?: string) => Promise<void>;
  /** Create a fresh session in a specific project (per-project "+" button). */
  createSessionInProject: (projectId: string) => Promise<void>;

  // History pagination / availability state.
  historyLoading: boolean;
  historyHasMore: boolean;
  historyAvailable: boolean | null;
  /** User-facing reason the last history read failed (null when it succeeded). */
  historyError: string | null;
  historyStartTurn: number;
  historyEndTurn: number;
  historyTotalTurns: number;

  // Live events that arrived while a history page was loading are buffered
  // here and merged after the page is applied (never dropped, never over-write).
  liveEventBuffer: Array<{ event: RuntimeEvent; subscription_id?: string }>;
  // Number of live events dropped (bounded buffer cap) while history loaded.
  liveBufferDroppedCount: number;
  // The subscription live events are currently attributed to (for stale-session
  // filtering while switching sessions).
  activeSubscriptionId: string | null;

  // MCP
  mcpServers: Array<{
    name: string;
    transport: string;
    enabled: boolean;
    toolPrefix?: string | null;
    attached?: boolean;
  }>;
  mcpEnabled: boolean;
  canSetThinking: boolean;
  canToggleMcpGlobal: boolean;
  toggleMcpServer: (serverName: string) => Promise<void>;
  toggleMcpGlobal: () => Promise<void>;

  // Active Turn & HITL
  activeTurnId: string | null;

  // Metrics
  metricsLabel: string;
  modelName: string;
  availableModels: string[];
  thinkingLevel: string | null;
  thinkingLevels: string[];
  setModel: (m: string) => Promise<void>;
  /**
   * Write one session's reasoning level. Resolves true only when the runtime
   * accepted and persisted it; a refusal or failure rolls the optimistic value
   * back and publishes a visible reason in `thinkingLevelError`.
   */
  setThinkingLevel: (l: string) => Promise<boolean>;
  /** User-facing reason the last reasoning-level write failed (null when fine). */
  thinkingLevelError: string | null;
  /**
   * The project's own default reasoning level, or null while the runtime has
   * not reported one. Never the session's level: a session may have rebound its
   * own, and the project default only applies to sessions opened afterwards.
   */
  projectThinkingLevel: string | null;
  /** Whether `runtime.project.thinking.set` exists on this peer. */
  canSetProjectThinking: boolean;
  /** User-facing reason the last project-default write failed (null when fine). */
  projectThinkingError: string | null;
  /**
   * Write the project's default reasoning level. Resolves true only when the
   * runtime persisted it; the current session is never rebound.
   */
  setProjectThinkingLevel: (l: string) => Promise<boolean>;
  fetchRuntimeConfig: () => Promise<void>;
  mcpStatus: string;
  runtimeStatus: 'idle' | 'running';
  /** Transient "what the agent is doing now" line, driven by activity_* events. */
  activity: ActivityView | null;
  /** Latest `usage_updated` metrics, also rendered as `metricsLabel`. */
  usage: UsageView | null;
  /** Current session's long-running goal, or null when it has none. */
  goal: SessionGoalView | null;

  // Timeline Transcript
  messages: TranscriptMessage[];
  steerQueueCount: number;
  addUserMessage: (text: string) => void;
  toggleMessageExpand: (id: string) => void;
}

// Monotonic epoch guarding every async session attach/load so a stale response
// from a previously switched-away session can never over-write the current one.
let sessionEpoch = 0;

// The session this store attached a live watch to (before any drop). Reconnect
// recovery only resumes the *same* (project_id, thread_id); a user switch while
// disconnected is already handled by its own attach flow.
let lastAttachedSession: { project_id: string; thread_id: string } | null = null;
let lastAttachedEpoch = 0;

/** Maximum number of live events held while a history page is loading. */
const MAX_LIVE_BUFFER = 2000;

function markAttached(session: SessionRef, epoch: number): void {
  lastAttachedSession = { project_id: session.project_id, thread_id: session.thread_id };
  lastAttachedEpoch = epoch;
}

function clearAttached(): void {
  lastAttachedSession = null;
  lastAttachedEpoch = 0;
  lastLiveEpoch = null;
  attachCoverage = null;
}

/** Broker epoch recorded when the pre-drop watch was attached (or null). */
let lastLiveEpoch: string | null = null;

/**
 * Durable coverage snapshot captured during the *current* attach. It is used
 * to drop buffered live events whose turn was already covered (durable) at the
 * snapshot instant: their content is rendered by the history page instead of
 * being replayed a second time. Cleared with every attach/switch.
 */
let attachCoverage: SessionRecoverabilityResult | null = null;


/**
 * Apply buffered live events that belong to the current subscription.
 *
 * When ``applyCoverageDedupe`` is true (the fresh-attach / full-resync path),
 * buffered events whose turn the attach snapshot already reports as durable
 * are dropped: the history page just rendered that turn, so replaying it would
 * duplicate content. Cursor-resume paths never dedupe (their replays are by
 * construction events the client has not seen).
 */
function flushBufferedLiveEvents(applyCoverageDedupe = false): void {
  const store = useConsoleStore;
  const state = store.getState();
  const activeId = state.activeSubscriptionId;
  if (activeId === null || state.liveEventBuffer.length === 0) return;
  const buffered = state.liveEventBuffer.filter(
    (entry) => entry.subscription_id === undefined || entry.subscription_id === activeId,
  );
  const pending = applyCoverageDedupe
    ? buffered.filter(
        (entry) => !isCoveredTurn(attachCoverage, entry.event.turn_id ?? null),
      )
    : buffered;
  store.setState({ liveEventBuffer: [] });
  for (const entry of pending) {
    store.setState((s) => reduceRuntimeEvent(s, entry.event));
  }
}

/**
 * Refresh the read-only runtime configuration for the currently attached
 * session through the RPC.  Stale responses (a newer attach/switch bumped the
 * session epoch or the project/thread changed while the request was in
 * flight) are discarded, and the session model resolved from `open.view` is
 * never over-written by a project-level default.
 */
async function refreshRuntimeConfig(epoch: number): Promise<void> {
  const store = useConsoleStore;
  const { client, currentSession } = store.getState();
  if (!client || client.getState() !== 'connected') return;
  const target = {
    project_id: currentSession.project_id,
    thread_id: currentSession.thread_id,
  };
  try {
    const view = await client.getRuntimeConfig({ session: currentSession });
    if (epoch !== sessionEpoch) return;
    const latest = store.getState();
    if (
      latest.currentSession.project_id !== target.project_id ||
      latest.currentSession.thread_id !== target.thread_id
    ) {
      return;
    }
    // preserveModel=true: the model resolved from open.view is authoritative
    // for the session; a config refresh must never reset it to the project default.
    store.setState(mapRuntimeConfig(view, { preserveModel: true }));
  } catch (err) {
    if (epoch === sessionEpoch) {
      console.warn('fetchRuntimeConfig note:', err);
    }
  }
}

/**
 * Refresh the attached session's long-running goal (`runtime.session.goal`).
 *
 * Read-only and non-fatal: a peer that predates the method, a thread without a
 * goal, and any transport failure all degrade to "no goal" instead of breaking
 * the attach that triggered the read.  Stale responses (a newer attach/switch
 * bumped the session epoch, or the project/thread changed while the request was
 * in flight) are discarded, mirroring `refreshRuntimeConfig`.
 */
async function refreshSessionGoal(epoch: number): Promise<void> {
  const store = useConsoleStore;
  const { client, currentSession } = store.getState();
  if (!client || client.getState() !== 'connected') return;
  const target = {
    project_id: currentSession.project_id,
    thread_id: currentSession.thread_id,
  };
  const stillCurrent = (): boolean => {
    const latest = store.getState().currentSession;
    return (
      latest.project_id === target.project_id && latest.thread_id === target.thread_id
    );
  };
  try {
    const payload = await client.getSessionGoal(currentSession);
    if (epoch !== sessionEpoch || !stillCurrent()) return;
    store.setState({ goal: parseSessionGoal(payload) });
  } catch (err) {
    if (epoch !== sessionEpoch || !stillCurrent()) return;
    console.warn('fetchSessionGoal note:', err);
    store.setState({ goal: null });
  }
}

/**
 * Open + attach the live watch for a session and load its newest history page.
 * Live events are buffered while history loads, then merged on top, so nothing
 * is dropped and history messages are never over-written by live deltas.
 * Watching starts from `open.view.latest_sequence` so already-completed turns
 * that history renders are never replayed and duplicated.
 */
async function attachToSession(session: SessionRef, title?: string): Promise<void> {
  const store = useConsoleStore;
  const client = store.getState().client;
  const epoch = ++sessionEpoch;
  clearAttached();
  store.setState({
    currentSession: session,
    sessionTitle: title || session.thread_id,
    messages: [],
    liveEventBuffer: [],
    liveBufferDroppedCount: 0,
    recoveryState: 'idle',
    recoveryDetail: null,
    historyLoading: true,
    historyHasMore: false,
    historyAvailable: null,
    historyError: null,
    historyStartTurn: 0,
    historyEndTurn: 0,
    historyTotalTurns: 0,
    activeSubscriptionId: null,
    activeTurnId: null,
    runtimeStatus: 'idle',
    steerQueueCount: 0,
    pendingApproval: null,
    activity: null,
    usage: null,
    metricsLabel: '',
    goal: null,
    thinkingLevelError: null,
    projectThinkingLevel: null,
    canSetProjectThinking: false,
    projectThinkingError: null,
  });
  if (!client) return;
  try {
    await client.unwatchEvents();
    if (epoch !== sessionEpoch) return;
    const opened = await client.openSession(session);
    if (epoch !== sessionEpoch) return;
    if (opened.view) {
      store.setState({
        runtimeStatus: opened.view.status === 'running' ? 'running' : 'idle',
        activeTurnId: opened.view.active_turn_id ?? null,
        modelName: opened.view.active_model || opened.view.model || store.getState().modelName,
      });
    }
    // Start watching at the session's current sequence: no full replay of
    // already-completed turns (they come from the history projection instead).
    const watch = await client.watchEvents(session, opened.view?.latest_sequence ?? 0);
    if (epoch !== sessionEpoch) return;
    store.setState({ activeSubscriptionId: watch.subscription_id });
    markAttached(session, epoch);
    await captureLiveEpoch(epoch);
    void refreshSessionGoal(epoch);
    await loadInitialHistory(epoch);
    await refreshRuntimeConfig(epoch);
  } catch (err) {
    if (epoch === sessionEpoch) {
      console.error('Failed to attach session:', err);
      store.setState({ historyLoading: false, activeSubscriptionId: null });
      clearAttached();
    }
  }
}

/**
 * Best-effort baseline capture of the live broker epoch after an attach.
 *
 * The stored epoch is what later resume logic compares against to detect that
 * the broker stream was replaced (session reopened / daemon restarted) while
 * the socket was down.  A peer that predates ``runtime.session.reconcile`` or
 * a transient read failure leaves a null baseline; resume then degrades to the
 * legacy bounded behavior and never fakes lossless recovery.
 */
async function captureLiveEpoch(epoch: number): Promise<void> {
  const store = useConsoleStore;
  const { client, currentSession } = store.getState();
  if (!client || client.getState() !== 'connected') return;
  try {
    // Probe the turn ids of events buffered while this attach was in flight so
    // the snapshot can also drive duplicate suppression at flush time.
    const buffered = store.getState().liveEventBuffer;
    const seen = new Set<string>();
    for (const entry of buffered) {
      const turnId = entry.event.turn_id;
      if (turnId) seen.add(turnId);
    }
    const activeTurnId = store.getState().activeTurnId;
    if (activeTurnId) seen.add(activeTurnId);
    const snapshot: SessionRecoverabilityResult = await client.reconcileSession({
      session: currentSession,
      probe_turn_ids: [...seen].slice(0, 32),
    });
    if (epoch === sessionEpoch) {
      lastLiveEpoch = snapshot.live_epoch;
      attachCoverage = snapshot;
    }
  } catch {
    if (epoch === sessionEpoch) {
      lastLiveEpoch = null;
      attachCoverage = null;
    }
  }
}

/**
 * Resume the watch for the session that was attached before the drop.
 *
 * Recovery semantics (phase-4):
 * - the watch resumes from the exact last delivered cursor kept by the client,
 *   so the server replay never duplicates already-rendered events and never
 *   silently skips events emitted while the socket was down;
 * - a stale cursor (server evicted retained events => `replay_gap` /
 *   `invalid_cursor`) is an explicit gap: the store falls back to a full
 *   resync from the authoritative history snapshot (`attachToSession`), which
 *   reloads the newest history page and only then watches from
 *   `open.view.latest_sequence`. `after=0` is never used to fake a recovery.
 * - a user switch during the outage is guarded by `lastAttachedSession` and
 *   the epoch; it never resumes the old session over the new one.
 */
async function resumeAttachedWatch(): Promise<void> {
  const store = useConsoleStore;
  const client = store.getState().client;
  const session = lastAttachedSession;
  if (!client || client.getState() !== 'connected' || !session) return;
  const epoch = lastAttachedEpoch;
  if (epoch === 0 || epoch !== sessionEpoch) return; // switched away meanwhile
  const current = store.getState().currentSession;
  if (current.project_id !== session.project_id || current.thread_id !== session.thread_id) {
    return; // the switch flow owns the new session's attach
  }
  const cursor = client.getWatchCursor();
  store.setState({ recoveryState: 'resuming', recoveryDetail: null });
  try {
    const opened = await client.openSession(session);
    if (epoch !== sessionEpoch) return;
    const fallbackAfter = opened.view?.latest_sequence ?? 0;
    // Formal recovery snapshot (phase-4A): ask the server for durable coverage
    // + live broker state before deciding whether the stored cursor is still
    // continuable. A peer that predates `runtime.session.reconcile` answers a
    // typed method/feature error and degrades to the legacy bounded cursor
    // behavior below - never a silent after=0 and never a fake lossless resume.
    let snapshot: SessionRecoverabilityResult | null = null;
    try {
      snapshot = await client.reconcileSession({ session });
    } catch (err) {
      const e = err as RpcCallError;
      const serviceCode = e?.service_code;
      const code = e?.code;
      const unsupported =
        serviceCode === 'method_not_found' ||
        serviceCode === 'invalid_request' ||
        serviceCode === 'malformed_reconcile_result' ||
        code === -32601;
      if (!unsupported) throw err; // transport / typed failures surface as failed
    }
    const decision = decideResumeAfterDrop({
      snapshot,
      baselineEpoch: lastLiveEpoch,
      cursor,
      fallbackAfter,
      hadEvents: cursor !== null,
    });
    if (decision.action === 'resume' || decision.action === 'legacy_resume') {
      const watch = await client.watchEvents(session, decision.after);
      if (epoch !== sessionEpoch) return;
      store.setState({
        activeSubscriptionId: watch.subscription_id,
        recoveryState: 'resumed',
      });
      if (snapshot !== null) lastLiveEpoch = snapshot.live_epoch;
      flushBufferedLiveEvents();
      void refreshRuntimeConfig(epoch);
      return;
    }
    // resync / incomplete: full resync from the authoritative history snapshot,
    // then keep the decision observable. Never a silent after=0.
    const title = store.getState().sessionTitle;
    const detail = decision.detail;
    await attachToSession(session, title);
    const latest = store.getState();
    if (
      latest.currentSession.project_id === session.project_id &&
      latest.currentSession.thread_id === session.thread_id
    ) {
      // attachToSession resets recoveryState to idle internally; publish the
      // explicit decision AFTER the snapshot lands so it stays observable.
      store.setState({
        recoveryState: decision.action === 'incomplete' ? 'incomplete' : 'resync',
        recoveryDetail: detail,
      });
    }
  } catch (err) {
    if (epoch !== sessionEpoch) return;
    const code = (err as RpcCallError)?.service_code;
    if (code === 'replay_gap' || code === 'invalid_cursor') {
      // Explicit gap surfaced by the watch handshake itself: full resync from
      // the history snapshot, then watch from the fresh latest sequence.
      const detail = `watch cursor ${cursor ?? 'n/a'} stale (${code}); resynced from history snapshot`;
      await attachToSession(session, store.getState().sessionTitle);
      const latest = store.getState();
      if (
        latest.currentSession.project_id === session.project_id &&
        latest.currentSession.thread_id === session.thread_id
      ) {
        store.setState({ recoveryState: 'resync', recoveryDetail: detail });
      }
    } else {
      store.setState({
        recoveryState: 'failed',
        recoveryDetail: String((err as Error)?.message ?? err),
      });
    }
  }
}

async function loadInitialHistory(epoch: number): Promise<void> {
  const store = useConsoleStore;
  const { client, currentSession } = store.getState();
  if (!client) return;
  const historyRef = { project_id: currentSession.project_id, thread_id: currentSession.thread_id };
  try {
    // A content-rich session can exceed the runtime's per-page cap; walk down
    // to a smaller page instead of leaving the transcript looking empty.
    const res = await readHistoryPage((limit) =>
      client.readSessionHistory(latestHistoryParams(historyRef, limit)),
    );
    if (epoch !== sessionEpoch) return;
    if (res.available) {
      store.setState({
        messages: mapHistoryEvents(res.events, { startTurn: res.start_turn, pageTag: 'latest' }),
        historyLoading: false,
        historyAvailable: true,
        historyError: null,
        historyHasMore: res.has_more,
        historyStartTurn: res.start_turn,
        historyEndTurn: res.end_turn,
        historyTotalTurns: res.total_turns,
      });
    } else {
      // No transcript projection: show an explicit unavailable state instead of
      // pretending the conversation is empty; never fall back to a checkpoint.
      store.setState({
        historyLoading: false,
        historyAvailable: false,
        historyError: null,
        historyHasMore: false,
        historyStartTurn: 0,
        historyEndTurn: 0,
        historyTotalTurns: 0,
      });
    }
    // Fresh-attach flush: drop buffered events whose turn is already durable
    // in the attach snapshot (history page renders them; no duplicate replay).
    flushBufferedLiveEvents(true);
  } catch (err) {
    if (epoch === sessionEpoch) {
      console.error('loadSessionHistory error:', err);
      // Never leave the user staring at an empty transcript with no reason.
      store.setState({
        historyLoading: false,
        historyError: describeHistoryFailure(err),
      });
      flushBufferedLiveEvents(true);
    }
  }
}

/**
 * Fetch one project's session page for the sidebar tree.
 *
 * Only *expanded, non-active* projects are fetched this way — the active
 * project's list already lives in `sessions` — so opening the console never
 * fans out into one RPC per registered project.
 */
async function loadProjectSessions(projectId: string): Promise<void> {
  const store = useConsoleStore;
  const client = requireRuntimeClient();
  if (!client) return;
  store.setState((s) => ({ loadingProjectIds: [...s.loadingProjectIds, projectId] }));
  try {
    const view = toSessionListView(await client.listSessions({ project_id: projectId }));
    store.setState((s) => ({
      projectSessions: { ...s.projectSessions, [projectId]: view.items },
      loadingProjectIds: s.loadingProjectIds.filter((id) => id !== projectId),
    }));
  } catch (err) {
    console.error('Failed to load project sessions:', err);
    store.setState((s) => ({
      loadingProjectIds: s.loadingProjectIds.filter((id) => id !== projectId),
    }));
  }
}

/**
 * Point the console at another project: header context, session list and a
 * cleared transcript, without attaching to any session (callers decide what to
 * open next).  Returns `false` when a newer switch superseded this one, so a
 * caller never attaches a session to the wrong project.
 */
async function activateProject(projectId: string): Promise<boolean> {
  const store = useConsoleStore;
  if (!requireRuntimeClient()) return false;
  if (projectId === store.getState().activeProjectId) return true;
  const entry = store.getState().projects.find((item) => item.project_id === projectId);
  // Keep the project we are leaving browsable: its loaded page moves into the
  // per-project cache instead of vanishing with the active list.
  const previousProjectId = store.getState().activeProjectId;
  const previousSessions = store.getState().sessions;
  const cachedSessions = { ...store.getState().projectSessions };
  if (previousProjectId !== '' && previousSessions.length > 0) {
    cachedSessions[previousProjectId] = previousSessions;
  }
  // Expanding the project we just moved to keeps the result of the switch (or of
  // the per-project "+") visible instead of leaving a collapsed row behind.
  const expanded = store.getState().expandedProjectIds;
  // The header context follows the switch, and the transcript is cleared in the
  // same update so the previous project's conversation is never shown under the
  // new project's header.  `fetchSessions`/`createNewSession` read
  // `currentSession.project_id`, so it must be set before they run.
  store.setState({
    activeProjectId: projectId,
    projectSessions: cachedSessions,
    expandedProjectIds: expanded.includes(projectId) ? expanded : [...expanded, projectId],
    workspacePath: entry?.workspace_path ?? '',
    gitBranch: entry?.git_branch ?? '',
    gitDirty: false,
    currentSession: { project_id: projectId, thread_id: '' },
    sessionTitle: '',
    sessions: [],
    sessionsNextOffset: null,
    sessionsTotal: 0,
    messages: [],
    activeTurnId: null,
    runtimeStatus: 'idle',
    steerQueueCount: 0,
    pendingApproval: null,
    activity: null,
    usage: null,
    metricsLabel: '',
    goal: null,
    thinkingLevelError: null,
    projectThinkingLevel: null,
    canSetProjectThinking: false,
    projectThinkingError: null,
    historyLoading: false,
    historyHasMore: false,
    historyAvailable: null,
    historyError: null,
    activeSubscriptionId: null,
  });
  await store.getState().fetchSessions();
  return store.getState().activeProjectId === projectId;
}

export const useConsoleStore = create<ConsoleStore>((set, get) => ({
  connectionState: 'disconnected',
  recoveryState: 'idle',
  recoveryDetail: null,
  runtimeDiagnostics: RUNTIME_DIAGNOSTICS_IDLE,
  client: null,
  // Unauthenticated until proven otherwise: no project, no session, no socket.
  pairingState: 'checking',
  pairingError: null,
  rpcBlockedReason: null,
  activeSubscriptionId: null,
  liveEventBuffer: [],
  liveBufferDroppedCount: 0,
  historyLoading: false,
  historyHasMore: false,
  historyAvailable: null,
  historyError: null,
  historyStartTurn: 0,
  historyEndTurn: 0,
  historyTotalTurns: 0,
  sessionsNextOffset: null,
  sessionsTotal: 0,
  sessionsLoading: false,
  sessionQuery: '',
  searchFocusToken: 0,
  projects: [],
  activeProjectId: '',
  expandedProjectIds: [],
  projectSessions: {},
  loadingProjectIds: [],
  isSidebarCollapsed: false,
  toggleSidebar: () => set((s) => ({ isSidebarCollapsed: !s.isSidebarCollapsed })),

  workspacePath: '',
  gitBranch: '',
  gitDirty: false,

  // Explicitly empty until the host reports the authenticated project context.
  currentSession: {
    project_id: '',
    thread_id: '',
  },
  createNewSession: async () => {
    const client = requireRuntimeClient();
    if (!client) return;
    const { currentSession } = get();
    // Generate clean 12-char hex session thread_id (matching Synapse standard format)
    const chars = '0123456789abcdef';
    let newThreadId = '';
    for (let i = 0; i < 12; i++) {
      newThreadId += chars[Math.floor(Math.random() * chars.length)];
    }
    const newTitle = `新会话 ${newThreadId.slice(0, 6)}`;
    const nextSession: SessionRef = {
      project_id: currentSession.project_id,
      thread_id: newThreadId,
    };
    const newItem: SessionItem = {
      thread_id: newThreadId,
      title: newTitle,
      updated_at: new Date().toISOString(),
      time_label: '刚刚',
    };
    const epoch = ++sessionEpoch;
    set((s) => ({
      sessions: [newItem, ...s.sessions],
      // The session exists server-side the moment it is opened, so the total has
      // to follow the prepended row (otherwise the footer reads "loaded 7 / 6").
      sessionsTotal: s.sessionsTotal + 1,
      currentSession: nextSession,
      sessionTitle: newTitle,
      messages: [],
      liveEventBuffer: [],
      liveBufferDroppedCount: 0,
      recoveryState: 'idle',
      recoveryDetail: null,
      historyLoading: false,
      historyHasMore: false,
      historyAvailable: null,
      historyError: null,
      historyStartTurn: 0,
      historyEndTurn: 0,
      historyTotalTurns: 0,
      activeSubscriptionId: null,
      activeTurnId: null,
      runtimeStatus: 'idle',
      steerQueueCount: 0,
      pendingApproval: null,
      activity: null,
      usage: null,
      metricsLabel: '',
      thinkingLevelError: null,
    projectThinkingLevel: null,
    canSetProjectThinking: false,
    projectThinkingError: null,
    }));
    if (client) {
      try {
        await client.unwatchEvents();
        if (epoch !== sessionEpoch) return;
        const opened = await client.openSession(nextSession);
        if (epoch !== sessionEpoch) return;
        if (opened.view) {
          set({
            modelName: opened.view.active_model || opened.view.model || get().modelName,
          });
        }
        const watch = await client.watchEvents(nextSession, opened.view?.latest_sequence ?? 0);
        if (epoch !== sessionEpoch) return;
        set({ activeSubscriptionId: watch.subscription_id });
        markAttached(nextSession, epoch);
        await captureLiveEpoch(epoch);
        flushBufferedLiveEvents();
        await refreshRuntimeConfig(epoch);
      } catch (err) {
        if (epoch === sessionEpoch) {
          console.error('Failed to initialize new session with runtime:', err);
          clearAttached();
        }
      }
    }
  },
  fetchSessions: async () => {
    const client = requireRuntimeClient();
    if (!client) return;
    const { currentSession } = get();
    if (client.getState() !== 'connected') {
      await client.connect();
    }
    try {
      // Readable only after protocol negotiation has completed on this socket.
      const view = toSessionListView(
        await client.listSessions({ project_id: currentSession.project_id }),
      );
      set({
        sessions: view.items,
        sessionsNextOffset: view.next_offset,
        sessionsTotal: view.total,
      });
    } catch (e) {
      console.error('Failed to fetch sessions:', e);
    }
  },
  loadMoreSessions: async () => {
    const client = requireRuntimeClient();
    if (!client) return;
    const { currentSession, sessions, sessionsNextOffset, sessionsLoading } = get();
    if (sessionsLoading || sessionsNextOffset === null) return;
    set({ sessionsLoading: true });
    try {
      const view = toSessionListView(
        await client.listSessions({
          project_id: currentSession.project_id,
          offset: sessionsNextOffset,
        }),
      );
      set((s) => ({
        // A refresh that landed while this page was in flight wins: never append
        // a page onto a list it was not read from.
        sessions: s.sessions === sessions ? [...s.sessions, ...view.items] : s.sessions,
        sessionsNextOffset: view.next_offset,
        sessionsTotal: view.total,
        sessionsLoading: false,
      }));
    } catch (e) {
      console.error('Failed to load more sessions:', e);
      set({ sessionsLoading: false });
    }
  },
  setSessionQuery: (query) => set({ sessionQuery: query }),
  requestSessionSearchFocus: () =>
    set((s) => ({ searchFocusToken: s.searchFocusToken + 1, isSidebarCollapsed: false })),
  loadProjects: async () => {
    if (get().pairingState !== 'paired') return;
    try {
      const projects = await fetchProjects();
      set({ projects });
    } catch (err) {
      console.error('Failed to load projects:', err);
    }
  },
  toggleProjectExpanded: async (projectId) => {
    const { expandedProjectIds, activeProjectId, projectSessions } = get();
    if (expandedProjectIds.includes(projectId)) {
      set({ expandedProjectIds: expandedProjectIds.filter((id) => id !== projectId) });
      return;
    }
    set({ expandedProjectIds: [...expandedProjectIds, projectId] });
    if (projectId === activeProjectId) return; // the active list is already loaded
    if (projectSessions[projectId] !== undefined) return; // served from cache
    await loadProjectSessions(projectId);
  },
  switchProject: async (projectId, threadId) => {
    if (!requireRuntimeClient()) return;
    if (projectId === get().activeProjectId) {
      if (threadId !== undefined) await get().switchSession(threadId);
      return;
    }
    if (!(await activateProject(projectId))) return; // a newer switch won
    const target = threadId ?? get().sessions[0]?.thread_id;
    if (target === undefined) {
      // Registered but never used: create its first session through the runtime.
      await get().createNewSession();
      return;
    }
    const title = get().sessions.find((item) => item.thread_id === target)?.title;
    await attachToSession({ project_id: projectId, thread_id: target }, title);
  },
  createSessionInProject: async (projectId) => {
    // Per-project "+" in the sidebar: point the console at that project first,
    // then open a fresh session in it — never a session in the project the
    // console happened to be attached to.
    if (projectId !== get().activeProjectId && !(await activateProject(projectId))) return;
    await get().createNewSession();
  },
  cancelActiveTurn: async () => {
    const client = requireRuntimeClient();
    if (!client) return;
    const { currentSession, activeTurnId } = get();
    if (!activeTurnId) return;
    await client.cancelTurn({
      session: currentSession,
      expected_turn_id: activeTurnId,
      reason: 'User cancelled',
    });
    set({ runtimeStatus: 'idle' });
  },
  sessionTitle: '',
  sessions: [],
  switchSession: async (threadId, title) => {
    if (!requireRuntimeClient()) return;
    const { currentSession } = get();
    const nextSession: SessionRef = {
      project_id: currentSession.project_id,
      thread_id: threadId,
    };
    await attachToSession(nextSession, title);
  },
  loadSessionHistory: async (session: SessionRef) => {
    if (!requireRuntimeClient()) return;
    // Full (re)load of the newest page for a session, mirroring a fresh attach.
    await attachToSession(session, session.thread_id);
  },
  loadEarlierHistory: async () => {
    const client = requireRuntimeClient();
    if (!client) return;
    const { currentSession, historyLoading, historyHasMore, historyStartTurn } = get();
    if (historyLoading || !historyHasMore) return;
    const epoch = sessionEpoch;
    const historyRef = { project_id: currentSession.project_id, thread_id: currentSession.thread_id };
    set({ historyLoading: true });
    try {
      const res = await readHistoryPage((limit) =>
        client.readSessionHistory(earlierHistoryParams(historyRef, historyStartTurn, limit)),
      );
      if (epoch !== sessionEpoch) return;
      if (!res.available) {
        set({
          historyLoading: false,
          historyAvailable: false,
          historyError: null,
          historyHasMore: false,
        });
        return;
      }
      const earlier = mapHistoryEvents(res.events, {
        startTurn: res.start_turn,
        pageTag: `earlier-${res.start_turn}`,
      });
      set((s) => ({
        messages: [...earlier, ...s.messages],
        historyLoading: false,
        historyAvailable: true,
        historyError: null,
        historyHasMore: res.has_more,
        historyStartTurn: res.start_turn,
        historyEndTurn: res.end_turn,
        historyTotalTurns: res.total_turns,
      }));
      flushBufferedLiveEvents();
    } catch (err) {
      if (epoch === sessionEpoch) {
        console.error('loadEarlierHistory error:', err);
        set({ historyLoading: false, historyError: describeHistoryFailure(err) });
        flushBufferedLiveEvents();
      }
    }
  },

  // No demo metrics/model: every value here is filled from the runtime config
  // read (`runtime.config.get`) of the authenticated session.
  metricsLabel: '',
  modelName: '',
  availableModels: [],
  thinkingLevel: null,
  thinkingLevels: [],
  mcpStatus: '',
  mcpServers: [],
  mcpEnabled: false,
  canSetThinking: false,
  canToggleMcpGlobal: false,
  // The project default is unknown until the runtime reports it, and it is not
  // writable until the peer advertises the capability.
  projectThinkingLevel: null,
  canSetProjectThinking: false,
  projectThinkingError: null,
  runtimeStatus: 'idle',
  activity: null,
  usage: null,
  goal: null,
  thinkingLevelError: null,
  fetchRuntimeConfig: async () => {
    // RPC-backed read only; the session model is resolved from open.view and
    // preserved across the refresh (see refreshRuntimeConfig).
    if (!requireRuntimeClient()) return;
    await refreshRuntimeConfig(sessionEpoch);
  },
  setModel: async (m: string) => {
    const client = requireRuntimeClient();
    if (!client) return;
    const { currentSession, modelName } = get();
    if (m === modelName) return;
    const epoch = sessionEpoch;
    const target = {
      project_id: currentSession.project_id,
      thread_id: currentSession.thread_id,
    };
    set({ modelName: m });
    try {
      if (client.getState() !== 'connected') {
        await client.connect();
      }
      await client.openSession(currentSession);
      const result = await client.rebindSession({ session: currentSession, model: m });
      // Stale-response guard: a rebind that resolves after the user switched
      // sessions belongs to the previous session and must never overwrite the
      // newly attached session's model (mirrors refreshRuntimeConfig).
      if (epoch !== sessionEpoch) return;
      const latest = get().currentSession;
      if (
        latest.project_id !== target.project_id ||
        latest.thread_id !== target.thread_id
      ) {
        return;
      }
      set({ modelName: result.model });
    } catch (e) {
      if (epoch === sessionEpoch) {
        set({ modelName });
      }
      console.warn('Failed to switch runtime model:', e);
    }
  },
  toggleMcpServer: async (serverName: string) => {
    const client = requireRuntimeClient();
    if (!client) return;
    const { currentSession, mcpServers } = get();
    const epoch = sessionEpoch;
    const target = {
      project_id: currentSession.project_id,
      thread_id: currentSession.thread_id,
    };
    const current = mcpServers.find((server) => server.name === serverName);
    if (!current) return;
    const enabled = !current.enabled;
    try {
      const result = await client.reloadMcp({
        session: currentSession,
        server: serverName,
        enabled,
      });
      // Stale-response guard: never apply a reload result for a session that
      // was switched away while the RPC was in flight.
      if (epoch !== sessionEpoch) return;
      const latest = get().currentSession;
      if (
        latest.project_id !== target.project_id ||
        latest.thread_id !== target.thread_id
      ) {
        return;
      }
      const list = get().mcpServers.map((server) =>
        server.name === serverName
          ? { ...server, enabled: result.enabled, attached: result.attached }
          : server
      );
      set({
        mcpServers: list,
        mcpStatus: mcpStatusLabel(list, get().mcpEnabled),
      });
    } catch (error) {
      console.error('Failed to reload MCP server:', error);
      throw error;
    }
  },
  toggleMcpGlobal: async () => {
    // No real write path exists: the runtime config surface is read-only.
    if (!get().canToggleMcpGlobal) {
      console.warn('runtime config is read-only: global MCP toggling is unavailable');
    }
  },
  setThinkingLevel: async (l: string) => {
    // The capability flag is the backend's own answer to "does a write port
    // exist"; while it is false the console must refuse instead of pretending
    // a save could succeed.
    if (!get().canSetThinking) {
      console.warn('runtime config is read-only: thinking level is unavailable');
      return false;
    }
    const client = requireRuntimeClient();
    if (!client) return false;
    const { currentSession, thinkingLevel } = get();
    if (l === thinkingLevel) return true;
    const epoch = sessionEpoch;
    const target = {
      project_id: currentSession.project_id,
      thread_id: currentSession.thread_id,
    };
    const previous = thinkingLevel;
    // Optimistic: the level flips immediately, and a failure rolls it back with
    // a visible reason (never a silent no-op).
    set({ thinkingLevel: l, thinkingLevelError: null });
    try {
      if (client.getState() !== 'connected') {
        await client.connect();
      }
      // A session that was never opened has no binding to rebind.
      await client.openSession(currentSession);
      const result = await client.setThinkingLevel(currentSession, l);
      // Stale-response guard: a write that resolves after the user switched
      // sessions belongs to the previous session and must not touch the newly
      // attached one (mirrors setModel / refreshRuntimeConfig).
      if (epoch !== sessionEpoch) return false;
      const latest = get().currentSession;
      if (
        latest.project_id !== target.project_id ||
        latest.thread_id !== target.thread_id
      ) {
        return false;
      }
      set({
        ...mapRuntimeConfig(result.view, { preserveModel: true }),
        thinkingLevelError: null,
      });
      return true;
    } catch (e) {
      if (epoch === sessionEpoch) {
        const latest = get().currentSession;
        if (
          latest.project_id === target.project_id &&
          latest.thread_id === target.thread_id
        ) {
          set({ thinkingLevel: previous, thinkingLevelError: describeError(e) });
        }
      }
      console.warn('Failed to switch reasoning level:', e);
      return false;
    }
  },
  setProjectThinkingLevel: async (l: string) => {
    // The capability flag is the backend's own answer to "does the project-level
    // write port exist"; while it is false the console must refuse instead of
    // pretending a save could succeed.
    if (!get().canSetProjectThinking) {
      console.warn('project reasoning default is read-only: no write port');
      return false;
    }
    const client = requireRuntimeClient();
    if (!client) return false;
    const { currentSession, projectThinkingLevel } = get();
    const projectId = currentSession.project_id;
    if (!projectId) return false;
    if (l === projectThinkingLevel) return true;
    // No optimistic flip: the project default only changes once the runtime has
    // persisted it, so the dialog reports the *stored* value, never a guess.
    set({ projectThinkingError: null });
    try {
      if (client.getState() !== 'connected') {
        await client.connect();
      }
      const result = await client.setProjectThinkingLevel(projectId, l);
      // Stale-response guard: a write that resolves after the user switched
      // projects belongs to the previous project and must not be shown as the
      // newly activated project's default.
      if (get().currentSession.project_id !== projectId) return false;
      set({ projectThinkingLevel: result.level, projectThinkingError: null });
      return true;
    } catch (e) {
      if (get().currentSession.project_id === projectId) {
        set({ projectThinkingError: describeError(e) });
      }
      console.warn('Failed to set the project reasoning default:', e);
      return false;
    }
  },
  activeTurnId: null,
  pendingApproval: null,

  steerQueueCount: 0,
  messages: [],

  initClient: () => {
    const existing = get();
    if (existing.client || existing.pairingState === 'paired') {
      // Already authenticated: a repeated call must never open a second socket.
      return Promise.resolve();
    }
    if (initPromise) return initPromise;
    const attempt = runConsoleInit().finally(() => {
      if (initPromise === attempt) initPromise = null;
    });
    initPromise = attempt;
    return attempt;
  },
  submitPairingCode: async (code: string) => {
    const normalized = normalizePairingCode(code);
    if (normalized === null) {
      // Local pre-validation: a malformed code is never sent to the host.
      set({
        pairingState: 'unpaired',
        pairingError: 'pairing code must be 8 Crockford base32 characters (no I/L/O/U)',
      });
      return false;
    }
    const epoch = authEpoch;
    set({ pairingState: 'pairing', pairingError: null, rpcBlockedReason: null });
    let project: ConsoleProject;
    try {
      project = await pairConsole(normalized);
    } catch (err) {
      if (epoch !== authEpoch) return false;
      // Explicit failure state: still unauthenticated, still no socket, and the
      // reason is shown instead of falling back to a demo project.
      console.error('Console pairing failed:', err);
      set({
        pairingState: 'error',
        connectionState: 'error',
        pairingError: describeError(err),
      });
      return false;
    }
    if (epoch !== authEpoch) return false;
    startAuthenticatedRuntime(project, epoch);
    return true;
  },
  logoutConsole: async () => {
    const epoch = ++authEpoch;
    const { client } = get();
    try {
      await requestConsoleLogout();
    } catch (err) {
      // The server-side invalidation is best effort; the browser drops every
      // local trace regardless, so a failed logout can never leave a usable
      // console behind.
      console.warn('Console logout note:', err);
    }
    if (epoch !== authEpoch) return;
    if (client) client.disconnect();
    clearAttached();
    sessionEpoch++;
    // The next pairing gets a fresh diagnostics read and a fresh latch.
    resetRuntimeDiagnostics();
    set({
      client: null,
      pairingState: 'unpaired',
      pairingError: null,
      rpcBlockedReason: null,
      connectionState: 'disconnected',
      recoveryState: 'idle',
      recoveryDetail: null,
      runtimeDiagnostics: RUNTIME_DIAGNOSTICS_IDLE,
      activeSubscriptionId: null,
      liveEventBuffer: [],
      liveBufferDroppedCount: 0,
      workspacePath: '',
      gitBranch: '',
      gitDirty: false,
      currentSession: { project_id: '', thread_id: '' },
      sessionTitle: '',
      sessions: [],
      sessionsNextOffset: null,
      sessionsTotal: 0,
      sessionQuery: '',
      projects: [],
      activeProjectId: '',
      expandedProjectIds: [],
      projectSessions: {},
      loadingProjectIds: [],
      messages: [],
      activeTurnId: null,
      runtimeStatus: 'idle',
      steerQueueCount: 0,
      pendingApproval: null,
      activity: null,
      usage: null,
      metricsLabel: '',
      goal: null,
      thinkingLevelError: null,
    projectThinkingLevel: null,
    canSetProjectThinking: false,
    projectThinkingError: null,
      historyLoading: false,
      historyHasMore: false,
      historyAvailable: null,
      historyError: null,
      historyStartTurn: 0,
      historyEndTurn: 0,
      historyTotalTurns: 0,
      modelName: '',
      availableModels: [],
      thinkingLevel: null,
      thinkingLevels: [],
      mcpServers: [],
      mcpStatus: '',
      mcpEnabled: false,
      canSetThinking: false,
      canToggleMcpGlobal: false,
    });
  },
  closeRuntime: () => {
    // User-initiated close: cancel any pending reconnect budget and detach the
    // watch. This is a deliberate detach (watch.detach != cancel) — any running
    // daemon turn keeps executing; a later reconnect attaches again.
    const { client } = get();
    if (client) {
      client.disconnect();
    }
    clearAttached();
    set({
      connectionState: 'disconnected',
      recoveryState: 'idle',
      recoveryDetail: null,
      activeSubscriptionId: null,
    });
  },
  loadRuntimeDiagnostics: async (options) => {
    const trigger = options?.trigger ?? 'manual';
    const detail = options?.detail ?? null;
    // Gate (C3-2): an unpaired browser must never touch this endpoint.  The
    // pairing state is the same gate every other console call uses, so no new
    // authentication path is introduced here.
    if (get().pairingState !== 'paired') return;
    // Single-flight + once-per-pairing latch: a burst of failure triggers (or a
    // repeated failure) issues exactly one request.  Only an explicit user
    // refresh (`force`) reads again.
    if (runtimeDiagnosticsPromise) return runtimeDiagnosticsPromise;
    if (options?.force !== true && runtimeDiagnosticsAttempted) return;
    const epoch = runtimeDiagnosticsEpoch;
    const attempt = readRuntimeDiagnostics(trigger, detail, epoch).finally(() => {
      runtimeDiagnosticsAttempted = true;
      if (runtimeDiagnosticsPromise === attempt) runtimeDiagnosticsPromise = null;
    });
    runtimeDiagnosticsPromise = attempt;
    return attempt;
  },
  resolveApproval: async (kind: 'allow_once' | 'reject_once') => {
    const client = requireRuntimeClient();
    if (!client) return;
    const { currentSession, pendingApproval } = get();
    if (!pendingApproval) return;
    const decisions: ApprovalDecision[] = pendingApproval.actions.map(() => ({
      kind,
      message: null,
    }));
    try {
      await client.resumeApproval(currentSession, pendingApproval.turn_id, decisions);
      set({ pendingApproval: null });
    } catch (err) {
      console.error('Failed to resolve approval:', err);
    }
  },
  submitPrompt: async (text: string) => {
    // Refused before authentication: no local transcript mutation and no RPC.
    const client = requireRuntimeClient();
    if (!client) return;
    const { currentSession, runtimeStatus, activeTurnId } = get();
    get().addUserMessage(text);

    if (runtimeStatus === 'running' && activeTurnId) {
      set((s) => ({ steerQueueCount: s.steerQueueCount + 1 }));
      try {
        await client.steerTurn({
          session: currentSession,
          expected_turn_id: activeTurnId,
          text,
        });
      } catch (err) {
        if (err instanceof ConnectionLostError && err.unknownOutcome) {
          // The steer request may have reached the daemon before the drop; the
          // receipt is unknown. Never blindly re-send (duplicate steer).
          set({
            recoveryState: 'unknown',
            recoveryDetail: 'steer submitted during a connection drop; outcome unknown',
          });
        } else {
          throw err;
        }
      }
    } else {
      set({ runtimeStatus: 'running' });
      try {
        const opened = await client.openSession(currentSession);
        if (!get().activeSubscriptionId) {
          const watch = await client.watchEvents(currentSession, opened.view?.latest_sequence ?? 0);
          set({ activeSubscriptionId: watch.subscription_id });
        }
      } catch (err) {
        console.warn('Ensure open session note:', err);
      }
      try {
        const receipt = await client.submitTurn({
          session: currentSession,
          text,
        });
        if (receipt.turn_id) {
          set({ activeTurnId: receipt.turn_id, recoveryState: 'idle', recoveryDetail: null });
        }
      } catch (err) {
        if (err instanceof ConnectionLostError && err.unknownOutcome) {
          // The submit may have reached the daemon and started a turn; the
          // receipt was lost in the drop. Do NOT re-submit (duplicate tool
          // execution). Surface an explicit unknown and let the user query the
          // session state after reconnect.
          set({
            recoveryState: 'unknown',
            recoveryDetail: 'submit sent but connection dropped before the receipt; outcome unknown',
          });
        } else {
          set({ runtimeStatus: 'idle', recoveryState: 'failed', recoveryDetail: String((err as Error)?.message ?? err) });
          console.error('submit failed:', err);
        }
      }
    }
  },
  addUserMessage: (text: string) => {
    const newMsg: TranscriptMessage = {
      id: `usr-${Date.now()}`,
      type: 'user',
      timestamp: new Date().toLocaleTimeString().slice(0, 5),
      content: text,
    };
    set((s) => ({ messages: [...s.messages, newMsg] }));
  },
  toggleMessageExpand: (id) =>
    set((s) => ({ messages: s.messages.map((m) => (m.id === id ? { ...m, expanded: !m.expanded } : m)) })),
}));
