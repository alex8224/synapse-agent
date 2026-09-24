/**
 * Changes & Code Review Tab for the Right Auxiliary Dock (inspired by ZCode & Codex).
 *
 * Implements:
 * - Filter dropdown: 全部 / 未暂存 / 已暂存
 * - ZCode file row layout: base name emphasized, relative parent dir subtle, change code pill
 * - ZCode Accordion Inline Diff: click file to expand syntax-highlighted diff directly beneath the item
 * - Dual view modes: Accordion (inline) vs Split panel
 * - Quick file actions: open with external IDE (VS Code / Explorer), copy path
 */
import {
  Branch16Regular,
  ArrowClockwise16Regular,
  DocumentEdit16Regular,
  ChevronDown16Regular,
  ChevronRight16Regular,
  FolderOpen16Regular,
  Code16Regular,
  Copy16Regular,
  Checkmark16Regular,
} from '@fluentui/react-icons';
import React, { useCallback, useEffect, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import { GitDiffView } from '../GitDiffView.tsx';
import type { RightDockContext, RightDockTabDefinition } from './contract.ts';
import { fetchGitStatus, fetchGitDiff, revealInFileManager } from '../../client/tauriGitFs.ts';
import {
  changeStatusCode,
  changeStatusLabel,
  type GitDiffView as GitDiffPayload,
  type GitFileChangeView,
  type GitStatusView,
} from '../../runtime-client/git.ts';

export type ChangeFilterKind = 'all' | 'unstaged' | 'staged';

function splitPath(fullPath: string): { fileName: string; dirName: string } {
  const normalized = fullPath.replace(/\\/g, '/');
  const lastSlash = normalized.lastIndexOf('/');
  if (lastSlash === -1) {
    return { fileName: normalized, dirName: '' };
  }
  return {
    fileName: normalized.slice(lastSlash + 1),
    dirName: normalized.slice(0, lastSlash + 1),
  };
}

export const ChangesContent: React.FC<{ context: RightDockContext }> = () => {
  const {
    client,
    currentSession,
    gitStatus,
    loadGitStatus,
    projects,
    activeProjectId,
  } = useConsoleStore(
    useShallow((state) => ({
      client: state.client,
      currentSession: state.currentSession,
      gitStatus: state.gitStatus,
      loadGitStatus: state.loadGitStatus,
      projects: state.projects,
      activeProjectId: state.activeProjectId,
    })),
  );

  const activeProject = projects.find((p) => p.project_id === activeProjectId);
  const [localStatus, setLocalStatus] = useState<GitStatusView | null>(null);
  const [filter, setFilter] = useState<ChangeFilterKind>('all');
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(new Set());
  const [diffCache, setDiffCache] = useState<
    Record<string, { diff: GitDiffPayload | null; error: string | null; loading: boolean }>
  >({});
  const [loading, setLoading] = useState(false);
  const [copiedPath, setCopiedPath] = useState<string | null>(null);

  const refreshStatus = useCallback(async () => {
    if (!client || client.getState() !== 'connected') return;
    setLoading(true);
    try {
      await loadGitStatus();
      const st = await fetchGitStatus(client, currentSession, activeProject?.workspace_path);
      setLocalStatus(st);
    } catch {
      // Degrade gracefully
    } finally {
      setLoading(false);
    }
  }, [client, currentSession, loadGitStatus, activeProject?.workspace_path]);

  useEffect(() => {
    void refreshStatus();
  }, [refreshStatus]);

  const loadDiffForPath = useCallback(
    async (path: string) => {
      if (!client || client.getState() !== 'connected') return;
      if (diffCache[path]?.diff || diffCache[path]?.loading) return;

      setDiffCache((prev) => ({
        ...prev,
        [path]: { diff: null, error: null, loading: true },
      }));

      try {
        const res = await fetchGitDiff(client, currentSession, path, activeProject?.workspace_path);
        setDiffCache((prev) => ({
          ...prev,
          [path]: { diff: res, error: null, loading: false },
        }));
      } catch (err) {
        setDiffCache((prev) => ({
          ...prev,
          [path]: {
            diff: null,
            error: err instanceof Error ? err.message : String(err),
            loading: false,
          },
        }));
      }
    },
    [client, currentSession, diffCache, activeProject?.workspace_path],
  );

  const toggleExpand = useCallback(
    (path: string) => {
      setExpandedPaths((prev) => {
        const next = new Set(prev);
        if (next.has(path)) {
          next.delete(path);
        } else {
          next.add(path);
          void loadDiffForPath(path);
        }
        return next;
      });
    },
    [loadDiffForPath],
  );

  const expandAll = useCallback(() => {
    const rawFiles = localStatus?.files ?? gitStatus?.files ?? [];
    const allPaths = new Set(rawFiles.map((f) => f.path));
    setExpandedPaths(allPaths);
    rawFiles.forEach((f) => {
      void loadDiffForPath(f.path);
    });
  }, [localStatus, gitStatus, loadDiffForPath]);

  const collapseAll = useCallback(() => {
    setExpandedPaths(new Set());
  }, []);

  const handleCopyPath = useCallback((e: React.MouseEvent, path: string) => {
    e.stopPropagation();
    void navigator.clipboard.writeText(path);
    setCopiedPath(path);
    setTimeout(() => setCopiedPath(null), 1500);
  }, []);

  const handleOpenVSCode = useCallback((e: React.MouseEvent, path: string) => {
    e.stopPropagation();
    const cleanPath = path.replace(/\\/g, '/');
    window.open(`vscode://file/${encodeURI(cleanPath)}`, '_self');
  }, []);

  const handleReveal = useCallback((e: React.MouseEvent, path: string) => {
    e.stopPropagation();
    void revealInFileManager(path, activeProject?.workspace_path);
  }, [activeProject?.workspace_path]);

  const allFiles = localStatus?.files ?? gitStatus?.files ?? [];
  const filteredFiles = allFiles.filter((file: GitFileChangeView) => {
    if (filter === 'unstaged') {
      return file.worktreeStatus !== ' ' && file.worktreeStatus !== '?';
    }
    if (filter === 'staged') {
      return file.indexStatus !== ' ' && file.indexStatus !== '?';
    }
    return true;
  });

  return (
    <div className="flex h-full flex-col overflow-hidden text-xs bg-surface select-none">
      {/* Top action bar: filter selector + stats + refresh + expand/collapse controls */}
      <div className="flex items-center justify-between border-b border-line bg-sunken/40 px-2.5 py-1.5 shrink-0">
        <div className="flex items-center gap-2">
          <select
            value={filter}
            onChange={(e) => setFilter(e.target.value as ChangeFilterKind)}
            className="rounded border border-line bg-surface px-1.5 py-0.5 text-xs text-gray-700 outline-none hover:border-accent"
          >
            <option value="all">全部改动 ({allFiles.length})</option>
            <option value="unstaged">未暂存</option>
            <option value="staged">已暂存</option>
          </select>

          {gitStatus !== null && gitStatus.insertions !== null && gitStatus.deletions !== null && (
            <span className="font-mono text-[11px]">
              <span className="text-emerald-600 font-semibold">+{gitStatus.insertions}</span>{' '}
              <span className="text-red-600 font-semibold">-{gitStatus.deletions}</span>
            </span>
          )}
        </div>

        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={expandedPaths.size > 0 ? collapseAll : expandAll}
            title={expandedPaths.size > 0 ? '全部折叠' : '全部展开'}
            className="rounded px-1.5 py-0.5 text-[11px] text-gray-500 hover:bg-surface-hover hover:text-gray-800 transition-colors"
          >
            {expandedPaths.size > 0 ? '全部折叠' : '全部展开'}
          </button>
          <button
            type="button"
            onClick={() => void refreshStatus()}
            title="重新扫描 Git 变更"
            className="ui-icon-button ui-compact text-gray-500 hover:text-gray-800"
          >
            <ArrowClockwise16Regular className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {/* Accordion File & Diff list */}
      <div className="fluent-scrollbar flex-1 overflow-y-auto overflow-x-hidden p-1.5 space-y-1">
        {filteredFiles.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-center text-gray-400 font-sans">
            <DocumentEdit16Regular className="mb-2 text-2xl text-gray-300" />
            <p>工作区很干净</p>
            <p className="text-[11px] text-gray-400">没有检测到未提交的代码改动</p>
          </div>
        ) : (
          filteredFiles.map((file) => {
            const isExpanded = expandedPaths.has(file.path);
            const { fileName, dirName } = splitPath(file.path);
            const cache = diffCache[file.path];

            return (
              <div
                key={file.path}
                className="overflow-hidden rounded-control border border-line/80 bg-surface shadow-xs"
              >
                <div
                  onClick={() => toggleExpand(file.path)}
                  className={`group flex h-8 cursor-pointer items-center justify-between px-2 transition-colors ${
                    isExpanded ? 'bg-sunken/60 border-b border-line/60' : 'hover:bg-surface-hover'
                  }`}
                >
                  <div className="flex min-w-0 flex-1 items-center gap-1.5 mr-2">
                    <span className="shrink-0 text-gray-400">
                      {isExpanded ? (
                        <ChevronDown16Regular className="text-xs" />
                      ) : (
                        <ChevronRight16Regular className="text-xs" />
                      )}
                    </span>
                    <span className="font-semibold text-gray-900 truncate shrink-0">{fileName}</span>
                    {dirName && (
                      <span className="text-[11px] text-gray-400 truncate min-w-0">
                        {dirName}
                      </span>
                    )}
                  </div>

                  <div className="flex shrink-0 items-center gap-1.5">
                    <div className="opacity-0 group-hover:opacity-100 transition-opacity flex items-center gap-0.5 mr-1">
                      <button
                        type="button"
                        onClick={(e) => handleOpenVSCode(e, file.path)}
                        title="在 VS Code 中打开"
                        className="flex h-5 w-5 items-center justify-center rounded text-gray-400 hover:text-blue-600 hover:bg-surface-hover transition-colors"
                      >
                        <Code16Regular className="text-xs" />
                      </button>
                      <button
                        type="button"
                        onClick={(e) => handleReveal(e, file.path)}
                        title="在系统文件管理器中定位"
                        className="flex h-5 w-5 items-center justify-center rounded text-gray-400 hover:text-amber-600 hover:bg-surface-hover transition-colors"
                      >
                        <FolderOpen16Regular className="text-xs" />
                      </button>
                      <button
                        type="button"
                        onClick={(e) => handleCopyPath(e, file.path)}
                        title="复制文件路径"
                        className="flex h-5 w-5 items-center justify-center rounded text-gray-400 hover:text-gray-700 hover:bg-surface-hover transition-colors"
                      >
                        {copiedPath === file.path ? (
                          <Checkmark16Regular className="text-xs text-emerald-600" />
                        ) : (
                          <Copy16Regular className="text-xs" />
                        )}
                      </button>
                    </div>
                    <span
                      title={changeStatusLabel(file)}
                      className="shrink-0 rounded bg-sunken px-1.5 py-0.5 font-mono text-[9px] font-semibold uppercase text-gray-600"
                    >
                      {changeStatusCode(file)}
                    </span>
                  </div>
                </div>

                {isExpanded && (
                  <div className="bg-surface max-h-[26rem] overflow-auto border-t border-line/40">
                    {cache?.loading ? (
                      <div className="flex items-center justify-center py-6 text-gray-400">
                        <ArrowClockwise16Regular className="animate-spin mr-1.5" />
                        <span>正在计算差异…</span>
                      </div>
                    ) : (
                      <GitDiffView
                        diff={cache?.diff ?? null}
                        diffError={cache?.error ?? null}
                        selectedPath={file.path}
                      />
                    )}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
};

export const changesTab: RightDockTabDefinition = {
  id: 'changes',
  label: '审查',
  order: 20,
  Icon: Branch16Regular,
  badge: () => {
    const status = useConsoleStore.getState().gitStatus;
    const count = status?.files?.length ?? 0;
    return count > 0 ? count : null;
  },
  badgeVariant: 'diff',
  Content: ChangesContent,
};
