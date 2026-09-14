import { PanelLeft20Regular, Folder20Regular, Branch20Regular, Chat20Regular, ChevronRight16Regular } from '@fluentui/react-icons';
import React, { useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../stores/useConsoleStore';
import { projectLabel } from '../stores/sessionList.ts';
import { GitExplorer } from './GitExplorer.tsx';

/**
 * Header of the workspace column: the session title on the centre line.
 *
 * The sidebar owns the left edge of the window and runs the full height of it,
 * so this bar starts to the right of the sidebar.  It is a three-track grid with
 * two equal `1fr` sides and the session title in the middle, so the title sits on
 * the centre line of the workspace column and cannot drift with the width of the
 * chips on either side; the tracks are separate cells, so a narrow window never
 * overlaps the title with them (the secondary project chip drops first).  The
 * left track carries identity (sidebar toggle, project, branch), the right track
 * the change statistics.  The branch chip and the statistics chip both re-read
 * `runtime.git.status` and then open the read-only git explorer, so the explorer
 * stays reachable even before
 * `runtime.git.status` has been read.  The workspace path moved to the sidebar's
 * identity row, the context actions (session info, workspace files, runtime
 * diagnostics, logout) live with the settings entry there, and the telemetry
 * lives in the status strip under the composer.
 */

export const TopBar: React.FC = () => {
  const {
    toggleSidebar,
    projects,
    activeProjectId,
    gitBranch,
    gitDirty,
    gitStatus,
    sessionTitle,
    loadGitStatus,
  } = useConsoleStore(
    // Only the fields this bar paints: a reasoning delta must not re-render it.
    useShallow((state) => ({
      toggleSidebar: state.toggleSidebar,
      projects: state.projects,
      activeProjectId: state.activeProjectId,
      gitBranch: state.gitBranch,
      gitDirty: state.gitDirty,
      gitStatus: state.gitStatus,
      sessionTitle: state.sessionTitle,
      loadGitStatus: state.loadGitStatus,
    })),
  );
  const [explorerOpen, setExplorerOpen] = useState(false);

  // The chrome is a snapshot taken when the session was attached, so a click
  // re-reads it before opening the explorer (a commit made outside the console
  // is otherwise invisible until the next attach).
  const refreshAndOpenExplorer = () => {
    void loadGitStatus();
    setExplorerOpen(true);
  };

  // The project label comes from the project list the sidebar already holds, and
  // is simply omitted until that list has loaded: no placeholder name.
  const activeProject = projects.find((entry) => entry.project_id === activeProjectId);
  const projectName = activeProject === undefined ? '' : projectLabel(activeProject);
  // `runtime.git.status` is the only source of the line counts.  They are null
  // while the status is unread *and* when git could not answer, so the chip shows
  // no number rather than a fabricated `+0 -0`; the dot and the explorer entry
  // stay reachable either way.
  const insertions = gitStatus === null ? null : gitStatus.insertions;
  const deletions = gitStatus === null ? null : gitStatus.deletions;
  const hasLineCounts = insertions !== null && deletions !== null;

  return (
    <>
    <header className="material-chrome relative z-20 grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-2 h-chrome w-full shrink-0 border-b border-line px-3 select-none text-sm font-sans">
      {/* Left track: identity controls and context.  `min-w-0` lets the chips
          truncate instead of widening the track and nudging the centre line. */}
      <div className="flex min-w-0 items-center gap-1.5">
        <button
          onClick={toggleSidebar}
          // The same themed box the collapsed rail's buttons use, so the control
          // does not change size when the sidebar folds.
          className="ui-icon-button"
          title="Toggle Sidebar (Ctrl+B)"
          aria-label="切换侧栏"
        >
          <PanelLeft20Regular aria-hidden="true" />
        </button>

        {/* Project context: the directory label of the project the session belongs
            to (same label the sidebar tree shows), with the full path on hover.
            Secondary context: below the `lg` breakpoint it is hidden together with
            its separator so the session tab and the branch keep their room. */}
        {projectName !== '' && (
          <>
            <span className="hidden h-3.5 w-px bg-line/80 mx-1 lg:inline-block" aria-hidden="true" />
            <div
              className="hidden min-w-0 items-center gap-1.5 rounded-control px-2 py-1 text-xs font-normal text-gray-700 hover:bg-surface-hover/80 transition-colors lg:flex cursor-default"
              title={activeProject?.workspace_path}
            >
              <Folder20Regular aria-hidden="true" className="shrink-0 text-gray-600" />
              <span className="max-w-[14rem] truncate">{projectName}</span>
            </div>
          </>
        )}

        {/* Branch context only exists for a git-managed workspace: a non-git
            project renders no chip (and no orphan separator) at all.  The chip is
            a button too, so the explorer is reachable even before
            `runtime.git.status` has been read. */}
        {gitBranch !== '' && (
          <>
            {projectName !== '' ? (
              <ChevronRight16Regular aria-hidden="true" className="hidden shrink-0 text-gray-400 lg:inline" />
            ) : (
              <span className="h-3.5 w-px bg-line/80 mx-1 inline-block" aria-hidden="true" />
            )}
            {/* Clicking the branch chip opens the same read-only git explorer as
                the statistics chip; the tracking counts come from
                `runtime.git.status`. */}
            <button
              type="button"
              onClick={refreshAndOpenExplorer}
              title="刷新并打开 Git Explorer（只读：变更文件与逐文件 diff）"
              aria-label="查看 Git 变更"
              className="ui-button min-w-0 h-7 text-xs px-2 rounded-control text-gray-700 hover:bg-surface-hover active:bg-surface-pressed transition-colors"
            >
              <Branch20Regular aria-hidden="true" className="shrink-0 text-gray-500" />
              <span className="max-w-[14rem] truncate font-medium">{gitBranch}</span>
              {gitStatus !== null && gitStatus.ahead > 0 && (
                <span className="text-emerald-600 font-numeric text-[11px]">↑{gitStatus.ahead}</span>
              )}
              {gitStatus !== null && gitStatus.behind > 0 && (
                <span className="text-amber-600 font-numeric text-[11px]">↓{gitStatus.behind}</span>
              )}
              <span className="inline-block h-3 w-px bg-line mx-0.5" aria-hidden="true" />
              <span
                className={`inline-block h-2 w-2 rounded-full ${
                  gitDirty ? 'bg-amber-500' : 'bg-emerald-500'
                }`}
              ></span>
              {hasLineCounts && (
                <span className="font-numeric text-[11px] tracking-tight shrink-0">
                  <span className="text-emerald-600">+{insertions}</span>{' '}
                  <span className="text-red-600">-{deletions}</span>
                </span>
              )}
            </button>
          </>
        )}
      </div>

      {/* Centre track: the open session, centred on the workspace column because
          both side tracks are equal `1fr`.  The chip is capped and truncates, so
          a long title cannot widen its track past the cap -- wide enough to read
          a session name instead of a fragment of it -- and `min-w-0` lets a
          narrow window shrink the chip rather than push the side tracks. */}
      <div
        className="ui-session-title flex min-w-0 max-w-[32rem] items-center gap-2 rounded-control px-2.5 py-1 text-gray-800 hover:bg-surface-hover/50 transition-colors"
        title={sessionTitle}
      >
        <Chat20Regular aria-hidden="true" className="shrink-0 text-accent" />
        <span className="truncate font-medium">{sessionTitle}</span>
      </div>

      {/* Right track: deliberately empty.  The change statistics used to sit here
          on the right edge; they now hang off the branch chip, so the numbers are
          read next to the branch they describe.  The track stays in the grid
          because the two equal `1fr` sides are what keep the title centred. */}
      <div />
    </header>
    {explorerOpen && <GitExplorer onClose={() => setExplorerOpen(false)} />}
    </>
  );
};
