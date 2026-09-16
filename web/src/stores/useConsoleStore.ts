import { create } from 'zustand';
import { SynapseRuntimeClient } from '../client/SynapseRuntimeClient.ts';
import type { GitStatusView } from '../runtime-client/git.ts';
import {
  ConsoleAuthRequiredError,
  deriveRuntimeSocketUrl,
  fetchConsoleSession,
  normalizePairingCode,
  pairConsole,
  requestConsoleLogout,
} from '../client/bootstrap.ts';
import type { ConsoleProject, ConsoleSession } from '../client/bootstrap.ts';
import {
  ConnectionLostError,
  RpcCallError,
} from '../client/SynapseRuntimeClient.ts';
import type { ConnectionState } from '../client/SynapseRuntimeClient.ts';
import type {
  RuntimeEvent,
  SessionRef,
  ApprovalDecision,
  ProjectListItem,
  ListDirectoriesResult,
  ReloadMcpResult,
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
import { toWorkspacePath } from '../markdown/filePaths.ts';
import { bindWorkTurn } from './turnWork.ts';
import {
  clearTranscriptViews, readTranscriptViews, restoreTranscriptViews, saveTranscriptViews,
} from './transcriptCache.ts';
import { mapRuntimeConfig } from './runtimeConfigMapper.ts';
import {
  mcpRuntimePatch,
  mcpRuntimeStatusLabel,
} from './mcpRuntimeView.ts';
import type { McpRuntimeServerState } from './mcpRuntimeView.ts';
import { decideResumeAfterDrop } from './recoveryDecider.ts';
import type { ActivityView } from './liveEventReducer.ts';
import { isTurnTerminalKind } from './liveEventReducer.ts';
import type { PendingApproval } from './liveEventReducer.ts';
import {
  coalesceLiveEvents,
  DELTA_COALESCE_MS,
  foldLiveEvents,
  isCoalescibleDeltaKind,
  type LiveEventEntry,
} from './liveDeltaBatch.ts';
import type { UsageView } from './usageView.ts';
import { parseSessionUsage, type SessionUsage } from './usageView.ts';
import {
  AttachmentUploadCancelledError,
  readUploadSource,
  selectAttachmentCandidates,
  uploadAttachment,
} from '../runtime-client/attachments.ts';
import type { AttachmentUploadSource } from '../runtime-client/attachments.ts';
import type { TranscriptAttachment } from './historyAttachments.ts';
import {
  SESSION_TITLE_MAX,
  displaySessionTitle,
  isPlaceholderSessionTitle,
  normalizeSessionTitle,
  sessionTitleFrom,
} from './sessionList.ts';
import {
  GOAL_OBJECTIVE_MAX_CHARS,
  normalizeGoalObjective,
  parseSessionGoal,
  parseSessionGoalResult,
} from './goalView.ts';
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
 * Monotonic generation for the project-list enumeration
 * (`runtime.project.list`).  A newer `loadProjects` (sidebar refresh) supersedes
 * an in-flight one, and a logout / re-pairing bumps `authEpoch`; either way a
 * stale page can never be published into the next pairing's state.
 */
let projectsGeneration = 0;

/**
 * Hard cap on the pages one project enumeration reads.  `runtime.project.list`
 * is paginated (`PROJECT_LIST_PAGE_SIZE`, server cap 100) with monotonic
 * offsets, but the loop is still bounded so a malformed cursor can never turn
 * the console into an unbounded reader.
 */
const PROJECT_LIST_MAX_PAGES = 20;

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
        // Anything still queued for the display window belongs to the dead
        // subscription and is applied now, while it is still attributable.
        flushPendingDeltas();
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
        flushPendingDeltas();
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
      const buffering = store.getState().historyLoading || activeId === null;
      if (buffering) {
        // Holding this event for the snapshot is an ordering boundary: whatever
        // is still queued for the display window must land first.
        flushPendingDeltas();
        // History page not yet applied (or watch handshake in flight): hold the
        // event and merge it after the snapshot lands so nothing is dropped and
        // history is never over-written by live deltas. The buffer is bounded:
        // once full, the oldest events are dropped and the drop is observable.
        store.setState((s) => {
          const next = [...s.liveEventBuffer, { event, subscription_id: subId }];
          const dropped = next.length - MAX_LIVE_BUFFER;
          return dropped > 0
            ? {
                liveEventBuffer: next.slice(dropped),
                liveBufferDroppedCount: s.liveBufferDroppedCount + dropped,
              }
            : { liveEventBuffer: next };
        });
        return;
      }
      if (isCoalescibleDeltaKind(event.kind)) {
        // Streamed text: held for the display window and merged with the chunks
        // that arrive behind it instead of costing one store update each.
        queueDelta({ event, subscription_id: subId });
        return;
      }
      // Every other event is an ordering boundary for the deltas queued behind
      // it (a completed thought must close *after* its text has been applied).
      flushPendingDeltas();
      // The terminal-totals fold lives in `applyLiveEvents`, after the update, so
      // the reduction itself stays pure.
      applyLiveEvents([{ event, subscription_id: subId }]);
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
    sessionSearch: {
      query: '',
      items: [],
      total: 0,
      nextOffset: null,
      loading: false,
      error: null,
      generation: 0,
    },
    sessionActionError: null,
    sessionNotice: null,
    projects: [],
    activeProjectId: project.project_id,
    expandedProjectIds: [project.project_id],
    projectSessions: {},
    loadingProjectIds: [],
    messages: [],
    settledTurnIds: [],
    activeTurnId: null,
    runtimeStatus: 'idle',
    steerQueueCount: 0,
    pendingApproval: null,
    activity: null,
    usage: null,
    sessionUsage: null,
    metricsLabel: '',
    goal: null,
    goalBusy: false,
    goalActionError: null,
    goalNotice: null,
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
  // The switchable project list is a runtime RPC (`runtime.project.list`), read
  // once per pairing and re-read on demand from the sidebar.  The host's
  // `GET /api/projects` survives only as a deprecated compatibility route and is
  // never a business entry point (bootstrap is host identity/pairing only).
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

/**
 * One server-side metadata search of the active project.
 *
 * `query` is the trimmed text the last issued request used (`''` while the box is
 * empty), and `generation` fences the page: a response from an older query can
 * never overwrite a newer result set.
 */
export interface SessionSearchState {
  query: string;
  items: SessionItem[];
  total: number;
  nextOffset: number | null;
  loading: boolean;
  error: string | null;
  generation: number;
}

/**
 * User-facing text for a failed session management RPC.
 *
 * A busy session is the one failure the user can act on, and it is reported as
 * "stop the turn first" rather than as a generic error: the console never
 * cancels a running turn to force a delete through.
 */
function describeSessionActionError(error: unknown, fallback: string): string {
  if (error instanceof RpcCallError) {
    if (error.service_code === 'conflict') {
      return '该会话正在运行中，无法删除或修改：请先停止当前回合（不会自动取消）。';
    }
    if (error.service_code === 'not_found') return '会话不存在或已被删除。';
    if (error.service_code === 'permission_denied') return '没有权限执行该会话操作。';
    if (error.service_code === 'invalid_request') return '请求无效：请检查会话标题等参数。';
  }
  return fallback;
}

/**
 * User-facing text for a failed goal write.
 *
 * `conflict` is the one failure with a specific fix: either the goal was replaced
 * since it was read (refresh and retry) or an unfinished goal already exists
 * (`set` never overwrites one).  `not_found` means there is no goal to act on.
 */
function describeGoalActionError(error: unknown, fallback: string): string {
  if (error instanceof RpcCallError) {
    if (error.service_code === 'conflict') {
      return '目标状态已变化（可能已被替换或已存在未完成目标）：请刷新后重试。';
    }
    if (error.service_code === 'not_found') return '当前会话没有目标。';
    if (error.service_code === 'permission_denied') return '没有权限管理该会话的目标。';
    if (error.service_code === 'invalid_request') return '请求无效：请检查目标描述与 token 预算。';
  }
  return fallback;
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
  /**
   * Read the workspace's git status (`runtime.git.status`).
   *
   * Read-only and re-readable: the header chip and the git explorer both show
   * what it returns, and a workspace where git cannot answer keeps the last
   * known value rather than clearing the branch the host reported.
   */
  loadGitStatus: () => Promise<void>;

  pendingApproval: PendingApproval | null;
  resolveApproval: (decision: 'allow_once' | 'reject_once') => Promise<void>;
  submitPrompt: (text: string) => Promise<void>;
  cancelActiveTurn: () => Promise<void>;
  /** Explicit user close: cancel reconnect budget and detach the watch. */
  closeRuntime: () => void;

  // Image attachments of the composer (bound to the session that was active
  // when they were picked).  A session/project switch or a logout cancels the
  // in-flight uploads and aborts their partial bytes best-effort; finalized
  // refs are never deleted by the console.
  attachments: PendingAttachment[];
  /** Visible reason the last attachment pick/upload failed (null when fine). */
  attachmentError: string | null;
  /** Validate + upload picked/dropped files for the current session. */
  addAttachments: (sources: AttachmentUploadSource[]) => Promise<void>;
  /** Drop one composer row; an in-flight upload is cancelled (partial aborted). */
  removeAttachment: (localId: string) => void;
  /** Cancel every in-flight upload and clear the composer (switch / logout). */
  cancelAttachments: () => void;

  // Layout
  isSidebarCollapsed: boolean;
  toggleSidebar: () => void;

  // Workspace & Branch
  workspacePath: string;
  gitBranch: string;
  gitDirty: boolean;
  /** Live git status for the attached session's workspace, or null while unknown. */
  gitStatus: GitStatusView | null;

  // A file path the model wrote in its answer, opened by a click.  The path is
  // already normalised to workspace-relative POSIX; `requestId` makes a repeat
  // click on the same path re-open (and re-read) the file.
  fileViewer: { path: string; requestId: number } | null;
  openFileViewer: (rawPath: string) => void;
  closeFileViewer: () => void;

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
  /** Sidebar search text; it drives a server-side metadata search. */
  sessionQuery: string;
  setSessionQuery: (query: string) => void;
  /** Server-side metadata search of the active project (`runtime.session.search`). */
  sessionSearch: SessionSearchState;
  runSessionSearch: () => Promise<void>;
  loadMoreSessionSearch: () => Promise<void>;
  /** Last session-management failure (rename/delete), cleared on success. */
  sessionActionError: string | null;
  /** Explicit notice for the last session-management write (e.g. retained history). */
  sessionNotice: string | null;
  /** Dismiss the session-management banner (failure or notice). */
  dismissSessionAlert: () => void;
  /** Rename one session's title; returns whether the server accepted it. */
  renameSession: (threadId: string, title: string) => Promise<boolean>;
  /** Delete one session's metadata row and goal; returns whether it succeeded. */
  deleteSession: (threadId: string) => Promise<boolean>;
  /** Monotonic signal asking the sidebar to focus its search box (Ctrl+K). */
  searchFocusToken: number;
  requestSessionSearchFocus: () => void;

  // Multi-project sidebar (project -> session tree, mirroring the TUI drawer)
  projects: ProjectListItem[];
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
  /**
   * Register a host workspace directory as a project, refresh the switchable
   * list, switch to it and open a fresh session (the composer "+" action).
   * Resolves ``null`` on success, else the user-facing reason so the dialog can
   * stay open.
   */
  addProject: (workspacePath: string) => Promise<string | null>;
  /**
   * List one host directory's immediate sub-directories for the "add project"
   * picker (``runtime.fs.list``).  Rejects when the runtime is not ready or the
   * listing fails; the dialog surfaces the reason.
   */
  listDirectories: (path: string | null) => Promise<ListDirectoriesResult>;

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
  /**
   * Live MCP state per server, filled only from a reload result: the config
   * view never claims attachment.
   */
  mcpRuntime: Record<string, McpRuntimeServerState>;
  /** Warnings reported by the last MCP reload (connection failures included). */
  mcpWarnings: string[];
  /** True while the session's MCP attach/reload RPC is in flight. */
  mcpConnecting: boolean;
  /**
   * Whether a reload result has ever reported this session's MCP state. While
   * false the panel must not claim a server is attached *or* unattached.
   */
  mcpRuntimeKnown: boolean;
  /**
   * Attach/reload every enabled MCP server for the attached session (the TUI's
   * startup attach + `/mcp reload`) and refresh the reported runtime state.
   */
  refreshMcpRuntime: () => Promise<void>;
  /** Persist one server's tool whitelist and reconnect it. */
  saveMcpTools: (serverName: string, tools: string[]) => Promise<void>;
  toggleMcpGlobal: () => Promise<void>;

  // Active Turn & HITL
  activeTurnId: string | null;

  // Metrics
  metricsLabel: string;
  modelName: string;
  /**
   * Bumped whenever the runtime *confirms* a new model binding for the session.
   *
   * `setModel` publishes the target optimistically (the picker label must not lag
   * a round-trip), so `modelName` alone cannot tell a consumer whether the server
   * has caught up: a read issued right after the publish is answered by the
   * *previous* binding.  This counter is the confirmed-binding signal, so a
   * server-side reader can re-ask when the rebind actually lands.  It is not a
   * display value.
   */
  modelRevision: number;
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
  /**
   * Session-cumulative token totals, from `runtime.session.open`'s session view.
   *
   * Distinct from `usage` on purpose: `usage` is the *current turn's* telemetry
   * (speed, steps, TTFT), while this is what the whole conversation has spent so
   * far — the number the TUI prints in its bottom bar.
   */
  sessionUsage: SessionUsage | null;
  /**
   * Selected model's input context size (`runtime.config.get`), or `null`.
   *
   * The denominator of the context-occupancy share; without it the bar prints
   * the bare token count rather than inventing a fraction.
   */
  contextWindow: number | null;
  /** Current session's long-running goal, or null when it has none. */
  goal: SessionGoalView | null;
  /** True while a goal write (`runtime.session.goal.*`) is in flight. */
  goalBusy: boolean;
  /** User-facing reason the last goal write failed (null when fine). */
  goalActionError: string | null;
  /** Notice for the last goal write, e.g. that pausing asked a turn to stop. */
  goalNotice: string | null;
  /** Dismiss the goal alert (failure or notice). */
  dismissGoalAlert: () => void;
  /**
   * Create the session's goal (`runtime.session.goal.set`). Resolves true only
   * when the server accepted it; an unfinished goal is refused with a visible
   * reason instead of being silently replaced.
   */
  setGoal: (objective: string, tokenBudget?: number | null) => Promise<boolean>;
  /** Rewrite the current goal's objective (`runtime.session.goal.edit`). */
  editGoal: (objective: string) => Promise<boolean>;
  /**
   * Remove the current goal (`runtime.session.goal.clear`).  The optional
   * `expectedGoalId` binds the clear to the goal the caller displayed, so a goal
   * replaced since then is refused (`conflict`) instead of clearing the wrong
   * one; omitting it clears whatever goal is current.
   */
  clearGoal: (expectedGoalId?: string) => Promise<boolean>;
  /** Pause the current goal (`runtime.session.goal.pause`). */
  pauseGoal: () => Promise<boolean>;
  /** Resume the current goal (`runtime.session.goal.resume`, status-only). */
  resumeGoal: () => Promise<boolean>;

  // Timeline Transcript
  messages: TranscriptMessage[];
  settledTurnIds?: string[];
  steerQueueCount: number;
  addUserMessage: (text: string, attachments?: TranscriptAttachment[]) => void;
  toggleMessageExpand: (id: string) => void;
  toggleWorkExpand: (id: string) => void;
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

/**
 * Streamed text deltas waiting for the display window to close.
 *
 * Folding every chunk as it arrived made a fast reasoning stream pay a full store
 * update, a re-render of every subscriber and a Markdown re-parse of the whole
 * accumulated thought per chunk.  The queue is applied by `flushPendingDeltas`,
 * which is also called at every ordering boundary, so events are still folded in
 * arrival order.
 */
let pendingDeltas: LiveEventEntry[] = [];
let pendingDeltaTimer: ReturnType<typeof setTimeout> | null = null;

function cancelPendingDeltaTimer(): void {
  if (pendingDeltaTimer !== null) {
    clearTimeout(pendingDeltaTimer);
    pendingDeltaTimer = null;
  }
}

/**
 * Fold a run of live events into the store as one state update.
 *
 * A run of one entry is exactly the per-event fold the store did before, so
 * batching cannot change what a single event does.
 */
function applyLiveEvents(entries: readonly LiveEventEntry[]): void {
  if (entries.length === 0) return;
  const merged = coalesceLiveEvents(entries);
  useConsoleStore.setState((state) => {
    const next = foldLiveEvents(state, merged);
    return { ...next, messages: restoreTranscriptViews(next.messages, readTranscriptViews(state.currentSession)) };
  });
  let turnEnded = false;
  for (const entry of merged) {
    // A finished turn changes the session's cumulative totals.
    if (isTurnTerminalKind(entry.event.kind)) {
      foldTurnUsage(entry.event.payload);
      turnEnded = true;
    }
  }
  // A finished turn may also have changed the workspace (the agent edits files),
  // so re-read the read-only git chrome once per batch.  `loadGitStatus` is
  // best-effort and scoped to the current session, so a switch mid-flight is safe.
  if (turnEnded) void useConsoleStore.getState().loadGitStatus();
}

/** Apply every queued delta now: the display window closed, or an event needs the order kept. */
function flushPendingDeltas(): void {
  cancelPendingDeltaTimer();
  if (pendingDeltas.length === 0) return;
  const queued = pendingDeltas;
  pendingDeltas = [];
  const activeId = useConsoleStore.getState().activeSubscriptionId;
  applyLiveEvents(
    queued.filter(
      (entry) =>
        entry.subscription_id === undefined ||
        activeId === null ||
        entry.subscription_id === activeId,
    ),
  );
}

/** Drop queued deltas without applying them (the session they belong to is gone). */
function discardPendingDeltas(): void {
  cancelPendingDeltaTimer();
  pendingDeltas = [];
}

/** Hold one delta until the display window closes. */
function queueDelta(entry: LiveEventEntry): void {
  pendingDeltas.push(entry);
  if (pendingDeltaTimer === null) {
    pendingDeltaTimer = setTimeout(flushPendingDeltas, DELTA_COALESCE_MS);
  }
}

/**
 * One image in the composer, bound to the session that was active when it was
 * picked.  `uploading` rows are cancellable; `ready` rows carry the finalized
 * opaque attachment id that a submit references.
 */
export interface PendingAttachment {
  localId: string;
  name: string;
  mime: string;
  size: number;
  status: 'uploading' | 'ready' | 'failed';
  uploadedBytes: number;
  attachmentId: string | null;
  error: string | null;
  /**
   * The local pick this row came from, kept so the composer can show the image
   * *before* the turn is submitted (a `File`/`Blob` in the browser).
   *
   * This module stays DOM-free on purpose: it only holds the reference, while
   * `AttachmentPreview` creates and revokes the object URL.
   */
  source: AttachmentUploadSource;
}

/** Local id sequence for composer rows (never sent over the wire). */
let attachmentLocalSeq = 0;

/**
 * Rows the user removed / cancelled (or that a session switch abandoned).  The
 * in-flight upload polls this between chunks, so a cancel aborts the partial
 * upload best-effort instead of streaming the rest into a dead session.
 */
const cancelledAttachments = new Set<string>();

/** Whether any composer row is still streaming bytes. */
export function hasPendingUploads(pending: readonly PendingAttachment[]): boolean {
  return pending.some((entry) => entry.status === 'uploading');
}

/** Finalized attachment ids of the composer, in pick order (submit refs). */
export function attachmentRefsOf(pending: readonly PendingAttachment[]): string[] {
  const refs: string[] = [];
  for (const entry of pending) {
    if (entry.status === 'ready' && entry.attachmentId) refs.push(entry.attachmentId);
  }
  return refs;
}

/** Finalized composer rows as transcript metadata (live user message display). */
export function attachmentDisplaysOf(pending: readonly PendingAttachment[]): TranscriptAttachment[] {
  const out: TranscriptAttachment[] = [];
  for (const entry of pending) {
    if (entry.status !== 'ready' || !entry.attachmentId) continue;
    out.push({
      attachmentId: entry.attachmentId,
      imageId: null,
      name: entry.name,
      mime: entry.mime,
      size: entry.size,
      revision: null,
    });
  }
  return out;
}

/** Normalized MIME of one picked file (`image/jpg` stays as the server allows it). */
function normalizeAttachmentMime(mime: string): string {
  return (mime || '').split(';')[0].trim().toLowerCase();
}

/**
 * Stream one composer row, patching its progress into the store.
 *
 * A cancelled row (or one whose session was switched away) removes itself and
 * lets `uploadAttachment` abort the partial upload; a real failure keeps the
 * row with an explicit reason — the filename is never turned into prompt text.
 */
async function uploadPendingAttachment(
  entry: PendingAttachment,
  source: AttachmentUploadSource,
): Promise<void> {
  const store = useConsoleStore;
  const epoch = sessionEpoch;
  const session = store.getState().currentSession;
  const patch = (next: Partial<PendingAttachment>): void => {
    store.setState((state) => ({
      attachments: state.attachments.map((row) =>
        row.localId === entry.localId ? { ...row, ...next } : row,
      ),
    }));
  };
  const isGone = (): boolean =>
    epoch !== sessionEpoch || cancelledAttachments.has(entry.localId);
  try {
    const bytes = await readUploadSource(source);
    if (isGone()) throw new AttachmentUploadCancelledError();
    const client = store.getState().client;
    if (!client) throw new Error('runtime client unavailable');
    const finished = await uploadAttachment(client, {
      session,
      bytes,
      mime: entry.mime,
      displayName: entry.name,
      onProgress: (uploaded) => {
        if (isGone()) return;
        patch({ uploadedBytes: uploaded });
      },
      isCancelled: isGone,
    });
    if (epoch !== sessionEpoch) return; // the switch flow already dropped the row
    patch({ status: 'ready', attachmentId: finished.attachmentId, uploadedBytes: entry.size, error: null });
  } catch (err) {
    if (err instanceof AttachmentUploadCancelledError || epoch !== sessionEpoch) {
      store.setState((state) => ({
        attachments: state.attachments.filter((row) => row.localId !== entry.localId),
      }));
      return;
    }
    const reason = describeError(err);
    patch({ status: 'failed', error: reason });
    store.setState({ attachmentError: `附件「${entry.name}」上传失败：${reason}` });
  } finally {
    cancelledAttachments.delete(entry.localId);
  }
}

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
        (entry) => isTurnTerminalKind(entry.event.kind) ||
          entry.event.turn_id === state.activeTurnId ||
          !isCoveredTurn(attachCoverage, entry.event.turn_id ?? null),
      )
    : buffered;
  store.setState({ liveEventBuffer: [] });
  // Replayed as one run: the same events, one state update instead of one per
  // event, and the terminal-totals fold still happens for each of them.
  applyLiveEvents(pending);
}

