/**
 * Read-only projection of one session's collaboration requests.
 *
 * Pure and DOM-free, like `sessionViews`: the console paints what this returns,
 * and the Node test runner exercises it directly.  Two rules are the whole point
 * of the module:
 *
 * - *Who said what* is decided here, from the request's own participant fields,
 *   so a row never guesses the reader's side from a message's position.
 * - *What the state means* is a table, so a new server state is a visible
 *   fallback rather than a row that silently renders nothing.
 *
 * Nothing here starts work: the surface is a read plus the stop control.
 */
import type {
  CollaborationMessageView,
  CollaborationRequestView,
  CollaborationResult,
} from '../runtime-client/types.ts';
import { displaySessionTitle, projectLabel } from './sessionList.ts';

/** One message, already oriented for the reader. */
export interface CollaborationMessageRow {
  messageId: string;
  /** `request` / `clarification` / `supplement` / `reply`, as the server names it. */
  kind: string;
  kindLabel: string;
  body: string;
  /** True when this session sent it. */
  mine: boolean;
  /** True when it is the target's final answer. */
  isReply: boolean;
}

export const EXECUTION_LABELS: Record<string, string> = {
  pending: '排队中', claimed: '等待执行', running: '执行中',
  settled: '执行已结束', cancelled: '执行已撤销', review: '执行待核验',
  waiting_approval: '等待审批', cancelling: '正在停止', starting: '正在启动',
  queued: '等待执行额度', failed: '执行失败',
  settling: '正在保存与收尾', idle: '执行已结束',
};

const EXECUTION_REASON_LABELS: Record<string, string> = {
  execution_cancelled: '执行已取消', execution_failed: '执行失败', no_result: '未交付协作结果',
  interrupted: '执行因服务中断而结束', target_unavailable: '目标会话暂不可用',
  session_unavailable: '执行会话暂不可用', start_failed: '执行未能启动',
  scope_disabled: '自动接续已关闭', assignment_disabled: '交办执行许可已关闭',
  stopped: '执行已停止', superseded: '此消息已被后续答复替代',
};

export function executionReasonLabel(reason: string | null): string | null {
  return reason === null ? null : EXECUTION_REASON_LABELS[reason] ?? '执行状态需要核验';
}

/** Controls change future/running work, not a completed historical record. */
export function canControlAssignment(request: CollaborationRequestRow): boolean {
  return request.role === 'source' && request.continuationState !== 'revoked'
    && (request.active || request.pending > 0);
}

export const RESULT_LABELS: Record<string, string> = {
  not_received: '尚未交付', received: '结果已到达，待处理', processing: '正在处理结果',
  processed: '结果处理轮次已结束', interrupted: '结果处理被中断', review: '结果处理待核验',
  waiting_approval: '结果处理等待审批',
};

/** One request, already oriented for the reader. */
export interface CollaborationRequestRow {
  requestId: string;
  collaborationId: string;
  controlVersion: number;
  continuationState: string;
  executionState: string;
  executionTurnId: string | null;
  executionReason: string | null;
  resultState: string;
  /** This session's role in the request. */
  role: 'source' | 'target';
  roleLabel: string;
  /** The other side, labelled for display. */
  peerProjectId: string;
  peerThreadId: string;
  state: string;
  stateLabel: string;
  /** True while the request is still moving (queued, working or waiting). */
  active: boolean;
  /** True once the peer delivered a final answer. */
  replied: boolean;
  messages: CollaborationMessageRow[];
  /** How many messages exist in total, so a short window is not read as a short talk. */
  messageTotal: number;
  /** Unresolved deliveries: the reader's cue that work is still in flight. */
  pending: number;
  updatedAtMs: number;
}

/** The continuation control the reader can exercise right now. */
export interface CollaborationStopTarget {
  scopeId: string;
  version: number;
  state: string;
}

/** Everything the collaboration surface paints for one session. */
export interface CollaborationView {
  requests: CollaborationRequestRow[];
  /** The session's own continuation generation, when it has one. */
  stopTarget: CollaborationStopTarget | null;
}

export const COLLABORATION_STATE_LABELS: Record<string, string> = {
  pending: '等待对方处理',
  working: '对方处理中',
  waiting_input: '等待补充',
  replied: '已收到结论',
  cancelled: '已停止',
  failed: '未返回结论',
  review: '需要核验',
};

