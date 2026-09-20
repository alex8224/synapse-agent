/**
 * Shared runtime wire DTOs for the Synapse Agent Runtime Service, plus the
 * browser console's own UX constants.
 *
 * Every wire shape (the JSON-RPC 2.0 envelopes, the method params/results and
 * the per-kind event payloads) is generated from the Python contract registry
 * in `./contract.generated.ts`.  This module only re-exports those DTOs so the
 * existing console import path (`../client/types.ts`) keeps resolving and a
 * hand-written copy can never silently drift from the generated contract.
 *
 * The only client-local members are the bounded page-size constants: the wire
 * declares per-method defaults, but the console picks its own page sizes.
 */
export type {
  AbortAttachmentCommand,
  AbortAttachmentResult,
  AppendAttachmentChunkCommand,
  AppendAttachmentChunkResult,
  ApprovalActionView,
  ApprovalDecision,
  AttachmentChunk,
  AttachmentMetadata,
  AttachmentRef,
  BeginAttachmentCommand,
  BeginAttachmentResult,
  CancelTurnParams,
  ClearSessionGoalParams,
  CommandReceipt,
  CreateSessionParams,
  CreateSessionResult,
  DeleteSessionParams,
  DeleteSessionResult,
  DirectoryEntry,
  DirectoryListing,
  EditSessionGoalParams,
  EventNotificationMeta,
  FinishAttachmentCommand,
  FinishAttachmentResult,
  GetRuntimeConfigParams,
  HistoryAttachment,
  HistoryEvent,
  HistoryToolCall,
  HistoryToolResult,
  JsonRpcNotification,
  JsonRpcRequest,
  JsonRpcResponse,
  ListDirectoriesParams,
  ListDirectoriesResult,
  ListProjectsParams,
  ListSessionsParams,
  ListSkillsParams,
  ListSkillsResult,
  McpServerState,
  McpServerView,
  NegotiateParams,
  OpenSessionResult,
  PauseSessionGoalParams,
  PendingApprovalView,
  ProjectListItem,
  ProjectListResult,
  ReadAttachmentQuery,
  ReadSessionHistoryParams,
  RebindSessionParams,
  RebindSessionResult,
  ReconcileSessionParams,
  RegisterProjectParams,
  RegisterProjectResult,
  ReloadMcpParams,
  ReloadMcpResult,
  RenameSessionParams,
  RenameSessionResult,
  ResumeSessionGoalParams,
  RevertTurnChangeCommand,
  RevertTurnChangeResult,
  RuntimeConfigResult,
  RuntimeEvent,
  SearchSessionsParams,
  SessionGoalResult,
  SessionHistoryResult,
  SessionListResult,
  SessionMetadataItem,
  SessionRecoverabilityResult,
  SessionRef,
  SessionSearchResult,
  SetProjectThinkingLevelResult,
  SetSessionGoalParams,
  SetThinkingLevelResult,
  SkillEntry,
  SkillListPage,
  StatAttachmentQuery,
  SteerTurnParams,
  SttAppendResult,
  SttBeginResult,
  SttCancelResult,
  SttFinishResult,
  SttStatusView,
  SubmitTurnParams,
  TurnCoverageProbe,
  WatchEventsParams,
} from './contract.generated.ts';

/** Bounded page size for `runtime.session.list` (backend default is 50, cap 100). */
export const SESSION_LIST_PAGE_SIZE = 50;
/** Bounded page size for `runtime.session.search` (backend default is 50, cap 100). */
export const SESSION_SEARCH_PAGE_SIZE = 50;
/** Bounded page size for `runtime.project.list` (backend default is 50, cap 100). */
export const PROJECT_LIST_PAGE_SIZE = 100;
/** Bounded page size for `runtime.fs.list` (backend default is 200, cap 1000). */
export const DIRECTORY_LIST_PAGE_SIZE = 200;
/** Bounded page size for `runtime.session.history` (backend default is 20, cap 100). */
export const HISTORY_PAGE_SIZE = 20;