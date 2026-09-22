/**
 * Shared runtime protocol core (browser console + future desktop shell).
 *
 * This module owns the JSON-RPC 2.0 WebSocket protocol semantics only: the
 * handshake, per-generation request/response correlation, watch cursors, the
 * bounded reconnect budget and the typed protocol errors.  It deliberately
 * carries no browser bootstrap: pairing, the console session cookie, the
 * same-origin `/runtime-ws` URL and the host/daemon discovery stay in
 * `src/client/bootstrap.ts` / `src/client/runtimeStatus.ts`, and the desktop
 * process lifecycle is out of scope entirely.
 *
 * Host coupling is limited to the injected `SocketLike` transport; the default
 * factory reads the host global `WebSocket` behind a runtime guard, so the core
 * loads and type-checks without DOM libs.
 */
import {
  SESSION_LIST_PAGE_SIZE,
  SESSION_SEARCH_PAGE_SIZE,
  HISTORY_PAGE_SIZE,
  PROJECT_LIST_PAGE_SIZE,
  DIRECTORY_LIST_PAGE_SIZE,
} from './types.ts';
import type {
  JsonRpcNotification,
  JsonRpcRequest,
  NegotiateParams,
  OpenSessionResult,
  RuntimeEvent,
  SessionRef,
  SubmitTurnParams,
  SteerTurnParams,
  CancelTurnParams,
  CommandReceipt,
  PendingApprovalView,
  ApprovalDecision,
  RebindSessionParams,
  RebindSessionResult,
  ReloadMcpParams,
  ReloadMcpResult,
  EventNotificationMeta,
  ModelListResult,
  TestModelResult,
  ListSessionsParams,
  SessionListResult,
  CreateSessionParams,
  CreateSessionResult,
  RenameSessionParams,
  RenameSessionResult,
  DeleteSessionParams,
  DeleteSessionResult,
  SearchSessionsParams,
  SessionSearchResult,
  ListDirectoriesParams,
  ListDirectoriesResult,
  ListSkillsParams,
  ListSkillsResult,
  ListProjectsParams,
  ProjectListResult,
  ReadSessionHistoryParams,
  SessionHistoryResult,
  GetRuntimeConfigParams,
  RuntimeConfigResult,
  ReconcileSessionParams,
  RegisterProjectParams,
  RegisterProjectResult,
  SessionRecoverabilityResult,
  SetProjectThinkingLevelResult,
  SetThinkingLevelResult,
  SetSessionGoalParams,
  EditSessionGoalParams,
  ClearSessionGoalParams,
  PauseSessionGoalParams,
  ResumeSessionGoalParams,
  SessionGoalResult,
  RevertTurnChangeCommand,
  RevertTurnChangeResult,
} from './types.ts';
import { EVENT_VERSION, WIRE_VERSION } from './contract.generated.ts';
import type {
  AbortAttachmentResult,
  AppendAttachmentChunkResult,
  CancelTurnResult,
  CloseSessionCommand,
  CloseSessionResult,
  EventPage,
  NegotiateResult,
  ResumeTurnResult,
  SessionView,
  SteerTurnResult,
  SttAppendResult,
  SttBeginResult,
  SttCancelResult,
  SttFinishResult,
  SttStatusView,
  UnwatchResult,
  WatchStartResult,
  WireMethod,
} from './contract.generated.ts';
import { parseRecoverabilityResult } from './recoverability.ts';
import {
  ARTIFACT_CHUNK_BYTES,
  ARTIFACT_LIST_LIMIT,
  parseArtifactChunk,
  parseArtifactMetadata,
  parseArtifactPage,
} from './artifacts.ts';
import type { ArtifactChunkView, ArtifactEntry, ArtifactPageView } from './artifacts.ts';
import { parseGitDiff, parseGitStatus } from './git.ts';
import type { GitDiffView, GitStatusView } from './git.ts';
import { parseExternalAppPage, parseOpenExternalResult } from './externalApps.ts';
import type { ExternalAppsView, OpenExternalMode, OpenExternalResultView } from './externalApps.ts';
import {
  parseScreenshotCancel,
  parseScreenshotStart,
  parseScreenshotStatus,
  parseScreenshotToolStatus,
  toWireSettings,
} from './screenshot.ts';
import type {
  ScreenshotSettingsView,
  ScreenshotStartView,
  ScreenshotStatusView,
  ScreenshotToolView,
} from './screenshot.ts';
import {
  CODEX_RESET_CONSUME_METHOD,
  CODEX_RESET_CREDITS_METHOD,
  CODEX_USAGE_METHOD,
  parseCodexConsumeResult,
  parseCodexResetCreditsView,
  parseCodexUsageView,
} from './codexUsage.ts';
import type {
  CodexConsumeResult,
  CodexResetCreditsView,
  CodexUsageView,
  ConsumeCodexResetParams,
} from './codexUsage.ts';
import {
  ATTACHMENT_READ_BYTES,
  parseAbortAttachmentResult,
  parseAppendAttachmentChunkResult,
  parseAttachmentChunk,
  parseAttachmentMetadata,
  parseBeginAttachmentResult,
  parseFinishAttachmentResult,
} from './attachments.ts';
import type {
  AttachmentChunkView,
  AttachmentEntry,
  BeginAttachmentView,
  FinishAttachmentView,
} from './attachments.ts';

export type ConnectionState = 'disconnected' | 'connecting' | 'connected' | 'error';

/** Bounded, user-visible recovery bookkeeping (phase-4 recovery contract). */
export interface RecoveryInfo {
  phase: 'reconnecting' | 'reconnected' | 'failed';
  attempt: number;
  maxAttempts: number;
  reason?: string;
  delayMs?: number;
}

/** How the console reacts to an unexpected drop after a healthy connect. */
export interface ReconnectPolicy {
  maxAttempts: number;
  baseDelayMs: number;
  maxDelayMs: number;
}

export const DEFAULT_RECONNECT_POLICY: ReconnectPolicy = {
  maxAttempts: 5,
  baseDelayMs: 300,
  maxDelayMs: 8000,
};

/** A late/duplicate response is intentionally ignored; the request stays open. */
export class ConnectionLostError extends Error {
  readonly unknownOutcome: boolean;
  constructor(message = 'connection lost', unknownOutcome = false) {
    super(message);
    this.name = 'ConnectionLostError';
    this.unknownOutcome = unknownOutcome;
  }
}

/** A JSON-RPC error that preserves the server `service_code`. */
export class RpcCallError extends Error {
  readonly code: number;
  readonly service_code?: string;
  constructor(message: string, code = -32000, service_code?: string) {
    super(message);
    this.name = 'RpcCallError';
    this.code = code;
    this.service_code = service_code;
  }
}

/** A JSON integer `>= 0` (session sequence / turn sequence). */
function isCount(value: unknown): boolean {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0;
}

/**
 * A JSON integer `>= 0` that is exactly representable (a watch cursor).
 *
 * The wire cursor is the stream's raw scanned session sequence
 * (`_Subscription.pump` sends `cursor = stream.cursor.sequence` next to the very
 * event it belongs to), so a cursor that is not a non-negative safe integer
 * cannot be resumed from.
 */
function isCursor(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}

/**
 * Client-local protocol codes for one `runtime.event` frame that cannot be
 * consumed: the envelope carries a version this client does not implement, or a
 * required v1 field is missing / mistyped.
 */
export type RuntimeEventRejection = 'unsupported_event_version' | 'malformed_runtime_event';

/**
 * Every reason a live watch can be *fenced*: the frame was unconsumable
 * (`RuntimeEventRejection`), carried a cursor that sits below the event's own
 * session sequence or that does not advance past the last delivered position, or
 * could not be handed to the consumer.
 */
type WatchFenceReason =
  | RuntimeEventRejection
  | 'invalid_event_cursor'
  | 'event_cursor_mismatch'
  | 'event_delivery_failed';

/**
 * Validate one `runtime.event` envelope against the frozen v1 shape.
 *
 * v1 freezes the envelope version (`EVENT_VERSION`) and the six envelope fields
 * (`sequence`, `turn_sequence`, `turn_id`, `kind`, `payload`, `version`).  An
 * unknown *kind* and unknown extra envelope/payload fields are same-version
 * additive and pass through untouched (`payload` stays whatever JSON the
 * producer sent, including the bare `info` string), so a newer daemon can add
 * event kinds and fields without breaking an older console.
 *
 * A frame that carries an unsupported `version`, or that is missing a required
 * field, is *not* a v1 event and is never silently dropped: this returns the
 * precise client-local reason so the caller can route it through the existing
 * protocol-error path, and `null` only when the frame is a valid v1 event.
 */
export function classifyRuntimeEvent(value: unknown): RuntimeEventRejection | null {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return 'malformed_runtime_event';
  }
  const event = value as Record<string, unknown>;
  // The version gates whether the rest of the envelope is even interpretable,
  // so it is checked first: a future envelope may rename or drop v1 fields.
  if (!isCount(event.version)) return 'malformed_runtime_event';
  if (event.version !== EVENT_VERSION) return 'unsupported_event_version';
  if (typeof event.kind !== 'string' || event.kind === '') return 'malformed_runtime_event';
  if (typeof event.turn_id !== 'string') return 'malformed_runtime_event';
  if (!isCount(event.sequence) || !isCount(event.turn_sequence)) return 'malformed_runtime_event';
  if (!('payload' in event)) return 'malformed_runtime_event';
  return null;
}

