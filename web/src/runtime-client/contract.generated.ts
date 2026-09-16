/**
 * GENERATED FILE - DO NOT EDIT.
 *
 * Authoritative source: src/synapse/runtime/service/contract_registry.py
 * Regenerate: uv run --no-sync python scripts/export_contract_manifest.py
 * Verify:     uv run --no-sync python scripts/export_contract_manifest.py --check
 *
 * contract_version=1 wire_version="1" event_version=1
 *
 * Field comments record the *Python DTO* default, which is not a statement about
 * the wire rules: the wire defaults are declared per method in the manifest.
 * Nothing here carries UI-only data (consumer lists, timestamps, presentation
 * flags); unknown event kinds stay compatible through the fallback payload type.
 */

export const CONTRACT_VERSION = 1;
export const WIRE_VERSION = "1";
export const EVENT_VERSION = 1;

export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

/**
 * The 48 wire methods: 46 service methods
 * plus the connection-state methods runtime.protocol.negotiate and
 * runtime.events.unwatch.
 */
export const WIRE_METHODS = [
  "runtime.artifacts.list",
  "runtime.artifacts.read",
  "runtime.artifacts.stat",
  "runtime.attachments.abort",
  "runtime.attachments.append",
  "runtime.attachments.begin",
  "runtime.attachments.finish",
  "runtime.attachments.read",
  "runtime.attachments.stat",
  "runtime.codex.reset_credits.consume",
  "runtime.codex.reset_credits.get",
  "runtime.codex.usage.get",
  "runtime.config.get",
  "runtime.events.read",
  "runtime.events.unwatch",
  "runtime.events.watch",
  "runtime.fs.list",
  "runtime.git.diff",
  "runtime.git.status",
  "runtime.project.list",
  "runtime.project.register",
  "runtime.project.thinking.set",
  "runtime.protocol.negotiate",
  "runtime.session.close",
  "runtime.session.create",
  "runtime.session.delete",
  "runtime.session.get",
  "runtime.session.goal",
  "runtime.session.goal.clear",
  "runtime.session.goal.edit",
  "runtime.session.goal.pause",
  "runtime.session.goal.resume",
  "runtime.session.goal.set",
  "runtime.session.history",
  "runtime.session.list",
  "runtime.session.mcp.reload",
  "runtime.session.open",
  "runtime.session.rebind",
  "runtime.session.reconcile",
  "runtime.session.rename",
  "runtime.session.search",
  "runtime.session.thinking.set",
  "runtime.turn.approval.get",
  "runtime.turn.approval.resume",
  "runtime.turn.cancel",
  "runtime.turn.steer",
  "runtime.turn.submit",
  "runtime.workspace.revert",
] as const;
export type WireMethod = (typeof WIRE_METHODS)[number];

/**
 * Protocol feature flags returned by runtime.protocol.negotiate.
 * They are transport features only and never take part in ACL checks.
 */
export const PROTOCOL_FEATURES = {
  approval_resume: true,
  legacy_v1: true,
  raw_cursor: true,
  watch_resume: true,
} as const;
export type ProtocolFeature = keyof typeof PROTOCOL_FEATURES;

/** Authorization capabilities enforced by the ACL layer (33). */
export const AUTHORIZATION_CAPABILITIES = [
  "artifacts.list",
  "artifacts.read",
  "artifacts.stat",
  "attachments.read",
  "attachments.write",
  "codex.reset.consume",
  "codex.usage.read",
  "events.read",
  "events.watch",
  "fs.list",
  "git.diff",
  "git.status",
  "project.list",
  "project.register",
  "project.thinking",
  "session.close",
  "session.create",
  "session.delete",
  "session.goal",
  "session.list",
  "session.mcp.reload",
  "session.open",
  "session.read",
  "session.rebind",
  "session.rename",
  "session.search",
  "session.thinking",
  "turn.approval.read",
  "turn.approval.resume",
  "turn.cancel",
  "turn.steer",
  "turn.submit",
  "workspace.revert",
] as const;
export type AuthorizationCapability =
  (typeof AUTHORIZATION_CAPABILITIES)[number];

/**
 * HITL decision kinds accepted by runtime.turn.approval.resume.
 */
export const APPROVAL_DECISION_KINDS = [
  "allow_once",
  "allow_always",
  "reject_once",
  "reject_always",
] as const;
export type ApprovalDecisionKind = (typeof APPROVAL_DECISION_KINDS)[number];

/**
 * Server-to-client notification methods pushed outside a response.
 */
export const TRANSPORT_NOTIFICATIONS = [
  "runtime.event",
  "runtime.subscription.complete",
  "runtime.subscription.error",
] as const;

/**
 * Authorization capability per service method.
 */
export const WIRE_METHOD_CAPABILITIES: Partial<
  Record<WireMethod, AuthorizationCapability>
> = {
  "runtime.artifacts.list": "artifacts.list",
  "runtime.artifacts.read": "artifacts.read",
  "runtime.artifacts.stat": "artifacts.stat",
  "runtime.attachments.abort": "attachments.write",
  "runtime.attachments.append": "attachments.write",
  "runtime.attachments.begin": "attachments.write",
  "runtime.attachments.finish": "attachments.write",
  "runtime.attachments.read": "attachments.read",
  "runtime.attachments.stat": "attachments.read",
  "runtime.codex.reset_credits.consume": "codex.reset.consume",
  "runtime.codex.reset_credits.get": "codex.usage.read",
  "runtime.codex.usage.get": "codex.usage.read",
  "runtime.config.get": "session.read",
  "runtime.events.read": "events.read",
  "runtime.events.watch": "events.watch",
  "runtime.fs.list": "fs.list",
  "runtime.git.diff": "git.diff",
  "runtime.git.status": "git.status",
  "runtime.project.list": "project.list",
  "runtime.project.register": "project.register",
  "runtime.project.thinking.set": "project.thinking",
  "runtime.session.close": "session.close",
  "runtime.session.create": "session.create",
  "runtime.session.delete": "session.delete",
  "runtime.session.get": "session.read",
  "runtime.session.goal": "session.read",
  "runtime.session.goal.clear": "session.goal",
  "runtime.session.goal.edit": "session.goal",
  "runtime.session.goal.pause": "session.goal",
  "runtime.session.goal.resume": "session.goal",
  "runtime.session.goal.set": "session.goal",
  "runtime.session.history": "session.read",
  "runtime.session.list": "session.list",
  "runtime.session.mcp.reload": "session.mcp.reload",
  "runtime.session.open": "session.open",
  "runtime.session.rebind": "session.rebind",
  "runtime.session.reconcile": "session.read",
  "runtime.session.rename": "session.rename",
  "runtime.session.search": "session.search",
  "runtime.session.thinking.set": "session.thinking",
  "runtime.turn.approval.get": "turn.approval.read",
  "runtime.turn.approval.resume": "turn.approval.resume",
  "runtime.turn.cancel": "turn.cancel",
  "runtime.turn.steer": "turn.steer",
  "runtime.turn.submit": "turn.submit",
  "runtime.workspace.revert": "workspace.revert",
};

