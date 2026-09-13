import React, { useEffect, useMemo, useRef, useState } from 'react';
import { CONSOLE_VERSION } from '../consoleInfo.ts';
import { useConsoleStore } from '../stores/useConsoleStore';
import {
  SESSION_TITLE_MAX,
  filterSessions,
  groupSessionsByTime,
  matchesProject,
  projectLabel,
} from '../stores/sessionList.ts';
import { SettingsDialog } from './SettingsDialog.tsx';

/** Sessions shown per expanded project before the "show all" row (TUI parity). */
const VISIBLE_SESSIONS = 5;

/**
 * Sidebar: a two-level project -> session tree, mirroring the TUI drawer.
 *
 * Level 1 is one row per switchable project (directory label, session count,
 * current-project marker).  Level 2 lists that project's sessions grouped by
 * relative time.  Only the active project is expanded on load; every other
 * project fetches its page lazily the first time it is expanded, so opening the
 * console never fans out into one RPC per registered project.
 */
export const SideBar: React.FC = () => {
  const {
    isSidebarCollapsed,
    projects,
    activeProjectId,
    expandedProjectIds,
    projectSessions,
    loadingProjectIds,
    toggleProjectExpanded,
    switchProject,
    sessions,
    sessionsTotal,
    sessionsNextOffset,
    sessionsLoading,
    sessionQuery,
    setSessionQuery,
    sessionSearch,
    loadMoreSessionSearch,
    renameSession,
    deleteSession,
    sessionActionError,
    sessionNotice,
    dismissSessionAlert,
    searchFocusToken,
    requestSessionSearchFocus,
    loadMoreSessions,
    createNewSession,
    createSessionInProject,
    currentSession,
  } = useConsoleStore();

  const searchRef = useRef<HTMLInputElement>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [showAllProjects, setShowAllProjects] = useState<string[]>([]);
  // The session currently being renamed inline, and the draft title.
  const [renaming, setRenaming] = useState<{ threadId: string; draft: string } | null>(null);
  // The session awaiting an explicit delete confirmation.
  const [deletingThreadId, setDeletingThreadId] = useState<string | null>(null);

  // Ctrl+K bumps a token instead of reaching into the DOM from the App shell,
  // so a collapsed sidebar is expanded by the same store update.
  useEffect(() => {
    if (searchFocusToken > 0) searchRef.current?.focus();
  }, [searchFocusToken]);

  const query = sessionQuery.trim();
  // A non-empty query is answered by the server-side metadata search; the local
  // project filter only runs while the box is empty.
  const searching = query !== '';
  const searchItems = sessionSearch.items;

  const commitRename = async (threadId: string, draft: string) => {
    const accepted = await renameSession(threadId, draft);
    if (accepted) setRenaming(null);
  };

  const confirmDelete = async (threadId: string) => {
    setDeletingThreadId(null);
    await deleteSession(threadId);
  };

  const visibleProjects = useMemo(() => {
    if (query === '') return projects;
    // With a query the active project is answered by the server-side metadata
    // search (``searchItems``), not by the locally loaded page; the other
    // projects keep matching their cached page so the tree stays navigable.
    return projects.filter(
      (project) =>
        matchesProject(project, query) ||
        (project.project_id === activeProjectId
          ? searchItems.length > 0
          : filterSessions(projectSessions[project.project_id] ?? [], query).length > 0),
    );
  }, [projects, query, activeProjectId, searchItems, projectSessions]);

  if (isSidebarCollapsed) {
    // Collapsed to a minimal rail: the workspace stays reachable instead of
    // disappearing (new session, search, and the loaded count).  Expanding is
    // the top bar's toggle (or Ctrl+B) — a second toggle here was a duplicate.
    return (
      <nav className="bg-[#f8f9fa] border-r border-[#e5e7eb] h-full w-[44px] flex flex-col items-center py-3 gap-1.5 shrink-0 select-none">
        <button
          onClick={() => createNewSession()}
          title="在当前项目新建会话 (Ctrl+N)"
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
          title={`${projects.length} 个项目 / 已加载 ${sessions.length} 个会话`}
          className="mt-auto mb-1 font-mono text-[10px] text-gray-400"
        >
          {projects.length}
        </div>
      </nav>
    );
  }

  return (
    <nav className="bg-[#f8f9fa] border-r border-[#e5e7eb] h-full w-[240px] flex flex-col py-3 shrink-0 select-none text-xs font-sans">
      <div className="px-3">
        <div className="mb-2 px-1">
          <span className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider font-mono">
            PROJECTS
          </span>
        </div>
        <div className="relative">
          <span className="material-symbols-outlined absolute left-1.5 top-1/2 -translate-y-1/2 text-[14px] text-gray-400">
            search
          </span>
          <input
            ref={searchRef}
            id="session-search"
            name="session-search"
            type="text"
            value={sessionQuery}
            onChange={(event) => setSessionQuery(event.target.value)}
            placeholder="搜索项目 / 会话"
            title="搜索项目与会话 (Ctrl+K)"
            spellCheck={false}
            className="w-full rounded border border-gray-200 bg-white pl-6 pr-6 py-1 text-xs text-gray-800 placeholder:text-gray-400 focus:outline-none focus:border-blue-500"
          />
          {query !== '' && (
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
        {visibleProjects.length === 0 && (
          <div className="px-2 py-3 text-[11px] text-gray-400 font-mono">
            {query !== '' ? '没有匹配的项目或会话' : '暂无项目'}
          </div>
        )}

        {visibleProjects.map((project) => {
          const isActive = project.project_id === activeProjectId;
          // A search hit must be visible even if the user collapsed the project
          // it belongs to, otherwise the sidebar would report a match with
          // nothing to click.
          const expanded =
            expandedProjectIds.includes(project.project_id) ||
            (isActive && searching && searchItems.length > 0);
          const raw =
            project.project_id === activeProjectId
              ? sessions
              : (projectSessions[project.project_id] ?? []);
          // The active project's query hits come from the server-side metadata
          // search; other projects keep filtering their cached page.
          const matching = isActive
            ? searching
              ? searchItems
              : filterSessions(raw, '')
            : filterSessions(raw, query);
          // Cap the expanded subtree so one long project cannot push every other
          // project off the sidebar (the TUI drawer caps at five as well).
          const shown = showAllProjects.includes(project.project_id)
            ? matching
            : matching.slice(0, VISIBLE_SESSIONS);
          const groups = groupSessionsByTime(shown);
          const loading = isActive && searching ? sessionSearch.loading : loadingProjectIds.includes(project.project_id);
          // `runtime.project.list` reports no per-project session count, so a
          // non-active row shows how many of its sessions are actually loaded
          // (the same page the subtree renders), never a fabricated total.
          const sessionCount = isActive
            ? searching
              ? sessionSearch.total
              : sessionsTotal || raw.length
            : raw.length;

          return (
            <div key={project.project_id} className="mt-1">
              <div
                className={`group flex w-full items-center gap-0.5 rounded px-1 py-1 transition-colors hover:bg-gray-200/60 ${
                  isActive ? 'text-gray-900' : 'text-gray-600'
                }`}
              >
                <button
                  type="button"
                  onClick={() => {
                    void toggleProjectExpanded(project.project_id);
                  }}
                  title={project.workspace_path}
                  className="flex min-w-0 flex-1 items-center gap-1 text-left"
                >
                  <span className="material-symbols-outlined text-[15px] text-gray-400">
                    {expanded ? 'expand_more' : 'chevron_right'}
                  </span>
                  <span className="material-symbols-outlined text-[14px] text-gray-500">folder</span>
                  <span className={`truncate ${isActive ? 'font-medium' : ''}`}>
                    {projectLabel(project)}
                  </span>
                  {isActive && (
                    <span
                      className="material-symbols-outlined text-[12px] text-blue-600"
                      title="当前项目"
                    >
                      check_circle
                    </span>
                  )}
                </button>
                {/* Row actions stay in the layout (so the label never jumps) but
                    only surface on hover/focus — they are not status readouts. */}
                <span className="shrink-0 font-mono text-[10px] text-gray-400 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
                  {sessionCount > 0 ? sessionCount : ''}
                </span>
                {/* Per-project action: a session is always created in *this*
                    project, so the row never depends on which one is active. */}
                <button
                  type="button"
                  onClick={() => {
                    void createSessionInProject(project.project_id);
                  }}
                  title={`在 ${projectLabel(project)} 新建会话`}
                  className="material-symbols-outlined shrink-0 cursor-pointer rounded text-[15px] text-gray-400 opacity-0 transition-opacity hover:bg-gray-300/60 hover:text-gray-900 group-hover:opacity-100 group-focus-within:opacity-100"
                >
                  add
                </button>
              </div>

              {expanded && (
                <div className="ml-3 border-l border-gray-200 pl-2">
                  {loading && raw.length === 0 && (
                    <div className="px-1 py-1 font-mono text-[10px] text-gray-400">加载中…</div>
                  )}
                  {!loading && groups.length === 0 && (
                    <div className="px-1 py-1 font-mono text-[10px] text-gray-400">
                      {query !== '' ? '无匹配会话' : '暂无会话'}
                    </div>
                  )}
                  {groups.map((group) => (
                    <div key={group.key} className="mt-1">
                      <div className="px-1 mb-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider text-gray-400">
                        {group.label} · {group.items.length}
                      </div>
                      <ul className="space-y-1">
                        {group.items.map((sess) => {
                          const selected =
                            isActive && sess.thread_id === currentSession.thread_id;
                          const isRenaming = renaming?.threadId === sess.thread_id;
                          if (isRenaming) {
                            return (
                              <li key={sess.thread_id} className="px-1 py-0.5">
                                <input
                                  autoFocus
                                  id="session-rename"
                                  name="session-rename"
                                  type="text"
                                  value={renaming.draft}
                                  maxLength={SESSION_TITLE_MAX}
                                  onChange={(event) =>
                                    setRenaming({ threadId: sess.thread_id, draft: event.target.value })
                                  }
                                  onKeyDown={(event) => {
                                    if (event.key === 'Enter') {
                                      void commitRename(sess.thread_id, renaming.draft);
                                    }
                                    if (event.key === 'Escape') setRenaming(null);
                                  }}
                                  onBlur={() => setRenaming(null)}
                                  title={`重命名会话（1-${SESSION_TITLE_MAX} 个字符）`}
                                  className="w-full rounded border border-blue-400 px-1 py-0.5 text-xs text-gray-800 focus:outline-none"
                                />
                              </li>
                            );
                          }
                          return (
                            <li
                              key={sess.thread_id}
                              className={`group flex items-center gap-0.5 rounded transition-colors hover:bg-gray-200/60 ${
                                selected
                                  ? 'bg-gray-200/80 font-medium text-gray-900 shadow-2xs'
                                  : 'text-gray-600'
                              }`}
                            >
                              <button
                                type="button"
                                onClick={() => {
                                  void switchProject(project.project_id, sess.thread_id);
                                }}
                                title={`${sess.title}\n${sess.thread_id}`}
                                className="min-w-0 flex-1 cursor-pointer truncate px-1.5 py-1 text-left"
                              >
                                {sess.title}
                              </button>
                              {/* Write actions stay in the layout but only surface on
                                  hover/focus: they are not status readouts. */}
                              <button
                                type="button"
                                onClick={() =>
                                  setRenaming({ threadId: sess.thread_id, draft: sess.title })
                                }
                                title="重命名会话"
                                className="material-symbols-outlined shrink-0 cursor-pointer rounded text-[13px] text-gray-400 opacity-0 transition-opacity hover:bg-gray-300/60 hover:text-gray-900 group-hover:opacity-100 group-focus-within:opacity-100"
                              >
                                edit
                              </button>
                              <button
                                type="button"
                                onClick={() => setDeletingThreadId(sess.thread_id)}
                                title="删除会话记录"
                                className="material-symbols-outlined shrink-0 cursor-pointer rounded text-[13px] text-gray-400 opacity-0 transition-opacity hover:bg-gray-300/60 hover:text-red-600 group-hover:opacity-100 group-focus-within:opacity-100"
                              >
                                delete
                              </button>
                            </li>
                          );
                        })}
                      </ul>
                    </div>
                  ))}
                  {matching.length > VISIBLE_SESSIONS && (
                    <button
                      type="button"
                      onClick={() =>
                        setShowAllProjects((ids) =>
                          ids.includes(project.project_id)
                            ? ids.filter((id) => id !== project.project_id)
                            : [...ids, project.project_id],
                        )
                      }
                      className="mt-1 w-full rounded px-1 py-0.5 text-left font-mono text-[10px] text-gray-400 transition-colors hover:bg-gray-200/60 hover:text-gray-700"
                    >
                      {showAllProjects.includes(project.project_id)
                        ? '收起'
                        : `显示全部 ${matching.length} 条`}
                    </button>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="px-3 pt-2">
        {searching
          ? sessionSearch.nextOffset !== null && (
              <button
                type="button"
                onClick={() => {
                  void loadMoreSessionSearch();
                }}
                disabled={sessionSearch.loading}
                className="w-full rounded border border-gray-200 bg-white px-2 py-1 text-[11px] font-mono text-gray-600 hover:bg-gray-100 transition-colors disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
              >
                {sessionSearch.loading ? '加载中…' : '加载更多搜索结果'}
              </button>
            )
          : sessionsNextOffset !== null && (
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
        <div className="mt-1 font-mono text-[10px] text-gray-400">
          {searching
            ? sessionSearch.error !== null
              ? sessionSearch.error
              : `服务端元数据搜索命中 ${sessionSearch.total} 条（不含对话全文）`
            : `当前项目已加载 ${sessions.length} / ${sessionsTotal}`}
        </div>
      </div>

      {(sessionActionError !== null || sessionNotice !== null) && (
        <div className="mx-3 mt-2 rounded border border-amber-200 bg-amber-50 px-2 py-1.5 text-[10px] text-amber-900">
          <div className="flex items-start gap-1">
            <span className="flex-1">{sessionActionError ?? sessionNotice}</span>
            <button
              type="button"
              onClick={dismissSessionAlert}
              title="关闭提示"
              className="material-symbols-outlined cursor-pointer text-[13px] text-amber-700"
            >
              close
            </button>
          </div>
        </div>
      )}

      {deletingThreadId !== null && (
        // Deleting removes the session *record* only: the confirmation says so
        // explicitly, because the conversation itself is retained on disk.
        <div className="mx-3 mt-2 rounded border border-red-200 bg-red-50 px-2 py-1.5 text-[10px] text-red-900">
          <div className="mb-1">删除该会话的记录？</div>
          <div className="mb-1 text-red-800">
            仅删除会话记录（元数据与目标）；对话历史（检查点与转录）仍保留在磁盘上，不会被删除。
            运行中的会话需先停止当前回合。
          </div>
          <div className="flex gap-1">
            <button
              type="button"
              onClick={() => {
                void confirmDelete(deletingThreadId);
              }}
              className="rounded bg-red-600 px-1.5 py-0.5 text-white hover:bg-red-700 cursor-pointer"
            >
              删除记录
            </button>
            <button
              type="button"
              onClick={() => setDeletingThreadId(null)}
              className="rounded border border-red-300 px-1.5 py-0.5 text-red-800 hover:bg-red-100 cursor-pointer"
            >
              取消
            </button>
          </div>
        </div>
      )}

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