/**
 * Minimal runtime guard for one `runtime.event` envelope: the v1 event, or
 * `null` when the frame cannot be consumed (see `classifyRuntimeEvent`).
 */
export function parseRuntimeEvent(value: unknown): RuntimeEvent | null {
  return classifyRuntimeEvent(value) === null ? (value as RuntimeEvent) : null;
}

/**
 * Protocol feature flags this client's v1 wire behavior actually depends on:
 * the v1 envelope (`legacy_v1`), the raw sequence cursors (`raw_cursor`), the
 * resumable watch lease (`watch_resume`) and the approval-resume write
 * (`approval_resume`).
 *
 * This is an explicit literal on purpose.  Deriving it from the generated
 * `PROTOCOL_FEATURES` would make every flag a newer registry adds silently
 * mandatory the moment it appears; the required set is the v1 four, and extra
 * advertised flags stay additive.
 */
const REQUIRED_PROTOCOL_FEATURES = [
  'approval_resume',
  'legacy_v1',
  'raw_cursor',
  'watch_resume',
] as const;

/**
 * Strictly validate one `runtime.protocol.negotiate` result.
 *
 * The handshake must select the wire version this client offered *and*
 * implements (`WIRE_VERSION`, the single token in the request), the selection
 * must sit in the peer's own `supported_versions`, and the peer must advertise
 * the v1 feature flags this client depends on.  Extra advertised versions and
 * extra capability flags are additive growth and pass through, but any
 * violation is a typed protocol error so business frames never flow over a
 * version or feature set the client cannot speak.
 */
export function parseNegotiateResult(value: unknown): NegotiateResult {
  const malformed = () =>
    new RpcCallError('malformed negotiate result', -32603, 'malformed_negotiate_result');
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw malformed();
  const result = value as Record<string, unknown>;
  const selected = result.wire_version;
  if (selected !== WIRE_VERSION) {
    // `WIRE_VERSION` is the only version this client offers, so a selection that
    // is not it is neither in the request nor implemented here.
    throw new RpcCallError(
      `unsupported wire version ${String(selected)}`,
      -32603,
      'unsupported_wire_version',
    );
  }
  if (
    !Array.isArray(result.supported_versions) ||
    result.supported_versions.some((version) => typeof version !== 'string') ||
    !result.supported_versions.includes(selected)
  ) {
    // The selection must sit in the intersection of what this client offered and
    // what the peer still reports it supports.  A peer that advertises *more*
    // versions (additive growth) is fine.
    throw new RpcCallError(
      `wire version ${selected} is not in the peer's supported versions`,
      -32603,
      'unsupported_wire_version',
    );
  }
  const capabilities = result.capabilities;
  if (
    capabilities === null ||
    typeof capabilities !== 'object' ||
    Array.isArray(capabilities) ||
    Object.values(capabilities).some((flag) => typeof flag !== 'boolean')
  ) {
    throw malformed();
  }
  const advertised = capabilities as Record<string, unknown>;
  for (const feature of REQUIRED_PROTOCOL_FEATURES) {
    if (advertised[feature] !== true) {
      // Protocol feature flags are not authorization capabilities: only the
      // features this client's v1 wire behavior depends on must be advertised,
      // and any extra flag a newer peer adds is ignored.
      throw new RpcCallError(
        `peer does not advertise required capability ${feature}`,
        -32603,
        'unsupported_wire_version',
      );
    }
  }
  return value as NegotiateResult;
}

/**
 * Minimal WebSocket surface used by the runtime client. The browser `WebSocket`
 * satisfies it structurally; tests inject a fake implementation.
 */
export interface SocketLike {
  readyState: number;
  send(data: string): void;
  close(): void;
  onopen: (() => void) | null;
  onmessage: ((ev: { data: any }) => void) | null;
  onerror: (() => void) | null;
  /**
   * Browsers call this with the CloseEvent (`code` / `reason`); a fake socket
   * may call it with nothing at all.  The console host closes a relay it cannot
   * attach with `1011` and the frozen reason `runtime daemon unavailable`, so
   * the code/reason is the only transport-level diagnostic the browser gets.
   */
  onclose: ((ev?: { code?: number; reason?: string }) => void) | null;
}

export interface ClientOptions {
  url: string;
  onStateChange?: (state: ConnectionState, reason?: string) => void;
  onEvent?: (event: RuntimeEvent, meta?: EventNotificationMeta) => void;
  /** Recovery notifications (bounded reconnect lifecycle + watch termination). */
  onRecovery?: (info: RecoveryInfo) => void;
  onSubscriptionNotice?: (notice: SubscriptionNotice) => void;
  /** Test seam: inject a fake socket factory. Defaults to the global WebSocket. */
  socketFactory?: (url: string) => SocketLike;
  reconnect?: Partial<ReconnectPolicy>;
}

/**
 * One watch lifecycle notice delivered through `ClientOptions.onSubscriptionNotice`.
 *
 * `subscription_id` is always the *resolved* watch the notice belongs to, even
 * when the server sent it unattributed (a legacy frame with exactly one live
 * watch): the console routes by this id.
 */
export interface SubscriptionNotice {
  type: 'complete' | 'error';
  subscription_id?: string;
  service_code?: string;
  cursor?: number;
}

const OPEN = 1;

/** Constructor shape of the host's global `WebSocket` (browser / Node / desktop). */
type WebSocketConstructor = new (url: string) => SocketLike;

/**
 * Default transport factory: the host's own global `WebSocket`.
 *
 * The shared core is deliberately host-agnostic: it reads the global through
 * `globalThis` behind a runtime guard instead of referencing the browser
 * `WebSocket` binding directly, so the module type-checks and loads without DOM
 * libs.  A host that has no `WebSocket` (or a different transport, e.g. a
 * desktop shell bridge) must inject `socketFactory`.
 */
function defaultSocketFactory(url: string): SocketLike {
  const ctor = (globalThis as unknown as { WebSocket?: WebSocketConstructor }).WebSocket;
  if (typeof ctor !== 'function') {
    throw new ConnectionLostError(
      'no WebSocket implementation in this host; inject a socketFactory',
      false,
    );
  }
  return new ctor(url);
}

/** Longest close reason copied into user-visible copy (bounded, never a secret). */
const CLOSE_REASON_LIMIT = 120;

/**
 * Upper bound on concurrent live watches, matching the daemon's own
 * per-connection subscription cap (`MAX_SUBSCRIPTIONS` in
 * `src/synapse/runtime/transport/websocket.py`).  A watch past this bound is
 * refused client-side instead of being rejected by the daemon mid-handshake.
 */
const MAX_WATCHES = 32;

/**
 * Bounded, user-facing description of one socket close.  Only the numeric close
 * code and the server-supplied reason string are used; a missing code/reason
 * degrades to the generic wording so nothing is invented.
 */
export function describeSocketClose(ev?: { code?: number; reason?: string }): string {
  const code = typeof ev?.code === 'number' ? ev.code : null;
  const rawReason = typeof ev?.reason === 'string' ? ev.reason.trim() : '';
  const reason =
    rawReason.length > CLOSE_REASON_LIMIT ? rawReason.slice(0, CLOSE_REASON_LIMIT) : rawReason;
  if (code === null && reason === '') return 'connection closed';
  if (reason === '') return `connection closed (code ${code})`;
  if (code === null) return `connection closed (${reason})`;
  return `connection closed (code ${code}: ${reason})`;
}

/**
 * One registered `runtime.events.watch`: the session it streams and its resume
 * cursor (the last scanned session sequence this client delivered for it).
 */
interface WatchEntry {
  session: SessionRef;
  cursor: number | null;
}

/** Whether two session refs name the same thread in the same project. */
function sameSession(a: SessionRef, b: SessionRef): boolean {
  return a.project_id === b.project_id && a.thread_id === b.thread_id;
}

/**
 * One persistent JSON-RPC WebSocket connection with:
 *
 * - per-socket connection generations so late responses / events / close
 *   frames from a replaced socket can never pollute the current connection;
 * - typed errors that keep the server `service_code` (`replay_gap`,
 *   `event_overflow`, `invalid_cursor`, ...) so callers can distinguish an
 *   explicit gap from a transient transport failure;
 * - an optional bounded reconnect budget (never infinite) that is armed only
 *   after a healthy `connect()` and cancelled by `disconnect()` / user close;
 * - a registry of concurrent watches, each with its own resume cursor, so
 *   several sessions can stream at once and a re-attach can resume from the
 *   exact last delivered scanned position (monotonically, no duplicate, no
 *   silent `after=0`).
 *
 * The wire contract is unchanged: every method below sends the same JSON-RPC
 * frames as before; this file only adds client-side recovery semantics.
 */