export const COLLABORATION_KIND_LABELS: Record<string, string> = {
  request: '协作请求',
  clarification: '对方提问',
  supplement: '补充答复',
  reply: '结论',
  progress: '进度报告',
};

/** Human label for one server state, never blank for an unknown future state. */
export function collaborationStateLabel(state: string): string {
  return COLLABORATION_STATE_LABELS[state] ?? state;
}

/** Human label for one message kind, never blank for an unknown future kind. */
export function collaborationKindLabel(kind: string): string {
  return COLLABORATION_KIND_LABELS[kind] ?? kind;
}

function messageRow(
  message: CollaborationMessageView,
  sourceProjectId: string,
  sourceThreadId: string,
): CollaborationMessageRow {
  return {
    messageId: message.message_id,
    kind: message.kind,
    kindLabel: collaborationKindLabel(message.kind),
    body: message.body,
    mine:
      message.sender_project === sourceProjectId &&
      message.sender_thread === sourceThreadId,
    isReply: message.kind === 'reply',
  };
}

/**
 * Project one page onto the surface's view model.
 *
 * `session` is the session the reader is looking at, and it is what decides the
 * reader's role in each request: a request this session sent reads as "我请对方"
 * even when the newest message came from the peer.
 */
export function collaborationView(
  page: CollaborationResult | null,
  session: { project_id: string; thread_id: string },
): CollaborationView {
  if (page === null) return { requests: [], stopTarget: null };
  const requests: CollaborationRequestRow[] = page.requests.map(
    (request: CollaborationRequestView) => {
    const isSource =
      request.source_project === session.project_id &&
      request.source_thread === session.thread_id;
    const state = request.state;
    return {
      requestId: request.request_id,
      collaborationId: request.collaboration_id || request.scope_id,
      controlVersion: request.control_version ?? 0,
      continuationState: request.continuation_state ?? 'active',
      executionState: request.execution_state ?? 'pending',
      executionTurnId: request.execution_turn_id ?? null,
      executionReason: executionReasonLabel(request.execution_reason ?? null),
      resultState: request.result_state ?? 'not_received',
      role: isSource ? ('source' as const) : ('target' as const),
      roleLabel: isSource ? '我请对方' : '对方请求',
      peerProjectId: isSource ? request.target_project : request.source_project,
      peerThreadId: isSource ? request.target_thread : request.source_thread,
      state,
      stateLabel: isSource ? collaborationStateLabel(state) : ({
        pending: '待处理交办', working: '本会话处理中', waiting_input: '等待指挥者补充',
        replied: '已交付结论', cancelled: '交办已撤销', failed: '未交付结论', review: '需要核验',
      } as Record<string, string>)[state] ?? state,
      active: state === 'pending' || state === 'working' || state === 'waiting_input',
      replied: state === 'replied',
      messages: request.messages.map((message) =>
        messageRow(message, session.project_id, session.thread_id),
      ),
      messageTotal: request.message_total,
      pending: request.pending,
      updatedAtMs: request.updated_at_ms,
      };
    },
  );
  return {
    requests,
    stopTarget:
      page.scope_id === null || page.scope_version === null
        ? null
        : {
            scopeId: page.scope_id,
            version: page.scope_version,
            state: page.scope_state ?? '',
          },
  };
}

/**
 * Whether the stop control is offered.
 *
 * Only a *live* generation can be fenced: a session with no continuation left
 * has nothing to stop, and offering the control anyway would suggest the
 * console could cancel the peer, which it deliberately cannot.
 */
export function canStopCollaboration(view: CollaborationView): boolean {
  return view.stopTarget !== null && view.stopTarget.state === 'active';
}

/** Whether any request of this session is still moving. */
export function hasPendingCollaboration(view: CollaborationView): boolean {
  return view.requests.some((request) => request.active);
}

/** Shorten a long hex collaboration/assignment identifier for compact display. */
export function shortCollaborationId(id: string, chars = 8): string {
  const trimmed = id.trim();
  return trimmed.length <= chars ? trimmed : trimmed.slice(0, chars);
}