// --- DTOs, event payloads, and transport-only shapes ----------------------

export interface AbortAttachmentCommand {
  ref: AttachmentRef;
}

export interface AbortAttachmentResult {
  ref: AttachmentRef;
  removed: boolean;
}

export interface ActivityPayload {
  phase: string;
  /**
   * python_default_kind=value python_default=""
   */
  detail: string;
  /**
   * python_default_kind=value python_default=false
   */
  reset_timer: boolean;
}

/**
 * ``data_base64`` is bounded to 349528 characters
 * (a 256 KiB chunk) so one chunk always fits inside the 1 MiB frame cap.
 */
export interface AppendAttachmentChunkCommand {
  ref: AttachmentRef;
  expected_offset: number;
  data_base64: string;
}

export interface AppendAttachmentChunkResult {
  ref: AttachmentRef;
  received_bytes: number;
  next_offset: number;
}

export interface ApprovalActionPayload {
  name: string;
  /**
   * python_default_kind=factory python_default="dict"
   * Arbitrary JSON object copied from the HITL interrupt; no field schema is promised.
   */
  args: Record<string, JsonValue>;
  /**
   * python_default_kind=value python_default=""
   */
  description: string;
  /**
   * python_default_kind=value python_default=[]
   */
  allowed_decisions: string[];
}

export interface ApprovalActionView {
  index: number;
  name: string;
  /**
   * Arbitrary JSON object supplied by the approval producer; the value is deep-copied and must be JSON-serializable, but no field schema is promised.
   */
  args: JsonValue;
}

/**
 * ``kind`` is one of APPROVAL_DECISION_KINDS; ``message`` is optional and
 * bounded to 256 bytes by the wire decoder.
 */
export interface ApprovalDecision {
  /**
   * One of APPROVAL_DECISION_KINDS; anything else is invalid_params.
   */
  kind: ApprovalDecisionKind;
  /**
   * python_default_kind=value python_default=null
   */
  message?: string | null;
}

export interface ApprovalPayload {
  /**
   * python_default_kind=value python_default=[]
   */
  actions: ApprovalActionPayload[];
}

export interface ArtifactChunk {
  ref: ArtifactRef;
  offset: number;
  data_base64: string;
  byte_length: number;
  next_offset: number;
  eof: boolean;
  metadata: ArtifactMetadata;
}

export interface ArtifactMetadata {
  ref: ArtifactRef;
  path: string;
  kind: string;
  size: number;
  modified_at: string | null;
  media_type: string;
  revision: string | null;
}

export interface ArtifactPage {
  session: SessionRef;
  path: string;
  entries: ArtifactMetadata[];
  next_cursor: string | null;
}

/**
 * Session-scoped workspace-relative POSIX path; ``path`` is logical, never absolute.
 */
export interface ArtifactRef {
  session: SessionRef;
  path: string;
}

export interface AttachmentChunk {
  ref: AttachmentRef;
  offset: number;
  data_base64: string;
  byte_length: number;
  next_offset: number;
  eof: boolean;
  metadata: AttachmentMetadata;
}

export interface AttachmentMetadata {
  ref: AttachmentRef;
  size: number;
  mime: string;
  revision: string | null;
  display_name: string;
  created_at: string;
  finalized: boolean;
}

/**
 * Session-scoped opaque attachment id (<project, thread, 128-bit hex id>);
 * the id is server-generated and is never used as a path segment.
 */
export interface AttachmentRef {
  session: SessionRef;
  attachment_id: string;
}

/**
 * ``size`` is bounded to 1..4000000 bytes and
 * ``mime`` must be an allowed image type; ``display_name`` is optional and
 * display-only (it never becomes a path segment).
 */
export interface BeginAttachmentCommand {
  session: SessionRef;
  size: number;
  mime: string;
  /**
   * python_default_kind=value python_default=""
   */
  display_name?: string;
}

export interface BeginAttachmentResult {
  ref: AttachmentRef;
  chunk_bytes: number;
  chunk_base64_chars: number;
  expires_at: string;
  /**
   * python_default_kind=value python_default=0
   */
  next_offset: number;
}

export interface CancelTurnCommand {
  session: SessionRef;
  expected_turn_id: string;
  /**
   * python_default_kind=value python_default="user"
   */
  reason?: string;
  /**
   * python_default_kind=factory python_default="<lambda>"
   */
  command_id?: string;
}

export interface CancelTurnResult {
  command_id: string;
  session: SessionRef;
  turn_id: string;
  cancellation_requested: boolean;
}

/**
 * ``expected_goal_id`` must still be the persisted goal (otherwise ``conflict``).
 */
export interface ClearSessionGoalCommand {
  session: SessionRef;
  expected_goal_id: string;
  /**
   * python_default_kind=factory python_default="<lambda>"
   */
  command_id?: string;
}

export interface CloseSessionCommand {
  session: SessionRef;
  /**
   * python_default_kind=value python_default=false
   */
  cancel_active?: boolean;
  /**
   * python_default_kind=factory python_default="<lambda>"
   */
  command_id?: string;
}

export interface CloseSessionResult {
  command_id: string;
  session: SessionRef;
  closed: boolean;
  active_turn_id: string | null;
  cancellation_requested: boolean;
}