export class SynapseRuntimeClient {
  private readonly options: ClientOptions;
  private ws: SocketLike | null = null;
  private reqId = 0;
  private pending = new Map<
    string | number,
    { resolve: (val: any) => void; reject: (err: any) => void; gen: number; sent: boolean }
  >();
  private state: ConnectionState = 'disconnected';
  private generation = 0;
  private armed = false;
  private closingUser = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectAttempts = 0;
  private reconnectPolicy: ReconnectPolicy = { ...DEFAULT_RECONNECT_POLICY };
  private opening: Promise<void> | null = null;
  private openReject: ((err: ConnectionLostError) => void) | null = null;
  private watchQueueSize = 128;
  /**
   * Every registered watch, keyed by subscription id and held in registration
   * order.  The Map order answers the legacy single-watch getters (the last
   * entry is the most recently registered watch).  A fenced watch keeps its
   * slot and its frozen cursor until it is re-watched or detached, so a resume
   * can still read the last good position exactly as the old field did.
   */
  private watches = new Map<string, WatchEntry>();
  /**
   * Fenced subscriptions: a watch whose stream reported a frame this client
   * cannot consume, mapped to the connection generation the fence was raised
   * in.  The sequence it reported was never replayed, so nothing from that
   * subscription may be delivered or advance its cursor again.  Only a
   * successful `watchEvents()` for its session lifts the fence (by replacing
   * the dead watch); an unattributable frame is fenced while any fence is open.
   */
  private fences = new Map<string, number>();

  constructor(options: ClientOptions) {
    this.options = options;
    if (options.reconnect) {
      this.reconnectPolicy = {
        maxAttempts: options.reconnect.maxAttempts ?? DEFAULT_RECONNECT_POLICY.maxAttempts,
        baseDelayMs: options.reconnect.baseDelayMs ?? DEFAULT_RECONNECT_POLICY.baseDelayMs,
        maxDelayMs: options.reconnect.maxDelayMs ?? DEFAULT_RECONNECT_POLICY.maxDelayMs,
      };
    }
  }

  public getState(): ConnectionState {
    return this.state;
  }

  /**
   * Cursor of the most recently registered watch (its last delivered session
   * sequence), or `null` before any watch.
   *
   * With an explicit `subscriptionId` it is that watch's own cursor (`null`
   * when the id is not registered).  The no-argument form is the legacy
   * single-watch view and is unchanged: a fenced watch keeps its last good
   * cursor, so a resume can read it after the fence was raised.
   */
  public getWatchCursor(subscriptionId?: string): number | null {
    return this.registeredWatch(subscriptionId)?.cursor ?? null;
  }

  /**
   * Session of the most recently registered watch, or of `subscriptionId` when
   * given.  The no-argument form is the legacy single-watch view.
   */
  public getWatchSession(subscriptionId?: string): SessionRef | null {
    return this.registeredWatch(subscriptionId)?.session ?? null;
  }

  /** The named watch, or the most recently registered one when no id is given. */
  private registeredWatch(subscriptionId?: string): WatchEntry | null {
    if (subscriptionId !== undefined) return this.watches.get(subscriptionId) ?? null;
    let last: WatchEntry | null = null;
    for (const entry of this.watches.values()) last = entry;
    return last;
  }

  /** Number of the current socket generation (observable for tests). */
  public getGeneration(): number {
    return this.generation;
  }

  public getPendingCount(): number {
    return this.pending.size;
  }

  public async reloadMcp(params: ReloadMcpParams): Promise<ReloadMcpResult> {
    // Optional keys are omitted rather than sent as null: the daemon decodes
    // "no server" as attach-all and rejects an explicit null payload.
    const payload: Record<string, unknown> = { session: params.session };
    if (params.server !== undefined) payload.server = params.server;
    if (params.enabled !== undefined) payload.enabled = params.enabled;
    if (params.include_tools !== undefined) payload.include_tools = params.include_tools;
    if (params.command_id !== undefined) payload.command_id = params.command_id;
    return this.call<ReloadMcpResult>('runtime.session.mcp.reload', payload);
  }

  public async listSessions(params: ListSessionsParams): Promise<SessionListResult> {
    return this.call<SessionListResult>('runtime.session.list', {
      project_id: params.project_id,
      limit: params.limit ?? SESSION_LIST_PAGE_SIZE,
      offset: params.offset ?? 0,
    });
  }

  /**
   * Persist one session's metadata row without opening a runtime
   * (`runtime.session.create`).
   *
   * `thread_id` is optional: when it is omitted the server allocates the real id
   * and returns it, so the console never invents a session identity of its own.
   * This is deliberately not `runtime.session.open`; the caller still opens the
   * returned session afterwards.
   */
  public async createSession(params: CreateSessionParams): Promise<CreateSessionResult> {
    // Optional keys are omitted rather than sent as null: the daemon reads an
    // absent thread_id as "allocate one" and rejects an explicit null.
    const payload: Record<string, unknown> = { project_id: params.project_id };
    if (params.title !== undefined) payload.title = params.title;
    if (params.thread_id !== undefined) payload.thread_id = params.thread_id;
    if (params.command_id !== undefined) payload.command_id = params.command_id;
    return this.call<CreateSessionResult>('runtime.session.create', payload);
  }

  public async renameSession(params: RenameSessionParams): Promise<RenameSessionResult> {
    const payload: Record<string, unknown> = {
      session: params.session,
      title: params.title,
    };
    if (params.command_id !== undefined) payload.command_id = params.command_id;
    return this.call<RenameSessionResult>('runtime.session.rename', payload);
  }

  /**
   * Delete one session and its conversation (`runtime.session.delete`).
   *
   * The server removes the metadata row and the thread goal and purges the thread
   * from the checkpoint store, the transcript projection, the full-text search
   * index and its turn snapshots.  `retained_history` reports whether anything
   * survived -- true only when a store refused, with the store named in
   * `purge_failures` -- so a caller must report *that* rather than assuming either
   * outcome.  A session with an active turn is refused by the server with
   * `conflict`; this never cancels the turn.
   */
  public async deleteSession(params: DeleteSessionParams): Promise<DeleteSessionResult> {
    const payload: Record<string, unknown> = { session: params.session };
    if (params.command_id !== undefined) payload.command_id = params.command_id;
    return this.call<DeleteSessionResult>('runtime.session.delete', payload);
  }

  /**
   * Search one project's persisted session metadata (`runtime.session.search`).
   *
   * This is a metadata search (title / summary / ids / model), never a full-text
   * transcript search; an empty `text` lists the project's sessions newest-first.
   */
  public async searchSessions(params: SearchSessionsParams): Promise<SessionSearchResult> {
    return this.call<SessionSearchResult>('runtime.session.search', {
      project_id: params.project_id,
      text: params.text ?? '',
      limit: params.limit ?? SESSION_SEARCH_PAGE_SIZE,
      offset: params.offset ?? 0,
    });
  }

  /**
   * Enumerate the registered projects this connection may see
   * (`runtime.project.list`).
   *
   * The daemon computes the visible set (its own catalog visibility
   * intersected with any trusted connection scope) and applies it before
   * pagination, so the browser never sends -- and cannot widen -- a scope.  This
   * replaces the old `GET /api/projects` business list.
   */
  public async listProjects(params: ListProjectsParams = {}): Promise<ProjectListResult> {
    return this.call<ProjectListResult>('runtime.project.list', {
      limit: params.limit ?? PROJECT_LIST_PAGE_SIZE,
      offset: params.offset ?? 0,
    });
  }

  /**
   * Register one host workspace directory as a project
   * (`runtime.project.register`).
   *
   * The browser never resolves the path: the daemon validates the host path and
   * upserts the user-layer catalog row, so the console can switch to the
   * returned project and open a session in it.  Idempotent per workspace path.
   */
  public async registerProject(
    params: RegisterProjectParams,
  ): Promise<RegisterProjectResult> {
    return this.call<RegisterProjectResult>('runtime.project.register', {
      workspace_path: params.workspace_path,
    });
  }

  /**
   * List one host directory's immediate sub-directories (`runtime.fs.list`).
   *
   * Backs the console's "add project" picker: the browser cannot resolve a host
   * path itself, so the daemon answers a bounded, read-only listing.  A null
   * `path` means the daemon's home directory.
   */
  public async listDirectories(
    params: ListDirectoriesParams = {},
  ): Promise<ListDirectoriesResult> {
    return this.call<ListDirectoriesResult>('runtime.fs.list', {
      path: params.path ?? null,
      limit: params.limit ?? DIRECTORY_LIST_PAGE_SIZE,
    });
  }

  /**
   * List discoverable Agent Skills (`runtime.skills.list`).
   *
   * An optional `project_id` parameter resolves project-specific skills paths;
   * when omitted, default skills paths are searched.
   */
  public async listSkills(
    params: ListSkillsParams = {},
  ): Promise<ListSkillsResult> {
    return this.call<ListSkillsResult>('runtime.skills.list', {
      project_id: params.project_id ?? null,
    });
  }

  public async readSessionHistory(
    params: ReadSessionHistoryParams,
  ): Promise<SessionHistoryResult> {
    return this.call<SessionHistoryResult>('runtime.session.history', {
      session: params.session,
      before_turn: params.before_turn ?? null,
      limit: params.limit ?? HISTORY_PAGE_SIZE,
    });
  }

  /**
   * Request one read-only history/live recovery snapshot
   * (`runtime.session.reconcile`).  Only meaningful on a connection that
   * already negotiated successfully (all business frames require the
   * handshake).  The response is strictly parsed (epoch/retention/probes), so
   * a malformed server result rejects instead of driving a fake recovery.
   */
  public async reconcileSession(
    params: ReconcileSessionParams,
  ): Promise<SessionRecoverabilityResult> {
    const result = await this.call<unknown>('runtime.session.reconcile', {
      session: params.session,
      probe_turn_ids: params.probe_turn_ids ?? [],
    });
    try {
      return parseRecoverabilityResult(result);
    } catch (err) {
      throw new RpcCallError(
        err instanceof Error ? err.message : 'malformed reconcile result',
        -32603,
        'malformed_reconcile_result',
      );
    }
  }

