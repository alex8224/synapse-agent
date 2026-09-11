import React, { useEffect, useRef } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';

export const Transcript: React.FC = () => {
  const {
    messages,
    toggleThoughtExpand,
    pendingApproval,
    resolveApproval,
    historyLoading,
    historyHasMore,
    historyAvailable,
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

  return (
    <div className="flex-1 overflow-y-auto px-8 py-6 pb-36 font-sans">
      <div className="max-w-4xl space-y-5">
        {historyAvailable === false && (
          <div className="rounded-lg border border-amber-200 bg-amber-50/70 p-3 text-xs text-amber-800 font-mono leading-relaxed">
            此会话在 transcript 投影中不可用（history.available=false）。以下只显示建立连接后的实时内容；
            不按“空历史”显示，也不会回退到 checkpoint。
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

        {messages.length === 0 && !historyLoading && historyAvailable !== false && (
          <div className="py-16 text-center text-gray-400 text-xs font-mono select-none">
            当前会话已建立长连接，在下方输入指令即可开始与 Synapse Agent 对话
          </div>
        )}

        {messages.map((m) => {
          if (m.type === 'user') {
            return (
              <div key={m.id} className="pt-4 border-t border-gray-100/80 first:border-t-0">
                <div className="flex items-baseline space-x-2 mb-1.5">
                  <span className="font-bold text-gray-900 text-sm">User</span>
                  <span className="text-gray-400 font-mono text-xs">{m.timestamp}</span>
                </div>
                <div className="text-gray-900 text-sm leading-relaxed flex items-start bg-gray-50/70 p-3 rounded-lg border border-gray-100">
                  <span className="text-xs mr-2 text-blue-600">●</span>
                  <span>{m.content}</span>
                </div>
              </div>
            );
          }
          if (m.type === 'thought') {
            return (
              <div key={m.id}>
                <div
                  onClick={() => toggleThoughtExpand(m.id)}
                  className="inline-flex items-center space-x-2 px-3 py-1.5 rounded bg-[#f3f4f5] border border-gray-200 text-gray-700 text-xs cursor-pointer hover:bg-gray-200/80 transition-colors select-none font-mono"
                >
                  <span className="material-symbols-outlined text-[15px] text-gray-600">psychology</span>
                  <span>{m.duration ? `Thought (${m.duration})` : 'Thinking...'}</span>
                  <span className="text-gray-400">{m.expanded ? '(收起)' : '(展开)'}</span>
                </div>
                {m.expanded && (
                  <div className="mt-2 p-3 bg-gray-50 border border-gray-200 rounded text-xs font-mono text-gray-600 whitespace-pre-wrap">
                    {m.content}
                  </div>
                )}
              </div>
            );
          }
          if (m.type === 'tool_group') {
            const toolList = m.tools || [];
            const maxShow = 4;
            const shownTools = toolList.slice(0, maxShow);
            const remainingCount = toolList.length - maxShow;
            return (
              <div key={m.id} className="py-1">
                <div className="inline-flex items-center space-x-2 px-3 py-1.5 rounded bg-[#f3f4f5] border border-gray-200 text-gray-700 text-xs select-none font-mono">
                  <span className="material-symbols-outlined text-[15px] text-gray-500">arrow_drop_down</span>
                  <span>{toolList.length} tools executed:</span>
                  {shownTools.map((t, idx) => (
                    <span key={idx} className="inline-flex items-center space-x-1">
                      <span className="material-symbols-outlined text-[13px] text-gray-500">search</span>
                      <span className="text-blue-600 font-medium">{t.name}</span>
                      {idx < shownTools.length - 1 && <span className="text-gray-300">·</span>}
                    </span>
                  ))}
                  {remainingCount > 0 && (
                    <span className="text-gray-400 font-medium">+{remainingCount} more</span>
                  )}
                </div>
              </div>
            );
          }
          if (m.type === 'assistant') {
            return (
              <div key={m.id} className="pt-2">
                <div className="mb-1.5 flex items-center justify-between">
                  <span className="font-bold text-gray-900 text-sm">Assistant</span>
                  <span className="text-gray-400 font-mono text-xs">{m.timestamp}</span>
                </div>
                <div className="text-gray-900 text-sm leading-relaxed whitespace-pre-wrap font-sans bg-white p-4 rounded-lg border border-gray-200/80 shadow-2xs">
                  {m.content}
                </div>
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
        <div ref={bottomRef} />
      </div>
    </div>
  );
};