export interface CodexConsumeResult {
  session: SessionRef;
  model: string;
  command_id: string;
  /**
   * One of CODEX_CONSUME_OUTCOMES.
   */
  outcome: 'reset' | 'alreadyRedeemed' | 'nothingToReset' | 'noCredit' | 'unknown';
}

export interface CodexResetCredit {
  id: string;
  reset_type: string;
  status: string;
  granted_at: number | null;
  expires_at: number | null;
  title: string | null;
  description: string | null;
}

export interface CodexResetCreditsView {
  session: SessionRef;
  model: string;
  available_count: number;
  credits: CodexResetCredit[];
}

export interface CodexUsageView {
  session: SessionRef;
  model: string;
  primary: CodexUsageWindow | null;
  secondary: CodexUsageWindow | null;
  captured_at: number;
  available_reset_count: number | null;
}

export interface CodexUsageWindow {
  used_percent: number | null;
  window_minutes: number | null;
  reset_at: number | null;
}

/**
 * A receipt is returned only after the turn actually started; it never
 * carries a runtime handle.  There is no ``accepted_at`` field.
 */
export interface CommandReceipt {
  command_id: string;
  session: SessionRef;
  turn_id: string;
  /**
   * python_default_kind=value python_default=true
   */
  accepted: boolean;
}

/**
 * ``confirmed`` must be the literal true; the wire rejects anything else, so
 * a caller cannot redeem a credit without explicit confirmation.
 */
export interface ConsumeCodexResetCommand {
  session: SessionRef;
  expected_model: string;
  credit_id: string;
  command_id: string;
  confirmed: boolean;
}

/**
 * Metadata-only create: it persists the session row and never opens a runtime or builds an agent.
 * ``thread_id`` is optional; when it is absent the server allocates the real id and returns it, so a client never invents one.
 * ``title`` is optional, non-empty, and at most 120 characters.
 */
export interface CreateSessionCommand {
  project_id: string;
  /**
   * python_default_kind=value python_default=null
   */
  title?: string | null;
  /**
   * python_default_kind=value python_default=null
   */
  thread_id?: string | null;
  /**
   * python_default_kind=factory python_default="<lambda>"
   */
  command_id?: string;
}

/**
 * ``session`` carries the real persisted identity (server-allocated when the request omitted ``thread_id``); ``created`` is false for an idempotent re-create of an existing row.
 */
export interface CreateSessionResult {
  command_id: string;
  session: SessionRef;
  created: boolean;
  title: string;
}

/**
 * Removes the metadata row and the thread goal only; LangGraph checkpoints and the transcript projection are retained.
 */
export interface DeleteSessionCommand {
  session: SessionRef;
  /**
   * python_default_kind=factory python_default="<lambda>"
   */
  command_id?: string;
}

/**
 * ``retained_history`` is always true: checkpoints and the transcript projection are kept, so a UI must not claim the conversation was erased.
 */
export interface DeleteSessionResult {
  command_id: string;
  session: SessionRef;
  deleted: boolean;
  /**
   * python_default_kind=value python_default=true
   */
  retained_history: boolean;
}

export interface DiffPayload {
  call_id: string;
  path: string;
  new_text: string;
  /**
   * python_default_kind=value python_default=null
   */
  old_text: string | null;
}

/**
 * One immediate sub-directory: its display name and absolute host path.
 */
export interface DirectoryEntry {
  name: string;
  path: string;
}

/**
 * One bounded directory listing.  ``parent`` is null at a filesystem root;
 * ``truncated`` marks that ``entries`` hit the caller's limit.
 * ``roots`` are the platform's top-level entry points (drives on Windows,
 * mounts on POSIX) so a picker can jump between them.
 */
export interface DirectoryListing {
  path: string;
  parent: string | null;
  entries: DirectoryEntry[];
  truncated: boolean;
  roots: string[];
}

/**
 * ``expected_goal_id`` must still be the persisted goal (otherwise ``conflict``); ``objective`` follows the same bounds as ``set``.
 */
export interface EditSessionGoalCommand {
  session: SessionRef;
  expected_goal_id: string;
  objective: string;
  /**
   * python_default_kind=factory python_default="<lambda>"
   */
  command_id?: string;
}

export interface EventCursor {
  sequence: number;
}

/**
 * Request-only AND filter: when ``filter`` is present the wire requires both
 * ``kinds`` and ``turn_ids``; each is a list of strings.
 */
export interface EventFilter {
  kinds: string[];
  turn_ids: string[];
}

/**
 * Params of the runtime.event notification pushed for one live event.
 */
export interface EventNotification {
  subscription_id: string;
  event: RuntimeEvent;
  cursor: number;
}

/**
 * Consumer-side view of the runtime.event notification context.
 */
export interface EventNotificationMeta {
  subscription_id?: string;
  cursor?: number;
}

export interface EventPage {
  session: SessionRef;
  events: RuntimeEvent[];
  cursor: EventCursor;
  latest_sequence: number;
  /**
   * python_default_kind=value python_default=false
   */
  has_more: boolean;
  /**
   * python_default_kind=value python_default=null
   */
  scanned_through: EventCursor | null;
}

export interface FinishAttachmentCommand {
  ref: AttachmentRef;
  expected_size: number;
  expected_mime: string;
}

export interface FinishAttachmentResult {
  ref: AttachmentRef;
  size: number;
  mime: string;
  revision: string;
}

export interface GetCodexResetCreditsQuery {
  session: SessionRef;
  /**
   * python_default_kind=value python_default=false
   */
  force?: boolean;
}

export interface GetCodexUsageQuery {
  session: SessionRef;
  /**
   * python_default_kind=value python_default=false
   */
  force?: boolean;
}

export interface GetRuntimeConfigQuery {
  session: SessionRef;
}

export interface GetSessionGoalQuery {
  session: SessionRef;
}

export interface GetSessionQuery {
  session: SessionRef;
}

export interface GitDiffQuery {
  session: SessionRef;
  path: string;
  /**
   * python_default_kind=value python_default=false
   */
  staged?: boolean;
}

/**
 * ``text`` is capped at 262144 bytes and then
 * ``truncated`` is true; ``binary`` means the diff was not decoded and
 * ``empty`` means there is nothing to show (unchanged or untracked).
 */
export interface GitDiffResult {
  path: string;
  text: string;
  binary: boolean;
  truncated: boolean;
  empty: boolean;
}

