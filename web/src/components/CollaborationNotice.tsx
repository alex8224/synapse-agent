/**
 * The in-flow collaboration strip under the conversation.
 *
 * It answers the two questions the reader actually has while the source session
 * works: *is the other session still on it* and *did the answer arrive*.  It is
 * a strip and not a transcript row on purpose: the transcript is the runtime's
 * own projection, and a locally merged row would be wiped by the next history
 * load (and reappear only on the next refresh), so a receipt would flicker.
 *
 * Only the newest few requests are shown; the dock's 协作 panel is the full list,
 * and "查看详情" opens it.  The stop control appears here only while this session
 * is waiting for something, because that is the moment it is needed.
 */
import {
  ArrowRight16Regular,
  CheckmarkCircle16Regular,
  Clock16Regular,
  Dismiss16Regular,
  Pause16Regular,
  Chat16Regular,
} from '@fluentui/react-icons';
import React, { useEffect } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../stores/useConsoleStore.ts';
import { useRightDockStore } from '../stores/useRightDockStore.ts';
import {
  collaborationView,
  canControlAssignment,
  hasPendingCollaboration,
} from '../stores/collaborationView.ts';

/** How many requests the strip shows before deferring to the dock. */
const VISIBLE_REQUESTS = 2;

export const CollaborationNotice: React.FC = () => {
  const {
    currentSession,
    collaborationPage,
    collaborationError,
    settledCount,
    loadCollaborations,
    stopCollaboration,
  } = useConsoleStore(
    useShallow((state) => ({
      currentSession: state.currentSession,
      collaborationPage: state.collaborationPage,
      collaborationError: state.collaborationError,
      // A settled turn is the moment a receipt can appear; a live stream must
      // not re-trigger the read, so only the count is watched.
      settledCount: state.settledTurnIds?.length ?? 0,
      loadCollaborations: state.loadCollaborations,
      stopCollaboration: state.stopCollaboration,
    })),
  );
  const setActiveTab = useRightDockStore((state) => state.setActiveTab);
  const toggleOpen = useRightDockStore((state) => state.toggleOpen);

  // Read on session change (the strip describes one session) and once per
  // settled turn (the moment a receipt can appear).  A live stream does not
  // re-trigger this: `settledTurnIds` only grows when a turn actually ends.
  useEffect(() => {
    void loadCollaborations();
  }, [loadCollaborations, currentSession.project_id, currentSession.thread_id, settledCount]);

  const view = collaborationView(collaborationPage, currentSession);
  const pending = hasPendingCollaboration(view) || view.requests.some((request) => request.pending > 0);
  useEffect(() => {
    if (!pending) return;
    // Receipts can arrive without a source turn (receive-only). Read the mailbox
    // while work is outstanding, rather than manufacturing a continuation turn.
    const timer = window.setInterval(() => void loadCollaborations(), 2000);
    return () => window.clearInterval(timer);
  }, [pending, loadCollaborations, currentSession.project_id, currentSession.thread_id]);
  if (view.requests.length === 0 && collaborationError === null) return null;

  const openDetail = (): void => {
    setActiveTab('collaboration');
    toggleOpen(true);
  };

  return (
    <section
      className="console-gutter shrink-0 pb-1"
      aria-label="会话协作"
      data-testid="collaboration-notice"
    >
      <div className="console-column space-y-1">
        {collaborationError !== null && (
          <p className="rounded-control bg-red-50 px-2 py-1 text-xs text-red-700">
            读取协作状态失败：{collaborationError}
          </p>
        )}
        {view.requests.slice(0, VISIBLE_REQUESTS).map((request) => (
          <div
            key={request.requestId}
            className="flex flex-wrap items-center gap-2 rounded-control border border-line/60 bg-sunken/30 px-2 py-1 text-xs"
          >
            {request.replied ? (
              <CheckmarkCircle16Regular
                aria-hidden="true"
                className="shrink-0 text-emerald-600"
              />
            ) : request.active ? (
              <Clock16Regular aria-hidden="true" className="shrink-0 text-amber-600" />
            ) : (
              <Chat16Regular aria-hidden="true" className="shrink-0 text-gray-500" />
            )}
            <span className="shrink-0 text-gray-600">{request.roleLabel}</span>
            <span
              className="min-w-0 truncate font-mono text-[11px] text-gray-500"
              title={`${request.peerProjectId}:${request.peerThreadId}`}
            >
              {request.peerThreadId}
            </span>
            <span className="shrink-0 text-gray-600">{request.stateLabel}</span>
            <button
              type="button"
              className="ui-button ui-compact ml-auto shrink-0"
              onClick={openDetail}
            >
              查看详情
              <ArrowRight16Regular aria-hidden="true" />
            </button>
            {canControlAssignment(request) && request.continuationState === 'active' && (
              <button type="button" className="ui-button ui-compact shrink-0"
                onClick={() => void stopCollaboration({ assignmentId: request.requestId, receiveOnly: true })}>
                <Pause16Regular aria-hidden="true" />仅接收结果
              </button>
            )}
            {canControlAssignment(request) && (
              <button type="button" className="ui-button ui-compact shrink-0"
                onClick={() => void stopCollaboration({ assignmentId: request.requestId })}>
                <Dismiss16Regular aria-hidden="true" />撤销此项交办
              </button>
            )}
          </div>
        ))}
        {view.requests.length > VISIBLE_REQUESTS && (
          <button type="button" className="ui-button ui-compact" onClick={openDetail}>
            还有 {view.requests.length - VISIBLE_REQUESTS} 项协作
          </button>
        )}
      </div>
    </section>
  );
};