/**
 * Add one finished turn's tokens to the session totals.
 *
 * The terminal payload carries that turn's own `input/output/cache` counts, and
 * the runtime accumulates exactly those in its own settle step, so folding them
 * here keeps the bar exact.  Re-asking `runtime.session.open` at this point does
 * *not* work: the wire event is emitted before the runtime has folded the turn
 * in, so the reply still carries the pre-turn totals.  The authoritative read
 * happens on attach (and again at the next submit), so a session used elsewhere
 * is re-synced rather than drifting.
 */
function foldTurnUsage(payload: unknown): void {
  if (payload === null || typeof payload !== 'object') return;
  const record = payload as Record<string, unknown>;
  const count = (key: string): number => {
    const value = record[key];
    return typeof value === 'number' && Number.isFinite(value) ? Math.trunc(value) : 0;
  };
  const input = count('input_tokens');
  const output = count('output_tokens');
  const cache = count('cache_tokens');
  if (input === 0 && output === 0 && cache === 0) return;
  useConsoleStore.setState((s) => {
    const base = s.sessionUsage ?? { input: 0, output: 0, cache: 0 };
    return {
      sessionUsage: {
        input: base.input + input,
        output: base.output + output,
        cache: base.cache + cache,
      },
    };
  });
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
    const latest = store.getState();
    if (
      latest.currentSession.project_id !== target.project_id ||
      latest.currentSession.thread_id !== target.thread_id
    ) {
      return;
    }
    // preserveModel=true: the model resolved from open.view is authoritative
    // for the session; a config refresh must never reset it to the project default.
    const patch = mapRuntimeConfig(view, { preserveModel: true });
    // The config projection only knows the enabled flags; keep the footer label
    // derived from the real attach state when one has already been reported.
    patch.mcpStatus = mcpRuntimeStatusLabel(
      patch.mcpServers ?? latest.mcpServers,
      patch.mcpEnabled ?? latest.mcpEnabled,
      latest.mcpRuntime,
      latest.mcpConnecting,
      latest.mcpRuntimeKnown,
    );
    store.setState(patch);
  } catch (err) {
    if (epoch === sessionEpoch) {
      console.warn('fetchRuntimeConfig note:', err);
    }
  }
}

