import {
  ArrowDownLeft16Regular,
  ArrowRight16Regular,
  ArrowSync16Regular,
  Checkmark16Regular,
  CheckmarkCircle16Regular,
  Clock16Regular,
  Copy16Regular,
  DismissCircle16Regular,
  SpinnerIos20Regular,
} from '@fluentui/react-icons';
import React, { useMemo } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { resolveCollaborationPeer } from '../../stores/collaborationView.ts';
import type { ToolItemView } from '../../stores/historyMapper.ts';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import { Markdown } from '../Markdown.tsx';
import type { RowRenderProps } from './context.ts';
import { useCopyFlag } from './useCopyFlag.ts';

interface ParsedCollaborationMessage {
  summary: string;
  body: string;
  senderRaw: string;
  kindText: string;
}

/** Parse incoming collaboration text formatted by runtime.py. */
function parseCollaborationText(content: string): ParsedCollaborationMessage {
  const newlineIdx = content.indexOf('\n');
  if (newlineIdx === -1) {
    return { summary: content.trim(), body: '', senderRaw: '', kindText: content.trim() };
  }
  const summary = content.slice(0, newlineIdx).trim();
  const body = content.slice(newlineIdx + 1).trim();

  // Format: "<KindLabel> · 来自 <SenderRef>"
  const fromMatch = summary.match(/^(.*?)\s*·\s*来自\s+([^\s]+)/);
  const kindText = fromMatch ? fromMatch[1].trim() : '协作消息';
  const senderRaw = fromMatch ? fromMatch[2].trim() : '';

  return { summary, body, senderRaw, kindText };
}

/**
 * Rich card renderer for incoming collaboration events in the main transcript stream.
 * Replaces the crude plain-text InfoRow for collaboration inputs.
 */
export const CollaborationRow = React.memo(function CollaborationRow({
  message,
}: RowRenderProps) {
  const { activeProjectId, projects, sessions, projectSessions } = useConsoleStore(
    useShallow((state) => ({
      activeProjectId: state.activeProjectId,
      projects: state.projects,
      sessions: state.sessions,
      projectSessions: state.projectSessions,
    })),
  );

  const [copied, copy] = useCopyFlag();
  const rawContent = message.content ?? '';
  const parsed = useMemo(() => parseCollaborationText(rawContent), [rawContent]);

  const peerCatalog = useMemo(
    () => ({ activeProjectId, projects, sessions, projectSessions }),
    [activeProjectId, projects, sessions, projectSessions],
  );

  const resolvedPeer = useMemo(() => {
    if (!parsed.senderRaw) return null;
    const parts = parsed.senderRaw.split(':');
    const projectId = parts.length > 1 ? parts[0] : '';
    const threadId = parts.length > 1 ? parts[1] : parts[0];
    return resolveCollaborationPeer(projectId, threadId, peerCatalog);
  }, [parsed.senderRaw, peerCatalog]);

  const isReply = parsed.kindText.includes('结论');
  const isQuestion = parsed.kindText.includes('提问') || parsed.kindText.includes('澄清');
  const isProgress = parsed.kindText.includes('进度');

  return (
    <div data-turn-id={message.turnId} className="flex flex-col gap-1.5 w-full min-w-0 max-w-[88%]">
      <div className="rounded-card border border-line/80 bg-surface/90 shadow-card p-3 backdrop-blur-sm transition-all">
        {/* Header */}
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line/60 pb-2">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <span
              className={`inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium leading-none ${
                isReply
                  ? 'bg-emerald-500/10 text-emerald-700 border border-emerald-500/25'
                  : isQuestion
                    ? 'bg-blue-500/10 text-blue-500 border border-blue-500/25'
                    : isProgress
                      ? 'bg-amber-500/10 text-amber-700 border border-amber-500/25'
                      : 'bg-gray-500/10 text-gray-700 border border-gray-500/25'
              }`}
            >
              {isReply ? (
                <CheckmarkCircle16Regular aria-hidden="true" className="shrink-0" />
              ) : isProgress ? (
                <Clock16Regular aria-hidden="true" className="shrink-0" />
              ) : (
                <ArrowDownLeft16Regular aria-hidden="true" className="shrink-0" />
              )}
              <span>{parsed.kindText || '协作回传'}</span>
            </span>

            {/* Preserves the exact text format for testing and clear semantic identity */}
            <span className="font-medium text-xs text-gray-800">
              {parsed.summary}
            </span>

            {resolvedPeer && resolvedPeer.displayName !== parsed.senderRaw && (
              <span
                className="min-w-0 truncate rounded-full bg-sunken/60 px-2 py-0.5 font-sans text-xs text-gray-600"
                title={resolvedPeer.fullRef}
              >
                {resolvedPeer.displayName}
              </span>
            )}
          </div>

          <div className="flex items-center gap-1.5 shrink-0 ml-auto">
            <button
              type="button"
              onClick={() => copy(parsed.body || rawContent)}
              title={copied ? '已复制' : '复制内容'}
              aria-label="复制内容"
              className="p-1 rounded text-gray-400 hover:text-gray-700 hover:bg-surface-hover cursor-pointer transition-colors"
            >
              {copied ? (
                <Checkmark16Regular aria-hidden="true" className="text-accent-fg" />
              ) : (
                <Copy16Regular aria-hidden="true" />
              )}
            </button>
            <span className="font-mono text-[10px] text-gray-400">{message.timestamp}</span>
          </div>
        </div>

        {/* Body */}
        <div className="mt-2.5 min-w-0 break-words text-sm leading-relaxed text-gray-800">
          {parsed.body ? (
            <Markdown text={parsed.body} />
          ) : (
            <Markdown text={parsed.summary} />
          )}
        </div>
      </div>
    </div>
  );
});