  /** Read the read-only runtime configuration for one session (`runtime.config.get`). */
  public async getRuntimeConfig(params: GetRuntimeConfigParams): Promise<RuntimeConfigResult> {
    return this.call<RuntimeConfigResult>('runtime.config.get', {
      session: params.session,
    });
  }

  public async rebindSession(params: RebindSessionParams): Promise<RebindSessionResult> {
    return this.call<RebindSessionResult>('runtime.session.rebind', params);
  }

  /**
   * Set one session's reasoning level (`runtime.session.thinking.set`).
   *
   * Session-scoped write: the level is validated server-side against the target
   * session's thinking-level whitelist, and only that thread's binding changes.
   * The reply carries the refreshed config view.
   */
  public async setThinkingLevel(
    session: SessionRef,
    level: string,
  ): Promise<SetThinkingLevelResult> {
    return this.call<SetThinkingLevelResult>('runtime.session.thinking.set', {
      session,
      level,
    });
  }

  /**
   * Set one project's default reasoning level (`runtime.project.thinking.set`).
   *
   * Project-scoped write: the level is validated server-side against the same
   * whitelist the read surface advertises and persisted into the project's
   * settings layer, so it applies to sessions opened afterwards and survives a
   * daemon restart. The current session is deliberately not rebound.
   */
  public async setProjectThinkingLevel(
    projectId: string,
    level: string,
  ): Promise<SetProjectThinkingLevelResult> {
    return this.call<SetProjectThinkingLevelResult>('runtime.project.thinking.set', {
      project_id: projectId,
      level,
    });
  }

  /**
   * Read one artifact's metadata (`runtime.artifacts.stat`).
   *
   * The result goes through a strict whitelist decoder, so an unexpected field
   * set surfaces as a typed error instead of reaching the UI.
   */
  public async statArtifact(session: SessionRef, path: string): Promise<ArtifactEntry> {
    return parseArtifactMetadata(
      await this.call('runtime.artifacts.stat', { ref: { session, path } }),
    );
  }

  /**
   * Read the workspace's git status (`runtime.git.status`).
   *
   * Read-only: branch, upstream tracking counts and the changed-file list, all
   * through the same strict decoder rule as the artifact calls.  Nothing here
   * stages or commits.
   */
  public async gitStatus(session: SessionRef): Promise<GitStatusView> {
    return parseGitStatus(await this.call('runtime.git.status', { session }));
  }

  /**
   * Read one file's unified diff (`runtime.git.diff`).
   *
   * `staged` compares the index instead of the worktree.  The server caps the
   * text and reports `truncated`, so one huge diff cannot blow up the panel.
   */
  public async gitDiff(
    session: SessionRef,
    path: string,
    staged = false,
  ): Promise<GitDiffView> {
    return parseGitDiff(await this.call('runtime.git.diff', { session, path, staged }));
  }

  /**
   * Undo one file's part in one finished turn (`runtime.workspace.revert`).
   *
   * The one call in this client that writes to the reader's own files: it restores that
   * single path from the copy the runtime kept before the turn.  The runtime refuses --
   * with a typed `service_code` on the error -- while a turn is running, when the file no
   * longer holds what the turn left there, and when the turn's record is gone, so a
   * failure here is always a refusal to explain rather than a half-done edit.
   */
  public async revertTurnChange(
    params: RevertTurnChangeCommand,
  ): Promise<RevertTurnChangeResult> {
    const payload: Record<string, unknown> = {
      session: params.session,
      turn_id: params.turn_id,
      path: params.path,
    };
    if (params.command_id !== undefined) payload.command_id = params.command_id;
    return this.call<RevertTurnChangeResult>('runtime.workspace.revert', payload);
  }

  /**
   * List the applications the host can start on a workspace file (`runtime.apps.list`).
   *
   * A host property, not a session one, so it takes no parameters and is read once
   * per console: the answer carries names, roles, claimed extensions and glyph ids.
   * An application's own path never travels, and this call authorizes no launch.
   */
  public async listExternalApps(): Promise<ExternalAppsView> {
    return parseExternalAppPage(await this.call('runtime.apps.list', {}));
  }

  /**
   * Start one host application on one workspace-relative path
   * (`runtime.workspace.open_external`).
   *
   * The program is named by an id the host itself enumerated (absent means the
   * operating system's association) and the path must resolve inside the session's
   * own workspace.  Refusals carry a `service_code` that names the condition, so a
   * failure here is always something the console can word.
   */
  public async openExternal(params: {
    session: SessionRef;
    path: string;
    appId?: string;
    mode?: OpenExternalMode;
  }): Promise<OpenExternalResultView> {
    const payload: Record<string, unknown> = {
      session: params.session,
      path: params.path,
    };
    if (params.appId !== undefined) payload.app_id = params.appId;
    if (params.mode !== undefined) payload.mode = params.mode;
    return parseOpenExternalResult(
      await this.call('runtime.workspace.open_external', payload),
    );
  }

  /**
   * Read the host capture tool's status and the session's capture task
   * (`runtime.screenshot.status`).
   *
   * Read-only: it never starts the tool or captures.  An empty `taskId` asks for
   * the session's own current task, which is `state: 'idle'` when none exists.
   */
  public async getScreenshotStatus(session: SessionRef, taskId = ''): Promise<ScreenshotStatusView> {
    return parseScreenshotStatus(
      await this.call('runtime.screenshot.status', { session, task_id: taskId }),
    );
  }

  /**
   * Open (or focus) the capture tool's own settings window
   * (`runtime.screenshot.settings.open`).
   *
   * Starts the host GUI but captures nothing and writes no attachment; the
   * result is the tool's resident status.
   */
  public async openScreenshotSettings(session: SessionRef): Promise<ScreenshotToolView> {
    return parseScreenshotToolStatus(
      await this.call('runtime.screenshot.settings.open', { session }),
    );
  }

  /**
   * Queue one asynchronous window capture (`runtime.screenshot.capture`).
   *
   * Returns immediately with the task snapshot; progress arrives through
   * `getScreenshotStatus` and the capture may be cancelled with
   * `cancelScreenshotCapture`.  A second call while one is queued/running is
   * idempotent on the server (the running task is returned).
   */
  public async startScreenshotCapture(params: {
    session: SessionRef;
    settings?: ScreenshotSettingsView;
    saveConfig?: boolean;
    /**
     * Upper bound on the frames this capture may produce, independent of the
     * tool's saved `count`: the runtime starts the job with
     * `min(saved_count, maxFrames)`.  The console passes its composer's free
     * image slots so a saved `count` of 600 can never over-capture.
     */
    maxFrames?: number;
  }): Promise<ScreenshotStartView> {
    const payload: Record<string, unknown> = { session: params.session };
    if (params.settings !== undefined) payload.settings = toWireSettings(params.settings);
    if (params.saveConfig !== undefined) payload.save_config = params.saveConfig;
    if (params.maxFrames !== undefined) payload.max_frames = params.maxFrames;
    return parseScreenshotStart(await this.call('runtime.screenshot.capture', payload));
  }

  /**
   * Request cancellation of the session's capture task
   * (`runtime.screenshot.cancel`).
   *
   * Idempotent: cancelling a terminal task reports its settled state.
   */
  public async cancelScreenshotCapture(
    session: SessionRef,
    taskId: string,
  ): Promise<{ taskId: string; state: ScreenshotStatusView['state']; cancelled: boolean }> {
    return parseScreenshotCancel(
      await this.call('runtime.screenshot.cancel', { session, task_id: taskId }),
    );
  }

  /**
   * Read the session's Codex OAuth usage windows (`runtime.codex.usage.get`).
   *
   * Read-only and strictly decoded: the peer returns the primary/secondary
   * windows with their *real* `window_minutes`, so the console labels a 7-day
   * window as `7d` instead of assuming a length.  No credential is part of the
   * result — the daemon owns the OAuth exchange.
   */
  public async getCodexUsage(session: SessionRef, force = false): Promise<CodexUsageView> {
    return parseCodexUsageView(
      await this.call(CODEX_USAGE_METHOD, { session, force }),
    );
  }

  /**
   * Read the session's reset-credit rows (`runtime.codex.reset_credits.get`).
   *
   * A read, so `force` only bypasses the daemon's own cache; the console keeps
   * its own 300s read cache on top (see the Codex usage controller).
   */
  public async getCodexResetCredits(
    session: SessionRef,
    force = false,
  ): Promise<CodexResetCreditsView> {
    return parseCodexResetCreditsView(
      await this.call(CODEX_RESET_CREDITS_METHOD, { session, force }),
    );
  }

  /**
   * Redeem one reset credit (`runtime.codex.reset_credits.consume`).
   *
   * This is the *only* write on this surface and it really spends one credit of
   * the account's quota, so the caller has to have shown the user a confirmation
   * first: `confirmed` is typed as the literal `true`, and `command_id` is the
   * caller's own idempotency key (minted once, never regenerated for a replay).
   */
  public async consumeCodexResetCredit(
    params: ConsumeCodexResetParams,
  ): Promise<CodexConsumeResult> {
    return parseCodexConsumeResult(
      await this.call(CODEX_RESET_CONSUME_METHOD, {
        session: params.session,
        expected_model: params.expected_model,
        credit_id: params.credit_id,
        command_id: params.command_id,
        confirmed: params.confirmed,
      }),
    );
  }