/**
 * Merge one MCP reload result into the console state.
 *
 * `mcpRuntimeKnown` flips true here: from this point on the panel may say
 * "已连接" or "未连接". A per-server reload still reports every configured
 * server, so a merge (not a replace) keeps the map complete.
 */
function applyMcpResult(result: ReloadMcpResult): void {
  const store = useConsoleStore;
  const patch = mcpRuntimePatch(result);
  const state = store.getState();
  const runtime = { ...state.mcpRuntime, ...patch.servers };
  store.setState({
    mcpRuntime: runtime,
    mcpWarnings: patch.warnings,
    mcpConnecting: false,
    mcpRuntimeKnown: true,
    mcpServers: state.mcpServers.map((server) => {
      const live = runtime[server.name];
      return live === undefined ? server : { ...server, attached: live.attached };
    }),
    mcpStatus: mcpRuntimeStatusLabel(
      state.mcpServers,
      state.mcpEnabled,
      runtime,
      false,
      true,
    ),
  });
}

/**
 * Enter the "connecting" phase.
 *
 * The footer label is recomputed here, not only when the result arrives: while
 * the attach RPC runs the console must say 启动中 (the TUI-style startup state)
 * instead of showing the previous label as if nothing were happening.
 */
function beginMcpConnect(): void {
  const store = useConsoleStore;
  const state = store.getState();
  store.setState({
    mcpConnecting: true,
    mcpStatus: mcpRuntimeStatusLabel(
      state.mcpServers,
      state.mcpEnabled,
      state.mcpRuntime,
      true,
      state.mcpRuntimeKnown,
    ),
  });
}

