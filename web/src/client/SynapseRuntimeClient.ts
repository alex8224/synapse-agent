import { SESSION_LIST_PAGE_SIZE, HISTORY_PAGE_SIZE } from './types.ts';
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
  ListSessionsParams,
  SessionListResult,
  ReadSessionHistoryParams,
  SessionHistoryResult,
  GetRuntimeConfigParams,
  RuntimeConfigResult,
  ReconcileSessionParams,
  SessionRecoverabilityResult,
  SetProjectThinkingLevelResult,
  SetThinkingLevelResult,
} from './types.ts';
import { parseRecoverabilityResult } from './recoverability.ts';
import {
  ARTIFACT_CHUNK_BYTES,
  ARTIFACT_LIST_LIMIT,
  parseArtifactChunk,
  parseArtifactMetadata,
  parseArtifactPage,
} from './artifacts.ts';
import type { ArtifactChunkView, ArtifactEntry, ArtifactPageView } from './artifacts.ts';

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
  onSubscriptionNotice?: (notice: {
    type: 'complete' | 'error';
    subscription_id?: string;
    service_code?: string;
    cursor?: number;
  }) => void;
  /** Test seam: inject a fake socket factory. Defaults to the global WebSocket. */
  socketFactory?: (url: string) => SocketLike;
  reconnect?: Partial<ReconnectPolicy>;
}

const OPEN = 1;