export interface GitFileChange {
  path: string;
  index_status: string;
  worktree_status: string;
}

export interface GitStatusQuery {
  session: SessionRef;
}

/**
 * ``files`` is capped at 200 entries and then
 * ``truncated`` is true; ``branch`` is null on a detached HEAD.
 * ``insertions``/``deletions`` are the tracked line counts from
 * `git diff --numstat HEAD` (staged and unstaged combined, never
 * summed twice); they are null when git cannot answer, and binary or
 * untracked changes contribute no lines.
 */
export interface GitStatusResult {
  branch: string | null;
  upstream: string | null;
  ahead: number;
  behind: number;
  dirty: boolean;
  files: GitFileChange[];
  truncated: boolean;
  insertions: number | null;
  deletions: number | null;
}

/**
 * Durable metadata for one image attached to a persisted user turn: the
 * opaque ``attachment_id`` is loaded through ``runtime.attachments.read``
 * and ``image_id`` is the per-turn ``[image#N]`` placeholder.  No base64
 * image bytes are ever carried here.
 */
export interface HistoryAttachment {
  attachment_id: string;
  image_id: number;
  name: string;
  mime: string;
  size: number;
  /**
   * python_default_kind=value python_default=null
   */
  revision: string | null;
}

/**
 * ``kind`` is one of user / answer / thought / tools / meta;
 * ``tool_calls`` / ``tool_results`` are plain JSON dicts, never LangChain objects.
 */
export interface HistoryEvent {
  kind: string;
  text: string;
  tool_calls: Record<string, JsonValue>[];
  tool_results: Record<string, JsonValue>[];
  /**
   * python_default_kind=value python_default=[]
   */
  attachments: HistoryAttachment[];
  /**
   * python_default_kind=value python_default=[]
   */
  changes: TurnChange[];
  /**
   * python_default_kind=value python_default=0
   */
  changes_total: number;
  /**
   * python_default_kind=value python_default=[]
   */
  reverted_paths: string[];
  /**
   * python_default_kind=value python_default=null
   */
  turn_id: string | null;
  /**
   * python_default_kind=value python_default=null
   */
  elapsed_s: number | null;
}

export interface JsonRpcError {
  code: number;
  message: string;
  data?: JsonRpcErrorData;
}

export interface JsonRpcErrorData {
  service_code?: string;
}

export interface JsonRpcMeta {
  wire_version: string;
}

export interface JsonRpcNotification<T = JsonValue> {
  jsonrpc: '2.0';
  method: string;
  params: T;
  meta?: JsonRpcMeta;
}

export interface JsonRpcRequest<T = JsonValue> {
  jsonrpc: '2.0';
  id: string | number;
  method: string;
  params: T;
}

export interface JsonRpcResponse<T = JsonValue> {
  jsonrpc: '2.0';
  id: string | number;
  result?: T;
  error?: JsonRpcError;
  meta?: JsonRpcMeta;
}

export interface ListArtifactsQuery {
  session: SessionRef;
  /**
   * python_default_kind=value python_default="."
   */
  path?: string;
  /**
   * python_default_kind=value python_default=null
   */
  cursor?: string | null;
  /**
   * python_default_kind=value python_default=100
   */
  limit?: number;
}

/**
 * ``path`` is null for the daemon's home directory or an absolute host path;
 * ``limit`` is bounded to 1..1000 by the wire decoder.
 */
export interface ListDirectoriesQuery {
  /**
   * python_default_kind=value python_default=null
   */
  path?: string | null;
  /**
   * python_default_kind=value python_default=200
   */
  limit?: number;
}

/**
 * ``limit`` is bounded to 1..100 and ``offset`` to 0..100000 by the wire
 * decoder; the visibility filter is applied before pagination.
 */
export interface ListProjectsQuery {
  /**
   * python_default_kind=value python_default=50
   */
  limit?: number;
  /**
   * python_default_kind=value python_default=0
   */
  offset?: number;
  /**
   * wire=unsupported python_default_kind=value python_default=[]
   * Server-computed visibility filter: the principal's ACL visibility intersected with the trusted connection scope. The wire decoder rejects an explicit value, so a client can never widen the projects it may enumerate.
   */
  visible_project_ids?: never[];
}

export interface ListSessionsQuery {
  project_id: string;
  /**
   * python_default_kind=value python_default=50
   */
  limit?: number;
  /**
   * python_default_kind=value python_default=0
   */
  offset?: number;
}

export interface McpServerStateView {
  name: string;
  enabled: boolean;
  attached: boolean;
  /**
   * python_default_kind=value python_default=[]
   */
  include_tools: string[];
  /**
   * python_default_kind=value python_default=[]
   */
  discovered: string[];
  /**
   * python_default_kind=value python_default=[]
   */
  loaded: string[];
}

/**
 * Whitelisted projection: command / args / env / url / headers / API keys and
 * per-tool include-exclude lists are never carried.
 */
export interface McpServerView {
  name: string;
  transport: string;
  enabled: boolean;
  /**
   * python_default_kind=value python_default=null
   */
  tool_prefix: string | null;
}

/**
 * ``capabilities`` carries protocol feature flags only, never authorization capabilities.
 */
export interface NegotiateResult {
  wire_version: string;
  supported_versions: string[];
  /**
   * The PROTOCOL_FEATURES map.
   */
  capabilities: Record<string, boolean>;
}

/**
 * Connection-state request: no service DTO and no authorization capability.
 */
export interface Negotiation {
  /**
   * 1..16 unique ASCII version tokens matching ^[A-Za-z0-9][A-Za-z0-9._~-]*$
   */
  versions: string[];
  client?: NegotiationClient;
}

/**
 * Client identity in the negotiate request; never used for authorization.
 */
export interface NegotiationClient {
  name: string;
  version: string;
}

export interface OpenSessionCommand {
  session: SessionRef;
  /**
   * python_default_kind=factory python_default="<lambda>"
   */
  command_id?: string;
}

/**
 * There is no ``opened_at`` field; ``created`` reports first-open only.
 */
export interface OpenSessionResult {
  command_id: string;
  session: SessionRef;
  created: boolean;
  view: SessionView;
}