/** Leave the connecting phase after a failure, keeping the label truthful. */
function failMcpConnect(reason: string): void {
  const store = useConsoleStore;
  const state = store.getState();
  store.setState({
    mcpConnecting: false,
    mcpWarnings: [reason],
    mcpStatus: mcpRuntimeStatusLabel(
      state.mcpServers,
      state.mcpEnabled,
      state.mcpRuntime,
      false,
      state.mcpRuntimeKnown,
    ),
  });
}

/**
 * Attach/reload the attached session's MCP servers and record the real state.
 *
 * This is the console's equivalent of the TUI's startup attach: enabled servers
 * render as 启动中 while the RPC runs, and the result decides between 已连接 and
 * 未连接 (plus any warning the daemon reported, e.g. a failed connection). The
 * call is non-fatal — a peer without the method, a refused call and a transport
 * failure all degrade to a visible warning instead of breaking the attach.
 */
async function refreshMcpRuntime(epoch: number): Promise<void> {
  const store = useConsoleStore;
  const { client, currentSession, mcpEnabled, mcpServers } = store.getState();
  if (!client || client.getState() !== 'connected') return;
  if (!mcpEnabled || !mcpServers.some((server) => server.enabled)) return;
  const target = {
    project_id: currentSession.project_id,
    thread_id: currentSession.thread_id,
  };
  beginMcpConnect();
  try {
    const result = await client.reloadMcp({ session: currentSession });
    if (epoch !== sessionEpoch) return;
    const latest = store.getState();
    if (
      latest.currentSession.project_id !== target.project_id ||
      latest.currentSession.thread_id !== target.thread_id
    ) {
      return;
    }
    applyMcpResult(result);
  } catch (err) {
    if (epoch !== sessionEpoch) return;
    failMcpConnect(err instanceof Error ? err.message : String(err));
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
/**
 * Best-known title for a session being attached.
 *
 * Order: the caller's own title, the title already shown for the same thread,
 * then any row the sidebar has loaded.  Only when nothing is known does the raw
 * thread id stand in, so a refresh can never turn a real title back into an
 * internal id.
 */
function resolveSessionTitle(session: SessionRef, title?: string): string {
  if (title !== undefined && title !== '') return title;
  const state = useConsoleStore.getState();
  if (state.currentSession.thread_id === session.thread_id && state.sessionTitle !== '') {
    return state.sessionTitle;
  }
  const loaded: SessionItem[] = [
    ...state.sessions,
    ...Object.values(state.projectSessions).flat(),
    ...state.sessionSearch.items,
  ];
  // Nothing is known about the thread yet: show the console's own placeholder
  // label rather than a raw id.
  return sessionTitleFrom(loaded, session.thread_id) ?? displaySessionTitle('', session.thread_id);
}

/**
 * Read back a title the daemon may just have bound.
 *
 * A session's name comes from its first user message, derived server-side on
 * `runtime.turn.submit` (the same rule the TUI applies), so a session that was
 * still unnamed when the turn was accepted needs one list read to show its real
 * name in the header and the sidebar.  A session that already has a title is left
 * alone: the read is only worth paying while the row is still a placeholder.
 */
function refreshSessionTitleAfterTurn(session: SessionRef, shown: string): void {
  if (!isPlaceholderSessionTitle(shown, session.thread_id)) return;
  void useConsoleStore
    .getState()
    .fetchSessions()
    .then(() => {
      const state = useConsoleStore.getState();
      // The user may have switched sessions while the list was in flight.
      if (state.currentSession.thread_id !== session.thread_id) return;
      const bound = sessionTitleFrom(state.sessions, session.thread_id);
      if (bound !== null) useConsoleStore.setState({ sessionTitle: bound });
    })
    .catch((err: unknown) => {
      // A name is cosmetic: a failed read must never surface as a send failure.
      console.warn('Failed to refresh the session title:', err);
    });
}

async function attachToSession(session: SessionRef, title?: string): Promise<void> {
  const store = useConsoleStore;
  const client = store.getState().client;
  const epoch = ++sessionEpoch;
  // The composer is bound to the session being left: cancel any in-flight
  // upload (aborting its partial bytes best-effort) before the new attach.
  store.getState().cancelAttachments();
  clearAttached();
  // Queued deltas belong to the session being left: apply them before its
  // transcript (and its subscription) is replaced.
  flushPendingDeltas();
  store.setState({
    currentSession: session,
    sessionTitle: resolveSessionTitle(session, title),
    messages: [],
    settledTurnIds: [],
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
    // The previous session's totals must not linger while this one loads.
    sessionUsage: null,
    metricsLabel: '',
    goal: null,
    goalBusy: false,
    goalActionError: null,
    goalNotice: null,
    thinkingLevelError: null,
    projectThinkingLevel: null,
    canSetProjectThinking: false,
    projectThinkingError: null,
    // The runtime state belongs to the session being left: a new attach must
    // start from "unknown" until its own reload result arrives.
    mcpRuntime: {},
    mcpWarnings: [],
    mcpConnecting: false,
    mcpRuntimeKnown: false,
  });
  // Git chrome is per workspace and read-only: refresh it on every attach so a
  // switch cannot leave the previous session's tree on screen.
  void store.getState().loadGitStatus();
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
        // The session view carries the runtime's cumulative totals; the console
        // never accumulates them itself, so a reload cannot lose history.
        sessionUsage: parseSessionUsage(opened.view.usage),
      });
    }
    // Only an intact active turn may replay from its first event. Settled turns
    // come from history; never silently replay the entire session from zero.
    await captureLiveEpoch(epoch);
    if (epoch !== sessionEpoch) return;
    const coverage = attachCoverage as SessionRecoverabilityResult | null;
    const active = opened.view?.status === 'running' ? opened.view.active_turn_id : null;
    const replayActive = active && coverage?.latest_turn_id === active &&
      coverage.latest_turn_intact && coverage.latest_turn_first_sequence !== null;
    const after = replayActive ? coverage.latest_turn_first_sequence! - 1 : opened.view?.latest_sequence ?? 0;
    let watch;
    if (epoch !== sessionEpoch) return;
    try {
      watch = await client.watchEvents(session, after);
    } catch (err) {
      if (epoch !== sessionEpoch) return;
      const code = (err as RpcCallError)?.service_code;
      if (!replayActive || (code !== 'replay_gap' && code !== 'invalid_cursor')) throw err;
      const fresh = await client.openSession(session);
      if (epoch !== sessionEpoch) return;
      watch = await client.watchEvents(session, fresh.view?.latest_sequence ?? 0);
      store.setState({ recoveryState: 'incomplete', recoveryDetail: '运行轮次的早期事件已过期，部分步骤暂不可恢复。' });
    }
    if (epoch !== sessionEpoch) return;
    store.setState({ activeSubscriptionId: watch.subscription_id });
    markAttached(session, epoch);
    if (active && !replayActive) {
      store.setState({ recoveryState: 'incomplete', recoveryDetail: '运行轮次的早期步骤不可完整恢复；已保存历史不受影响。' });
    }
    void refreshSessionGoal(epoch);
    await loadInitialHistory(epoch);
    await refreshRuntimeConfig(epoch);
    // Same convention as the TUI: attach the configured MCP servers for this
    // session in the background, then report what actually got loaded. The
    // config flag alone must never be presented as "running".
    void refreshMcpRuntime(epoch);
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
      let messages = mapHistoryEvents(res.events, { startTurn: res.start_turn, pageTag: 'latest' });
      const state = store.getState();
      const active = state.runtimeStatus === 'running' ? state.activeTurnId : null;
      if (active && messages.some((m) => m.turnId === active)) {
        messages = messages.map((m) => m.turnId === active && m.work
          ? { ...m, work: { ...m.work, ended: false } } : m);
      }
      if (active && !messages.some((m) => m.turnId === active)) {
        // The projection is settlement-only: never attach active replay to the
        // previous history user. No invented prompt is stored in this placeholder.
        messages.push({
          id: `work-${active}`, type: 'info', timestamp: '', turnId: active,
          content: '当前轮次运行中', work: { ended: false },
        });
      }
      messages = restoreTranscriptViews(messages, readTranscriptViews(currentSession));
      store.setState({
        messages,
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
        messages: store.getState().activeTurnId ? restoreTranscriptViews([{
          id: `work-${store.getState().activeTurnId}`, type: 'info', timestamp: '',
          turnId: store.getState().activeTurnId!, content: '当前轮次历史尚未保存',
          work: { ended: false },
        }], readTranscriptViews(currentSession)) : [],
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
  // The composer belongs to the project being left; a cross-project switch must
  // not carry (or keep uploading) its images into the new project.
  store.getState().cancelAttachments();
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
  // The switch replaces the session as well: queued deltas are applied while
  // they are still attributable to the session being left.
  flushPendingDeltas();
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
    settledTurnIds: [],
    activeTurnId: null,
    runtimeStatus: 'idle',
    steerQueueCount: 0,
    pendingApproval: null,
    activity: null,
    usage: null,
    metricsLabel: '',
    goal: null,
    goalBusy: false,
    goalActionError: null,
    goalNotice: null,
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
  attachments: [],
  attachmentError: null,
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
  sessionSearch: {
    query: '',
    items: [],
    total: 0,
    nextOffset: null,
    loading: false,
    error: null,
    generation: 0,
  },
  sessionActionError: null,
  sessionNotice: null,
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
  gitStatus: null,

  fileViewer: null,
  openFileViewer: (rawPath) => {
    const path = toWorkspacePath(rawPath, get().workspacePath);
    if (path === null || path === '') return;
    set((s) => ({ fileViewer: { path, requestId: (s.fileViewer?.requestId ?? 0) + 1 } }));
  },
  closeFileViewer: () => set({ fileViewer: null }),

  // Explicitly empty until the host reports the authenticated project context.
  currentSession: {
    project_id: '',
    thread_id: '',
  },
  createNewSession: async () => {
    const client = requireRuntimeClient();
    if (!client) return;
    // A brand-new session starts with an empty composer: cancel any upload that
    // was still bound to the session being left.
    get().cancelAttachments();
    const { currentSession } = get();
    if (currentSession.project_id === '') return;
    // The server owns the identity *and* the metadata row: `runtime.session.create`
    // allocates the thread id and persists the row, and this console opens the
    // returned session afterwards.  Inventing an id locally and only calling
    // `runtime.session.open` produced a session the daemon had never stored, so
    // rename/delete/goal on it failed with `not_found` until its first turn
    // happened to insert the row.
    // No title is sent: the server stores its own placeholder, and the daemon
    // binds the real title from the first user message (`runtime.turn.submit`).
    // A locally invented name would be persisted as a real title and would block
    // that binding, which is exactly what left every session called "新会话 xxxx".
    const created = await client
      .createSession({ project_id: currentSession.project_id })
      .catch((err: unknown) => {
        console.error('Failed to create session:', err);
        set({ sessionActionError: describeSessionActionError(err, '新建会话失败') });
        return null;
      });
    if (created === null) return;
    const nextSession: SessionRef = created.session;
    // The server echoes its placeholder until the first turn names the session;
    // it is shown as this console's own label, and never written back.
    const newTitle = displaySessionTitle(created.title, nextSession.thread_id);
    const newItem: SessionItem = {
      thread_id: nextSession.thread_id,
      title: newTitle,
      updated_at: new Date().toISOString(),
      time_label: '刚刚',
    };
    const epoch = ++sessionEpoch;
    // The session is being replaced: apply whatever the display window still
    // holds before its transcript is cleared.
    flushPendingDeltas();
    set((s) => ({
      sessions: [
        newItem,
        ...s.sessions.filter((item) => item.thread_id !== nextSession.thread_id),
      ],
      // The row is persisted before it is shown, so the total follows it — but
      // only when the server really inserted it (`created` is false for an
      // idempotent re-create).
      sessionsTotal: created.created ? s.sessionsTotal + 1 : s.sessionsTotal,
      currentSession: nextSession,
      sessionTitle: newTitle,
      sessionActionError: null,
      messages: [],
      settledTurnIds: [],
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
            sessionUsage: parseSessionUsage(opened.view.usage),
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
          // The row is already persisted, so the session is not lost: say what
          // failed instead of leaving a console that silently never attaches.
          set({ sessionActionError: describeSessionActionError(err, '会话初始化失败') });
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
  setSessionQuery: (query) => {
    set({ sessionQuery: query });
    void get().runSessionSearch();
  },
  runSessionSearch: async () => {
    const client = requireRuntimeClient();
    const { currentSession, sessionQuery } = get();
    if (!client || currentSession.project_id === '') return;
    const query = sessionQuery.trim();
    if (query === '') {
      // An empty box returns to the plain list: no RPC is issued, and any page
      // still in flight from the previous query is fenced out by the bumped
      // generation instead of landing on the cleared state.
      set((s) => ({
        sessionSearch: {
          query: '',
          items: [],
          total: 0,
          nextOffset: null,
          loading: false,
          error: null,
          generation: s.sessionSearch.generation + 1,
        },
      }));
      return;
    }
    const generation = get().sessionSearch.generation + 1;
    set((s) => ({
      sessionSearch: { ...s.sessionSearch, query, loading: true, error: null, generation },
    }));
    try {
      const page = await client.searchSessions({
        project_id: currentSession.project_id,
        text: query,
      });
      // A stale page from an older query never overwrites the newer result set.
      if (get().sessionSearch.generation !== generation) return;
      const view = toSessionListView(page);
      set({
        sessionSearch: {
          query,
          items: view.items,
          total: view.total,
          nextOffset: view.next_offset,
          loading: false,
          error: null,
          generation,
        },
      });
    } catch (e) {
      if (get().sessionSearch.generation !== generation) return;
      console.error('Failed to search sessions:', e);
      set({
        sessionSearch: {
          query,
          items: [],
          total: 0,
          nextOffset: null,
          loading: false,
          error: '会话搜索失败',
          generation,
        },
      });
    }
  },
  loadMoreSessionSearch: async () => {
    const client = requireRuntimeClient();
    const { currentSession, sessionSearch } = get();
    if (!client || currentSession.project_id === '') return;
    if (sessionSearch.loading || sessionSearch.nextOffset === null) return;
    const generation = sessionSearch.generation;
    set({ sessionSearch: { ...sessionSearch, loading: true, error: null } });
    try {
      const page = await client.searchSessions({
        project_id: currentSession.project_id,
        text: sessionSearch.query,
        offset: sessionSearch.nextOffset,
      });
      const current = get().sessionSearch;
      if (current.generation !== generation) return;
      const view = toSessionListView(page);
      set({
        sessionSearch: {
          query: current.query,
          items: [...current.items, ...view.items],
          total: view.total,
          nextOffset: view.next_offset,
          loading: false,
          error: null,
          generation,
        },
      });
    } catch (e) {
      const current = get().sessionSearch;
      if (current.generation !== generation) return;
      console.error('Failed to load more search results:', e);
      set({ sessionSearch: { ...current, loading: false, error: '会话搜索失败' } });
    }
  },
  dismissSessionAlert: () => set({ sessionActionError: null, sessionNotice: null }),
  renameSession: async (threadId, title) => {
    const client = requireRuntimeClient();
    const { currentSession } = get();
    const normalized = normalizeSessionTitle(title);
    if (!client || currentSession.project_id === '') return false;
    if (normalized === null) {
      set({ sessionActionError: `标题需为 1-${SESSION_TITLE_MAX} 个字符（不能全为空白）。` });
      return false;
    }
    const session: SessionRef = { project_id: currentSession.project_id, thread_id: threadId };
    try {
      const result = await client.renameSession({ session, title: normalized });
      // The server echoes the stored title, which is what every list must show.
      const stored = result.title;
      const renamed = (item: SessionItem): SessionItem =>
        item.thread_id === threadId ? { ...item, title: stored } : item;
      set((s) => ({
        sessions: s.sessions.map(renamed),
        projectSessions: Object.fromEntries(
          Object.entries(s.projectSessions).map(([projectId, items]) => [
            projectId,
            items.map(renamed),
          ]),
        ),
        sessionSearch: { ...s.sessionSearch, items: s.sessionSearch.items.map(renamed) },
        sessionTitle:
          s.currentSession.thread_id === threadId ? stored : s.sessionTitle,
        sessionActionError: null,
      }));
      return true;
    } catch (e) {
      console.error('Failed to rename session:', e);
      set({ sessionActionError: describeSessionActionError(e, '重命名会话失败') });
      return false;
    }
  },
  deleteSession: async (threadId) => {
    const client = requireRuntimeClient();
    const { currentSession } = get();
    if (!client || currentSession.project_id === '') return false;
    const projectId = currentSession.project_id;
    const session: SessionRef = { project_id: projectId, thread_id: threadId };
    try {
      const result = await client.deleteSession({ session });
      // ``retained_history`` is always true today: only the metadata row and the
      // thread goal are gone.  The notice says exactly that instead of claiming
      // the conversation was erased.
      const notice = result.retained_history
        ? '已删除该会话的记录（元数据与目标）。对话历史（检查点与转录）仍保留在磁盘上，未被删除。'
        : '已删除该会话。';
      const without = (items: SessionItem[]): SessionItem[] =>
        items.filter((item) => item.thread_id !== threadId);
      set((s) => ({
        sessions: without(s.sessions),
        sessionsTotal: Math.max(0, s.sessionsTotal - 1),
        projectSessions: Object.fromEntries(
          Object.entries(s.projectSessions).map(([key, items]) => [key, without(items)]),
        ),
        sessionSearch: {
          ...s.sessionSearch,
          items: without(s.sessionSearch.items),
          total: Math.max(0, s.sessionSearch.total - 1),
        },
        sessionActionError: null,
        sessionNotice: notice,
      }));
      if (get().currentSession.thread_id === threadId) {
        // Never leave the console attached to a session that no longer exists:
        // switch to the next listed session, or create one (the existing path).
        const next = get().sessions[0];
        if (next) {
          await attachToSession({ project_id: projectId, thread_id: next.thread_id }, next.title);
        } else {
          await get().createNewSession();
        }
      }
      return true;
    } catch (e) {
      console.error('Failed to delete session:', e);
      set({ sessionActionError: describeSessionActionError(e, '删除会话失败') });
      return false;
    }
  },
  dismissGoalAlert: () => set({ goalActionError: null, goalNotice: null }),
  setGoal: async (objective, tokenBudget) => {
    const client = requireRuntimeClient();
    const { currentSession } = get();
    if (!client || currentSession.project_id === '') return false;
    const text = normalizeGoalObjective(objective);
    if (text === null) {
      set({
        goalActionError: `目标描述需为 1-${GOAL_OBJECTIVE_MAX_CHARS} 个字符（不能全为空白）。`,
      });
      return false;
    }
    if (tokenBudget !== null && tokenBudget !== undefined && !Number.isSafeInteger(tokenBudget)) {
      set({ goalActionError: 'token 预算需为正整数。' });
      return false;
    }
    set({ goalBusy: true });
    try {
      const result = await client.setSessionGoal({
        session: currentSession,
        objective: text,
        token_budget: tokenBudget ?? null,
      });
      const parsed = parseSessionGoalResult(result);
      set({ goal: parsed.goal, goalBusy: false, goalActionError: null, goalNotice: '已设置目标。' });
      return true;
    } catch (e) {
      console.error('Failed to set goal:', e);
      set({ goalBusy: false, goalActionError: describeGoalActionError(e, '设置目标失败') });
      return false;
    }
  },
  editGoal: async (objective) => {
    const client = requireRuntimeClient();
    const { currentSession, goal } = get();
    if (!client || currentSession.project_id === '' || goal === null) return false;
    const text = normalizeGoalObjective(objective);
    if (text === null) {
      set({
        goalActionError: `目标描述需为 1-${GOAL_OBJECTIVE_MAX_CHARS} 个字符（不能全为空白）。`,
      });
      return false;
    }
    set({ goalBusy: true });
    try {
      const result = await client.editSessionGoal({
        session: currentSession,
        expected_goal_id: goal.goal_id,
        objective: text,
      });
      const parsed = parseSessionGoalResult(result);
      set({ goal: parsed.goal, goalBusy: false, goalActionError: null, goalNotice: '已更新目标。' });
      return true;
    } catch (e) {
      console.error('Failed to edit goal:', e);
      set({ goalBusy: false, goalActionError: describeGoalActionError(e, '更新目标失败') });
      return false;
    }
  },
  clearGoal: async (expectedGoalId) => {
    const client = requireRuntimeClient();
    const { currentSession, goal } = get();
    if (!client || currentSession.project_id === '' || goal === null) return false;
    // Bind the clear to the goal the caller displayed.  A goal replaced since
    // the confirmation was shown is refused by the server (`conflict`) instead
    // of silently clearing a goal the user never confirmed.
    const expected = expectedGoalId ?? goal.goal_id;
    set({ goalBusy: true });
    try {
      const result = await client.clearSessionGoal({
        session: currentSession,
        expected_goal_id: expected,
      });
      // ``clear`` always answers with no goal: the thread has none any more.
      const parsed = parseSessionGoalResult(result);
      set({ goal: parsed.goal, goalBusy: false, goalActionError: null, goalNotice: '已清除目标。' });
      return true;
    } catch (e) {
      console.error('Failed to clear goal:', e);
      set({ goalBusy: false, goalActionError: describeGoalActionError(e, '清除目标失败') });
      return false;
    }
  },
  pauseGoal: async () => {
    const client = requireRuntimeClient();
    const { currentSession, goal } = get();
    if (!client || currentSession.project_id === '' || goal === null) return false;
    set({ goalBusy: true });
    try {
      const result = await client.pauseSessionGoal({
        session: currentSession,
        expected_goal_id: goal.goal_id,
      });
      const parsed = parseSessionGoalResult(result);
      set({
        goal: parsed.goal,
        goalBusy: false,
        goalActionError: null,
        // The pause itself always succeeds here; the flag only reports whether
        // this session's own live turn was asked to stop.
        goalNotice: parsed.cancellationRequested
          ? '已暂停目标，并请求取消该会话当前回合（不会影响其他会话）。'
          : '已暂停目标。',
      });
      return true;
    } catch (e) {
      console.error('Failed to pause goal:', e);
      set({ goalBusy: false, goalActionError: describeGoalActionError(e, '暂停目标失败') });
      return false;
    }
  },
  resumeGoal: async () => {
    const client = requireRuntimeClient();
    const { currentSession, goal } = get();
    if (!client || currentSession.project_id === '' || goal === null) return false;
    set({ goalBusy: true });
    try {
      const result = await client.resumeSessionGoal({
        session: currentSession,
        expected_goal_id: goal.goal_id,
      });
      const parsed = parseSessionGoalResult(result);
      set({
        goal: parsed.goal,
        goalBusy: false,
        goalActionError: null,
        // Status-only: resuming does not start a follow-up turn.
        goalNotice: '已恢复目标（不会自动续跑，需要继续请提交一条消息）。',
      });
      return true;
    } catch (e) {
      console.error('Failed to resume goal:', e);
      set({ goalBusy: false, goalActionError: describeGoalActionError(e, '恢复目标失败') });
      return false;
    }
  },
  requestSessionSearchFocus: () =>
    set((s) => ({ searchFocusToken: s.searchFocusToken + 1, isSidebarCollapsed: false })),
  loadProjects: async () => {
    const client = requireRuntimeClient();
    if (!client) return;
    // Fence this enumeration: a logout / re-pairing (authEpoch) or a newer
    // loadProjects (generation) makes every later page stale.
    const epoch = authEpoch;
    const generation = ++projectsGeneration;
    try {
      const collected: ProjectListItem[] = [];
      let offset = 0;
      for (let page = 0; page < PROJECT_LIST_MAX_PAGES; page += 1) {
        const result = await client.listProjects({ offset });
        if (epoch !== authEpoch || generation !== projectsGeneration) return;
        collected.push(...result.projects);
        if (result.next_offset === null) break;
        offset = result.next_offset;
      }
      set({ projects: collected });
    } catch (err) {
      if (epoch !== authEpoch || generation !== projectsGeneration) return;
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
  addProject: async (workspacePath) => {
    // The composer "+" flow: register the host directory, refresh the switchable
    // list so the new project is present, then activate it and open a session.
    const client = requireRuntimeClient();
    if (!client) return RUNTIME_RPC_NOT_READY;
    try {
      const registered = await client.registerProject({ workspace_path: workspacePath });
      await get().loadProjects();
      await get().createSessionInProject(registered.project_id);
      return null;
    } catch (err) {
      console.warn('Failed to add project:', err);
      return describeError(err);
    }
  },
  listDirectories: async (path) => {
    const client = requireRuntimeClient();
    if (!client) throw new Error(RUNTIME_RPC_NOT_READY);
    return client.listDirectories({ path });
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
    // No explicit title: the attach resolves the known one instead of writing the
    // thread id into the header.
    await attachToSession(session);
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
        messages: [...restoreTranscriptViews(earlier, readTranscriptViews(currentSession)), ...s.messages],
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
  modelRevision: 0,
  availableModels: [],
  thinkingLevel: null,
  thinkingLevels: [],
  mcpStatus: '',
  mcpServers: [],
  mcpEnabled: false,
  mcpRuntime: {},
  mcpWarnings: [],
  mcpConnecting: false,
  mcpRuntimeKnown: false,
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
  sessionUsage: null,
  contextWindow: null,
  goal: null,
  goalBusy: false,
  goalActionError: null,
  goalNotice: null,
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
      // The binding is confirmed now: consumers that read the server's own view of
      // this session (the Codex usage gate) re-ask, because the read they issued
      // against the optimistic publish was answered by the previous binding.
      set((state) => ({ modelRevision: state.modelRevision + 1 }));
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
    beginMcpConnect();
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
      set({
        mcpServers: get().mcpServers.map((server) =>
          server.name === serverName
            ? { ...server, enabled: result.enabled ?? !server.enabled }
            : server
        ),
      });
      // The result carries every server's real state, so this both records the
      // new flag and refreshes attachment/tools/warnings.
      applyMcpResult(result);
    } catch (error) {
      failMcpConnect(error instanceof Error ? error.message : String(error));
      console.error('Failed to reload MCP server:', error);
      throw error;
    }
  },
  refreshMcpRuntime: async () => {
    await refreshMcpRuntime(sessionEpoch);
  },
  saveMcpTools: async (serverName: string, tools: string[]) => {
    const client = requireRuntimeClient();
    if (!client) return;
    const { currentSession } = get();
    const epoch = sessionEpoch;
    const target = {
      project_id: currentSession.project_id,
      thread_id: currentSession.thread_id,
    };
    beginMcpConnect();
    try {
      const result = await client.reloadMcp({
        session: currentSession,
        server: serverName,
        include_tools: tools,
      });
      if (epoch !== sessionEpoch) return;
      const latest = get().currentSession;
      if (
        latest.project_id !== target.project_id ||
        latest.thread_id !== target.thread_id
      ) {
        return;
      }
      applyMcpResult(result);
    } catch (error) {
      failMcpConnect(error instanceof Error ? error.message : String(error));
      console.error('Failed to save MCP tools:', error);
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
    clearTranscriptViews();
    // Every upload is bound to the authenticated session; cancel the in-flight
    // ones (best-effort abort) and drop the composer before the state reset.
    get().cancelAttachments();
    sessionEpoch++;
    // The next pairing gets a fresh diagnostics read and a fresh latch.
    resetRuntimeDiagnostics();
    // The whole console state is being wiped: queued deltas have no session to
    // land in any more.
    discardPendingDeltas();
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
      sessionSearch: {
        query: '',
        items: [],
        total: 0,
        nextOffset: null,
        loading: false,
        error: null,
        generation: 0,
      },
      sessionActionError: null,
      sessionNotice: null,
      projects: [],
      activeProjectId: '',
      expandedProjectIds: [],
      projectSessions: {},
      loadingProjectIds: [],
      messages: [],
      settledTurnIds: [],
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
    // Deliberate detach: the queue belongs to the subscription being dropped.
    flushPendingDeltas();
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
  loadGitStatus: async () => {
    const client = requireRuntimeClient();
    if (!client) return;
    if (get().pairingState !== 'paired') return;
    const session = get().currentSession;
    if (!session.thread_id) return;
    try {
      const status = await client.gitStatus(session);
      // A session switch mid-flight must not paint another session's tree.
      if (get().currentSession.thread_id !== session.thread_id) return;
      set({
        gitStatus: status,
        // The host still reports the branch on pairing; the runtime's own view is
        // authoritative once it answers, and it also carries the tracking counts.
        gitBranch: status.branch ?? get().gitBranch,
        gitDirty: status.dirty,
      });
    } catch {
      // Best-effort chrome: a workspace where git cannot answer (no repository,
      // no binary) keeps the host-reported branch and shows no counts.
      set({ gitStatus: null });
    }
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
    const state = get();
    const pending = state.attachments;
    const refs = attachmentRefsOf(pending);
    const body = text.trim();
    // A submit while a chunk stream is still in flight would reference an id the
    // server has not finalized yet, so it is refused outright (never silently
    // dropped and never sent without the attachment).
    if (hasPendingUploads(pending)) {
      set({ attachmentError: '仍有附件正在上传，请等待完成或取消后再发送。' });
      return;
    }
    // An attachment-only turn is legal (the wire allows empty text when refs are
    // present); a fully empty submit still does nothing.
    if (body === '' && refs.length === 0) return;
    const { currentSession, runtimeStatus, activeTurnId } = state;
    const epoch = sessionEpoch;

    if (runtimeStatus === 'running' && activeTurnId) {
      if (refs.length > 0) {
        // `runtime.turn.steer` has no attachment_refs field: silently dropping
        // the images would be worse than refusing, so the composer keeps them.
        set({ attachmentError: '运行中无法携带附件插话：请等待当前轮次结束后再发送图片。' });
        return;
      }
      set({ attachmentError: null });
      get().addUserMessage(body);
      set((s) => ({ steerQueueCount: s.steerQueueCount + 1 }));
      try {
        await client.steerTurn({
          session: currentSession,
          expected_turn_id: activeTurnId,
          text: body,
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
      return;
    }

    set({ attachmentError: null });
    get().addUserMessage(body, attachmentDisplaysOf(pending));
    set({ runtimeStatus: 'running', activeTurnId: null, activity: null });
    try {
      const opened = await client.openSession(currentSession);
      // Re-sync the authoritative totals here: this call is already being made,
      // and it is what corrects a session that was also used elsewhere.
      if (epoch !== sessionEpoch) return;
      set({ sessionUsage: parseSessionUsage(opened.view?.usage) });
      if (!get().activeSubscriptionId) {
        const watch = await client.watchEvents(currentSession, opened.view?.latest_sequence ?? 0);
        if (epoch !== sessionEpoch) return;
        set({ activeSubscriptionId: watch.subscription_id });
      }
    } catch (err) {
      console.warn('Ensure open session note:', err);
    }
    if (epoch !== sessionEpoch) return;
    try {
      const receipt = await client.submitTurn({
        session: currentSession,
        text: body,
        // Optional key omitted rather than sent as an empty list: the daemon
        // reads "absent" as "no attachments" and rejects an explicit null.
        ...(refs.length > 0 ? { attachment_refs: refs } : {}),
      });
      if (epoch !== sessionEpoch) return;
      if (receipt.turn_id) {
        set((s) => ({
          messages: bindWorkTurn(s.messages, receipt.turn_id!, Date.now()),
          activeTurnId: s.messages.some((m) => m.turnId === receipt.turn_id && m.work?.ended)
            ? null : receipt.turn_id,
          recoveryState: 'idle', recoveryDetail: null,
        }));
      }
      // The turn was accepted, so the composer no longer owns these rows. The
      // finalized attachments stay server-side (never auto-deleted) and the
      // history projection renders them again after a refresh.
      set({ attachments: [] });
      // The daemon may just have named this session from the message that was
      // submitted: read the title back so the header and the list stop showing
      // the placeholder.
      refreshSessionTitleAfterTurn(currentSession, get().sessionTitle);
    } catch (err) {
      if (epoch !== sessionEpoch) return;
      if (err instanceof ConnectionLostError && err.unknownOutcome) {
        // The submit may have reached the daemon and started a turn; the
        // receipt was lost in the drop. Do NOT re-submit (duplicate tool
        // execution). Surface an explicit unknown and let the user query the
        // session state after reconnect. The composer rows are kept so nothing
        // is lost and nothing is re-sent automatically.
        set({
          recoveryState: 'unknown',
          recoveryDetail: 'submit sent but connection dropped before the receipt; outcome unknown',
        });
      } else {
        const reason = String((err as Error)?.message ?? err);
        set((s) => ({ messages: s.messages.map((m) => !m.turnId && m.work && !m.work.ended
          ? { ...m, work: { ...m.work, ended: true, elapsed: Math.max(0, (Date.now() - (m.work.startedAt ?? Date.now())) / 1000) } }
          : m) }));
        set({
          runtimeStatus: 'idle',
          activeTurnId: null,
          recoveryState: 'failed',
          recoveryDetail: reason,
          attachmentError: `发送失败：${reason}`,
        });
        console.error('submit failed:', err);
      }
    }
  },
  addAttachments: async (sources) => {
    const client = requireRuntimeClient();
    if (!client) return;
    const { currentSession } = get();
    if (!currentSession.project_id || !currentSession.thread_id) return;
    const candidates = sources.map((source) => ({
      name: source.name || 'image',
      mime: normalizeAttachmentMime(source.type),
      size: source.size,
      source,
    }));
    const { accepted, errors } = selectAttachmentCandidates(get().attachments.length, candidates);
    const rows: PendingAttachment[] = accepted.map((candidate) => ({
      localId: `att-${++attachmentLocalSeq}`,
      name: candidate.name,
      mime: candidate.mime,
      size: candidate.size,
      status: 'uploading',
      uploadedBytes: 0,
      attachmentId: null,
      error: null,
      source: candidate.source,
    }));
    if (rows.length > 0) {
      set((s) => ({ attachments: [...s.attachments, ...rows] }));
    }
    set({ attachmentError: errors.length > 0 ? errors.join('；') : null });
    // Rows stream in parallel but each one is bounded and independently
    // cancellable; a failure of one never blocks the others.
    await Promise.all(
      rows.map((row, index) => uploadPendingAttachment(row, accepted[index].source)),
    );
  },
  removeAttachment: (localId) => {
    const entry = get().attachments.find((row) => row.localId === localId);
    if (!entry) return;
    // Only an in-flight upload is aborted; a finalized attachment is left
    // server-side (the console never deletes a ref it already created).
    if (entry.status === 'uploading') cancelledAttachments.add(localId);
    set((s) => ({ attachments: s.attachments.filter((row) => row.localId !== localId) }));
  },
  cancelAttachments: () => {
    for (const entry of get().attachments) {
      if (entry.status === 'uploading') cancelledAttachments.add(entry.localId);
    }
    set({ attachments: [], attachmentError: null });
  },
  addUserMessage: (text: string, attachments?: TranscriptAttachment[]) => {
    const state = get();
    const newMsg: TranscriptMessage = {
      id: `usr-${Date.now()}-${state.messages.length}`,
      type: 'user',
      timestamp: new Date().toLocaleTimeString().slice(0, 5),
      content: text,
      ...(attachments && attachments.length > 0 ? { attachments } : {}),
      ...(state.runtimeStatus === 'running' && state.activeTurnId
        ? { turnId: state.activeTurnId, steer: true }
        : { work: { startedAt: Date.now(), ended: false } }),
    };
    set((s) => ({ messages: [...s.messages, newMsg] }));
  },
  toggleMessageExpand: (id) =>
    set((s) => ({ messages: s.messages.map((m) => (m.id === id ? { ...m, expanded: !m.expanded } : m)) })),
  toggleWorkExpand: (id) =>
    set((s) => ({ messages: s.messages.map((m) => (m.id === id ? { ...m, workExpanded: !m.workExpanded } : m)) })),
}));

// Store only view metadata on changes, never a per-second write or transcript
// content. Session storage is scoped to this tab/origin and cleared on logout.
useConsoleStore.subscribe((state, previous) => {
  if (state.pairingState === 'paired' && !state.historyLoading && state.messages !== previous.messages) {
    saveTranscriptViews(state.currentSession, state.messages);
  }
});