/** Longest close reason copied into user-visible copy (bounded, never a secret). */
const CLOSE_REASON_LIMIT = 120;

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
 * One persistent JSON-RPC WebSocket connection with:
 *
 * - per-socket connection generations so late responses / events / close
 *   frames from a replaced socket can never pollute the current connection;
 * - typed errors that keep the server `service_code` (`replay_gap`,
 *   `event_overflow`, `invalid_cursor`, ...) so callers can distinguish an
 *   explicit gap from a transient transport failure;
 * - an optional bounded reconnect budget (never infinite) that is armed only
 *   after a healthy `connect()` and cancelled by `disconnect()` / user close;
 * - cursor tracking for the active watch so a re-attach can resume from the
 *   exact last delivered sequence (no duplicate, no silent `after=0`).
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
  private activeSubscriptionId: string | null = null;
  private state: ConnectionState = 'disconnected';
  private generation = 0;
  private armed = false;
  private closingUser = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectAttempts = 0;
  private reconnectPolicy: ReconnectPolicy = { ...DEFAULT_RECONNECT_POLICY };
  private opening: Promise<void> | null = null;
  private openReject: ((err: ConnectionLostError) => void) | null = null;
  private watchCursor: number | null = null;
  private watchSession: SessionRef | null = null;
  private watchQueueSize = 128;

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

  /** Last delivered watch cursor (session sequence) or null before any watch. */
  public getWatchCursor(): number | null {
    return this.watchCursor;
  }

  public getWatchSession(): SessionRef | null {
    return this.watchSession;
  }

  /** Number of the current socket generation (observable for tests). */
  public getGeneration(): number {
    return this.generation;
  }

  public getPendingCount(): number {
    return this.pending.size;
  }

  public async reloadMcp(params: ReloadMcpParams): Promise<ReloadMcpResult> {
    return this.call<ReloadMcpResult>('runtime.session.mcp.reload', params);
  }

  public async listSessions(params: ListSessionsParams): Promise<SessionListResult> {
    return this.call<SessionListResult>('runtime.session.list', {
      project_id: params.project_id,
      limit: params.limit ?? SESSION_LIST_PAGE_SIZE,
      offset: params.offset ?? 0,
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
    if (this.options.socketFactory) {
      return this.options.socketFactory(url);
    }
    return new WebSocket(url) as unknown as SocketLike;
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
    const socket = this.createSocket(this.options.url);
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
      console.error('Failed to parse wire message:', raw, err);
      return;
    }
    if (data.method && !data.id) {
      this.handleNotification(socket, gen, data as JsonRpcNotification);
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

  private handleNotification(socket: SocketLike, gen: number, noti: JsonRpcNotification) {
    const params = noti.params ?? {};
    if (noti.method === 'runtime.event' || noti.method === 'runtime.events.notification') {
      if (socket !== this.ws || gen !== this.generation) return;
      const subId = params?.subscription_id;
      if (subId !== undefined && subId !== this.activeSubscriptionId) return;
      const cursor = params?.cursor;
      if (typeof cursor === 'number' && cursor >= 0) {
        if (this.watchCursor === null || cursor > this.watchCursor) {
          this.watchCursor = cursor;
        }
      }
      this.options.onEvent?.(params?.event as RuntimeEvent, {
        subscription_id: params?.subscription_id,
        cursor: params?.cursor,
      });
      return;
    }
    if (noti.method === 'runtime.subscription.complete') {
      if (socket !== this.ws || gen !== this.generation) return;
      if (params?.subscription_id !== undefined && params.subscription_id !== this.activeSubscriptionId) {
        return;
      }
      this.options.onSubscriptionNotice?.({
        type: 'complete',
        subscription_id: params?.subscription_id,
        cursor: params?.cursor,
      });
      return;
    }
    if (noti.method === 'runtime.subscription.error') {
      if (socket !== this.ws || gen !== this.generation) return;
      if (params?.subscription_id !== undefined && params.subscription_id !== this.activeSubscriptionId) {
        return;
      }
      const service_code = params?.error?.data?.service_code;
      this.options.onSubscriptionNotice?.({
        type: 'error',
        subscription_id: params?.subscription_id,
        service_code,
      });
      return;
    }
  }

  private call<T>(method: string, params: any): Promise<T> {
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

  public async negotiate(): Promise<any> {
    const params: NegotiateParams = {
      versions: ['1'],
      client: {
        name: 'synapse-web-console',
        version: '0.1.44',
      },
    };
    return this.call('runtime.protocol.negotiate', params);
  }

  public async openSession(session: SessionRef): Promise<OpenSessionResult> {
    return this.call<OpenSessionResult>('runtime.session.open', { session });
  }

  public async submitTurn(params: SubmitTurnParams): Promise<CommandReceipt> {
    return this.call<CommandReceipt>('runtime.turn.submit', params);
  }

  public async steerTurn(params: SteerTurnParams): Promise<any> {
    return this.call('runtime.turn.steer', params);
  }

  public async cancelTurn(params: CancelTurnParams): Promise<any> {
    return this.call('runtime.turn.cancel', params);
  }

  public async watchEvents(
    session: SessionRef,
    after?: number,
    queueSize?: number,
  ): Promise<{ subscription_id: string; cursor: number }> {
    const res = await this.call<{ subscription_id: string; cursor: number }>(
      'runtime.events.watch',
      {
        session,
        after: after ?? 0,
        queue_size: queueSize ?? this.watchQueueSize,
      },
    );
    this.activeSubscriptionId = res.subscription_id;
    this.watchCursor = typeof res.cursor === 'number' ? res.cursor : after ?? 0;
    this.watchSession = session;
    this.watchQueueSize = queueSize ?? this.watchQueueSize;
    return res;
  }

  public async unwatchEvents(): Promise<any> {
    if (!this.activeSubscriptionId) return;
    const subId = this.activeSubscriptionId;
    this.activeSubscriptionId = null;
    this.watchCursor = null;
    this.watchSession = null;
    return this.call('runtime.events.unwatch', { subscription_id: subId });
  }

  public async getPendingApproval(session: SessionRef, expected_turn_id: string): Promise<PendingApprovalView> {
    return this.call<PendingApprovalView>('runtime.turn.approval.get', {
      session,
      expected_turn_id,
    });
  }

  public async resumeApproval(session: SessionRef, expected_turn_id: string, decisions: ApprovalDecision[]): Promise<any> {
    return this.call('runtime.turn.approval.resume', {
      session,
      expected_turn_id,
      decisions,
    });
  }

  public async readEvents(session: SessionRef, after = 0, limit = 200): Promise<{ events: RuntimeEvent[]; cursor: { sequence: number }; latest_sequence: number }> {
    return this.call('runtime.events.read', {
      session,
      after,
      limit,
    });
  }

  public async getSession(session: SessionRef): Promise<any> {
    return this.call('runtime.session.get', {
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
}