  /**
   * List one workspace directory (`runtime.artifacts.list`).
   *
   * Bounded by construction: one page per call, `limit` capped at the server's
   * own page bound, and paging is driven explicitly by `next_cursor`.
   */
  public async listArtifacts(
    session: SessionRef,
    path: string,
    cursor: string | null = null,
    limit: number = ARTIFACT_LIST_LIMIT,
  ): Promise<ArtifactPageView> {
    return parseArtifactPage(
      await this.call('runtime.artifacts.list', { session, path, cursor, limit }),
    );
  }

  /**
   * Read one bounded byte range of an artifact (`runtime.artifacts.read`).
   *
   * Never reads a whole file: `limit` is clamped to the transport's own chunk
   * bound and the caller advances with `nextOffset` until `eof`.
   */
  public async readArtifact(
    session: SessionRef,
    path: string,
    offset = 0,
    limit: number = ARTIFACT_CHUNK_BYTES,
    expectedRevision: string | null = null,
  ): Promise<ArtifactChunkView> {
    return parseArtifactChunk(
      await this.call('runtime.artifacts.read', {
        ref: { session, path },
        offset,
        limit,
        expected_revision: expectedRevision,
      }),
    );
  }

  /**
   * Reserve one session-scoped image upload (`runtime.attachments.begin`).
   *
   * No bytes travel in the request: the client declares the exact size and MIME
   * type, receives the opaque id plus the server's own chunk budget, and then
   * streams bounded chunks through `appendAttachmentChunk`.  `display_name` is
   * display-only and is omitted when empty (the server quotes it, never uses it
   * as a path).
   */
  public async beginAttachment(
    session: SessionRef,
    size: number,
    mime: string,
    displayName = '',
  ): Promise<BeginAttachmentView> {
    const payload: Record<string, unknown> = { session, size, mime };
    if (displayName !== '') payload.display_name = displayName;
    return parseBeginAttachmentResult(await this.call('runtime.attachments.begin', payload));
  }

  /**
   * Stream one bounded base64 chunk at `expectedOffset`
   * (`runtime.attachments.append`).
   *
   * The server rejects an out-of-order or oversized chunk with a typed
   * attachment error and answers the authoritative `next_offset`, so the caller
   * must advance from that value rather than from its own chunk length.
   */
  public async appendAttachmentChunk(
    session: SessionRef,
    attachmentId: string,
    expectedOffset: number,
    dataBase64: string,
  ): Promise<AppendAttachmentChunkResult> {
    return parseAppendAttachmentChunkResult(
      await this.call('runtime.attachments.append', {
        ref: { session, attachment_id: attachmentId },
        expected_offset: expectedOffset,
        data_base64: dataBase64,
      }),
    );
  }

  /**
   * Finalize an upload (`runtime.attachments.finish`).
   *
   * The server verifies the received bytes against the declared size and MIME
   * type; the finalized attachment is durable across a restart and stays
   * readable by `readAttachment` afterwards.
   */
  public async finishAttachment(
    session: SessionRef,
    attachmentId: string,
    expectedSize: number,
    expectedMime: string,
  ): Promise<FinishAttachmentView> {
    return parseFinishAttachmentResult(
      await this.call('runtime.attachments.finish', {
        ref: { session, attachment_id: attachmentId },
        expected_size: expectedSize,
        expected_mime: expectedMime,
      }),
    );
  }

  /**
   * Discard one upload and its partial bytes (`runtime.attachments.abort`).
   *
   * Aborting twice is not an error; a finalized attachment is not removed here
   * (the console never deletes a ref it already submitted).
   */
  public async abortAttachment(
    session: SessionRef,
    attachmentId: string,
  ): Promise<AbortAttachmentResult> {
    return parseAbortAttachmentResult(
      await this.call('runtime.attachments.abort', {
        ref: { session, attachment_id: attachmentId },
      }),
    );
  }

  /**
   * Read one attachment's durable metadata (`runtime.attachments.stat`).
   *
   * The result goes through a strict whitelist decoder, so an unexpected field
   * set surfaces as a typed error instead of reaching the UI.
   */
  public async statAttachment(session: SessionRef, attachmentId: string): Promise<AttachmentEntry> {
    return parseAttachmentMetadata(
      await this.call('runtime.attachments.stat', {
        ref: { session, attachment_id: attachmentId },
      }),
    );
  }

  /**
   * Read one bounded byte window of a finalized attachment
   * (`runtime.attachments.read`).
   *
   * Never reads a whole image in one frame: `limit` is the transport's own
   * window and the caller advances with `nextOffset` until `eof`.
   */
  public async readAttachment(
    session: SessionRef,
    attachmentId: string,
    offset = 0,
    limit: number = ATTACHMENT_READ_BYTES,
  ): Promise<AttachmentChunkView> {
    return parseAttachmentChunk(
      await this.call('runtime.attachments.read', {
        ref: { session, attachment_id: attachmentId },
        offset,
        limit,
      }),
    );
  }

  /**
   * Read whether local speech input can run for the calling session
   * (`runtime.stt.status`).
   *
   * `engine` is the configured mode (`browser` or `local`); `available` and
   * `reason` describe the *local* engine only, so a missing extra or model set
   * is an ordinary `available: false` with a reason, never an error.
   */
  public async sttStatus(session: SessionRef): Promise<SttStatusView> {
    return this.call<SttStatusView>('runtime.stt.status', { session });
  }

  /**
   * Build the local speech models now (`runtime.stt.warm_up`).
   *
   * The build is the expensive part of local speech -- about a minute on a CPU --
   * so the console asks for it as soon as it learns the local engine is selected,
   * rather than paying it inside a live microphone. Idempotent, and the answer is
   * the same view as {@link sttStatus}.
   */
  public async sttWarmUp(session: SessionRef): Promise<SttStatusView> {
    return this.call<SttStatusView>('runtime.stt.warm_up', { session });
  }

  /**
   * Choose the speech engine (`runtime.stt.set_engine`).
   *
   * The host persists the choice and applies it to its own live settings in the
   * same call, so the next status read already reflects it -- no restart, no page
   * reload. `modelDir` is the local engine's model directory, or null to use the
   * engine's default. The answer is the *effective* status after the change, so a
   * caller learns in one round trip whether the chosen engine can run here.
   */
  public async sttSetEngine(
    session: SessionRef,
    engine: string,
    modelDir: string | null,
  ): Promise<SttStatusView> {
    return this.call<SttStatusView>('runtime.stt.set_engine', {
      session,
      engine,
      model_dir: modelDir,
    });
  }

  /**
   * Store a cloud provider's credential (`runtime.stt.set_api_key`).
   *
   * The key travels to the daemon and never comes back: the answer is the status
   * view, whose provider entries report only `key_configured`. An empty key clears
   * the stored credential.
   */
  public async sttSetApiKey(
    session: SessionRef,
    provider: string,
    apiKey: string,
  ): Promise<SttStatusView> {
    return this.call<SttStatusView>('runtime.stt.set_api_key', {
      session,
      provider,
      api_key: apiKey,
    });
  }

  /**
   * Start (or restart) one dictation for the session (`runtime.stt.begin`).
   *
   * The result announces the `sample_rate` every later chunk must be encoded at
   * (int16 little-endian mono PCM).
   */
  public async sttBegin(session: SessionRef): Promise<SttBeginResult> {
    return this.call<SttBeginResult>('runtime.stt.begin', { session });
  }

  /**
   * Stream one bounded base64 chunk of int16 PCM (`runtime.stt.append`).
   *
   * `partial` is provisional text the console may replace; `finalized` holds the
   * sentences this chunk completed, in order, and is authoritative.
   */
  public async sttAppend(session: SessionRef, dataBase64: string): Promise<SttAppendResult> {
    return this.call<SttAppendResult>('runtime.stt.append', {
      session,
      data_base64: dataBase64,
    });
  }

  /**
   * Flush the dictation and collect its last sentence (`runtime.stt.finish`).
   *
   * `finalized` is empty when no dictation was open.
   */
  public async sttFinish(session: SessionRef): Promise<SttFinishResult> {
    return this.call<SttFinishResult>('runtime.stt.finish', { session });
  }

  /**
   * Drop the dictation and its buffered audio (`runtime.stt.cancel`).
   *
   * Idempotent: `cancelled` is `false` when no dictation was open.
   */
  public async sttCancel(session: SessionRef): Promise<SttCancelResult> {
    return this.call<SttCancelResult>('runtime.stt.cancel', { session });
  }

  /**
   * List configured downstream model profiles (`runtime.models.list`).
   */
  public async modelsList(session: SessionRef): Promise<ModelListResult> {
    return this.call<ModelListResult>('runtime.models.list', { session });
  }

  /**
   * Add or update one model profile (`runtime.models.save`).
   */
  public async modelsSave(
    session: SessionRef,
    alias: string,
    profile: Record<string, unknown>,
    makeDefault = false,
  ): Promise<ModelListResult> {
    return this.call<ModelListResult>('runtime.models.save', {
      session,
      alias,
      profile,
      make_default: makeDefault,
    });
  }