/** Loaded project/session lookup tables used to humanize peer identifiers. */
export interface CollaborationPeerCatalog {
  activeProjectId?: string;
  projects?: ReadonlyArray<{
    project_id: string;
    workspace_path: string;
    workspace_name: string | null;
  }>;
  sessions?: ReadonlyArray<{ thread_id: string; title: string }>;
  projectSessions?: Readonly<
    Record<string, ReadonlyArray<{ thread_id: string; title: string }>>
  >;
}

/** Human-readable orientation for a peer session reference. */
export interface ResolvedCollaborationPeer {
  projectName: string | null;
  sessionTitle: string | null;
  /** Primary human label (`project · title` or `title`, falling back to `threadId`). */
  displayName: string;
  threadId: string;
  fullRef: string;
}

/**
 * Resolve a peer `(projectId, threadId)` against the console's loaded project
 * and session lists so the UI can show project/session names alongside the raw id.
 */
export function resolveCollaborationPeer(
  peerProjectId: string,
  peerThreadId: string,
  catalog?: CollaborationPeerCatalog,
): ResolvedCollaborationPeer {
  const effectiveProjectId = peerProjectId || catalog?.activeProjectId || '';
  const project = catalog?.projects?.find((item) => item.project_id === effectiveProjectId);
  const projectName = project ? projectLabel(project) : null;

  const projectList =
    effectiveProjectId === '' || effectiveProjectId === catalog?.activeProjectId
      ? catalog?.sessions
      : catalog?.projectSessions?.[effectiveProjectId];
  const fallbackList = [
    ...(catalog?.sessions ?? []),
    ...Object.values(catalog?.projectSessions ?? {}).flat(),
  ];
  const session =
    projectList?.find((item) => item.thread_id === peerThreadId) ??
    fallbackList.find((item) => item.thread_id === peerThreadId);
  const sessionTitle =
    session && session.title.trim() !== ''
      ? displaySessionTitle(session.title, peerThreadId)
      : null;

  const crossProject =
    projectName !== null &&
    projectName !== '' &&
    effectiveProjectId !== '' &&
    (catalog?.activeProjectId === undefined || effectiveProjectId !== catalog.activeProjectId);

  const displayName =
    sessionTitle !== null
      ? crossProject
        ? `${projectName} · ${sessionTitle}`
        : sessionTitle
      : crossProject
        ? `${projectName} · ${peerThreadId}`
        : peerThreadId;

  return {
    projectName,
    sessionTitle,
    displayName,
    threadId: peerThreadId,
    fullRef: effectiveProjectId ? `${effectiveProjectId}:${peerThreadId}` : peerThreadId,
  };
}

/** Stable key for dismissing a settled notice row until its state changes. */
export function collaborationDismissKey(request: CollaborationRequestRow): string {
  return `${request.requestId}:${request.controlVersion}:${request.state}:${request.resultState}`;
}

/** Only settled rows with no in-flight work or active control can be dismissed. */
export function canDismissCollaborationNotice(request: CollaborationRequestRow): boolean {
  return !canControlAssignment(request) && !request.active && request.pending === 0;
}

/** Segment of a user prompt after splitting inline `@session:<project>:<thread>` tokens. */
export type SessionMentionSegment =
  | { kind: 'text'; text: string }
  | { kind: 'session'; raw: string; projectId: string; threadId: string };

const SESSION_MENTION_RE = /@session:([A-Za-z0-9_-]+)(?::([A-Za-z0-9_-]+))?/g;

/** Split prompt text into plain text and `@session:<project>:<thread>` (or `@session:<thread>`) tokens. */
export function splitSessionMentions(text: string): SessionMentionSegment[] {
  if (!text.includes('@session:')) return [{ kind: 'text', text }];
  const segments: SessionMentionSegment[] = [];
  let lastIndex = 0;
  for (const match of text.matchAll(SESSION_MENTION_RE)) {
    const index = match.index ?? 0;
    if (index > lastIndex) {
      segments.push({ kind: 'text', text: text.slice(lastIndex, index) });
    }
    segments.push({
      kind: 'session',
      raw: match[0],
      projectId: match[2] !== undefined ? match[1] : '',
      threadId: match[2] !== undefined ? match[2] : match[1],
    });
    lastIndex = index + match[0].length;
  }
  if (lastIndex < text.length) {
    segments.push({ kind: 'text', text: text.slice(lastIndex) });
  }
  return segments.length > 0 ? segments : [{ kind: 'text', text }];
}
