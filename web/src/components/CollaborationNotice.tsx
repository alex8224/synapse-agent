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
import React, { useEffect, useMemo, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../stores/useConsoleStore.ts';
import { useRightDockStore } from '../stores/useRightDockStore.ts';
import { useScreenshotStore } from '../stores/screenshotTask.ts';
import {
  collaborationView,
  canControlAssignment,
  canDismissCollaborationNotice,
  collaborationDismissKey,
  hasPendingCollaboration,
  resolveCollaborationPeer,
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
    activeProjectId,
    projects,
    sessions,
    projectSessions,
    ensureProjectSessionsLoaded,
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
      activeProjectId: state.activeProjectId,
      projects: state.projects,
      sessions: state.sessions,
      projectSessions: state.projectSessions,
      ensureProjectSessionsLoaded: state.ensureProjectSessionsLoaded,
    })),
  );
  const setActiveTab = useRightDockStore((state) => state.setActiveTab);
  const toggleOpen = useRightDockStore((state) => state.toggleOpen);
  const [dismissedKeys, setDismissedKeys] = useState<string[]>([]);

  const screenshotBannerVisible = useScreenshotStore((s) => {
    const sameSession =
      s.origin !== null &&
      s.origin.projectId === currentSession.project_id &&
      s.origin.threadId === currentSession.thread_id;
    const state = s.status?.state ?? 'idle';
    return (
      s.starting ||
      (sameSession && (state === 'queued' || state === 'running')) ||
      (sameSession && s.importing) ||
      (sameSession && state === 'target_required') ||
      (sameSession && state === 'failed') ||
      (sameSession && state === 'cancelled') ||
      s.pending !== null ||
      s.notice !== null
    );
  });

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

  // Ensure peer project session titles are loaded so cross-project notices can
  // display human titles instead of bare thread ids.
  const peerProjectIdsKey = view.requests.map((r) => r.peerProjectId).join(',');
  useEffect(() => {
    if (peerProjectIdsKey === '') return;
    void ensureProjectSessionsLoaded?.(peerProjectIdsKey.split(','));
  }, [peerProjectIdsKey, ensureProjectSessionsLoaded]);

  const peerCatalog = useMemo(
    () => ({ activeProjectId, projects, sessions, projectSessions }),
    [activeProjectId, projects, sessions, projectSessions],
  );

  const visibleRequests = view.requests.filter(
    (request) => !dismissedKeys.includes(collaborationDismissKey(request)),
  );
  if (visibleRequests.length === 0 && collaborationError === null) return null;

  const openDetail = (): void => {
    setActiveTab('collaboration');
    toggleOpen(true);
  };

  return (
    <section
      className="console-gutter pointer-events-none absolute inset-x-0 z-20 flex w-full justify-center"
      style={{
        bottom: screenshotBannerVisible
          ? 'calc(var(--composer-h, 7rem) + 3.25rem)'
          : 'calc(var(--composer-h, 7rem) + 0.5rem)',
      }}
      aria-label="会话协作"
      data-testid="collaboration-notice"
    >
      <div className="console-column pointer-events-auto max-w-3xl space-y-1.5">
        {collaborationError !== null && (
          <p className="rounded-full bg-red-50 px-3 py-1 text-xs text-red-700 shadow-card border border-red-200">
            读取协作状态失败：{collaborationError}
          </p>
        )}
        {visibleRequests.slice(0, VISIBLE_REQUESTS).map((request) => {
          const peer = resolveCollaborationPeer(
            request.peerProjectId,
            request.peerThreadId,
            peerCatalog,
          );
          return (
            <div
              key={request.requestId}
              className="flex flex-wrap items-center gap-2 rounded-full border border-line/80 bg-surface/90 px-3 py-1 text-xs shadow-flyout material-flyout flyout-in backdrop-blur-md transition-all"
            >
              <div
                className={`inline-flex shrink-0 items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium leading-none ${
                  request.replied
                    ? 'bg-emerald-500/10 text-emerald-700 border border-emerald-500/25'
                    : request.active
                      ? 'bg-amber-500/10 text-amber-700 border border-amber-500/25'
                      : 'bg-gray-500/10 text-gray-700 border border-gray-500/25'
                }`}
              >
                {request.replied ? (
                  <CheckmarkCircle16Regular
                    aria-hidden="true"
                    className="shrink-0 text-emerald-600"
                  />
                ) : request.state === 'working' ? (
                  <span className="relative flex h-2 w-2 shrink-0">
                    <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-75"></span>
                    <span className="relative inline-flex h-2 w-2 rounded-full bg-amber-500"></span>
                  </span>
                ) : request.active ? (
                  <Clock16Regular aria-hidden="true" className="shrink-0 text-amber-600" />
                ) : (
                  <Chat16Regular aria-hidden="true" className="shrink-0 text-gray-500" />
                )}
                <span>{request.stateLabel}</span>
              </div>
              <div className="flex min-w-0 flex-1 items-center gap-1.5 text-xs">
                <span className="shrink-0 font-medium text-gray-500">
                  {request.roleLabel}
                </span>
                <span className="shrink-0 text-gray-300">·</span>
                <span
                  className="min-w-0 truncate font-semibold text-gray-900"
                  title={peer.fullRef}
                >
                  {peer.displayName}
                </span>
                {peer.displayName !== request.peerThreadId && (
                  <span
                    className="shrink-0 font-mono text-[10px] text-gray-400"
                    title={peer.fullRef}
                  >
                    ({request.peerThreadId})
                  </span>
                )}
              </div>
              <div className="ml-auto flex shrink-0 items-center gap-1.5">
                <button
                  type="button"
                  className="ui-button ui-compact rounded-full px-2.5 text-xs font-medium text-accent-fg hover:bg-accent/10"
                  onClick={openDetail}
                >
                  <span>查看详情</span>
                  <ArrowRight16Regular aria-hidden="true" className="text-xs" />
                </button>
                {canControlAssignment(request) && request.continuationState === 'active' && (
                  <button
                    type="button"
                    className="ui-button ui-compact rounded-full px-2 text-xs text-gray-600 hover:text-gray-900"
                    onClick={() =>
                      void stopCollaboration({ assignmentId: request.requestId, receiveOnly: true })
                    }
                  >
                    <Pause16Regular aria-hidden="true" />
                    <span>仅接收结果</span>
                  </button>
                )}
                {canControlAssignment(request) && (
                  <button
                    type="button"
                    className="ui-button ui-compact rounded-full px-2 text-xs text-gray-600 hover:text-red-600"
                    onClick={() => void stopCollaboration({ assignmentId: request.requestId })}
                  >
                    <Dismiss16Regular aria-hidden="true" />
                    <span>撤销此项交办</span>
                  </button>
                )}
                {canDismissCollaborationNotice(request) && (
                  <button
                    type="button"
                    className="ui-icon-button ui-compact rounded-full text-gray-400 hover:text-gray-700 hover:bg-gray-100"
                    aria-label="收起此条协作提示"
                    title="收起此条协作提示"
                    onClick={() =>
                      setDismissedKeys((prev) => [...prev, collaborationDismissKey(request)])
                    }
                  >
                    <Dismiss16Regular aria-hidden="true" />
                  </button>
                )}
              </div>
            </div>
          );
        })}
        {visibleRequests.length > VISIBLE_REQUESTS && (
          <div className="flex justify-center">
            <button
              type="button"
              className="ui-button ui-compact rounded-full text-xs text-gray-500 hover:text-gray-800"
              onClick={openDetail}
            >
              还有 {visibleRequests.length - VISIBLE_REQUESTS} 项协作
            </button>
          </div>
        )}
      </div>
    </section>
  );
};