  /**
   * Remove one model profile by alias (`runtime.models.delete`).
   */
  public async modelsDelete(session: SessionRef, alias: string): Promise<ModelListResult> {
    return this.call<ModelListResult>('runtime.models.delete', { session, alias });
  }

  /**
   * Make one existing profile the store's default (`runtime.models.set_default`).
   */
  public async modelsSetDefault(session: SessionRef, alias: string): Promise<ModelListResult> {
    return this.call<ModelListResult>('runtime.models.set_default', { session, alias });
  }

  /**
   * Probe one endpoint with a minimal request (`runtime.models.test`).
   */
  public async modelsTest(session: SessionRef, alias: string): Promise<TestModelResult> {
    return this.call<TestModelResult>('runtime.models.test', { session, alias });
  }

  private setState(next: ConnectionState, reason?: string) {
    if (this.state !== next) {
      this.state = next;
      this.options.onStateChange?.(next, reason);
    }
  }

  private emitRecovery(info: RecoveryInfo) {
    this.options.onRecovery?.(info);
  }

  private createSocket(url: string): SocketLike {
    return (this.options.socketFactory ?? defaultSocketFactory)(url);
  }

  /**
   * Open (or reuse) the connection and negotiate once. This arms the bounded
   * reconnect budget: a later unexpected drop reconnects with backoff instead
   * of leaving the console permanently disconnected.
   */
  public connect(): Promise<void> {
    if (this.ws?.readyState === OPEN && this.state === 'connected') {
      return Promise.resolve();
    }
    this.closingUser = false;
    return this.openOnce();
  }

  private openOnce(): Promise<void> {
    if (this.opening) return this.opening;
    this.opening = this.openSocket().finally(() => {
      this.opening = null;
    });
    return this.opening;
  }

  private openSocket(): Promise<void> {
    const gen = ++this.generation;
    let settled = false;
    this.setState('connecting');
    // No credentials are ever carried in the WebSocket URL: the console host
    // authenticates the same-origin session cookie and the daemon bearer token
    // stays server-side (phase-5 web host slice).
    let socket: SocketLike;
    try {
      socket = this.createSocket(this.options.url);
    } catch (err) {
      // A host with no usable transport must fail as a rejected `connect()`
      // promise (the declared contract), never as a synchronous throw.
      const detail = err instanceof Error ? err.message : 'failed to create socket';
      this.setState('error', detail);
      return Promise.reject(new ConnectionLostError(detail, false));
    }
    this.ws = socket;
    return new Promise<void>((resolve, reject) => {
      this.openReject = (err) => {
        if (!settled) {
          fail(err);
        }
      };
      const fail = (err: Error) => {
        if (settled) return;
        settled = true;
        this.openReject = null;
        if (socket === this.ws && gen === this.generation) {
          this.ws = null;
        }
        this.setState('error', err.message);
        reject(err);
      };
      socket.onopen = () => {
        if (socket !== this.ws || gen !== this.generation) return;
        (async () => {
          try {
            await this.negotiate();
          } catch (err) {
            fail(err as Error);
            return;
          }
          if (socket !== this.ws || gen !== this.generation) return;
          settled = true;
          this.openReject = null;
          this.setState('connected');
          // The bounded reconnect budget arms only after a healthy connect:
          // an initial connect failure stays a plain observable error instead
          // of silently retrying before the console ever reached the daemon.
          this.armed = true;
          this.reconnectAttempts = 0;
          resolve();
        })();
      };
      socket.onmessage = (ev) => this.handleMessage(socket, gen, ev.data);
      socket.onerror = () => {
        // Browsers usually follow error with close; the close handler owns
        // teardown. But a fake socket (or an endpoint that only errors) must
        // still fail the open instead of hanging the promise forever. The
        // `settled` guard keeps this idempotent when close follows.
        if (socket === this.ws && gen === this.generation && !settled) {
          fail(new ConnectionLostError('connection error', false));
        }
      };
      socket.onclose = (ev?: { code?: number; reason?: string }) => {
        if (socket !== this.ws || gen !== this.generation) {
          // Late close from a replaced socket generation: fence it entirely.
          return;
        }
        const detail = describeSocketClose(ev);
        this.ws = null;
        if (!settled) {
          settled = true;
          this.openReject = null;
          this.setState('error', detail);
          reject(new ConnectionLostError(detail, false));
        }
        this.rejectPendingForGeneration(gen, detail);
        if (this.armed) {
          // Only a connection that was healthy at least once triggers the
          // bounded recovery budget; a failed first connect does not.
          this.handleUnexpectedClose(detail);
        }
      };
    });
  }

  /**
   * User-initiated close: cancels any pending reconnect budget and marks the
   * client as intentionally disconnected (no automatic recovery follows).
   *
   * Unlike an unexpected drop (which keeps every watch and its cursor so the
   * bounded recovery can resume), a manual close has no reconnect, so the watch
   * leases are dropped here and no cursor survives it.
   */
  public disconnect() {
    this.closingUser = true;
    this.armed = false;
    this.clearReconnectTimer();
    const socket = this.ws;
    this.ws = null;
    if (socket) {
      try {
        socket.close();
      } catch {
        /* already closed */
      }
    }
    this.rejectPendingAll(new ConnectionLostError('disconnected by user', false));
    if (this.openReject) {
      this.openReject(new ConnectionLostError('disconnected by user', false));
    }
    this.setState('disconnected', 'closed by user');
    this.watches.clear();
    this.fences.clear();
  }

  /** Explicitly cancel a pending reconnect (idempotent; keeps state unchanged). */
  public cancelReconnect() {
    this.clearReconnectTimer();
  }

