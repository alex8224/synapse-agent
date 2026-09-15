import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Branch20Regular, ArrowSync20Regular, Dismiss20Regular } from '@fluentui/react-icons';
import { Portal } from './Portal.tsx';
import { useDialogKeyboardNav } from './keyboardNav.ts';
import { useConsoleStore } from '../stores/useConsoleStore';
import {
  MalformedGitPayloadError,
  changeStatusLabel,
  changeStatusCode,
  diffLineClass,
  type GitDiffView,
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
 */
export const GitExplorer: React.FC<{ onClose: () => void }> = ({ onClose }) => {
  const client = useConsoleStore((s) => s.client);
  const currentSession = useConsoleStore((s) => s.currentSession);
  const [status, setStatus] = useState<GitStatusView | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [mobileDetail, setMobileDetail] = useState(false);
  const [staged, setStaged] = useState(false);
  const [diff, setDiff] = useState<GitDiffView | null>(null);
  const [diffError, setDiffError] = useState<string | null>(null);
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
  }, [client, currentSession, selected, staged]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  const files = status?.files ?? [];
  const diffLines = diff === null ? [] : diff.text.replace(/\n$/, '').split('\n');

  return (
    // `Portal`: this panel is a window of its own, so an acrylic ancestor cannot
    // anchor its `fixed` box to itself.
    <Portal>
      <div
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/25 backdrop-blur-sm scrim-in"
        onClick={onClose}
      >
      <div
        role="dialog"
        aria-label="Git Explorer"
        ref={dialogRef}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        onClick={(event) => event.stopPropagation()}
        className="flex h-[76vh] w-[72rem] max-w-[calc(100vw-2rem)] flex-col overflow-hidden rounded-card border border-line/70 material-flyout flyout-in text-left shadow-flyout"
      >
        <div className="responsive-dialog-toolbar flex items-center gap-2 border-b border-gray-200 px-3 py-2">
          {mobileDetail && <button className="list-detail-back ui-button" onClick={() => setMobileDetail(false)}>
            返回文件列表
          </button>}
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
          <span className="flex-1" />
          <label className="flex cursor-pointer items-center gap-1 font-mono text-[11px] text-gray-600">
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
            onClick={() => void loadStatus()}
            disabled={busy}
            title="刷新"
            className="ui-icon-button ui-compact text-gray-500 hover:text-gray-900 disabled:opacity-40"
          >
            <ArrowSync20Regular aria-hidden="true" className={busy ? 'animate-spin' : ''} />
          </button>
          <button
            type="button"
            onClick={onClose}
            title="关闭 (Esc)"
            className="ui-icon-button ui-compact text-gray-400 hover:text-gray-700"
          >
            <Dismiss20Regular aria-hidden="true" />
          </button>
        </div>

        <div className="git-responsive-body flex min-h-0 flex-1" data-detail={mobileDetail}>
          <div id="git-file-list" className="fluent-scrollbar w-80 shrink-0 overflow-y-auto border-r border-gray-100 py-1">
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
                  // Clearing here (not in an effect) keeps the panel from showing
                  // the previous file's diff while the new one loads.
                  setDiff(null);
                  setSelected(file.path);
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

          <div className="fluent-scrollbar min-w-0 flex-1 overflow-auto bg-canvas px-3 py-2">
            {selected === null && <p className="text-[11px] text-gray-400">选择一个文件查看 diff。</p>}
            {diffError !== null && <p className="text-[11px] text-amber-700">{diffError}</p>}
            {diffError === null && diff !== null && diff.empty && (
              <p className="text-[11px] text-gray-400">
                没有差异（未修改或未跟踪；未跟踪文件请用「工作区文件」面板查看内容）。
              </p>
            )}
            {diffError === null && diff !== null && diff.binary && (
              <p className="text-[11px] text-gray-500">二进制文件，不显示 diff。</p>
            )}
            {diffError === null && diff !== null && !diff.empty && !diff.binary && (
              <pre className="whitespace-pre font-mono text-[11px] leading-relaxed">
                {diffLines.map((line, index) => (
                  <div key={`${index}-${line.slice(0, 12)}`} className={diffLineClass(line)}>
                    {line === '' ? ' ' : line}
                  </div>
                ))}
              </pre>
            )}
            {diffError === null && diff !== null && diff.truncated && (
              <p className="pt-1 text-[10px] text-amber-700">diff 超过服务端单文件上限，已截断。</p>
            )}
          </div>
        </div>
      </div>
      </div>
    </Portal>
  );
};
