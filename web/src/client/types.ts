/**
 * Strict JSON-RPC 2.0 types and DTOs for Synapse Agent Runtime Service.
 */

export interface SessionRef {
  project_id: string;
  thread_id: string;
}

export interface ReloadMcpParams {
  session: SessionRef;
  server: string;
  enabled: boolean;
  command_id?: string;
}

export interface ReloadMcpResult {
  command_id: string;
  session: SessionRef;
  server: string;
  enabled: boolean;
  attached: boolean;
  active_servers: string[];
  tool_count: number;
  warnings: string[];
}

export interface RebindSessionParams {
  session: SessionRef;
  model: string;
  command_id?: string;
}

export interface RebindSessionResult {
  command_id: string;
  session: SessionRef;
  model: string;
  view: OpenSessionResult['view'];
}

export interface JsonRpcRequest<T = any> {
  jsonrpc: '2.0';
  id: string | number;
  method: string;
  params: T;
}

export interface JsonRpcResponse<T = any> {
  jsonrpc: '2.0';
  id: string | number;
  result?: T;
  error?: {
    code: number;
    message: string;
    data?: {
      service_code?: string;
    };
  };
  meta?: {
    wire_version: string;
  };
}

export interface JsonRpcNotification<T = any> {
  jsonrpc: '2.0';
  method: string;
  params: T;
  meta?: {
    wire_version: string;
  };
}

export interface NegotiateParams {
  versions: string[];
  client?: {
    name: string;
    version: string;
  };
}

export interface CommandReceipt {
  command_id: string;
  accepted_at: string;
  turn_id?: string;
}

export interface OpenSessionResult {
  session: SessionRef;
  created: boolean;
  opened_at: string;
  view?: {
    project_id: string;
    thread_id: string;
    status: string;
    active_turn_id: string | null;
    latest_sequence: number;
    last_activity_at?: string | null;
    last_error?: string | null;
    active_model?: string | null;
    model?: string | null;
  };
}

export interface SubmitTurnParams {
  session: SessionRef;
  text: string;
  command_id?: string;
  config_overrides?: Record<string, any>;
}

export interface SteerTurnParams {
  session: SessionRef;
  expected_turn_id: string;
  text: string;
  command_id?: string;
}

export interface CancelTurnParams {
  session: SessionRef;
  expected_turn_id: string;
  reason?: string;
  command_id?: string;
}

export interface RuntimeEvent {
  sequence: number;
  turn_id: string;
  turn_sequence: number;
  kind: string;
  timestamp: string;
  payload: Record<string, any>;
}

export interface ApprovalActionView {
  index: number;
  name: string;
  args: Record<string, any>;
  description?: string;
  allowed_decisions?: string[];
}

export interface PendingApprovalView {
  turn_id: string;
  actions: ApprovalActionView[];
}

export interface ApprovalDecision {
  kind: 'allow_once' | 'allow_always' | 'reject_once' | 'reject_always';
  message?: string | null;
}

export interface WatchEventsParams {
  session: SessionRef;
  after?: number;
  queue_size?: number;
  filter?: {
    kinds?: string[];
    turn_ids?: string[];
  };
}

// --- Session list / history DTOs (runtime.session.list / runtime.session.history) ---

/** Bounded page size for `runtime.session.list` (backend default is 50, cap 100). */
export const SESSION_LIST_PAGE_SIZE = 50;
/** Bounded page size for `runtime.session.history` (backend default is 20, cap 100). */
export const HISTORY_PAGE_SIZE = 20;

export interface SessionMetadataItem {
  thread_id: string;
  title: string;
  model: string | null;
  active_model: string | null;
  created_at: string;
  updated_at: string;
  summary: string | null;
}

export interface ListSessionsParams {
  project_id: string;
  limit?: number;
  offset?: number;
}

export interface SessionListResult {
  items: SessionMetadataItem[];
  next_offset: number | null;
  total: number;
}

export interface HistoryToolCall {
  id?: string;
  name?: string;
  args?: Record<string, any>;
  [key: string]: any;
}

export interface HistoryToolResult {
  id?: string;
  name?: string;
  content?: string;
  status?: string;
  [key: string]: any;
}

/**
 * One structured transcript projection event. `kind` is one of
 * `user | answer | thought | tools | meta`; `tool_calls`/`tool_results`
 * carry plain JSON dicts, never LangChain objects.
 */
export interface HistoryEvent {
  kind: string;
  text: string;
  tool_calls: HistoryToolCall[];
  tool_results: HistoryToolResult[];
}

