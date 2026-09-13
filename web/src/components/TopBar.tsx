import React, { useState } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';
import { GitExplorer } from './GitExplorer.tsx';

/**
 * The header keeps identity only — sidebar toggle, workspace, branch and the
 * centred session label.  The context actions (session info, workspace files,
 * runtime diagnostics, logout) moved to the sidebar's settings row; the
 * telemetry lives in the status bar.
 */

export const TopBar: React.FC = () => {
  const {
    toggleSidebar,
    workspacePath,
    gitBranch,
    gitDirty,
    gitStatus,
    sessionTitle,
  } = useConsoleStore();
  const [explorerOpen, setExplorerOpen] = useState(false);

  return (
    <>
    <header className="bg-white border-b border-[#e5e7eb] grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-3 h-10 px-4 w-full shrink-0 z-20 select-none text-xs font-mono">
      <div className="flex min-w-0 items-center space-x-3">
        <button
          onClick={toggleSidebar}
          // Same 28px box as the collapsed rail's buttons, pulled left by the
          // header's padding so both icon centres land on the same vertical axis
          // (the rail centres its buttons in a 44px column, i.e. at x = 22px).
          className="-ml-2 flex h-7 w-7 items-center justify-center rounded text-gray-700 transition-colors hover:bg-gray-100"
          title="Toggle Sidebar"
        >
          <span className="material-symbols-outlined text-[18px]">dock_to_left</span>
        </button>

        <div className="flex min-w-0 items-center space-x-1.5 text-gray-800">
          <span className="material-symbols-outlined text-[16px] text-gray-600">folder</span>
          <span className="truncate">{workspacePath}</span>
        </div>

        {/* Branch context only exists for a git-managed workspace: a non-git
            project renders no chip (and no orphan separator) at all. */}
        {gitBranch !== '' && (
          <>
            <span className="text-gray-300 mx-1">|</span>
            {/* Clicking the chip opens the read-only git explorer.  The dot is the
                same dirty marker the TUI's chrome shows, and the tracking counts
                come from `runtime.git.status`. */}
            <button
              type="button"
              onClick={() => setExplorerOpen(true)}
              title="打开 Git Explorer（只读：变更文件与逐文件 diff）"
              className="flex min-w-0 cursor-pointer items-center space-x-1.5 rounded px-1 py-0.5 text-gray-800 transition-colors hover:bg-gray-100"
            >
              <span
                className={`inline-block h-2 w-2 rounded-full ${
                  gitDirty ? 'bg-amber-500' : 'bg-emerald-500'
                }`}
              ></span>
              <span className="material-symbols-outlined text-[15px] text-gray-600">
                fork_right
              </span>
              <span className="truncate">{gitBranch}</span>
              {gitStatus !== null && gitStatus.ahead > 0 && (
                <span className="text-emerald-600">↑{gitStatus.ahead}</span>
              )}
              {gitStatus !== null && gitStatus.behind > 0 && (
                <span className="text-amber-600">↓{gitStatus.behind}</span>
              )}
              {gitStatus !== null && gitStatus.dirty && (
                <span className="text-amber-600">
                  {gitStatus.files.length}f{gitStatus.truncated ? '+' : ''}
                </span>
              )}
            </button>
          </>
        )}
      </div>

      {/* Session label: centred in the header by the symmetric grid, so it never
          hangs off the branch chip. */}
      <div
        className="justify-self-center truncate font-medium text-blue-600"
        title={sessionTitle}
      >
        {sessionTitle}
      </div>

      {/* Right track: the context actions moved to the sidebar's settings row, so
          the header keeps identity only (and its empty third track still keeps the
          session label centred). */}
      <div className="justify-self-end" />
    </header>
    {explorerOpen && <GitExplorer onClose={() => setExplorerOpen(false)} />}
    </>
  );
};