/**
 * ``expected_goal_id`` must still be the persisted goal (otherwise ``conflict``).
 * Pausing also asks this session's own live turn to cancel; no other session is touched.
 */
export interface PauseSessionGoalCommand {
  session: SessionRef;
  expected_goal_id: string;
  /**
   * python_default_kind=factory python_default="<lambda>"
   */
  command_id?: string;
}

export interface PendingApprovalQuery {
  session: SessionRef;
  expected_turn_id: string;
}

/**
 * There is no ``description`` / ``allowed_decisions`` field on an approval action.
 */
export interface PendingApprovalView {
  turn_id: string;
  actions: ApprovalActionView[];
}

export interface PlanEntryPayload {
  content: string;
  /**
   * python_default_kind=value python_default="medium"
   */
  priority: string;
  /**
   * python_default_kind=value python_default="pending"
   */
  status: string;
}

export interface PlanPayload {
  plan_id: string;
  entries: PlanEntryPayload[];
}

export interface PlanRemovedPayload {
  plan_id: string;
}

/**
 * Project identity only: id, workspace name, git branch, and the workspace
 * path the loopback console already exposes for its own project.
 */
export interface ProjectListItem {
  project_id: string;
  workspace_name: string | null;
  git_branch: string | null;
  workspace_path: string;
}

export interface ProjectListPage {
  projects: ProjectListItem[];
  next_offset: number | null;
  total: number;
}

export interface ReadArtifactQuery {
  ref: ArtifactRef;
  /**
   * python_default_kind=value python_default=0
   */
  offset?: number;
  /**
   * python_default_kind=value python_default=65536
   */
  limit?: number;
  /**
   * python_default_kind=value python_default=null
   */
  expected_revision?: string | null;
}

/**
 * ``limit`` is bounded to 1..262144 bytes.
 */
export interface ReadAttachmentQuery {
  ref: AttachmentRef;
  /**
   * python_default_kind=value python_default=0
   */
  offset?: number;
  /**
   * python_default_kind=value python_default=65536
   */
  limit?: number;
}

export interface ReadEventsQuery {
  session: SessionRef;
  /**
   * python_default_kind=value python_default=0
   */
  after?: number;
  /**
   * python_default_kind=value python_default=256
   */
  limit?: number;
  /**
   * python_default_kind=opaque python_default="EventFilter"
   */
  filter?: EventFilter;
  /**
   * python_default_kind=value python_default=1024
   */
  scan_limit?: number;
  /**
   * python_default_kind=value python_default=1048576
   */
  max_event_bytes?: number;
}

export interface ReadSessionHistoryQuery {
  session: SessionRef;
  /**
   * python_default_kind=value python_default=null
   */
  before_turn?: number | null;
  /**
   * python_default_kind=value python_default=20
   */
  limit?: number;
}

export interface RebindSessionCommand {
  session: SessionRef;
  model: string;
  /**
   * python_default_kind=factory python_default="<lambda>"
   */
  command_id?: string;
}

export interface RebindSessionResult {
  command_id: string;
  session: SessionRef;
  model: string;
  view: SessionView;
}

export interface ReconcileSessionQuery {
  session: SessionRef;
  /**
   * python_default_kind=value python_default=[]
   */
  probe_turn_ids?: string[];
}

/**
 * One host workspace path to register as a project.  The daemon resolves
 * it against its own filesystem and upserts the catalog row; re-registering
 * a known path reuses its stable ``project_id``.
 */
export interface RegisterProjectCommand {
  workspace_path: string;
}

/**
 * Three wire shapes: ``server`` omitted attaches every enabled server (and
 * then ``enabled`` / ``include_tools`` must be absent); ``server`` +
 * ``enabled`` persists the on/off flag; ``server`` + ``include_tools``
 * persists the tool whitelist.
 */
export interface ReloadMcpCommand {
  session: SessionRef;
  /**
   * python_default_kind=value python_default=null
   */
  server?: string | null;
  /**
   * python_default_kind=value python_default=null
   */
  enabled?: boolean | null;
  /**
   * python_default_kind=value python_default=null
   */
  include_tools?: string[] | null;
  /**
   * python_default_kind=factory python_default="<lambda>"
   */
  command_id?: string;
}

export interface ReloadMcpResult {
  command_id: string;
  session: SessionRef;
  server: string | null;
  enabled: boolean | null;
  attached: boolean;
  active_servers: string[];
  tool_count: number;
  warnings: string[];
  /**
   * python_default_kind=value python_default=[]
   */
  tool_names: string[];
  /**
   * python_default_kind=value python_default=[]
   */
  servers: McpServerStateView[];
}

/**
 * ``title`` is required, non-empty, and at most 120 characters.
 */
export interface RenameSessionCommand {
  session: SessionRef;
  title: string;
  /**
   * python_default_kind=factory python_default="<lambda>"
   */
  command_id?: string;
}

export interface RenameSessionResult {
  command_id: string;
  session: SessionRef;
  title: string;
  /**
   * python_default_kind=value python_default=true
   */
  renamed: boolean;
}

/**
 * ``expected_goal_id`` must still be the persisted goal (otherwise ``conflict``).
 * Status-only: resuming does not start a follow-up turn.
 */
export interface ResumeSessionGoalCommand {
  session: SessionRef;
  expected_goal_id: string;
  /**
   * python_default_kind=factory python_default="<lambda>"
   */
  command_id?: string;
}

/**
 * ``decisions`` is a non-empty list of 1..256 ApprovalDecision objects.
 */
export interface ResumeTurnCommand {
  session: SessionRef;
  expected_turn_id: string;
  decisions: ApprovalDecision[];
  /**
   * python_default_kind=factory python_default="<lambda>"
   */
  command_id?: string;
}

export interface ResumeTurnResult {
  command_id: string;
  session: SessionRef;
  turn_id: string;
  /**
   * python_default_kind=value python_default=true
   */
  accepted: boolean;
}

/**
 * ``turn_id`` names the stored record of one finished turn and is bounded to
 * 128 bytes; ``path`` is workspace-relative and must be
 * one of the paths that
 * turn is reported to have changed.
 */
export interface RevertTurnChangeCommand {
  session: SessionRef;
  turn_id: string;
  path: string;
  /**
   * python_default_kind=value python_default=null
   */
  command_id?: string | null;
}