  private clearReconnectTimer() {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private handleUnexpectedClose(reason: string) {
    if (this.closingUser || !this.armed) {
      this.setState('disconnected', reason);
      return;
    }
    if (this.reconnectTimer !== null) return;
    this.scheduleReconnect(reason);
  }

  /** Run one reconnect attempt and keep the budget bounded. */
  private async runReconnectAttempt(reason: string, attempt: number): Promise<void> {
    try {
      await this.openOnce();
      if (this.closingUser) return;
      this.reconnectAttempts = 0;
      const policy = this.reconnectPolicy;
      this.emitRecovery({
        phase: 'reconnected',
        attempt,
        maxAttempts: policy.maxAttempts,
        reason,
      });
    } catch (err) {
      if (this.closingUser) return;
      // The failed generation's close already advanced the attempt counter and
      // scheduled the next retry (onclose -> handleUnexpectedClose). If the
      // failure came from a pure error with no close (rare), schedule here.
      // Never reset `reconnectAttempts` to the stale `attempt`: doing so would
      // let an exhausted budget loop forever.
      this.handleUnexpectedClose((err as Error)?.message || 'reconnect failed');
    }
  }

  private scheduleReconnect(reason: string) {
    const attempt = this.reconnectAttempts + 1;
    const policy = this.reconnectPolicy;
    if (attempt > policy.maxAttempts) {
      this.setState('error', `reconnect budget exhausted after ${policy.maxAttempts} attempts`);
      this.emitRecovery({
        phase: 'failed',
        attempt: attempt - 1,
        maxAttempts: policy.maxAttempts,
        reason,
      });
      return;
    }
    const delayMs = Math.min(policy.baseDelayMs * 2 ** (attempt - 1), policy.maxDelayMs);
    this.emitRecovery({
      phase: 'reconnecting',
      attempt,
      maxAttempts: policy.maxAttempts,
      reason,
      delayMs,
    });
    this.reconnectAttempts = attempt;
    this.setState('connecting', `reconnect attempt ${attempt}`);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      void this.runReconnectAttempt(reason, attempt);
    }, delayMs);
  }

  private rejectPendingForGeneration(gen: number, reason: string) {
    for (const [id, entry] of this.pending) {
      if (entry.gen !== gen) continue;
      this.pending.delete(id);
      const unknown = entry.sent;
      if (!entry.resolve) continue;
      entry.reject(new ConnectionLostError(reason, unknown));
    }
  }

  private rejectPendingAll(error: Error) {
    for (const [, entry] of this.pending) {
      entry.reject(error);
    }
    this.pending.clear();
  }

  private handleMessage(socket: SocketLike, gen: number, raw: any) {
    // Fence every frame by the socket that delivered it: a late message from
    // a replaced socket must never touch the current connection or its
    // pending requests.
    if (socket !== this.ws || gen !== this.generation) return;
    let data: any;
    try {
      data = JSON.parse(raw);
    } catch (err) {
      // Never echo the raw frame: it may carry user content.  The parse failure
      // is a transport-level defect, so only the error itself is logged.
      console.error(
        'Failed to parse wire message:',
        err instanceof Error ? err.message : 'invalid JSON',
      );
      return;
    }
    if (data.method && !data.id) {
      this.handleNotification(socket, gen, data as JsonRpcNotification<Record<string, any>>);
      return;
    }
    if (data.id === undefined) return;
    const deferred = this.pending.get(data.id);
    if (!deferred) return; // late / unknown / already cancelled response
    if (deferred.gen !== gen) return; // owned by a different generation
    this.pending.delete(data.id);
    if (data.error) {
      const service_code = data.error?.data?.service_code;
      deferred.reject(
        service_code
          ? new RpcCallError(data.error.message || 'RPC error', data.error.code, service_code)
          : new RpcCallError(data.error.message || 'RPC error', data.error.code ?? -32000),
      );
    } else {
      deferred.resolve(data.result);
    }
  }

  private handleNotification(
    socket: SocketLike,
    gen: number,
    noti: JsonRpcNotification<Record<string, any>>,
  ) {
    const params = noti.params ?? {};
    if (noti.method === 'runtime.event' || noti.method === 'runtime.events.notification') {
      if (socket !== this.ws || gen !== this.generation) return;
      const subId = params?.subscription_id;
      // A fenced subscription, and any unattributable frame while a fence is
      // open, may not be delivered; an unknown subscription id is dropped.
      if (this.isFencedFrame(subId, gen)) return;
      const target = this.resolveWatchTarget(subId);
      if (target === null) return;
      const rejection = classifyRuntimeEvent(params?.event);
      if (rejection !== null) {
        // A frame that cannot be consumed is never counted as delivered: the
        // cursor stays where it is so a re-attach re-reads this sequence, the
        // subscription is fenced so no later frame may jump the gap, and the
        // failure is surfaced through the existing subscription-notice channel
        // (the console's error/resync path) instead of a silent drop.
        this.fenceSubscription(gen, target.id, rejection);
        return;
      }
      const event = params?.event as RuntimeEvent;
      const cursor = params?.cursor;
      // The wire cursor is the stream's raw scanned progress, not necessarily the
      // delivered event's own sequence: every filtered-out sequence scanned after
      // this event pushes the cursor *past* `event.sequence`, so equality is not
      // the invariant.  A missing cursor, a fraction/NaN/negative, or a cursor
      // below the event's own sequence (that sequence was never scanned) still
      // cannot be resumed from and is refused rather than written into the watch
      // cursor.
      if (!isCursor(cursor)) {
        this.fenceSubscription(gen, target.id, 'invalid_event_cursor');
        return;
      }
      if (cursor < event.sequence) {
        this.fenceSubscription(gen, target.id, 'event_cursor_mismatch');
        return;
      }
      // The resume point must strictly advance: a frame that repeats or rewinds
      // the watch's own last delivered cursor is a non-monotonic replay, so it
      // is fenced instead of being delivered a second time (or moving the resume
      // point back over an already consumed sequence).  Two sessions have
      // independent sequences, so this reads the entry the frame names, never a
      // shared cursor.
      const previous = target.entry.cursor;
      if (previous !== null && cursor <= previous) {
        this.fenceSubscription(gen, target.id, 'event_cursor_mismatch');
        return;
      }
      // The consumer runs before the cursor moves: a view that throws did not
      // take the event, so the cursor must stay on the last good sequence (and
      // the subscription is fenced) instead of skipping it.
      try {
        this.options.onEvent?.(event, {
          // Forward the *resolved* id, not the raw one: an unattributed frame
          // belongs to the sole live watch, and the store routes by this id --
          // the original `undefined` would land it on whichever session happens
          // to be active (during a switch, the one being left).
          subscription_id: target.id,
          cursor,
        });
      } catch {
        this.fenceSubscription(gen, target.id, 'event_delivery_failed');
        return;
      }
      // The checks above already proved this is a forward move (or the first
      // delivered position), so the scanned cursor is committed as it stands.
      target.entry.cursor = cursor;
      return;
    }
    if (noti.method === 'runtime.subscription.complete') {
      if (socket !== this.ws || gen !== this.generation) return;
      const subId = params?.subscription_id;
      // A fenced watch stays fenced: a late completion of the dead subscription
      // must not advance the watch state (nor re-arm the console) while the
      // unreplayed gap is still open.
      if (this.isFencedFrame(subId, gen)) return;
      const target = this.resolveWatchTarget(subId);
      if (target === null) return;
      this.options.onSubscriptionNotice?.({
        type: 'complete',
        // The resolved id, so an unattributed completion is attributed to the
        // sole live watch instead of being treated as the active session's.
        subscription_id: target.id,
        cursor: params?.cursor,
      });
      return;
    }
    if (noti.method === 'runtime.subscription.error') {
      if (socket !== this.ws || gen !== this.generation) return;
      const subId = params?.subscription_id;
      // Same fence as `complete`: the subscription already reported its own
      // failure exactly once, so a later server-side error notice for it is not
      // a second, fresh failure.
      if (this.isFencedFrame(subId, gen)) return;
      const target = this.resolveWatchTarget(subId);
      if (target === null) return;
      const service_code = params?.error?.data?.service_code;
      this.options.onSubscriptionNotice?.({
        type: 'error',
        // Same resolution as `complete`: an unattributed error belongs to the
        // sole live watch, never to the active session by default.
        subscription_id: target.id,
        service_code,
      });
      return;
    }
  }

  /**
   * Whether one frame belongs to a subscription that already reported a frame
   * this client cannot consume.
   *
   * The fence is keyed by the failed subscription id *and* the connection
   * generation it failed in.  A frame that names no subscription at all cannot
   * be proven to belong to a healthy watch, so while any fence is open it is
   * fenced too.  A frame naming a subscription this client is not watching, from
   * the generation (or an older one) of an open fence, can only be a leftover of
   * the dead lease; a *live* watch is never fenced by another subscription's
   * fence.  Only a new `watchEvents()` for the fenced watch's session clears the
   * fence: it resumes from the last good cursor, which is exactly the replay the
   * fence preserves.
   */
  private isFencedFrame(subId: unknown, gen: number): boolean {
    if (this.fences.size === 0) return false;
    if (typeof subId !== 'string') return true;
    if (this.fences.has(subId)) return true;
    if (this.watches.has(subId)) return false;
    for (const fencedGeneration of this.fences.values()) {
      if (gen <= fencedGeneration) return true;
    }
    return false;
  }

  /**
   * Fence the subscription that produced an unconsumable frame and report it
   * exactly once per subscription.
   *
   * The watch's last good cursor is deliberately kept (a re-attach must re-read
   * the unreplayed sequence instead of skipping it), the subscription is marked
   * fenced so no later frame may be delivered or move its cursor, and nothing
   * else happens: no reconnect, no automatic retry and no connection teardown.
   * Only the client-local reason code travels in the notice, never the raw frame
   * or its payload.
   */
  private fenceSubscription(gen: number, subscriptionId: string, serviceCode: WatchFenceReason) {
    const alreadyFenced = this.fences.get(subscriptionId);
    this.fences.set(subscriptionId, gen);
    if (alreadyFenced !== undefined) return;
    this.options.onSubscriptionNotice?.({
      type: 'error',
      subscription_id: subscriptionId,
      service_code: serviceCode,
    });
  }

  /**
   * Resolve one `runtime.event` frame to the live watch it belongs to.
   *
   * A frame that names a subscription is routed to that watch; a frame naming
   * an unknown subscription is dropped.  A frame with *no* subscription id can
   * only be attributed when exactly one watch is live (and no fence is open), so
   * a legacy unattributed frame still reaches its sole consumer while an
   * ambiguous one is ignored rather than guessed.
   */
  private resolveWatchTarget(subId: unknown): { id: string; entry: WatchEntry } | null {
    if (typeof subId === 'string') {
      const entry = this.watches.get(subId);
      return entry === undefined ? null : { id: subId, entry };
    }
    if (this.fences.size > 0 || this.watches.size !== 1) return null;
    for (const [id, entry] of this.watches) return { id, entry };
    return null;
  }

  /** Whether any watch (live or fenced) already streams `session`. */
  private hasWatchForSession(session: SessionRef): boolean {
    for (const entry of this.watches.values()) {
      if (sameSession(entry.session, session)) return true;
    }
    return false;
  }

  /**
   * Detach every watch of one session (live or fenced) and lift its fences.
   *
   * Re-watching a session replaces its previous lease: the daemon-side
   * subscription behind it is dead (it reported an unconsumable frame, or the
   * socket that owned it dropped), so keeping it would both leak a slot against
   * the per-connection cap and let a stale frame be mistaken for the fresh one.
   */
  private detachWatchesForSession(session: SessionRef) {
    for (const [id, entry] of this.watches) {
      if (sameSession(entry.session, session)) {
        this.watches.delete(id);
        this.fences.delete(id);
      }
    }
  }

  private call<T>(method: WireMethod, params: any): Promise<T> {
    if (!this.ws || this.ws.readyState !== OPEN) {
      return Promise.reject(new ConnectionLostError('WebSocket is not connected', false));
    }
    const gen = this.generation;
    const id = ++this.reqId;
    const req: JsonRpcRequest = {
      jsonrpc: '2.0' as const,
      id,
      method,
      params: params || {},
    };
    return new Promise<T>((resolve, reject) => {
      const entry = { resolve, reject, gen, sent: false };
      this.pending.set(id, entry);
      try {
        this.ws!.send(JSON.stringify(req));
        entry.sent = true;
      } catch {
        this.pending.delete(id);
        reject(new ConnectionLostError('failed to send request', false));
      }
    });
  }

  public async negotiate(): Promise<NegotiateResult> {
    const params: NegotiateParams = {
      versions: [WIRE_VERSION],
      client: {
        name: 'synapse-web-console',
        version: '0.1.44',
      },
    };
    const result = await this.call<unknown>('runtime.protocol.negotiate', params);
    return parseNegotiateResult(result);
  }

  public async openSession(session: SessionRef): Promise<OpenSessionResult> {
    return this.call<OpenSessionResult>('runtime.session.open', { session });
  }

  public async submitTurn(params: SubmitTurnParams): Promise<CommandReceipt> {
    return this.call<CommandReceipt>('runtime.turn.submit', params);
  }

  public async steerTurn(params: SteerTurnParams): Promise<SteerTurnResult> {
    return this.call<SteerTurnResult>('runtime.turn.steer', params);
  }

  public async cancelTurn(params: CancelTurnParams): Promise<CancelTurnResult> {
    return this.call<CancelTurnResult>('runtime.turn.cancel', params);
  }

  /**
   * Close one session (`runtime.session.close`).
   *
   * Detaching a view is *not* a close and a close is not a cancel:
   * `cancel_active` defaults to `false`, so a session that still owns an active
   * turn is refused with the typed `conflict` service error instead of having
   * its turn cancelled as a side effect, and only an explicit
   * `cancel_active: true` requests the cancellation (the reply then reports the
   * captured `active_turn_id` and `cancellation_requested`).  Closing a missing
   * session is idempotent: the call resolves with `closed: false`.
   *
   * Nothing in this client calls it implicitly — disconnecting, dropping the
   * connection or switching project leaves the session running — so a close
   * always has to be an explicit caller decision.
   *
   * Optional keys are omitted rather than sent as `null`: the daemon decodes a
   * missing `cancel_active` as `false` and rejects an explicit `null`, so the
   * default is expressed by absence.  `command_id` is forwarded when the caller
   * supplies one (retry/idempotency key) and is otherwise left to the daemon;
   * the JSON-RPC request id stays owned by `call()`.
   */
  public async closeSession(params: CloseSessionCommand): Promise<CloseSessionResult> {
    const payload: Record<string, unknown> = { session: params.session };
    if (params.cancel_active !== undefined) payload.cancel_active = params.cancel_active;
    if (params.command_id !== undefined) payload.command_id = params.command_id;
    return this.call<CloseSessionResult>('runtime.session.close', payload);
  }

  /**
   * Register one `runtime.events.watch` lease.
   *
   * Each call registers an additional watch, so several sessions can stream
   * concurrently.  Watching a session that is already watched *replaces* its
   * previous lease (and lifts that session's fence): the old daemon-side
   * subscription is dead, so keeping it would leak a slot and let a stale frame
   * be mistaken for the fresh one.  At most `MAX_WATCHES` watches are held,
   * matching the daemon's per-connection cap; past it the call rejects with the
   * client-local twin of the daemon's own `transport_busy` error.
   */
  public async watchEvents(
    session: SessionRef,
    after?: number,
    queueSize?: number,
  ): Promise<WatchStartResult> {
    if (this.watches.size >= MAX_WATCHES && !this.hasWatchForSession(session)) {
      throw new RpcCallError(
        `watch limit reached (${MAX_WATCHES} concurrent subscriptions)`,
        -32001,
        'transport_busy',
      );
    }
    // A same-session watch this call is about to replace.  A *fenced* one whose
    // fence was raised on the current socket still holds a daemon lease (a
    // client-side fence does not kill it), so that lease is released best-effort
    // once the fresh watch is registered.  A fence from an older generation is
    // deliberately NOT unwatched: the daemon already closed that connection's
    // subscriptions with the socket, so there is no lease left to release -- and
    // this path stays exactly as it was before.
    const generation = this.generation;
    const orphaned: string[] = [];
    for (const [id, entry] of this.watches) {
      if (sameSession(entry.session, session) && this.fences.get(id) === generation) {
        orphaned.push(id);
      }
    }
    const res = await this.call<WatchStartResult>('runtime.events.watch', {
      session,
      after: after ?? 0,
      queue_size: queueSize ?? this.watchQueueSize,
    });
    // A successful watch is the explicit recovery from a fenced subscription:
    // it replaces this session's previous watch, so the new lease resumes from
    // the cursor the server returns and the fence is lifted here and only here
    // (never implicitly, and never for a rejected watch request, which leaves
    // the old fence in place).  Fences for other sessions stay open.
    this.detachWatchesForSession(session);
    this.watches.set(res.subscription_id, {
      session,
      cursor: isCursor(res.cursor) ? res.cursor : after ?? 0,
    });
    this.watchQueueSize = queueSize ?? this.watchQueueSize;
    // The replaced leases are released only while the socket that raised their
    // fences is still the live one; a drop during the handshake means the daemon
    // has already closed them.  Best-effort: a refused unwatch must not fail the
    // watch that just succeeded.
    if (this.generation === generation) {
      for (const id of orphaned) {
        // Never unwatch the subscription just registered: a live daemon assigns a
        // fresh id, but a peer (or a test transport) could reuse one, and
        // unwatching it would kill the fresh watch.
        if (id === res.subscription_id) continue;
        void this.call<UnwatchResult>('runtime.events.unwatch', { subscription_id: id }).catch(
          () => undefined,
        );
      }
    }
    return res;
  }

  /**
   * Detach one watch, or every watch when no id is given.
   *
   * The no-argument form is the legacy single-watch detach: it releases every
   * live watch and every fenced subscription (a fenced lease outlives a
   * client-side fence, so it is still released best-effort).  An explicit
   * `subscriptionId` detaches just that one.  No other state is touched: an idle
   * client sends nothing and returns `undefined` exactly as before.
   */
  public async unwatchEvents(subscriptionId?: string): Promise<UnwatchResult | undefined> {
    const ids =
      subscriptionId !== undefined
        ? this.watches.has(subscriptionId)
          ? [subscriptionId]
          : []
        : [...this.watches.keys()];
    for (const id of ids) {
      this.watches.delete(id);
      this.fences.delete(id);
    }
    if (ids.length === 0) return;
    const results = await Promise.all(
      ids.map((id) => this.call<UnwatchResult>('runtime.events.unwatch', { subscription_id: id })),
    );
    return results[results.length - 1];
  }

  public async getPendingApproval(session: SessionRef, expected_turn_id: string): Promise<PendingApprovalView> {
    return this.call<PendingApprovalView>('runtime.turn.approval.get', {
      session,
      expected_turn_id,
    });
  }

  public async resumeApproval(session: SessionRef, expected_turn_id: string, decisions: ApprovalDecision[]): Promise<ResumeTurnResult> {
    return this.call<ResumeTurnResult>('runtime.turn.approval.resume', {
      session,
      expected_turn_id,
      decisions,
    });
  }

  public async readEvents(session: SessionRef, after = 0, limit = 200): Promise<EventPage> {
    return this.call<EventPage>('runtime.events.read', {
      session,
      after,
      limit,
    });
  }

  public async getSession(session: SessionRef): Promise<SessionView> {
    return this.call<SessionView>('runtime.session.get', {
      session,
    });
  }

  /**
   * Read one session's persisted long-running goal
   * (`runtime.session.goal`); `null` means the thread has no goal.
   */
  public async getSessionGoal(session: SessionRef): Promise<unknown> {
    return this.call('runtime.session.goal', { session });
  }

  /**
   * Create one session's goal (`runtime.session.goal.set`).
   *
   * `token_budget` is optional and must be a positive integer; the server refuses
   * to overwrite an unfinished goal with `conflict`, so the caller must clear or
   * edit it first.
   */
  public async setSessionGoal(params: SetSessionGoalParams): Promise<SessionGoalResult> {
    const payload: Record<string, unknown> = {
      session: params.session,
      objective: params.objective,
    };
    if (params.token_budget !== undefined && params.token_budget !== null) {
      payload.token_budget = params.token_budget;
    }
    if (params.command_id !== undefined) payload.command_id = params.command_id;
    return this.call<SessionGoalResult>('runtime.session.goal.set', payload);
  }

  /**
   * Rewrite the current goal's objective (`runtime.session.goal.edit`).
   *
   * `expected_goal_id` must still be the persisted goal; a goal replaced in the
   * meantime answers `conflict` instead of being rewritten.
   */
  public async editSessionGoal(params: EditSessionGoalParams): Promise<SessionGoalResult> {
    return this.call<SessionGoalResult>('runtime.session.goal.edit', params);
  }

  /**
   * Remove the current goal (`runtime.session.goal.clear`).
   *
   * The result always carries `goal: null`: the thread has no goal afterwards.
   * Callers should confirm with the user before sending this.
   */
  public async clearSessionGoal(params: ClearSessionGoalParams): Promise<SessionGoalResult> {
    return this.call<SessionGoalResult>('runtime.session.goal.clear', params);
  }

  /**
   * Pause the current goal (`runtime.session.goal.pause`).
   *
   * The server also asks this session's own live turn to cancel and reports it
   * through `cancellation_requested`; no other session is touched.
   */
  public async pauseSessionGoal(params: PauseSessionGoalParams): Promise<SessionGoalResult> {
    return this.call<SessionGoalResult>('runtime.session.goal.pause', params);
  }

  /**
   * Resume the current goal (`runtime.session.goal.resume`).
   *
   * Status-only: no follow-up turn is started, so the caller must submit a turn
   * if the work should continue.
   */
  public async resumeSessionGoal(params: ResumeSessionGoalParams): Promise<SessionGoalResult> {
    return this.call<SessionGoalResult>('runtime.session.goal.resume', params);
  }
}
