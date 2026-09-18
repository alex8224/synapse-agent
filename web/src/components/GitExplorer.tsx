import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Branch20Regular, ArrowSync20Regular } from '@fluentui/react-icons';
import { PanelLeftContract16Regular, PanelLeftExpand16Regular } from '@fluentui/react-icons';
import { FloatingWindow } from './FloatingWindow.tsx';
import { GitDiffView } from './GitDiffView.tsx';
import { useDialogKeyboardNav } from './keyboardNav.ts';
import { OpenWithMenu } from './OpenWithMenu.tsx';
import { useConsoleStore } from '../stores/useConsoleStore';
import {
  MalformedGitPayloadError,
  changeStatusLabel,
  changeStatusCode,
  type GitDiffView as GitDiffPayload,
  type GitStatusView,
} from '../runtime-client/git.ts';

function describe(err: unknown): string {
  if (err instanceof MalformedGitPayloadError) return err.message;
  if (err instanceof Error && err.message) return err.message;
  return String(err);
}

/**
 * Read-only git explorer, opened from the header's branch chip.
 *
 * Mirrors the TUI's `dialogs/git_explore.py`: the changed-file list on the left,
 * the selected file's unified diff on the right.  It reads through
 * `runtime.git.status` / `runtime.git.diff`, so nothing here can stage, commit or
 * otherwise write — and a workspace where git cannot answer says so instead of
 * showing an empty tree.
 *
 * The window is the same one the file manager uses: a `FloatingWindow`, so it can
 * be dragged by its title bar, resized from the bottom-right grip and maximized /
 * restored (the double-click and the title-bar button), and its file-list column
 * collapses with the title-bar toggle so the diff spreads across the whole width.
 *
 * `initialPath` is the file to open on: a turn's change card asks for the file it
 * names, and the branch chip asks for none (the explorer picks its own first row).  A
 * path that is no longer in the changed list is not forced: the reader sees the list,
 * which is the honest answer to "review this file" once it is no longer changed.
 */
