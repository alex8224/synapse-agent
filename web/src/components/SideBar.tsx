import {
  Add20Regular, Search20Regular, Settings20Regular, Dismiss20Regular,
  ChevronDown20Regular, ChevronRight20Regular, Folder20Regular,
  CheckmarkCircle20Regular, Edit20Regular, Delete20Regular, SpinnerIos20Regular,
} from '@fluentui/react-icons';
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { ConsoleActions } from './ConsoleActions.tsx';
import { useConsoleStore } from '../stores/useConsoleStore';
import {
  SESSION_TITLE_MAX,
  filterSessions,
  groupSessionsByTime,
  matchesProject,
  projectLabel,
} from '../stores/sessionList.ts';
import {
  sessionKey,
  sessionStatusMarkers,
  type SessionStatusKind,
} from '../stores/sessionViews.ts';
import { SettingsDialog } from './SettingsDialog.tsx';
import { SessionDeleteDialog } from './SessionDeleteDialog.tsx';

/** Sessions shown per expanded project before the "show all" row (TUI parity). */
const VISIBLE_SESSIONS = 5;

/**
 * The live status of every session the console holds a view for, keyed by
 * `sessionKey`.
 *
 * The active session's own state counts too, so its row carries the marker while
 * its transcript is on screen.  Returned as a small string map so `useShallow`
 * can compare it entry by entry: a background delta rewrites its view on every
 * frame, and the sidebar must not re-render for a status that did not change.
 * A stale view (`subscriptionId === null`) contributes nothing -- its status is
 * unknown, so it must not leave a pulsing dot or an approval marker alive.
 */
function sessionStatusesOf(state: {
  backgroundViews: Record<
    string,
    { subscriptionId: string | null; runtimeStatus: 'idle' | 'running'; pendingApproval: unknown }
  >;
  currentSession: { project_id: string; thread_id: string };
  runtimeStatus: 'idle' | 'running';
  pendingApproval: unknown;
}): Record<string, SessionStatusKind> {
  return sessionStatusMarkers(state.backgroundViews, {
    key: state.currentSession.thread_id !== '' ? sessionKey(state.currentSession) : null,
    runtimeStatus: state.runtimeStatus,
    pendingApproval: state.pendingApproval,
  });
}

/**
 * Sidebar: a two-level project -> session tree, mirroring the TUI drawer.
 *
 * Level 1 is one row per switchable project (directory label, session count,
 * current-project marker).  Level 2 lists that project's sessions grouped by
 * relative time.  Only the active project is expanded on load; every other
 * project fetches its page lazily the first time it is expanded, so opening the
 * console never fans out into one RPC per registered project.
 */