/**
 * Dedicated Card for Outgoing Collaboration tool calls (contact_session / reply_to_collaboration)
 * inside ToolGroupRow, instead of a crude wrench fold.
 */
export const OutgoingCollaborationCard: React.FC<{
  tool: ToolItemView;
}> = React.memo(({ tool }) => {
  const { activeProjectId, projects, sessions, projectSessions } = useConsoleStore(
    useShallow((state) => ({
      activeProjectId: state.activeProjectId,
      projects: state.projects,
      sessions: state.sessions,
      projectSessions: state.projectSessions,
    })),
  );

  const peerCatalog = useMemo(
    () => ({ activeProjectId, projects, sessions, projectSessions }),
    [activeProjectId, projects, sessions, projectSessions],
  );

  const isContact = tool.name === 'contact_session';
  const isReply = tool.name === 'reply_to_collaboration';

  const args = (tool.args ?? {}) as Record<string, unknown>;
  const targetRaw = String(args.target ?? '');
  const requestBody = String(args.request ?? args.message ?? '');
  const expectDeliverable = typeof args.expect === 'string' && args.expect.trim() ? args.expect.trim() : null;
  const kindArg = typeof args.kind === 'string' ? args.kind : '';

  const resolvedPeer = useMemo(() => {
    if (!targetRaw) return null;
    const parts = targetRaw.replace(/^@session:/, '').split(':');
    const projectId = parts.length > 1 ? parts[0] : '';
    const threadId = parts.length > 1 ? parts[1] : parts[0];
    return resolveCollaborationPeer(projectId, threadId, peerCatalog);
  }, [targetRaw, peerCatalog]);

  const active = tool.status === 'running' || tool.status === 'pending';
  const failed = tool.status === 'failed' || Boolean(tool.error);

  const kindLabel =
    kindArg === 'reply'
      ? '结论'
      : kindArg === 'clarification'
        ? '提问澄清'
        : kindArg === 'supplement'
          ? '补充答复'
          : kindArg === 'progress'
            ? '进度报告'
            : '';

  return (
    <div className="rounded-card border border-line/80 bg-surface/80 p-3 shadow-card backdrop-blur-sm transition-all my-1.5 w-full min-w-0">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line/50 pb-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2 text-xs">
          <span
            className={`inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium leading-none ${
              isContact
                ? 'bg-blue-500/10 text-blue-500 border border-blue-500/25'
                : 'bg-emerald-500/10 text-emerald-700 border border-emerald-500/25'
            }`}
          >
            {isContact ? (
              <ArrowSync16Regular aria-hidden="true" className={active ? 'animate-spin' : ''} />
            ) : (
              <ArrowRight16Regular aria-hidden="true" />
            )}
            <span>{isContact ? '发出协作交办' : `交付协作${kindLabel}`}</span>
          </span>

          {isContact && (
            <div className="flex items-center gap-1 font-medium text-gray-700">
              <span className="text-gray-400">我请对方</span>
              <span>·</span>
              <span className="text-gray-900 font-semibold" title={resolvedPeer?.fullRef}>
                {resolvedPeer ? resolvedPeer.displayName : targetRaw}
              </span>
            </div>
          )}

          {isReply && (
            <span className="font-medium text-gray-700">
              向协作方回传{kindLabel}
            </span>
          )}
        </div>

        {/* Status indicator */}
        <div className="ml-auto flex items-center gap-1.5 shrink-0 text-[10px]">
          {active ? (
            <span className="inline-flex items-center gap-1 text-blue-500 font-medium">
              <SpinnerIos20Regular aria-hidden="true" className="animate-spin" style={{ fontSize: '12px' }} />
              <span>{isContact ? '等待对方处理' : '正在发送'}</span>
            </span>
          ) : failed ? (
            <span className="inline-flex items-center gap-1 text-red-600 font-medium">
              <DismissCircle16Regular aria-hidden="true" style={{ fontSize: '12px' }} />
              <span>发送失败</span>
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 text-emerald-600 font-medium">
              <CheckmarkCircle16Regular aria-hidden="true" style={{ fontSize: '12px' }} />
              <span>{isContact ? '交办已发出' : '已交付'}</span>
            </span>
          )}
        </div>
      </div>

      {/* Body */}
      {requestBody && (
        <div className="mt-2.5 text-sm leading-relaxed text-gray-800 min-w-0 break-words">
          <Markdown text={requestBody} />
        </div>
      )}

      {/* Expected deliverable */}
      {expectDeliverable && (
        <div className="mt-2 rounded-control bg-sunken/40 border border-line/40 px-2.5 py-1.5 text-xs text-gray-600">
          <span className="font-semibold text-gray-700">期望交付：</span>
          <span>{expectDeliverable}</span>
        </div>
      )}

      {/* Return preview / acknowledgment */}
      {tool.preview && (
        <div className="mt-2 font-mono text-[10px] text-gray-500 border-t border-line/40 pt-1.5 truncate" title={tool.preview}>
          {tool.preview}
        </div>
      )}
    </div>
  );
});
