/**
 * Collaboration tab for the right auxiliary dock.
 *
 * This is a read surface plus assignment controls. It shows the requests this session
 * takes part in — who asked, what was said, and whether the peer has answered —
 * and controls only the selected assignment's continuation and execution. It deliberately
 * cannot start a request or deliver a message: those are Agent actions, and a
 * panel that offered them would be a second, unaccountable way to make one
 * session contact another.
 */
import {
  ArrowSync16Regular,
  Chat16Regular,
  Dismiss16Regular,
  Pause16Regular,
} from '@fluentui/react-icons';
import React, { useEffect } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import {
  collaborationView,
  canControlAssignment,
  EXECUTION_LABELS,
  RESULT_LABELS,
  type CollaborationRequestRow,
} from '../../stores/collaborationView.ts';
import type { RightDockContext, RightDockTabDefinition } from './contract.ts';

const RequestBlock: React.FC<{
  request: CollaborationRequestRow;
  stop: (assignmentId: string, receiveOnly: boolean) => void;
  inspect: () => void;
}> = ({ request, stop, inspect }) => (
  <li className="rounded-control border border-line/60 bg-sunken/30 p-2.5">
    <div className="flex items-center gap-2">
      <span className="ui-section-label">{request.roleLabel}</span>
      <span
        className="min-w-0 truncate font-mono text-[11px] text-gray-500"
        title={`${request.peerProjectId}:${request.peerThreadId}`}
      >
        {request.peerThreadId}
      </span>
      <span
        className={
          'ml-auto shrink-0 rounded-control px-1.5 py-0.5 text-[11px] ' +
          (request.replied
            ? 'bg-emerald-50 text-emerald-700'
            : request.active
              ? 'bg-amber-50 text-amber-700'
              : 'bg-gray-100 text-gray-600')
        }
      >
        {request.stateLabel}
      </span>
    </div>
    <p className="mt-1 break-all font-mono text-[10px] text-gray-500">
      协作 {request.collaborationId} · 交办 {request.requestId}
    </p>
    {request.executionTurnId && (
      <button type="button" className="ui-button ui-compact mt-1 text-[11px]"
        title={request.executionTurnId} onClick={inspect}>
        查看执行会话
      </button>
    )}
    <p className="mt-1 text-[11px] text-gray-500">
      {EXECUTION_LABELS[request.executionState] ?? request.executionState} ·
      {RESULT_LABELS[request.resultState] ?? request.resultState}
      {request.executionReason ? ` · ${request.executionReason}` : ''}
      {request.continuationState === 'receive_only' ? ' · 仅接收，不自动继续' : ''}
      {request.continuationState === 'revoked' ? ' · 交办已撤销' : ''}
    </p>
    <ul className="mt-2 space-y-1.5">
      {request.messages.map((message) => (
        <li key={message.messageId} className="text-xs">
          <div className="flex items-center gap-1.5 text-[11px] text-gray-500">
            <Chat16Regular aria-hidden="true" />
            <span>{message.mine ? '本会话' : '对方'} · {message.kindLabel}</span>
          </div>
          <p
            className={
              'mt-0.5 whitespace-pre-wrap break-words ' +
              (message.isReply ? 'text-gray-900' : 'text-gray-700')
            }
          >
            {message.body}
          </p>
        </li>
      ))}
    </ul>
    {request.messageTotal > request.messages.length && (
      <p className="mt-1 text-[11px] text-gray-500">
        仅显示最近 {request.messages.length} 条，共 {request.messageTotal} 条。
      </p>
    )}
    {canControlAssignment(request) && (
      <div className="mt-2 flex flex-wrap gap-2">
        {request.continuationState === 'active' && (
          <button type="button" className="ui-button ui-compact"
            onClick={() => stop(request.requestId, true)}>
            <Pause16Regular aria-hidden="true" />仅接收此项结果
          </button>
        )}
        <button type="button" className="ui-button ui-compact"
          onClick={() => stop(request.requestId, false)}>
          <Dismiss16Regular aria-hidden="true" />撤销此项交办
        </button>
      </div>
    )}
    {request.pending > 0 && (
      <p className="mt-1 text-[11px] text-gray-500">仍有 {request.pending} 项投递未结束。</p>
    )}
  </li>
);

export const CollaborationContent: React.FC<{ context: RightDockContext }> = () => {
  const {
    currentSession,
    collaborationPage,
    collaborationLoading,
    collaborationError,
    loadCollaborations,
    stopCollaboration,
    switchProject,
  } = useConsoleStore(
    useShallow((state) => ({
      currentSession: state.currentSession,
      collaborationPage: state.collaborationPage,
      collaborationLoading: state.collaborationLoading,
      collaborationError: state.collaborationError,
      loadCollaborations: state.loadCollaborations,
      stopCollaboration: state.stopCollaboration,
      switchProject: state.switchProject,
    })),
  );

  // Read on open, and again when the reader switches session: the surface is a
  // snapshot, so it must follow the session it describes rather than go stale.
  useEffect(() => {
    void loadCollaborations();
  }, [loadCollaborations, currentSession.project_id, currentSession.thread_id]);

  const view = collaborationView(collaborationPage, currentSession);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2">
        <span className="ui-section-label">会话协作</span>
        <button
          type="button"
          className="ui-icon-button ui-compact ml-auto"
          aria-label="刷新协作状态"
          title="刷新协作状态"
          disabled={collaborationLoading}
          onClick={() => void loadCollaborations()}
        >
          <ArrowSync16Regular aria-hidden="true" />
        </button>
      </div>

      {collaborationError !== null && (
        <p className="border-b border-line px-3 py-2 text-xs text-red-700">
          读取协作状态失败：{collaborationError}
        </p>
      )}

      <div className="min-h-0 flex-1 overflow-auto p-3">
        {view.requests.length === 0 ? (
          <p className="text-xs text-gray-500">
            这个会话还没有协作请求。在输入框用 <code>@</code> 引用另一个会话，并说明请它做什么，
            Agent 会发起协作并把结论带回来。
          </p>
        ) : (
          <ul className="space-y-2">
            {view.requests.map((request) => (
              <RequestBlock key={request.requestId} request={request}
                inspect={() => void switchProject(
                  request.role === 'source' ? request.peerProjectId : currentSession.project_id,
                  request.role === 'source' ? request.peerThreadId : currentSession.thread_id,
                )}
                stop={(assignmentId, receiveOnly) => void stopCollaboration({ assignmentId, receiveOnly })} />
            ))}
          </ul>
        )}
      </div>

    </div>
  );
};

export const collaborationTab: RightDockTabDefinition = {
  id: 'collaboration',
  label: '协作',
  order: 55,
  Icon: Chat16Regular,
  Content: CollaborationContent,
};