export const GitExplorer: React.FC<{ onClose: () => void; initialPath?: string | null }> = ({
  onClose,
  initialPath,
}) => {
  const client = useConsoleStore((s) => s.client);
  const currentSession = useConsoleStore((s) => s.currentSession);
  const openExternalError = useConsoleStore((s) => s.openExternalError);
  const dismissOpenExternalError = useConsoleStore((s) => s.dismissOpenExternalError);
  const [status, setStatus] = useState<GitStatusView | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(initialPath ?? null);
  const [treeVisible, setTreeVisible] = useState(true);
  const [mobileDetail, setMobileDetail] = useState(false);
  const [staged, setStaged] = useState(false);
  const [diff, setDiff] = useState<GitDiffPayload | null>(null);
  const [diffError, setDiffError] = useState<string | null>(null);
  // Bumped by the refresh button: the diff effect already keys off the selection,
  // so re-reading the *same* file needs a second trigger to re-run it.
  const [diffReloadToken, setDiffReloadToken] = useState(0);
  const [busy, setBusy] = useState(false);
  const dialogRef = useRef<HTMLDivElement | null>(null);

  // The branch chip that opens this dialog keeps the focus, so the explorer has
  // to claim it -- and only once the file list has been read: the first row is
  // the initial focus, which a header button must not steal.
  const onKeyDown = useDialogKeyboardNav(dialogRef, status !== null, '#git-file-list button');

  const loadStatus = useCallback(async () => {
    if (client === null) return;
    setBusy(true);
    setStatusError(null);
    try {
      const next = await client.gitStatus(currentSession);
      setStatus(next);
      setSelected((current) => {
        if (current !== null && next.files.some((file) => file.path === current)) return current;
        return next.files.length > 0 ? next.files[0].path : null;
      });
    } catch (err) {
      setStatus(null);
      setStatusError(describe(err));
    } finally {
      setBusy(false);
    }
  }, [client, currentSession]);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  useEffect(() => {
    if (selected === null || client === null) return;
    let cancelled = false;
    void (async () => {
      setDiffError(null);
      try {
        const next = await client.gitDiff(currentSession, selected, staged);
        if (!cancelled) setDiff(next);
      } catch (err) {
        if (!cancelled) {
          setDiff(null);
          setDiffError(describe(err));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, currentSession, selected, staged, diffReloadToken]);

  const files = status?.files ?? [];

  return (
    <FloatingWindow
      label="Git Explorer"
      scrim="soft"
      initialWidth={1152}
      initialHeight={680}
      onClose={onClose}
      title={
        <div className="flex items-center gap-2">
          <Branch20Regular aria-hidden="true" className="shrink-0 text-gray-500" />
          <span className="text-sm font-semibold text-gray-900">Git Explorer</span>
          <span className="font-mono text-xs text-gray-500">{status?.branch ?? 'git'}</span>
          {status !== null && (
            <span className="font-mono text-[11px] text-gray-400">
              {files.length} file{files.length === 1 ? '' : 's'}
              {status.truncated ? '+' : ''}
              {status.ahead > 0 && ` · ↑${status.ahead}`}
              {status.behind > 0 && ` · ↓${status.behind}`}
            </span>
          )}
        </div>
      }
      actions={
        <div className="flex items-center gap-1.5">
          {/* Same collapse control as the file manager: the list column folds away
              so the diff spreads across the whole window. */}
          <button
            type="button"
            onClick={() => {
              // On a phone the list and the diff share one column, so folding the
              // list away has to land on the diff (not on the blank view where both
              // are hidden) and unfolding it has to come back to the list.  On the
              // desktop columns this only flips the hidden list.
              const next = !treeVisible;
              setTreeVisible(next);
              setMobileDetail(!next);
            }}
            title={treeVisible ? '收起文件列表' : '展开文件列表'}
            aria-label={treeVisible ? '收起文件列表' : '展开文件列表'}
            className="ui-icon-button ui-compact text-gray-400 hover:text-gray-700"
          >
            {treeVisible ? (
              <PanelLeftContract16Regular aria-hidden="true" />
            ) : (
              <PanelLeftExpand16Regular aria-hidden="true" />
            )}
          </button>
          {/* The reader's own editor for the selected file: a host-side launch, so it
              is offered here (a title-bar action) and never silently retried. */}
          <OpenWithMenu
            path={selected}
            disabledReason={selected === null ? '先选择一个文件' : '打开方式不可用'}
          />
          {/* The checkbox is a title-bar control: keep its pointer sequence out of
              the drag handler so a click toggles it instead of starting a drag. */}
          <label
            onPointerDown={(event) => event.stopPropagation()}
            className="flex cursor-pointer items-center gap-1 font-mono text-[11px] text-gray-600"
          >
            <input
              id="git-explorer-staged"
              name="git-explorer-staged"
              type="checkbox"
              checked={staged}
              onChange={(event) => setStaged(event.target.checked)}
              className="ui-check"
            />
            暂存区
          </label>
          <button
            type="button"
            onClick={() => {
              void loadStatus();
              // Re-read the open file too: the reader pressed refresh to see the
              // current diff again, not only the file list.
              if (selected !== null) setDiffReloadToken((token) => token + 1);
            }}
            disabled={busy}
            title="刷新"
            className="ui-icon-button ui-compact text-gray-500 hover:text-gray-900 disabled:opacity-40"
          >
            <ArrowSync20Regular aria-hidden="true" className={busy ? 'animate-spin' : ''} />
          </button>
        </div>
      }
    >
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        {openExternalError !== null && (
          <div className="flex items-center gap-2 border-b border-amber-100 bg-amber-50 px-3 py-1.5 text-[11px] leading-relaxed text-amber-800">
            <span className="min-w-0 flex-1">{openExternalError}</span>
            <button
              type="button"
              onClick={dismissOpenExternalError}
              className="ui-button ui-compact shrink-0 text-[11px]"
            >
              知道了
            </button>
          </div>
        )}

        <div
          ref={dialogRef}
          tabIndex={-1}
          onKeyDown={onKeyDown}
          className="git-responsive-body flex min-h-0 min-w-0 flex-1"
          data-detail={mobileDetail}
        >
          {/* The changed-file list.  Collapsed with the title-bar toggle: `hidden`
              removes the column entirely, so the diff below fills the full width. */}
          <div
            id="git-file-list"
            className={`fluent-scrollbar w-80 shrink-0 overflow-y-auto border-r border-gray-100 py-1 ${
              treeVisible ? '' : 'hidden'
            }`}
          >
            {statusError !== null && (
              <p className="px-3 py-2 text-[11px] leading-relaxed text-amber-700">
                {statusError}
              </p>
            )}
            {statusError === null && files.length === 0 && (
              <p className="px-3 py-2 text-[11px] text-gray-400">工作区干净，没有变更。</p>
            )}
            {files.map((file) => (
              <button
                key={file.path}
                type="button"
                onClick={() => {
                  // Re-clicking the file already open keeps its diff: clearing here
                  // would blank the pane the reader is looking at.  A *different*
                  // file is cleared (not in an effect) so the previous file's diff
                  // never flashes while the new one loads.
                  if (file.path !== selected) {
                    setDiff(null);
                    setSelected(file.path);
                  }
                  setMobileDetail(true);
                }}
                title={`${changeStatusLabel(file)} · ${changeStatusCode(file)}`}
                className={`flex w-full cursor-pointer items-center gap-2 px-2 py-1 text-left transition-colors hover:bg-gray-100 ${
                  file.path === selected ? 'bg-gray-100' : ''
                }`}
              >
                <span className="w-6 shrink-0 font-mono text-[10px] text-gray-500">
                  {changeStatusCode(file)}
                </span>
                <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-gray-800">
                  {file.path}
                </span>
              </button>
            ))}
          </div>

          {/* The diff pane is the professional viewer: it owns the unified/split
              toggle, the dual line numbers, the word-level highlights, the hunk
              banners and the +/- statistics.  It also owns every terminal state,
              including the empty one the reader sees when a file matches its
              baseline -- 没有差异（该文件与所选基线一致；可用上方「暂存区」比较另一侧）。 */}
          <div className="flex min-w-0 flex-1 flex-col overflow-hidden bg-canvas">
            <button
              type="button"
              className="list-detail-back ui-button"
              onClick={() => {
                setMobileDetail(false);
                setTreeVisible(true);
              }}
            >
              返回文件列表
            </button>
            <div className="flex min-h-0 flex-1 flex-col">
              <GitDiffView diff={diff} diffError={diffError} selectedPath={selected} />
            </div>
          </div>
        </div>
      </div>
    </FloatingWindow>
  );
};
