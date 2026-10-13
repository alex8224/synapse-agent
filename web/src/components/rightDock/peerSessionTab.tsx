/**
 * Read-only preview of the session this one is collaborating with.
 *
 * The reader's question is "what is the other side actually doing", and the
 * answer must not cost a context switch.  So this tab lists the peers of the
 * current session's collaborations, loads one peer's newest history page on
 * demand, and renders it as a bounded, read-only log.  It never opens a session
 * in the runtime, never watches it, and never sends anything: "进入完整会话" is
 * the one explicit way out, through the console's own session switch.
 */
import {
  ArrowRight16Regular,
  ArrowSync16Regular,
  Chat16Regular,
  Window16Regular,
} from '@fluentui/react-icons';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import { useRightDockStore } from '../../stores/useRightDockStore.ts';
import { collaborationView, resolveCollaborationPeer } from '../../stores/collaborationView.ts';
import type { HistoryEvent } from '../../runtime-client/types.ts';
import { RpcCallError } from '../../runtime-client/SynapseRuntimeClient.ts';
import type { RightDockContext, RightDockTabDefinition } from './contract.ts';

/** Bounded page: a preview, not a second transcript. */
const PREVIEW_TURNS = 12;

interface Peer {
  projectId: string;
  threadId: string;
  label: string;
  displayLabel: string;
}

/** Event kinds this preview labels; anything else shows its raw kind. */
const EVENT_LABELS: Record<string, string> = {
  user: '对方会话的输入',
  answer: '对方的回答',
  thought: '思考',
  tools: '工具调用',
  changes: '改动',
  meta: '信息',
};

function eventLabel(kind: string): string {
  return EVENT_LABELS[kind] ?? kind;
}