export const SideBar: React.FC<{ collapsed?: boolean; onExpand?: () => void }> = ({
  collapsed, onExpand,
}) => {
  const {
    isSidebarCollapsed: storedCollapsed,
    workspacePath,
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
  } = useConsoleStore(
    // Only the fields this column paints: a reasoning delta must not re-render
    // the session list.
    useShallow((state) => ({
      isSidebarCollapsed: state.isSidebarCollapsed,
      workspacePath: state.workspacePath,
      projects: state.projects,
      activeProjectId: state.activeProjectId,
      expandedProjectIds: state.expandedProjectIds,
      projectSessions: state.projectSessions,
      loadingProjectIds: state.loadingProjectIds,
      toggleProjectExpanded: state.toggleProjectExpanded,
      switchProject: state.switchProject,
      sessions: state.sessions,
      sessionsTotal: state.sessionsTotal,
      sessionsNextOffset: state.sessionsNextOffset,
      sessionsLoading: state.sessionsLoading,
      sessionQuery: state.sessionQuery,
      setSessionQuery: state.setSessionQuery,
      sessionSearch: state.sessionSearch,
      loadMoreSessionSearch: state.loadMoreSessionSearch,
      renameSession: state.renameSession,
      deleteSession: state.deleteSession,
      sessionActionError: state.sessionActionError,
      sessionNotice: state.sessionNotice,
      dismissSessionAlert: state.dismissSessionAlert,
      searchFocusToken: state.searchFocusToken,
      requestSessionSearchFocus: state.requestSessionSearchFocus,
      loadMoreSessions: state.loadMoreSessions,
      createNewSession: state.createNewSession,
      createSessionInProject: state.createSessionInProject,
      currentSession: state.currentSession,
    })),
  );

  // Live per-session status, kept in its own shallow selector so a background
  // delta (which rewrites its view every frame) only re-renders the rows whose
  // marker actually changed.
  const sessionStatuses = useConsoleStore(
    useShallow((state) => sessionStatusesOf(state)),
  );

  const isSidebarCollapsed = collapsed ?? storedCollapsed;
  const searchRef = useRef<HTMLInputElement>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [showAllProjects, setShowAllProjects] = useState<string[]>([]);
  // The session currently being renamed inline, and the draft title.
  const [renaming, setRenaming] = useState<{ threadId: string; draft: string } | null>(null);
  // The session awaiting an explicit delete confirmation, and whether that delete
  // is already on the wire.  The whole target is kept, not just its id: the dialog
  // is what names the session it deletes.
  const [deleting, setDeleting] = useState<{ threadId: string; title: string } | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

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

  const confirmDelete = async (target: { threadId: string; title: string }) => {
    setDeleteBusy(true);
    const accepted = await deleteSession(target.threadId);
    setDeleteBusy(false);
    // A refusal (the session is running) keeps the confirmation open with the
    // reason inline, so the user can retry or cancel; only an accepted delete
    // closes it.
    if (accepted) setDeleting(null);
  };

  // The previous action's notice or error must not sit behind this confirmation:
  // while the dialog is open it carries the alert of its own action.
  const openDelete = (target: { threadId: string; title: string }) => {
    dismissSessionAlert();
    setDeleting(target);
  };

  // Identity of the workspace this console is attached to.  The header used to
  // print the raw path; the header is now the session chip row, so the path lives
  // with the app-level actions at the foot of the sidebar.
  const identityLabel = workspacePath === '' ? '未绑定工作区' : workspacePath;

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

  return (
    <nav
      aria-label="项目与会话"
      className={`material-chrome relative h-full shrink-0 select-none border-r border-line overflow-hidden transition-[width] duration-300 ease-[cubic-bezier(0,0,0,1)] ${
        isSidebarCollapsed ? 'w-[44px]' : 'w-[240px]'
      }`}
    >
      {/* Minimal Rail View (shown when collapsed) */}
      <div
        inert={!isSidebarCollapsed}
        className={`absolute inset-0 flex flex-col items-center py-3 gap-1.5 transition-opacity duration-200 ${
          isSidebarCollapsed ? 'opacity-100 pointer-events-auto' : 'opacity-0 pointer-events-none'
        }`}
      >
        <button
          onClick={() => createNewSession()}
          title="在当前项目新建会话 (Ctrl+N)"
          aria-label="新建会话"
          className="ui-icon-button"
        >
          <Add20Regular aria-hidden="true" />
        </button>
        <button
          onClick={() => { onExpand?.(); requestSessionSearchFocus(); }}
          title="搜索会话 (Ctrl+K)"
          aria-label="搜索会话"
          className="ui-icon-button"
        >
          <Search20Regular aria-hidden="true" />
        </button>
        <div className="mt-auto flex flex-col items-center gap-1">
          <ConsoleActions orientation="column" />
          <button
            type="button"
            onClick={() => setSettingsOpen(true)}
            title="打开设置"
            aria-label="打开设置"
            className="ui-icon-button"
          >
            <Settings20Regular aria-hidden="true" />
          </button>
          <div
            title={`${projects.length} 个项目 / 已加载 ${sessions.length} 个会话`}
            className="mb-1 font-mono text-[10px] text-gray-400"
          >
            {projects.length}
          </div>
        </div>
      </div>

      {/* Expanded Sidebar View */}
      <div
        inert={isSidebarCollapsed}
        className={`w-[240px] h-full flex flex-col py-3 text-sm font-sans transition-opacity duration-200 ${
          isSidebarCollapsed ? 'opacity-0 pointer-events-none' : 'opacity-100 pointer-events-auto'
        }`}
      >
      <div className="px-3">
        {/* Nav entry the collapsed rail also carries, with the shortcut spelled
            out.  It creates in the *current* project, exactly like the rail's
            `+` and Ctrl+N; per-project creation stays on each project row. */}
        <button
          type="button"
          onClick={() => createNewSession()}
          title="在当前项目新建会话 (Ctrl+N)"
          className="ui-button ui-primary mb-4 w-full"
        >
          <Add20Regular aria-hidden="true" />
          <span className="min-w-0 flex-1 truncate text-left">新建任务</span>
          <kbd className="shrink-0 rounded-control px-1 font-kbd opacity-80">
            Ctrl+N
          </kbd>
        </button>
        <div className="mb-2 px-1">
          <span className="ui-section-label">
            项目与会话
          </span>
        </div>
        <div className="relative">
          <Search20Regular aria-hidden="true" className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-gray-500" />
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
            aria-label="搜索项目与会话"
            className="ui-field w-full pl-8 pr-12 placeholder:text-gray-500"
          />
          {query !== '' && (
            <button
              type="button"
              onClick={() => setSessionQuery('')}
              title="清除搜索"
              aria-label="清除搜索"
              className="ui-icon-button ui-compact absolute right-1 top-1/2 -translate-y-1/2"
            >
              <Dismiss20Regular aria-hidden="true" />
            </button>
          )}
          {/* Shortcut hint in the slot the clear button uses once a query exists. */}
          {query === '' && (
            <span className="pointer-events-none absolute right-1.5 top-1/2 -translate-y-1/2 select-none font-mono text-[10px] text-gray-400">
              Ctrl+K
            </span>
          )}
        </div>
      </div>

      {/* The tree scrolls with no visible scrollbar (`.no-scrollbar`).  It is
          focusable so the keyboard (arrows / PageUp / PageDown) scrolls it even
          before any row inside has focus. */}
      <div
        className="sidebar-scroll no-scrollbar flex-1 overflow-y-auto px-2 mt-3"
        aria-label="项目会话列表"
        tabIndex={0}
      >
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
                className={`ui-nav-row group flex w-full items-center gap-0.5 px-1 py-1 ${
                  isActive ? 'text-gray-900' : 'text-gray-600'
                }`}
              >
                <button
                  type="button"
                  onClick={() => {
                    void toggleProjectExpanded(project.project_id);
                  }}
                  title={project.workspace_path}
                  aria-expanded={expanded}
                  className="flex min-h-8 min-w-0 flex-1 items-center gap-1 text-left"
                >
                  {expanded
                    ? <ChevronDown20Regular aria-hidden="true" className="shrink-0 text-gray-500" />
                    : <ChevronRight20Regular aria-hidden="true" className="shrink-0 text-gray-500" />}
                  <Folder20Regular aria-hidden="true" className="shrink-0 text-gray-600" />
                  <span className={`truncate ${isActive ? 'font-medium' : ''}`}>
                    {projectLabel(project)}
                  </span>
                  {isActive && (
                    <CheckmarkCircle20Regular aria-label="当前项目" className="shrink-0 text-accent" />
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
                  aria-label={`在 ${projectLabel(project)} 新建会话`}
                  className="ui-icon-button ui-compact opacity-0 group-hover:opacity-100 group-focus-within:opacity-100"
                >
                  <Add20Regular aria-hidden="true" />
                </button>
              </div>

              {expanded && (
                <div className="ml-3 border-l border-line pl-2">
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
                      <div className="ui-section-label px-1 mb-1 mt-3">
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
                                  aria-label="重命名会话"
                                  className="ui-field w-full"
                                />
                              </li>
                            );
                          }
                          const status = sessionStatuses[
                            sessionKey({ project_id: project.project_id, thread_id: sess.thread_id })
                          ];
                          return (
                            <li
                              key={sess.thread_id}
                              data-selected={selected}
                              className={`ui-nav-row group flex items-center gap-0.5 ${
                                selected ? 'text-gray-900 font-medium' : 'text-gray-700'
                              }`}
                            >
                              <button
                                type="button"
                                onClick={() => {
                                  void switchProject(project.project_id, sess.thread_id);
                                }}
                                title={`${sess.title}\n${sess.thread_id}`}
                                aria-current={selected ? 'page' : undefined}
                                className="flex min-h-8 min-w-0 flex-1 cursor-pointer items-center gap-1.5 pl-3 pr-1 py-1.5 text-left"
                              >
                                {/* A real status readout, unlike the hover-only write
                                    actions below: it must stay visible without focus.
                                    Running is the same in-flight spinner the transcript
                                    paints for a tool call still executing
                                    (transcriptRows/ToolGroupRow), so one motion means one
                                    thing across the console; an approval is amber and wins
                                    when both apply. The slot is a fixed 12px because the
                                    two kinds differ in width (6px dot vs 12px spinner), so
                                    without it a title would start 6px further right for one
                                    kind than the other. */}
                                {status !== undefined && (
                                  <span
                                    role="img"
                                    aria-label={status === 'approval' ? '等待审批' : '运行中'}
                                    title={status === 'approval' ? '等待审批' : '运行中'}
                                    className="flex h-3 w-3 shrink-0 items-center justify-center"
                                  >
                                    {status === 'approval' ? (
                                      <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />
                                    ) : (
                                      <SpinnerIos20Regular
                                        aria-hidden="true"
                                        className="animate-spin text-blue-500"
                                        style={{ fontSize: '12px' }}
                                      />
                                    )}
                                  </span>
                                )}
                                <span className="min-w-0 flex-1 truncate">{sess.title}</span>
                              </button>
                              {/* Write actions stay in the layout but only surface on
                                  hover/focus: they are not status readouts. */}
                              <button
                                type="button"
                                onClick={() =>
                                  setRenaming({ threadId: sess.thread_id, draft: sess.title })
                                }
                                title="重命名会话"
                                aria-label="重命名会话"
                                className="ui-icon-button ui-compact opacity-0 group-hover:opacity-100 group-focus-within:opacity-100"
                              >
                                <Edit20Regular aria-hidden="true" />
                              </button>
                              <button
                                type="button"
                                onClick={() =>
                                  openDelete({ threadId: sess.thread_id, title: sess.title })
                                }
                                title="删除会话记录"
                                aria-label={`删除会话记录：${sess.title}`}
                                aria-haspopup="dialog"
                                className="ui-icon-button ui-compact opacity-0 hover:text-red-600 group-hover:opacity-100 group-focus-within:opacity-100"
                              >
                                <Delete20Regular aria-hidden="true" />
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
                      className="ui-button mt-1 w-full justify-start text-xs"
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
                className="ui-button w-full border border-line bg-surface"
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
                className="ui-button w-full border border-line bg-surface"
              >
                {sessionsLoading ? '加载中…' : '加载更多'}
              </button>
            )}
        <div className="mt-2 text-xs text-gray-500">
          {searching
            ? sessionSearch.error !== null
              ? sessionSearch.error
              : `服务端元数据搜索命中 ${sessionSearch.total} 条（不含对话全文）`
            : `当前项目已加载 ${sessions.length} / ${sessionsTotal}`}
        </div>
      </div>

      {/* Session-management feedback (create / rename / delete).  A failure is a
          danger banner and a notice is a plain result line: one amber box for both
          made a *finished* delete read like a pending question.  While the delete
          dialog is open it shows its own failure inline instead. */}
      {deleting === null && (sessionActionError !== null || sessionNotice !== null) && (
        <div
          role={sessionActionError !== null ? 'alert' : 'status'}
          className={`mx-3 mt-2 rounded-control border backdrop-blur-sm px-2 py-1.5 text-[10px] shadow-xs ${
            sessionActionError !== null
              ? 'border-red-200/80 bg-red-50/80 text-red-900'
              : 'border-line bg-surface/80 text-gray-700'
          }`}
        >
          <div className="flex items-start gap-1">
            <span className="flex-1">{sessionActionError ?? sessionNotice}</span>
            <button
              type="button"
              onClick={dismissSessionAlert}
              title="关闭提示"
              aria-label="关闭提示"
              className="ui-icon-button ui-compact"
            >
              <Dismiss20Regular aria-hidden="true" />
            </button>
          </div>
        </div>
      )}

      {/* Foot of the sidebar: the workspace identity, then the app-level row where
          the context actions sit with the settings entry (the version is shown
          inside the settings panel instead of this label). */}
      <div className="mt-2 border-t border-line px-3 pt-2">
        <div
          className="flex items-center gap-1.5 font-mono text-[10px] text-gray-500"
          title={identityLabel}
        >
          <Folder20Regular aria-hidden="true" className="shrink-0 text-gray-500" />
          <span className="truncate">{identityLabel}</span>
        </div>
        <div className="mt-1.5 flex items-center gap-1">
          <ConsoleActions />
          <button
            type="button"
            onClick={() => setSettingsOpen(true)}
            title="打开设置"
            aria-label="打开设置"
            className="ui-icon-button"
          >
            <Settings20Regular aria-hidden="true" />
          </button>
        </div>
      </div>

      </div>
      {settingsOpen && <SettingsDialog onClose={() => setSettingsOpen(false)} />}
      {/* Outside both sidebar states: a dialog opened from the tree must survive
          the rail collapsing under it (that view is `inert`). */}
      {deleting !== null && (
        <SessionDeleteDialog
          title={deleting.title}
          threadId={deleting.threadId}
          busy={deleteBusy}
          error={sessionActionError}
          onConfirm={() => {
            void confirmDelete(deleting);
          }}
          onClose={() => setDeleting(null)}
        />
      )}
    </nav>
  );
};
