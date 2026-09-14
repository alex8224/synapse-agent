import { ChevronLeft20Regular, Dismiss20Regular, Folder20Regular } from '@fluentui/react-icons';
import React, { useCallback, useEffect, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { Portal } from './Portal.tsx';
import { useConsoleStore } from '../stores/useConsoleStore';
import type { DirectoryListing } from '../client/types.ts';

export interface AddProjectDialogProps {
  onClose: () => void;
}

/**
 * "Add project" picker.
 *
 * The browser cannot resolve a host path (the File System Access API returns an
 * opaque handle, never a path), so the daemon lists the *host* filesystem
 * through `runtime.fs.list` (one bounded level at a time) and the operator
 * settles on a directory.  Three ways reach any directory: the drive/root chips,
 * drilling into rows, or pasting an absolute path.  A row's "选择" button
 * registers that directory directly; the footer registers the directory
 * currently shown.  Either way `runtime.project.register` upserts it, then the
 * console switches to it and opens a fresh session, so the dialog closes.
 *
 * The listing is read-only: only immediate sub-directory names are shown, never
 * file contents, and the daemon caps how many entries one level returns.
 */
export const AddProjectDialog: React.FC<AddProjectDialogProps> = ({ onClose }) => {
  const { addProject, listDirectories } = useConsoleStore(
    useShallow((state) => ({
      addProject: state.addProject,
      listDirectories: state.listDirectories,
    })),
  );

  const [listing, setListing] = useState<DirectoryListing | null>(null);
  const [pathInput, setPathInput] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (path: string | null, initial = false) => {
      // The first load already starts in the loading state, so it must not
      // setState synchronously inside the mount effect.
      if (!initial) {
        setLoading(true);
        setError(null);
      }
      try {
        const next = await listDirectories(path);
        setListing(next);
        setPathInput(next.path);
      } catch (err) {
        setListing(null);
        setError(err instanceof Error && err.message ? err.message : String(err));
      } finally {
        setLoading(false);
      }
    },
    [listDirectories],
  );

  // Open on the daemon's default root (its home directory).
  useEffect(() => {
    void load(null, true);
  }, [load]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  const confirm = async (path: string) => {
    setBusy(true);
    setError(null);
    const reason = await addProject(path);
    setBusy(false);
    if (reason === null) {
      onClose();
      return;
    }
    setError(reason);
  };

  return (
    <Portal>
      <div
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/25 backdrop-blur-sm p-4 scrim-in"
        onClick={onClose}
      >
        <div
          role="dialog"
          aria-label="添加项目"
          className="no-scrollbar flex max-h-[85vh] w-full max-w-lg flex-col rounded-card border border-line/70 material-flyout flyout-in p-5 font-sans shadow-flyout"
          onClick={(event) => event.stopPropagation()}
        >
          <div className="flex items-center justify-between border-b border-gray-100 pb-2">
            <span className="ui-settings-title text-gray-900">添加项目</span>
            <button
              onClick={onClose}
              title="关闭 (Esc)"
              aria-label="关闭添加项目"
              className="ui-icon-button"
            >
              <Dismiss20Regular aria-hidden="true" />
            </button>
          </div>

          {/* Drive/root jump targets: the parent chain stops at a filesystem
              root, so switching a Windows drive needs its own entry point. */}
          {listing !== null && listing.roots.length > 0 && (
            <div className="mt-3 flex flex-wrap items-center gap-1">
              {listing.roots.map((root) => (
                <button
                  key={root}
                  type="button"
                  onClick={() => void load(root)}
                  disabled={busy}
                  title={`转到 ${root}`}
                  className={`ui-button border text-xs ${
                    listing.path === root ? 'ui-primary border-accent' : 'border-line bg-surface'
                  }`}
                >
                  {root}
                </button>
              ))}
            </div>
          )}

          {/* Editable path: paste an absolute path and jump straight there. */}
          <form
            className="mt-2 flex items-center gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              const target = pathInput.trim();
              if (target !== '') void load(target);
            }}
          >
            <button
              type="button"
              onClick={() => void load(listing?.parent ?? null)}
              disabled={listing === null || listing.parent === null || loading || busy}
              title="上一级"
              aria-label="上一级"
              className="ui-icon-button"
            >
              <ChevronLeft20Regular aria-hidden="true" />
            </button>
            <input
              type="text"
              value={pathInput}
              onChange={(event) => setPathInput(event.target.value)}
              placeholder="输入或粘贴目录路径"
              aria-label="目录路径"
              spellCheck={false}
              autoComplete="off"
              className="ui-field min-w-0 flex-1 font-mono text-xs"
            />
            <button
              type="submit"
              disabled={pathInput.trim() === '' || busy}
              title="前往该路径"
              className="ui-button border border-line bg-surface"
            >
              前往
            </button>
          </form>

          <div className="no-scrollbar mt-2 min-h-[12rem] flex-1 overflow-y-auto rounded border border-line/60 bg-canvas/60 p-1">
            {loading ? (
              <p className="px-2 py-3 text-sm text-gray-500">正在读取目录…</p>
            ) : listing === null || listing.entries.length === 0 ? (
              <p className="px-2 py-3 text-sm text-gray-500">没有子目录</p>
            ) : (
              <ul>
                {listing.entries.map((entry) => (
                  <li key={entry.path} className="flex items-center gap-1">
                    <button
                      type="button"
                      onClick={() => void load(entry.path)}
                      disabled={busy}
                      title={`进入 ${entry.path}`}
                      className="flex min-w-0 flex-1 items-center gap-2 rounded px-2 py-1.5 text-left text-sm text-gray-800 hover:bg-gray-100"
                    >
                      <Folder20Regular aria-hidden="true" className="shrink-0 text-gray-500" />
                      <span className="min-w-0 truncate">{entry.name}</span>
                    </button>
                    <button
                      type="button"
                      onClick={() => void confirm(entry.path)}
                      disabled={busy}
                      title={`选择 ${entry.path} 并新建会话`}
                      className="ui-button shrink-0 border border-line bg-surface text-xs"
                    >
                      选择
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {listing?.truncated === true && (
            <p className="mt-1 font-mono text-[10px] text-gray-400">
              仅显示前 {listing.entries.length} 个子目录
            </p>
          )}

          {error !== null && (
            <div
              role="alert"
              className="mt-2 rounded border border-red-200 bg-red-50/70 px-2 py-1 font-mono text-[11px] leading-relaxed text-red-700"
            >
              {error}
            </div>
          )}

          <div className="mt-3 flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className="ui-button border border-line bg-surface"
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => {
                if (listing !== null) void confirm(listing.path);
              }}
              disabled={listing === null || loading || busy}
              title={listing === null ? '选择当前目录并新建会话' : `选择 ${listing.path} 并新建会话`}
              className="ui-button ui-primary border border-accent"
            >
              {busy ? '正在添加…' : '选择当前目录并新建会话'}
            </button>
          </div>
        </div>
      </div>
    </Portal>
  );
};
