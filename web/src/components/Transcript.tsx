import React, { useEffect, useRef } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';
import { expandHint, thoughtLabel, toolGroupLabel, toolStatusLabel } from '../stores/transcriptLabels.ts';
import { Markdown } from './Markdown.tsx';
import { AttachmentThumb } from './AttachmentThumb.tsx';

export const Transcript: React.FC = () => {
  const {
    messages,
    activity,
    toggleMessageExpand,
    pendingApproval,
    resolveApproval,
    historyLoading,
    historyHasMore,
    historyAvailable,
    historyError,
    loadEarlierHistory,
  } = useConsoleStore();
  const bottomRef = useRef<HTMLDivElement>(null);
  const skipAutoScroll = useRef(false);

  useEffect(() => {
    if (skipAutoScroll.current) {
      // Prepending an earlier history page must not yank the view back to bottom.
      skipAutoScroll.current = false;
      return;
    }
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const handleLoadEarlier = () => {
    skipAutoScroll.current = true;
    loadEarlierHistory();
  };

  /**
   * Expand/collapse one fold.
   *
   * This is a *view* change, not new content, but the store replaces the
   * `messages` array to flip the flag — and the auto-scroll effect keys off that
   * array identity.  Without the same guard the "load earlier" path uses, opening
   * a fold yanked the transcript to the bottom, so it never appeared to open in
   * place.
   */
  const handleToggleExpand = (id: string) => {
    skipAutoScroll.current = true;
    toggleMessageExpand(id);
  };

  return (
    <div className="flex-1 overflow-y-auto px-8 py-6 pb-36 font-sans">
      <div className="mx-auto max-w-4xl space-y-5">
        {historyAvailable === false && (
          <div className="rounded-lg border border-amber-200 bg-amber-50/70 p-3 text-xs text-amber-800 font-mono leading-relaxed">
            此会话在 transcript 投影中不可用（history.available=false）。以下只显示建立连接后的实时内容；
            不按“空历史”显示，也不会回退到 checkpoint。
          </div>
        )}

        {historyError !== null && (
          <div
            role="alert"
            className="rounded-lg border border-red-200 bg-red-50/70 p-3 text-xs text-red-800 font-mono leading-relaxed"
          >
            {historyError}
            以下只显示建立连接后的实时内容，不回退到 checkpoint。
          </div>
        )}

        {historyHasMore && messages.length > 0 && (
          <div className="flex justify-center pt-1">
            <button
              onClick={handleLoadEarlier}
              disabled={historyLoading}
              className="inline-flex items-center space-x-1.5 px-3 py-1.5 rounded border border-gray-200 bg-[#f8f9fa] text-gray-600 text-xs font-mono hover:bg-gray-200/70 transition-colors disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer select-none"
            >
              <span className="material-symbols-outlined text-[14px]">unfold_more</span>
              <span>{historyLoading ? '加载更早历史中…' : '加载更早历史'}</span>
            </button>
          </div>
        )}

        {historyLoading && messages.length === 0 && (
          <div className="py-6 text-center text-gray-400 text-xs font-mono select-none">正在加载历史…</div>
        )}

        {messages.length === 0 &&
          !historyLoading &&
          historyAvailable !== false &&
          historyError === null && (
          <div className="py-16 text-center text-gray-400 text-xs font-mono select-none">
            当前会话已建立长连接，在下方输入指令即可开始与 Synapse Agent 对话
          </div>
        )}

        {messages.map((m) => {
          if (m.type === 'user') {
            return (
              // Chat layout: the user's turn sits on the right, the assistant's on
              // the left, and the side it is on is the role — so no "User" /
              // "Assistant" heading is needed.
              <div key={m.id} className="flex justify-end">
                <div className="flex max-w-[80%] flex-col items-end gap-1.5">
                  {m.content !== '' && (
                    // No bubble: the side it sits on is the role, and the frame
                    // only added noise around the text.
                    <div className="whitespace-pre-wrap break-words text-base leading-relaxed text-gray-900">
                      {m.content}
                    </div>
                  )}
                  {m.attachments !== undefined && m.attachments.length > 0 && (
                    <div className="flex flex-wrap justify-end gap-2">
                      {m.attachments.map((attachment) => (
                        <AttachmentThumb key={attachment.attachmentId} attachment={attachment} />
                      ))}
                    </div>
                  )}
                  <span className="font-mono text-[10px] text-gray-400">{m.timestamp}</span>
                </div>
              </div>
            );
          }
          if (m.type === 'thought') {
            return (
              <div key={m.id} className="max-w-[85%]">
                <div
                  onClick={() => handleToggleExpand(m.id)}
                  className="inline-flex items-center space-x-2 px-3 py-1.5 rounded bg-[#f3f4f5] border border-gray-200 text-gray-700 text-xs cursor-pointer hover:bg-gray-200/80 transition-colors select-none font-mono"
                >
                  <span>{thoughtLabel(m.duration)}</span>
                  <span className="text-gray-400">{expandHint(m.expanded === true)}</span>
                </div>
                {m.expanded && (
                  <div className="mt-2 rounded border border-gray-200 bg-gray-50 p-3 text-sm text-gray-600">
                    <Markdown text={m.content ?? ''} />
                  </div>
                )}
              </div>
            );
          }
          if (m.type === 'tool_group') {
            const toolList = m.tools || [];
            const failed = toolList.filter((t) => t.error || t.status === 'failed').length;
            const running = toolList.filter(
              (t) => t.status === 'running' || t.status === 'pending',
            ).length;
            const expanded = m.expanded === true;
            return (
              <div key={m.id} className="max-w-[85%] py-1">
                <div
                  onClick={() => handleToggleExpand(m.id)}
                  title={expanded ? '收起工具详情' : '展开工具详情'}
                  className="inline-flex items-center space-x-2 px-3 py-1.5 rounded bg-[#f3f4f5] border border-gray-200 text-gray-700 text-xs cursor-pointer hover:bg-gray-200/80 transition-colors select-none font-mono"
                >
                  <span className="material-symbols-outlined text-[15px] text-gray-500">
                    {expanded ? 'arrow_drop_down' : 'arrow_right'}
                  </span>
                  <span>{toolGroupLabel(toolList.length, m.parallel === true)}</span>
                  {running > 0 && (
                    <span className="text-blue-600 font-medium">{running} running</span>
                  )}
                  {failed > 0 && <span className="text-red-600 font-medium">{failed} failed</span>}
                  {!expanded && toolList.length > 0 && (
                    <span className="text-gray-400 truncate">
                      {toolList.slice(0, 4).map((t) => t.name).join(' · ')}
                      {toolList.length > 4 ? ` +${toolList.length - 4}` : ''}
                    </span>
                  )}
                </div>
                {expanded && (
                  <div className="mt-2 space-y-1.5">
                    {toolList.map((t) => (
                      <div
                        key={t.id}
                        className={`rounded border px-2.5 py-1.5 font-mono text-[11px] ${
                          t.error ? 'border-red-200 bg-red-50/60' : 'border-gray-200 bg-white'
                        }`}
                      >
                        <div className="flex items-center space-x-2">
                          <span className="material-symbols-outlined text-[13px] text-gray-500">
                            {t.icon}
                          </span>
                          <span className="font-medium text-gray-900">{t.label || t.name}</span>
                          {t.sub && (
                            <span className="rounded bg-gray-100 px-1 text-[10px] text-gray-500">
                              sub
                            </span>
                          )}
                          {t.subagentName && (
                            <span className="text-[10px] text-gray-400">@{t.subagentName}</span>
                          )}
                          {t.path && <span className="truncate text-gray-500">{t.path}</span>}
                          <span
                            className={`ml-auto shrink-0 rounded px-1 text-[10px] ${
                              t.error
                                ? 'bg-red-100 text-red-700'
                                : t.status === 'completed'
                                  ? 'bg-green-100 text-green-700'
                                  : 'bg-blue-50 text-blue-600'
                            }`}
                          >
                            {t.subagentStatus
                              ? `${toolStatusLabel(t.status)} · ${t.subagentStatus}`
                              : toolStatusLabel(t.status)}
                          </span>
                        </div>
                        {t.preview && (
                          <div className="mt-1 whitespace-pre-wrap break-all text-gray-600">
                            {t.preview}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          }
          if (m.type === 'assistant') {
            return (
              <div key={m.id} className="flex max-w-[80%] flex-col items-start gap-1.5">
                <div className="text-base leading-relaxed font-sans text-gray-900">
                  <Markdown text={m.content ?? ''} />
                </div>
                <span className="font-mono text-[10px] text-gray-400">{m.timestamp}</span>
              </div>
            );
          }
          if (m.type === 'info') {
            const warning = m.infoLevel === 'warning';
            return (
              <div
                key={m.id}
                className={`max-w-[85%] rounded border px-3 py-1.5 font-mono text-[11px] leading-relaxed ${
                  warning
                    ? 'border-amber-200 bg-amber-50/70 text-amber-800'
                    : 'border-gray-200 bg-gray-50/70 text-gray-600'
                }`}
              >
                <span className="material-symbols-outlined align-middle text-[13px]">
                  {warning ? 'warning' : 'info'}
                </span>{' '}
                <span className="whitespace-pre-wrap break-all">{m.content}</span>
              </div>
            );
          }
          return null;
        })}

        {/* HITL Pending Approval Dialog */}
        {pendingApproval && (
          <div className="p-4 border border-amber-300 bg-amber-50/80 rounded-lg space-y-3">
            <div className="flex items-center space-x-2 text-amber-800 font-medium text-xs">
              <span className="material-symbols-outlined text-[18px]">gavel</span>
              <span>需要审批危险操作 (Turn: {pendingApproval.turn_id})</span>
            </div>
            <div className="space-y-1.5 font-mono text-xs text-gray-700">
              {pendingApproval.actions.map((act, idx) => (
                <div key={idx} className="p-2 bg-white rounded border border-amber-200">
                  <div className="font-bold text-gray-900">{act.name}</div>
                  <div className="text-gray-600 text-[11px] truncate">{JSON.stringify(act.args)}</div>
                </div>
              ))}
            </div>
            <div className="flex space-x-2 pt-1">
              <button
                onClick={() => resolveApproval('allow_once')}
                className="px-3 py-1 bg-green-600 text-white text-xs font-medium rounded hover:bg-green-700 transition-colors cursor-pointer"
              >
                批准本次
              </button>
              <button
                onClick={() => resolveApproval('reject_once')}
                className="px-3 py-1 bg-red-600 text-white text-xs font-medium rounded hover:bg-red-700 transition-colors cursor-pointer"
              >
                拒绝
              </button>
            </div>
          </div>
        )}
        {activity && activity.active && (
          <div className="flex items-center space-x-2 font-mono text-xs text-gray-500 select-none">
            <span className="w-1.5 h-1.5 rounded-full bg-blue-600 animate-pulse" />
            <span>{activity.phase}</span>
            {activity.detail && <span className="text-gray-400">{activity.detail}</span>}
          </div>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
};
