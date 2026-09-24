/**
 * Files Tab for the Right Auxiliary Dock (inspired by ZCode & Codex).
 *
 * Implements:
 * - Full-height workspace directory tree navigation
 * - Fuzzy file search input
 * - ZCode's key "Only Changed Files" filter toggle
 * - Action buttons on hover: open with built-in viewer, and quote (@filepath) to composer
 */
import {
  Folder16Regular,
  Document16Regular,
  ArrowClockwise16Regular,
  Search16Regular,
  Open16Regular,
  FolderOpen16Regular,
  Mention16Regular,
  Copy16Regular,
  Checkmark16Regular,
} from '@fluentui/react-icons';
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import { useRightDockStore } from '../../stores/useRightDockStore.ts';
import type { RightDockContext, RightDockTabDefinition } from './contract.ts';
import { fetchListArtifacts, openPathWithDefault, revealInFileManager } from '../../client/tauriGitFs.ts';
import type { ArtifactEntry } from '../../client/artifacts.ts';

function entryBaseName(fullPath: string): string {
  const parts = fullPath.split('/').filter(Boolean);
  return parts.length > 0 ? parts[parts.length - 1] : fullPath;
}

export const FilesContent: React.FC<{ context: RightDockContext }> = () => {
  const {
    client,
    currentSession,
    gitStatus,
    openFileViewer,
    projects,
    activeProjectId,
  } = useConsoleStore(
    useShallow((state) => ({
      client: state.client,
      currentSession: state.currentSession,
      gitStatus: state.gitStatus,
      openFileViewer: state.openFileViewer,
      projects: state.projects,
      activeProjectId: state.activeProjectId,
    })),
  );

  const activeProject = projects.find((p) => p.project_id === activeProjectId);
  const onlyChangedFiles = useRightDockStore((s) => s.onlyChangedFiles);
  const toggleOnlyChangedFiles = useRightDockStore((s) => s.toggleOnlyChangedFiles);
  const fileSearchQuery = useRightDockStore((s) => s.fileSearchQuery);
  const setFileSearchQuery = useRightDockStore((s) => s.setFileSearchQuery);

  const [currentPath, setCurrentPath] = useState('');
  const [entries, setEntries] = useState<ArtifactEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copiedPath, setCopiedPath] = useState<string | null>(null);

  const loadDirectory = useCallback(async (path: string) => {
    if (!client || client.getState() !== 'connected') return;
    setLoading(true);
    setError(null);
    try {
      const res = await fetchListArtifacts(client, currentSession, path, activeProject?.workspace_path);
      setEntries(res.entries);
      setCurrentPath(path);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [client, currentSession, activeProject?.workspace_path]);

  useEffect(() => {
    void loadDirectory('');
  }, [loadDirectory]);

  // Set of paths that have git modifications
  const changedPaths = useMemo(() => {
    const set = new Set<string>();
    if (!gitStatus || !gitStatus.files) return set;
    for (const f of gitStatus.files) {
      set.add(f.path);
      // Also mark parent directories as containing changes
      const parts = f.path.split('/');
      let prefix = '';
      for (let i = 0; i < parts.length - 1; i++) {
        prefix = prefix ? `${prefix}/${parts[i]}` : parts[i];
        set.add(prefix);
      }
    }
    return set;
  }, [gitStatus]);

  // Filter items based on search and changed filter
  const filteredEntries = useMemo(() => {
    let list = entries;
    if (fileSearchQuery.trim()) {
      const q = fileSearchQuery.trim().toLowerCase();
      list = list.filter((e) => entryBaseName(e.path).toLowerCase().includes(q));
    }
    if (onlyChangedFiles && changedPaths.size > 0) {
      list = list.filter((e) => changedPaths.has(e.path));
    }
    return list;
  }, [entries, fileSearchQuery, onlyChangedFiles, changedPaths, currentPath]);

  const handleEntryClick = (entry: ArtifactEntry) => {
    if (entry.kind === 'directory') {
      void loadDirectory(entry.path);
    } else {
      openFileViewer(entry.path);
    }
  };

  const handleGoUp = () => {
    if (!currentPath) return;
    const parts = currentPath.split('/');
    parts.pop();
    void loadDirectory(parts.join('/'));
  };

  const handleQuote = (e: React.MouseEvent, path: string) => {
    e.stopPropagation();
    const composer =
      document.getElementById('console-composer') ||
      document.querySelector<HTMLElement>('.composer-input, textarea');
    if (!composer) return;
    const quoteStr = `@${path} `;

    if (composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement) {
      const current = composer.value;
      const pad = current.endsWith(' ') || current === '' ? '' : ' ';
      composer.value = `${current}${pad}${quoteStr}`;
      composer.dispatchEvent(new Event('input', { bubbles: true }));
      composer.focus();
    } else if (composer instanceof HTMLElement) {
      composer.focus();
      const currentText = composer.textContent || '';
      const pad = currentText.endsWith(' ') || currentText === '' ? '' : ' ';
      const textToInsert = `${pad}${quoteStr}`;
      if (!document.execCommand('insertText', false, textToInsert)) {
        const node = document.createTextNode(textToInsert);
        composer.appendChild(node);
        const range = document.createRange();
        range.selectNodeContents(composer);
        range.collapse(false);
        const sel = window.getSelection();
        sel?.removeAllRanges();
        sel?.addRange(range);
      }
      composer.dispatchEvent(new Event('input', { bubbles: true }));
    }
  };

  const handleOpenDefault = (e: React.MouseEvent, path: string) => {
    e.stopPropagation();
    void openPathWithDefault(path, activeProject?.workspace_path);
  };

  const handleReveal = (e: React.MouseEvent, path: string) => {
    e.stopPropagation();
    void revealInFileManager(path, activeProject?.workspace_path);
  };

  const handleCopyPath = (e: React.MouseEvent, path: string) => {
    e.stopPropagation();
    void navigator.clipboard.writeText(path);
    setCopiedPath(path);
    setTimeout(() => setCopiedPath(null), 1500);
  };

  return (
    <div className="flex h-full flex-col overflow-hidden text-xs">
      {/* Search & ZCode-style "Only Changed" Filter */}
      <div className="flex items-center gap-1.5 border-b border-line/60 bg-sunken/40 px-2.5 py-1.5">
        <div className="flex flex-1 items-center gap-1 rounded-control border border-line bg-surface px-2 py-0.5">
          <Search16Regular className="text-gray-400" />
          <input
            type="text"
            value={fileSearchQuery}
            onChange={(e) => setFileSearchQuery(e.target.value)}
            placeholder="过滤文件..."
            className="w-full bg-transparent font-sans text-xs outline-none text-gray-900 placeholder:text-gray-400"
          />
        </div>
        <button
          type="button"
          onClick={toggleOnlyChangedFiles}
          title="仅显示本次任务或 Git 修改的文件 (ZCode 模式)"
          className={`ui-button ui-compact text-[11px] font-medium border ${
            onlyChangedFiles
              ? 'ui-primary border-transparent'
              : 'border-line/60 bg-surface/80 text-gray-700 hover:bg-surface'
          }`}
        >
          只看变更
        </button>
        <button
          type="button"
          onClick={() => void loadDirectory(currentPath)}
          title="刷新目录"
          className="ui-icon-button ui-compact text-gray-500 hover:text-gray-800"
        >
          <ArrowClockwise16Regular className={loading ? 'animate-spin' : ''} />
        </button>
      </div>

      {/* Breadcrumb path navigation */}
      <div className="flex items-center gap-1 border-b border-line/40 bg-surface px-3 py-1 font-mono text-[11px] text-gray-500">
        <button
          type="button"
          onClick={() => void loadDirectory('')}
          className="hover:text-gray-900 hover:underline"
        >
          根目录
        </button>
        {currentPath.split('/').filter(Boolean).map((seg, idx, arr) => (
          <React.Fragment key={idx}>
            <span>/</span>
            <button
              type="button"
              onClick={() => void loadDirectory(arr.slice(0, idx + 1).join('/'))}
              className="hover:text-gray-900 hover:underline truncate max-w-[100px]"
            >
              {seg}
            </button>
          </React.Fragment>
        ))}
      </div>

      {/* Directory listing */}
      <div className="fluent-scrollbar flex-1 overflow-y-auto overflow-x-hidden p-1.5 font-mono text-[11.5px]">
        {error && (
          <div className="m-2 rounded bg-red-50 p-2 font-sans text-xs text-red-600">
            {error}
          </div>
        )}

        {currentPath !== '' && (
          <button
            type="button"
            onClick={handleGoUp}
            className="flex h-7 w-full cursor-pointer items-center gap-2 rounded px-2 text-left text-gray-500 hover:bg-surface-hover hover:text-gray-800 transition-colors"
          >
            <Folder16Regular className="text-amber-500" />
            <span>.. (返回上级)</span>
          </button>
        )}

        {filteredEntries.map((entry) => {
          const name = entryBaseName(entry.path);
          const isChanged = changedPaths.has(entry.path);
          const isDir = entry.kind === 'directory';

          return (
            <div
              key={entry.path}
              onClick={() => handleEntryClick(entry)}
              className="group flex h-7 cursor-pointer items-center justify-between rounded px-2 hover:bg-surface-hover text-gray-800 transition-colors"
            >
              <div className="flex min-w-0 flex-1 items-center gap-2 truncate">
                {isDir ? (
                  <Folder16Regular className="shrink-0 text-amber-500" />
                ) : (
                  <Document16Regular className="shrink-0 text-gray-400" />
                )}
                <span className="truncate">{name}</span>
                {isChanged && !isDir && (
                  <span
                    className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-500"
                    title="有未提交修改"
                  />
                )}
              </div>

              {/* Hover actions */}
              <div className="opacity-0 group-hover:opacity-100 transition-opacity flex items-center gap-0.5 pl-2 font-sans shrink-0">
                <button
                  type="button"
                  onClick={(e) => handleQuote(e, entry.path)}
                  title="引用此路径到输入框 (@path)"
                  className="flex h-5 w-5 items-center justify-center rounded text-gray-400 hover:text-blue-600 hover:bg-surface transition-colors"
                >
                  <Mention16Regular className="text-xs" />
                </button>
                <button
                  type="button"
                  onClick={(e) => handleOpenDefault(e, entry.path)}
                  title={isDir ? '用系统默认方式打开目录' : '用系统默认程序打开文件'}
                  className="flex h-5 w-5 items-center justify-center rounded text-gray-400 hover:text-gray-800 hover:bg-surface transition-colors"
                >
                  <Open16Regular className="text-xs" />
                </button>
                <button
                  type="button"
                  onClick={(e) => handleReveal(e, entry.path)}
                  title="在系统文件管理器中定位"
                  className="flex h-5 w-5 items-center justify-center rounded text-gray-400 hover:text-amber-600 hover:bg-surface transition-colors"
                >
                  <FolderOpen16Regular className="text-xs" />
                </button>
                <button
                  type="button"
                  onClick={(e) => handleCopyPath(e, entry.path)}
                  title="复制文件相对路径"
                  className="flex h-5 w-5 items-center justify-center rounded text-gray-400 hover:text-gray-800 hover:bg-surface transition-colors"
                >
                  {copiedPath === entry.path ? (
                    <Checkmark16Regular className="text-xs text-emerald-600" />
                  ) : (
                    <Copy16Regular className="text-xs" />
                  )}
                </button>
              </div>
            </div>
          );
        })}

        {!loading && filteredEntries.length === 0 && (
          <div className="py-8 text-center font-sans text-xs text-gray-400">
            {onlyChangedFiles ? '当前目录下无变更文件' : '此目录为空'}
          </div>
        )}
      </div>
    </div>
  );
};

export const filesTab: RightDockTabDefinition = {
  id: 'files',
  label: '文件',
  order: 10,
  Icon: Folder16Regular,
  Content: FilesContent,
};