export interface ReadSessionHistoryParams {
  session: SessionRef;
  /** Fetch turns strictly before this turn number; `null` fetches the newest page. */
  before_turn?: number | null;
  limit?: number;
}

export interface SessionHistoryResult {
  events: HistoryEvent[];
  start_turn: number;
  end_turn: number;
  total_turns: number;
  has_more: boolean;
  /** `false` means no transcript projection exists; never render as empty history. */
  available: boolean;
}

// --- Read-only runtime configuration (runtime.config.get) ---

/**
 * One whitelisted MCP server projection. Never carries `command` / `args` /
 * `env` / `url` / `headers` / API-key fields.
 */
export interface McpServerView {
  name: string;
  transport: string;
  enabled: boolean;
  tool_prefix: string | null;
}

/**
 * Read-only effective runtime configuration for one session context.
 *
 * `can_set_thinking` / `can_toggle_mcp_global` are capability flags: the backend
 * reports `can_set_thinking = true` because the session-scoped
 * `runtime.session.thinking.set` write port exists, while
 * `can_toggle_mcp_global` stays `false` (no global write path). Clients must
 * render a control as read-only whenever its flag is false instead of pretending
 * a save could succeed. The view never carries goals, attachment state, API
 * keys, env vars, headers, URLs, commands, or args.
 */
export interface RuntimeConfigResult {
  current_model: string;
  available_models: string[];
  thinking_level: string | null;
  thinking_levels: string[];
  mcp_servers: McpServerView[];
  mcp_enabled: boolean;
  can_set_thinking: boolean;
  can_toggle_mcp_global: boolean;
  /**
   * The project's own default reasoning level (`null` when the peer does not
   * report one). It is not necessarily `thinking_level`: a session may have
   * rebound its own level, and the project default only applies to sessions
   * opened afterwards.
   */
  project_thinking_level?: string | null;
  /** Whether `runtime.project.thinking.set` is available on this peer. */
  can_set_project_thinking?: boolean;
}

/**
 * Result of the session-scoped reasoning-level write
 * (`runtime.session.thinking.set`).
 *
 * `level` is the canonical applied label (`off` when thinking was disabled) and
 * `view` is the refreshed config projection, so the console can render the new
 * state without a second round trip.
 */
export interface SetThinkingLevelResult {
  command_id: string;
  session: SessionRef;
  level: string;
  view: RuntimeConfigResult;
}

/**
 * Result of the project-scoped reasoning-default write
 * (`runtime.project.thinking.set`).
 *
 * `level` is the canonical label now stored in the project's settings layer.
 * The current session is deliberately unaffected: a project default applies to
 * sessions opened afterwards, and the target file is never reported (the read
 * surface does not hand out workspace-absolute paths).
 */
export interface SetProjectThinkingLevelResult {
  command_id: string;
  project_id: string;
  level: string;
}

export interface GetRuntimeConfigParams {
  session: SessionRef;
}

/** Context passed with each live event notification (`runtime.event`). */
export interface EventNotificationMeta {
  subscription_id?: string;
  cursor?: number;
}

// --- Read-only history/live recovery reconcile (runtime.session.reconcile) ---

/** One durable transcript membership answer for a probed turn id. */
export interface TurnCoverageProbe {
  turn_id: string;
  covered: boolean;
}

export interface ReconcileSessionParams {
  session: SessionRef;
  /**
   * Bounded probe set (max 32): turn ids already seen on the live stream whose
   * durable coverage the snapshot should confirm/refute. Duplicates collapse.
   */
  probe_turn_ids?: string[];
}

/**
 * One read-only recovery snapshot (history coverage + live broker state).
 *
 * - durable: `history_available` (false !== empty), `history_total_turns`, and
 *   `probe[]` coverage membership;
 * - live: `live_epoch` stream identity (session reopen / daemon restart
 *   changes it), retention bounds (`oldest_sequence`/`dropped_through`), and
 *   the newest observed turn replay boundary (`latest_turn_*`).
 *
 * A recovery client uses this as an explicit precondition and never fakes a
 * full restore with `after=0`.
 */
export interface SessionRecoverabilityResult {
  project_id: string;
  thread_id: string;
  history_available: boolean;
  history_total_turns: number;
  live_epoch: string;
  live_latest_sequence: number;
  live_oldest_sequence: number;
  live_dropped_through: number;
  active_turn_id: string | null;
  latest_turn_id: string | null;
  latest_turn_first_sequence: number | null;
  latest_turn_retained_from: number | null;
  latest_turn_intact: boolean;
  probe: TurnCoverageProbe[];
}