/**
 * ``action`` is ``restore`` (the pre-turn content was written back),
 * ``delete`` (the turn created the file, so it is gone again) or
 * ``already_reverted`` (the file already held its pre-turn state and
 * nothing was written); ``bytes_written`` is 0 unless content was restored.
 */
export interface RevertTurnChangeResult {
  turn_id: string;
  path: string;
  action: string;
  bytes_written: number;
}

export interface RuntimeConfigView {
  current_model: string;
  available_models: string[];
  thinking_level: string | null;
  thinking_levels: string[];
  mcp_servers: McpServerView[];
  mcp_enabled: boolean;
  /**
   * python_default_kind=value python_default=false
   */
  can_set_thinking: boolean;
  /**
   * python_default_kind=value python_default=false
   */
  can_toggle_mcp_global: boolean;
  /**
   * python_default_kind=value python_default=null
   */
  project_thinking_level: string | null;
  /**
   * python_default_kind=value python_default=false
   */
  can_set_project_thinking: boolean;
  /**
   * python_default_kind=value python_default=null
   */
  context_window: number | null;
  /**
   * python_default_kind=value python_default=false
   */
  codex_usage_enabled: boolean;
}

/**
 * ``sequence`` is the session cursor and ``turn_sequence`` the turn-local
 * sequence (both frozen in v1).  ``payload`` is a strict JSON projection; there
 * is no ``timestamp`` field.
 */
export interface RuntimeEvent {
  sequence: number;
  turn_sequence: number;
  turn_id: string;
  kind: string;
  payload: JsonValue;
  version: number;
}

/**
 * Metadata search only (title / summary / thread_id / model / active_model); it is never a full-text transcript search and creates no database.
 * ``text`` is optional and bounded to 200 characters; ``limit``/``offset`` bound the page.
 */
export interface SearchSessionsQuery {
  project_id: string;
  /**
   * python_default_kind=value python_default=""
   */
  text?: string;
  /**
   * python_default_kind=value python_default=50
   */
  limit?: number;
  /**
   * python_default_kind=value python_default=0
   */
  offset?: number;
}

/**
 * Outcome of one goal write: ``goal`` is the refreshed projection (null only after ``clear``, when the thread has no goal any more) and ``cancellation_requested`` is true only when ``pause`` asked this session's live turn to stop.
 */
export interface SessionGoalResult {
  command_id: string;
  session: SessionRef;
  goal: SessionGoalView | null;
  /**
   * python_default_kind=value python_default=false
   */
  cancellation_requested: boolean;
}

/**
 * ``token_budget`` is null when the goal has no budget; the goal object is not exposed.
 */
export interface SessionGoalView {
  thread_id: string;
  goal_id: string;
  status: string;
  label: string;
  objective: string;
  token_budget: number | null;
  tokens_used: number;
  time_used_seconds: number;
}

export interface SessionHistoryPage {
  events: HistoryEvent[];
  start_turn: number;
  end_turn: number;
  total_turns: number;
  has_more: boolean;
  /**
   * python_default_kind=value python_default=true
   */
  available: boolean;
}

export interface SessionListPage {
  items: SessionMetadataItem[];
  next_offset: number | null;
  total: number;
}

export interface SessionMetadataItem {
  thread_id: string;
  title: string;
  model: string | null;
  active_model: string | null;
  created_at: string;
  updated_at: string;
  summary: string | null;
}