export const PeerSessionContent: React.FC<{ context: RightDockContext }> = () => {
  const {
    currentSession,
    collaborationPage,
    loadCollaborations,
    switchProject,
    activeProjectId: consoleActiveProjectId,
    projects,
    sessions,
    projectSessions,
    ensureProjectSessionsLoaded,
  } = useConsoleStore(
    useShallow((state) => ({
      currentSession: state.currentSession,
      collaborationPage: state.collaborationPage,
      loadCollaborations: state.loadCollaborations,
      switchProject: state.switchProject,
      activeProjectId: state.activeProjectId,
      projects: state.projects,
      sessions: state.sessions,
      projectSessions: state.projectSessions,
      ensureProjectSessionsLoaded: state.ensureProjectSessionsLoaded,
    })),
  );
  const client = useConsoleStore((state) => state.client);
  const selectedPeerKey = useRightDockStore((state) => state.selectedPeerKey);
  const setSelectedPeerKey = useRightDockStore((state) => state.setSelectedPeerKey);

  const view = collaborationView(collaborationPage, currentSession);
  const peerProjectIdsKey = view.requests.map((r) => r.peerProjectId).join(',');
  useEffect(() => {
    if (peerProjectIdsKey === '') return;
    void ensureProjectSessionsLoaded?.(peerProjectIdsKey.split(','));
  }, [peerProjectIdsKey, ensureProjectSessionsLoaded]);

  const peerCatalog = useMemo(
    () => ({ activeProjectId: consoleActiveProjectId, projects, sessions, projectSessions }),
    [consoleActiveProjectId, projects, sessions, projectSessions],
  );

  const peers: Peer[] = [];
  for (const request of view.requests) {
    const key = `${request.peerProjectId}:${request.peerThreadId}`;
    if (peers.some((peer) => `${peer.projectId}:${peer.threadId}` === key)) continue;
    const resolved = resolveCollaborationPeer(
      request.peerProjectId,
      request.peerThreadId,
      peerCatalog,
    );
    peers.push({
      projectId: request.peerProjectId,
      threadId: request.peerThreadId,
      label: `${request.peerProjectId} / ${request.peerThreadId}`,
      displayLabel:
        resolved.displayName !== request.peerThreadId
          ? `${resolved.displayName} · ${request.peerThreadId}`
          : request.peerThreadId,
    });
  }

  const [selected, setSelected] = useState<string | null>(null);
  const [events, setEvents] = useState<HistoryEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const readVersion = useRef(0);

  useEffect(() => {
    void loadCollaborations();
  }, [loadCollaborations, currentSession.project_id, currentSession.thread_id]);

  const effectiveSelected =
    (selectedPeerKey !== null &&
    peers.some((peer) => `${peer.projectId}:${peer.threadId}` === selectedPeerKey)
      ? selectedPeerKey
      : null) ??
    (selected !== null &&
    peers.some((peer) => `${peer.projectId}:${peer.threadId}` === selected)
      ? selected
      : null) ??
    (peers[0] !== undefined ? `${peers[0].projectId}:${peers[0].threadId}` : null);

  const active =
    peers.find((peer) => `${peer.projectId}:${peer.threadId}` === effectiveSelected) ?? null;

  const activeProjectId = active?.projectId;
  const activeThreadId = active?.threadId;
  const loadPeer = useCallback(async (projectId: string, threadId: string): Promise<void> => {
    if (client === null) return;
    const version = ++readVersion.current;
    setLoading(true);
    try {
      const page = await client.readSessionHistory({
        session: { project_id: projectId, thread_id: threadId },
        limit: PREVIEW_TURNS,
      });
      if (version !== readVersion.current) return;
      setEvents(page.events);
      setError(page.available ? null : '对方会话还没有可读的转录记录。');
    } catch (err) {
      if (version !== readVersion.current) return;
      setEvents([]);
      setError(err instanceof RpcCallError && err.service_code === 'transport_busy'
        ? '连接正在处理其他请求，请稍后刷新对方会话。'
        : String((err as Error)?.message ?? err));
    } finally {
      if (version === readVersion.current) setLoading(false);
    }
  }, [client]);

  useEffect(() => {
    if (activeProjectId === undefined || activeThreadId === undefined) {
      setEvents([]);
      setError(null);
      return;
    }
    void loadPeer(activeProjectId, activeThreadId);
    return () => { readVersion.current += 1; };
  }, [activeProjectId, activeThreadId, loadPeer]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2">
        <span className="ui-section-label">对方会话（只读）</span>
        <button
          type="button"
          className="ui-icon-button ui-compact ml-auto"
          aria-label="刷新对方会话"
          title="刷新对方会话"
          disabled={loading || active === null}
          onClick={() => {
            if (active !== null) void loadPeer(active.projectId, active.threadId);
          }}
        >
          <ArrowSync16Regular aria-hidden="true" />
        </button>
      </div>

      {peers.length === 0 ? (
        <p className="p-3 text-xs text-gray-500">
          这个会话还没有协作对象。协作请求建立后，对方的会话会出现在这里。
        </p>
      ) : (
        <>
          <ul className="border-b border-line p-2">
            {peers.map((peer) => {
              const key = `${peer.projectId}:${peer.threadId}`;
              return (
                <li key={key}>
                  <button
                    type="button"
                    className="ui-nav-row flex w-full items-center gap-2 px-2 py-1 text-left text-xs"
                    data-selected={key === effectiveSelected}
                    onClick={() => {
                      setSelected(key);
                      setSelectedPeerKey(key);
                      if (key === effectiveSelected && !loading && (events.length === 0 || error !== null)) {
                        void loadPeer(peer.projectId, peer.threadId);
                      }
                    }}
                  >
                    <Chat16Regular aria-hidden="true" />
                    <span className="min-w-0 truncate" title={peer.label}>
                      {peer.displayLabel}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
          <div className="min-h-0 flex-1 overflow-auto p-3">
            {error !== null && <p className="mb-2 text-xs text-red-700">{error}</p>}
            {events.length === 0 && error === null && active !== null && !loading && (
              <p className="text-xs text-gray-500">对方会话最近没有可显示的内容。</p>
            )}
            <ul className="space-y-2">
              {events.map((event, index) => (
                <li key={`${event.kind}-${index}`} className="text-xs">
                  <div className="text-[11px] text-gray-500">{eventLabel(event.kind)}</div>
                  <p className="mt-0.5 whitespace-pre-wrap break-words text-gray-700">
                    {event.text}
                  </p>
                </li>
              ))}
            </ul>
          </div>
          {active !== null && (
            <div className="border-t border-line p-2">
              <button
                type="button"
                className="ui-button ui-compact w-full justify-start"
                onClick={() => void switchProject(active.projectId, active.threadId)}
              >
                <Chat16Regular aria-hidden="true" />
                进入完整会话
                <ArrowRight16Regular aria-hidden="true" className="ml-auto" />
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
};

export const peerSessionTab: RightDockTabDefinition = {
  id: 'peer-session',
  label: '对方会话',
  order: 56,
  Icon: Window16Regular,
  Content: PeerSessionContent,
};
