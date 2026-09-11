import React, { useEffect, useMemo, useRef, useState } from 'react';
import { CONSOLE_VERSION } from '../consoleInfo.ts';
import { useConsoleStore } from '../stores/useConsoleStore';
import { filterSessions, groupSessionsByTime } from '../stores/sessionList.ts';
import { SettingsDialog } from './SettingsDialog.tsx';

export const SideBar: React.FC = () => {
  const {
    isSidebarCollapsed,
    sessions,
    sessionsTotal,
    sessionsNextOffset,
    sessionsLoading,
    sessionQuery,
    setSessionQuery,
    searchFocusToken,
    requestSessionSearchFocus,
    loadMoreSessions,
    createNewSession,
    toggleSidebar,
    currentSession,
    switchSession,
  } = useConsoleStore();

  const searchRef = useRef<HTMLInputElement>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);

  // Ctrl+K bumps a token instead of reaching into the DOM from the App shell,
  // so a collapsed sidebar is expanded by the same store update.
  useEffect(() => {
    if (searchFocusToken > 0) searchRef.current?.focus();
  }, [searchFocusToken]);

  const groups = useMemo(
    () => groupSessionsByTime(filterSessions(sessions, sessionQuery)),
    [sessions, sessionQuery],
  );

  if (isSidebarCollapsed) {
    // Collapsed to a minimal rail: the workspace stays reachable instead of
    // disappearing (expand, new session, search, and the loaded count).
    return (
      <nav className="bg-[#f8f9fa] border-r border-[#e5e7eb] h-full w-[44px] flex flex-col items-center py-3 gap-1.5 shrink-0 select-none">
        <button
          onClick={toggleSidebar}
          title="展开侧栏 (Ctrl+B)"
          className="flex items-center justify-center w-7 h-7 rounded hover:bg-gray-200 text-gray-600 hover:text-gray-900 transition-colors cursor-pointer"
        >
          <span className="material-symbols-outlined text-[18px]">dock_to_right</span>
        </button>
        <button
          onClick={() => createNewSession()}
          title="新建会话 (Ctrl+N)"
          className="flex items-center justify-center w-7 h-7 rounded hover:bg-gray-200 text-gray-600 hover:text-gray-900 transition-colors cursor-pointer"
        >
          <span className="material-symbols-outlined text-[18px]">add</span>
        </button>
        <button
          onClick={requestSessionSearchFocus}
          title="搜索会话 (Ctrl+K)"
          className="flex items-center justify-center w-7 h-7 rounded hover:bg-gray-200 text-gray-600 hover:text-gray-900 transition-colors cursor-pointer"
        >
          <span className="material-symbols-outlined text-[18px]">search</span>
        </button>
        <div
          title={`已加载 ${sessions.length} / 共 ${sessionsTotal} 个会话`}
          className="mt-auto mb-1 font-mono text-[10px] text-gray-400"
        >
          {sessions.length}
        </div>
      </nav>
    );
  }

  const searching = sessionQuery.trim() !== '';

  return (
    <nav className="bg-[#f8f9fa] border-r border-[#e5e7eb] h-full w-[240px] flex flex-col py-3 shrink-0 select-none text-xs font-sans">
      <div className="px-3">
        <div className="flex items-center justify-between mb-2">
          <span className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider font-mono">
            RECENT SESSIONS
          </span>
          <button
            onClick={() => createNewSession()}
            className="flex items-center justify-center w-5 h-5 rounded hover:bg-gray-200 text-gray-500 hover:text-gray-900 transition-colors cursor-pointer"
            title="新建会话 (Ctrl+N)"
          >
            <span className="material-symbols-outlined text-[15px]">add</span>
          </button>
        </div>
        <div className="relative">
          <span className="material-symbols-outlined absolute left-1.5 top-1/2 -translate-y-1/2 text-[14px] text-gray-400">
            search
          </span>
          <input
            ref={searchRef}
            type="text"
            value={sessionQuery}
            onChange={(event) => setSessionQuery(event.target.value)}
            placeholder="搜索会话"
            title="搜索会话 (Ctrl+K)"
            spellCheck={false}
            className="w-full rounded border border-gray-200 bg-white pl-6 pr-6 py-1 text-xs text-gray-800 placeholder:text-gray-400 focus:outline-none focus:border-blue-500"
          />
          {searching && (
            <button
              type="button"
              onClick={() => setSessionQuery('')}
              title="清除搜索"
              className="material-symbols-outlined absolute right-1 top-1/2 -translate-y-1/2 text-[14px] text-gray-400 hover:text-gray-700 cursor-pointer"
            >
              close
            </button>
          )}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-2 mt-2">
        {groups.length === 0 && (
          <div className="px-2 py-3 text-[11px] text-gray-400 font-mono">
            {searching ? '没有匹配的会话' : '暂无会话'}
          </div>
        )}
        {groups.map((group) => (
          <div key={group.key} className="mt-1 px-1">
            <div className="px-1 mb-1 text-[10px] font-semibold text-gray-400 uppercase tracking-wider font-mono">
              {group.label} · {group.items.length}
            </div>
            <ul className="space-y-1 text-gray-600 text-xs">
              {group.items.map((sess) => {
                const isSelected = sess.thread_id === currentSession.thread_id;
                return (
                  <li
                    key={sess.thread_id}
                    onClick={() => switchSession(sess.thread_id, sess.title)}
                    title={`${sess.title}\n${sess.thread_id}`}
                    className={`cursor-pointer px-1.5 py-1 rounded hover:bg-gray-200/60 transition-colors truncate ${
                      isSelected
                        ? 'font-medium text-gray-900 bg-gray-200/80 shadow-2xs'
                        : 'text-gray-600'
                    }`}
                  >
                    {sess.title}
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </div>

      <div className="px-3 pt-2">
        {sessionsNextOffset !== null && (
          <button
            type="button"
            onClick={() => {
              void loadMoreSessions();
            }}
            disabled={sessionsLoading}
            className="w-full rounded border border-gray-200 bg-white px-2 py-1 text-[11px] font-mono text-gray-600 hover:bg-gray-100 transition-colors disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
          >
            {sessionsLoading ? '加载中…' : '加载更多'}
          </button>
        )}
        <div className="mt-1 text-[10px] font-mono text-gray-400">
          已加载 {sessions.length} / {sessionsTotal}
          {searching && sessionsNextOffset !== null ? '（搜索仅覆盖已加载）' : ''}
        </div>
      </div>

      <div className="mt-2 px-3 pt-3 border-t border-[#e5e7eb]">
        <button
          type="button"
          onClick={() => setSettingsOpen(true)}
          title="打开设置"
          className="flex w-full items-center space-x-2 text-gray-600 hover:bg-gray-200/60 p-1.5 rounded transition-colors text-xs cursor-pointer"
        >
          <span className="material-symbols-outlined text-[17px] text-gray-500">settings</span>
          <span className="flex-1 text-left">设置</span>
          <span className="text-gray-400 font-mono text-[11px]">Synapse {CONSOLE_VERSION}</span>
        </button>
      </div>

      {settingsOpen && <SettingsDialog onClose={() => setSettingsOpen(false)} />}
    </nav>
  );
};