export interface SessionRecoverabilityView {
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

/**
 * Shared session identity (<project_id, thread_id>); both keys are required.
 */
export interface SessionRef {
  project_id: string;
  thread_id: string;
}

export interface SessionSearchPage {
  items: SessionMetadataItem[];
  next_offset: number | null;
  total: number;
}

/**
 * The runtime goal object and any runtime handle are never projected.
 */
export interface SessionView {
  project_id: string;
  thread_id: string;
  status: string;
  active_turn_id: string | null;
  latest_sequence: number;
  usage: UsageView;
  last_error: string | null;
  last_activity_at: string;
  /**
   * python_default_kind=value python_default=null
   */
  active_model: string | null;
  /**
   * python_default_kind=value python_default=null
   */
  model: string | null;
}

export interface SetProjectThinkingLevelCommand {
  project_id: string;
  level: string;
  /**
   * python_default_kind=factory python_default="<lambda>"
   */
  command_id?: string;
}

export interface SetProjectThinkingLevelResult {
  command_id: string;
  project_id: string;
  level: string;
}

/**
 * ``objective`` is required, non-empty, and bounded to 10000 characters; ``token_budget`` is optional and must be a positive integer.
 * An unfinished goal is never overwritten: the call is refused with ``conflict`` instead of replacing it.
 */
export interface SetSessionGoalCommand {
  session: SessionRef;
  objective: string;
  /**
   * python_default_kind=value python_default=null
   */
  token_budget?: number | null;
  /**
   * python_default_kind=factory python_default="<lambda>"
   */
  command_id?: string;
}

export interface SetThinkingLevelCommand {
  session: SessionRef;
  level: string;
  /**
   * python_default_kind=factory python_default="<lambda>"
   */
  command_id?: string;
}

export interface SetThinkingLevelResult {
  command_id: string;
  session: SessionRef;
  level: string;
  view: RuntimeConfigView;
}

export interface StatArtifactQuery {
  ref: ArtifactRef;
}

export interface StatAttachmentQuery {
  ref: AttachmentRef;
}

export interface SteerTurnCommand {
  session: SessionRef;
  expected_turn_id: string;
  text: string;
  /**
   * python_default_kind=factory python_default="<lambda>"
   */
  command_id?: string;
}

export interface SteerTurnResult {
  command_id: string;
  session: SessionRef;
  turn_id: string;
  accepted: boolean;
  pending_count: number;
}

/**
 * Transient stage marker: never persisted and legitimately absent from a replay.
 */
export interface SubagentStatusPayload {
  parent_id: string;
  /**
   * python_default_kind=value python_default=null
   */
  status: string | null;
}

/**
 * ``text`` is required (may be empty only when ``attachment_refs`` is present);
 * at least one of ``text`` / ``attachment_refs`` must be supplied and
 * ``command_id`` is generated when omitted.
 */
export interface SubmitTurnCommand {
  session: SessionRef;
  text: string;
  /**
   * wire=unsupported python_default_kind=value python_default=[]
   * v1 does not support in-process attachment objects: the wire accepts only an absent or empty list and rejects anything else with invalid_params.
   */
  attachments?: never[];
  /**
   * python_default_kind=value python_default=[]
   * Opaque, server-generated attachment ids already finalized for this session; bounded to 8 ids.  The service resolves them from the trusted session workspace.  Mutually exclusive with the in-process ``attachments`` field.
   */
  attachment_refs?: string[];
  /**
   * wire=restricted python_default_kind=factory python_default="<lambda>"
   * The wire accepts a JSON object, but no key/value schema is promised: values stay in-process objects and only a deep copy is kept by the service.
   */
  config_overrides?: Record<string, JsonValue>;
  /**
   * python_default_kind=factory python_default="<lambda>"
   */
  command_id?: string;
}

/**
 * Params of the runtime.subscription.complete notification.
 */
export interface SubscriptionComplete {
  subscription_id: string;
  cursor: number;
}

/**
 * Params of the runtime.subscription.error notification.
 */
export interface SubscriptionError {
  subscription_id: string;
  error: JsonRpcError;
}

export interface TextPayload {
  text: string;
  /**
   * python_default_kind=value python_default=null
   */
  message_id: string | null;
}

export interface ToolBatchFinishedPayload {
  group_id: string;
}

/**
 * ``items`` is part of the frozen v1 shape even though producers currently send it empty.
 */
export interface ToolBatchPayload {
  calls: ToolCallPayload[];
  parallel: boolean;
  /**
   * python_default_kind=value python_default=null
   */
  group_id: string | null;
  /**
   * python_default_kind=value python_default=[]
   */
  items: ToolItemPayload[];
}

export interface ToolCallPayload {
  call_id: string;
  name: string;
  args_preview: string;
}

export interface ToolFinishedPayload {
  item_id: string;
  status: string;
  /**
   * python_default_kind=value python_default=null
   */
  preview: string | null;
  /**
   * python_default_kind=value python_default=false
   */
  error: boolean;
}

export interface ToolItemPayload {
  item_id: string;
  call_id: string | null;
  name: string;
  category: string;
  label: string;
  path: string | null;
  status: string;
  preview: string | null;
  error: boolean;
  sub: boolean;
  parent_id: string | null;
  /**
   * python_default_kind=value python_default=false
   */
  workspace_changed: boolean;
  /**
   * python_default_kind=value python_default=null
   */
  subagent_name: string | null;
  /**
   * python_default_kind=value python_default=null
   */
  subagent_model: string | null;
  /**
   * python_default_kind=value python_default=null
   */
  subagent_reasoning_effort: string | null;
  /**
   * python_default_kind=value python_default=false
   */
  subagent_model_inherited: boolean;
  /**
   * python_default_kind=value python_default=false
   */
  subagent_reasoning_inherited: boolean;
}

/**
 * Legacy per-item fallback; still emitted and rendered by every shell, so not deprecated.
 */
export interface ToolResultPayload {
  name: string;
  status: string;
  /**
   * python_default_kind=value python_default=false
   */
  sub: boolean;
  /**
   * python_default_kind=value python_default=null
   */
  call_id: string | null;
}

export interface TurnChange {
  path: string;
  status: string;
  /**
   * python_default_kind=value python_default=0
   */
  insertions: number;
  /**
   * python_default_kind=value python_default=0
   */
  deletions: number;
  /**
   * python_default_kind=value python_default=false
   */
  binary: boolean;
}

/**
 * The files one turn created, modified or deleted, emitted once as the turn
 * settles.  ``total`` is how many files changed and ``changes`` is the
 * bounded list of them; a count is that turn's own contribution, not the
 * workspace's standing delta against ``HEAD``.
 */
export interface TurnChangesPayload {
  /**
   * python_default_kind=value python_default=[]
   */
  changes: TurnChange[];
  /**
   * python_default_kind=value python_default=0
   */
  total: number;
}

export interface TurnCoverageProbe {
  turn_id: string;
  covered: boolean;
}

/**
 * Bounded terminal summary emitted exactly once per turn.
 */
export interface TurnTerminalPayload {
  status: string;
  /**
   * python_default_kind=value python_default=""
   */
  final_text: string;
  /**
   * python_default_kind=value python_default=null
   */
  error: string | null;
  /**
   * python_default_kind=value python_default=false
   */
  interrupted: boolean;
  /**
   * python_default_kind=value python_default=0
   */
  tool_calls: number;
  /**
   * python_default_kind=value python_default=0
   */
  input_tokens: number;
  /**
   * python_default_kind=value python_default=0
   */
  output_tokens: number;
  /**
   * python_default_kind=value python_default=0
   */
  cache_tokens: number;
  /**
   * python_default_kind=value python_default=0
   */
  compact_events: number;
  /**
   * python_default_kind=value python_default=0.0
   */
  elapsed_s: number;
}

export interface UnwatchRequest {
  subscription_id: string;
}

/**
 * Idempotent: an unknown subscription id is answered with removed=false.
 */
export interface UnwatchResult {
  removed: boolean;
}

export interface UsagePayload {
  /**
   * python_default_kind=value python_default=0
   */
  turn_input: number;
  /**
   * python_default_kind=value python_default=0
   */
  turn_output: number;
  /**
   * python_default_kind=value python_default=0
   */
  turn_cache: number;
  /**
   * python_default_kind=value python_default=0
   */
  last_input: number;
  /**
   * python_default_kind=value python_default=0
   */
  last_output: number;
  /**
   * python_default_kind=value python_default=0
   */
  last_cache: number;
  /**
   * python_default_kind=value python_default=null
   */
  output_tokens_per_second: number | null;
  /**
   * python_default_kind=value python_default=null
   */
  ttft_s: number | null;
  /**
   * python_default_kind=value python_default="end_to_end"
   */
  rate_basis: string;
  /**
   * python_default_kind=value python_default=false
   */
  rate_estimated: boolean;
  /**
   * python_default_kind=value python_default=null
   */
  context_size: number | null;
  /**
   * python_default_kind=value python_default=0
   */
  model_calls: number;
}

/**
 * Result-only projection; every field is always present on the wire.
 */
export interface UsageView {
  /**
   * python_default_kind=value python_default=0
   */
  input_tokens: number;
  /**
   * python_default_kind=value python_default=0
   */
  output_tokens: number;
  /**
   * python_default_kind=value python_default=0
   */
  cache_tokens: number;
}

/**
 * Transport-only request: the in-process port takes these arguments directly
 * and returns a lease, while the wire returns a subscription id and streams
 * notifications.
 */
export interface WatchSpec {
  session: SessionRef;
  /**
   * wire default 0
   */
  after?: number;
  /**
   * wire default 128, range 1..4096
   */
  queue_size?: number;
  /**
   * wire default {kinds: [], turn_ids: []}
   */
  filter?: EventFilter;
  /**
   * wire default 1048576
   */
  max_event_bytes?: number;
}

export interface WatchStartResult {
  subscription_id: string;
  cursor: number;
}

// --- event kind -> payload union ------------------------------------------

/**
 * Per-kind payload map.  An unknown kind falls back to JsonValue so a new
 * server-side kind never breaks an older client.
 */
export interface RuntimeEventPayloadMap {
  activity_started: ActivityPayload;
  activity_stopped: ActivityPayload;
  activity_updated: ActivityPayload;
  answer_completed: TextPayload;
  answer_delta: TextPayload;
  approval_required: ApprovalPayload;
  diff_updated: DiffPayload;
  info: string;
  plan_removed: PlanRemovedPayload;
  plan_updated: PlanPayload;
  reasoning_completed: TextPayload;
  reasoning_delta: TextPayload;
  subagent_status_changed: SubagentStatusPayload;
  tool_batch_finished: ToolBatchFinishedPayload;
  tool_batch_started: ToolBatchPayload;
  tool_finished: ToolFinishedPayload;
  tool_result: ToolResultPayload;
  tool_started: ToolItemPayload;
  tool_updated: ToolItemPayload;
  turn_cancelled: TurnTerminalPayload;
  turn_changes: TurnChangesPayload;
  turn_completed: TurnTerminalPayload;
  turn_failed: TurnTerminalPayload;
  turn_waiting_approval: TurnTerminalPayload;
  usage_updated: UsagePayload;
}
export type RuntimeEventKind = keyof RuntimeEventPayloadMap;
export type RuntimeEventPayloadUnion = RuntimeEventPayloadMap[RuntimeEventKind];

export const RUNTIME_EVENT_KINDS = [
  "activity_started",
  "activity_stopped",
  "activity_updated",
  "answer_completed",
  "answer_delta",
  "approval_required",
  "diff_updated",
  "info",
  "plan_removed",
  "plan_updated",
  "reasoning_completed",
  "reasoning_delta",
  "subagent_status_changed",
  "tool_batch_finished",
  "tool_batch_started",
  "tool_finished",
  "tool_result",
  "tool_started",
  "tool_updated",
  "turn_cancelled",
  "turn_changes",
  "turn_completed",
  "turn_failed",
  "turn_waiting_approval",
  "usage_updated",
] as const;

/**
 * Kinds frozen in v1 that the TUI and the web console deliberately do not
 * render (ACP consumes them).
 */
export const RUNTIME_EVENT_UI_IGNORED_KINDS = [
  "diff_updated",
  "plan_removed",
  "plan_updated",
] as const;

/**
 * A runtime event whose payload is narrowed by kind.  Use RuntimeEvent when
 * the kind is unknown or not yet modelled.
 */
export interface TypedRuntimeEvent<K extends RuntimeEventKind> {
  sequence: number;
  turn_sequence: number;
  turn_id: string;
  kind: K;
  payload: RuntimeEventPayloadMap[K];
  version: number;
}

// --- method Params / Result aliases ---------------------------------------

export type CancelTurnParams = CancelTurnCommand;
export type ClearSessionGoalParams = ClearSessionGoalCommand;
export type CodexResetCreditsResult = CodexResetCreditsView;
export type CodexUsageResult = CodexUsageView;
export type ConsumeCodexResetParams = ConsumeCodexResetCommand;
export type CreateSessionParams = CreateSessionCommand;
export type DeleteSessionParams = DeleteSessionCommand;
export type EditSessionGoalParams = EditSessionGoalCommand;
export type GetCodexResetCreditsParams = GetCodexResetCreditsQuery;
export type GetCodexUsageParams = GetCodexUsageQuery;
export type GetRuntimeConfigParams = GetRuntimeConfigQuery;
export type HistoryToolCall = Record<string, JsonValue>;
export type HistoryToolResult = Record<string, JsonValue>;
export type ListDirectoriesParams = ListDirectoriesQuery;
export type ListDirectoriesResult = DirectoryListing;
export type ListProjectsParams = ListProjectsQuery;
export type ListSessionsParams = ListSessionsQuery;
export type McpServerState = McpServerStateView;
export type NegotiateParams = Negotiation;
export type PauseSessionGoalParams = PauseSessionGoalCommand;
export type ProjectListResult = ProjectListPage;
export type ReadSessionHistoryParams = ReadSessionHistoryQuery;
export type RebindSessionParams = RebindSessionCommand;
export type ReconcileSessionParams = ReconcileSessionQuery;
export type RegisterProjectParams = RegisterProjectCommand;
export type RegisterProjectResult = ProjectListItem;
export type ReloadMcpParams = ReloadMcpCommand;
export type RenameSessionParams = RenameSessionCommand;
export type ResumeSessionGoalParams = ResumeSessionGoalCommand;
export type RuntimeConfigResult = RuntimeConfigView;
export type SearchSessionsParams = SearchSessionsQuery;
export type SessionHistoryResult = SessionHistoryPage;
export type SessionListResult = SessionListPage;
export type SessionRecoverabilityResult = SessionRecoverabilityView;
export type SessionSearchResult = SessionSearchPage;
export type SetSessionGoalParams = SetSessionGoalCommand;
export type SteerTurnParams = SteerTurnCommand;
export type SubmitTurnParams = SubmitTurnCommand;
export type WatchEventsParams = WatchSpec;
