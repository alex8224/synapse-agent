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
 * the change statistics.  The branch chip and the statistics chip both open the
 * read-only git explorer, so the explorer stays reachable even before
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
    })),
  );
  const [explorerOpen, setExplorerOpen] = useState(false);

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
    <header className="grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-2 h-10 w-full shrink-0 bg-white border-b border-[#e5e7eb] px-3 z-20 select-none text-xs font-mono">
      {/* Left track: identity controls and context.  `min-w-0` lets the chips
          truncate instead of widening the track and nudging the centre line. */}
      <div className="flex min-w-0 items-center gap-1.5">
        <button
          onClick={toggleSidebar}
          // The same 28px box the collapsed rail's buttons use, so the control
          // does not change size when the sidebar folds.
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded text-gray-700 transition-colors hover:bg-gray-100 cursor-pointer"
          title="Toggle Sidebar (Ctrl+B)"
        >
          <span className="material-symbols-outlined text-[18px]">dock_to_left</span>
        </button>

        {/* Project context: the directory label of the project the session belongs
            to (same label the sidebar tree shows), with the full path on hover.
            Secondary context: below the `lg` breakpoint it is hidden together with
            its separator so the session tab and the branch keep their room. */}
        {projectName !== '' && (
          <>
            <span className="hidden text-gray-300 mx-1 lg:inline">|</span>
            <div
              className="hidden min-w-0 items-center gap-1.5 text-gray-800 lg:flex"
              title={activeProject?.workspace_path}
            >
              <span className="material-symbols-outlined shrink-0 text-[16px] text-gray-600">
                folder
              </span>
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
            <span className="text-gray-300 mx-1">|</span>
            {/* Clicking the branch chip opens the same read-only git explorer as
                the statistics chip; the tracking counts come from
                `runtime.git.status`. */}
            <button
              type="button"
              onClick={() => setExplorerOpen(true)}
              title="打开 Git Explorer（只读：变更文件与逐文件 diff）"
              className="flex min-w-0 cursor-pointer items-center gap-1.5 rounded px-1 py-0.5 text-gray-800 transition-colors hover:bg-gray-100"
            >
              <span className="material-symbols-outlined text-[15px] text-gray-600">
                fork_right
              </span>
              <span className="max-w-[14rem] truncate">{gitBranch}</span>
              {gitStatus !== null && gitStatus.ahead > 0 && (
                <span className="text-emerald-600">↑{gitStatus.ahead}</span>
              )}
              {gitStatus !== null && gitStatus.behind > 0 && (
                <span className="text-amber-600">↓{gitStatus.behind}</span>
              )}
            </button>
          </>
        )}
      </div>

      {/* Centre track: the open session, centred on the workspace column because
          both side tracks are equal `1fr`.  The chip is capped and truncates, so
          a long title cannot widen its track past the cap. */}
      <div
        className="flex min-w-0 max-w-[20rem] items-center gap-1.5 rounded bg-[#f3f4f5] px-2 py-1 text-gray-900"
        title={sessionTitle}
      >
        <span className="material-symbols-outlined shrink-0 text-[15px] text-gray-500">forum</span>
        <span className="truncate font-medium">{sessionTitle}</span>
      </div>

      {/* Right track: the change statistics, pinned to the right edge of its
          track.  The dot is the same dirty marker the TUI's chrome shows, and
          this chip opens the read-only git explorer (changed files and per-file
          diff) alongside the branch chip.  `+N -M` are the real tracked
          added/removed lines from `runtime.git.status`; while they are unknown
          the chip shows no number at all rather than a misleading 0. */}
      <div className="flex min-w-0 items-center justify-self-end">
        {gitBranch !== '' && (
          <button
            type="button"
            onClick={() => setExplorerOpen(true)}
            title="打开 Git Explorer（只读：变更文件与逐文件 diff）"
            className="flex shrink-0 cursor-pointer items-center gap-1.5 rounded px-1 py-0.5 text-gray-800 transition-colors hover:bg-gray-100"
          >
            <span
              className={`inline-block h-2 w-2 rounded-full ${
                gitDirty ? 'bg-amber-500' : 'bg-emerald-500'
              }`}
            ></span>
            <span className="material-symbols-outlined text-[15px] text-gray-500">
              difference
            </span>
            {hasLineCounts && (
              <>
                <span className="text-emerald-600">+{insertions}</span>
                <span className="text-red-600">-{deletions}</span>
              </>
            )}
          </button>
        )}
      </div>
    </header>
    {explorerOpen && <GitExplorer onClose={() => setExplorerOpen(false)} />}
    </>
  );
};
