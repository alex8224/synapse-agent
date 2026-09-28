/**
 * Git tab for the right auxiliary dock: branches → one revision's commits → one
 * commit's files and diff.
 *
 * Three levels in one column, never side by side: the dock is 280–760px wide, so
 * the panel drills down behind a breadcrumb instead of splitting into panes. That
 * is also the shape a narrow screen wants later -- the same state, painted full
 * width -- which is why the position lives in `gitHistoryStore` rather than in
 * this component's `useState`.
 *
 * The panel is desktop-shell only: every read is a `tauri_git_*` call with no
 * runtime RPC behind it, so the tab is registered with the store's availability
 * source and a plain browser never paints it.
 *
 * Nothing here recomputes git: a commit's file list is the commit's own (against
 * its first parent). The console store's standing git status is the workspace's
 * delta against `HEAD` -- it counts every earlier turn again, so a commit's files
 * are never derived from it.
 */
import {
  ArrowClockwise16Regular,
  BranchFork16Regular,
  ChevronDown16Regular,
  ChevronLeft16Regular,
  ChevronRight16Regular,
  Clock16Regular,
  CloudArrowDown16Regular,
  Folder16Regular,
  Tag16Regular,
} from '@fluentui/react-icons';
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import type { GitBranchView, GitCommitView, GitTagView } from '../../runtime-client/git.ts';
import { gitHistoryAvailability, useGitHistoryStore } from '../../stores/gitHistoryStore.ts';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import { GitDiffView } from '../GitDiffView.tsx';
import type { RightDockContext, RightDockTabDefinition } from './contract.ts';

/** A commit date as a relative time, the way a history list is read. */
function relativeTime(iso: string): string {
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return '';
  const minutes = Math.round((Date.now() - at) / 60000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days} 天前`;
  const months = Math.round(days / 30);
  if (months < 12) return `${months} 个月前`;
  return `${Math.round(months / 12)} 年前`;
}

/** A branch's tip date, as a plain calendar day. */
function calendarDay(iso: string): string {
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return iso;
  const date = new Date(at);
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** The colour of one git status letter, by its role. */
const STATUS_TONE: Record<string, string> = {
  A: 'text-emerald-600 bg-emerald-500/10 border-emerald-500/30',
  M: 'text-amber-600 bg-amber-500/10 border-amber-500/30',
  D: 'text-red-600 bg-red-500/10 border-red-500/30',
  R: 'text-purple-600 bg-purple-500/10 border-purple-500/30',
  C: 'text-purple-600 bg-purple-500/10 border-purple-500/30',
  T: 'text-blue-600 bg-blue-500/10 border-blue-500/30',
};

const StatusBadge: React.FC<{ status: string }> = ({ status }) => {
  const letter = status.slice(0, 1) || 'M';
  const tone = STATUS_TONE[letter] ?? 'text-gray-600 bg-surface-hover border-line';
  return (
    <span
      title={status}
      className={`shrink-0 rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold leading-none ${tone}`}
    >
      {letter}
    </span>
  );
};

/** A `+N -M` pair, with the same colours the changes tab uses. */
const LineCounts: React.FC<{ insertions: number | null; deletions: number | null }> = ({
  insertions,
  deletions,
}) => (
  <span className="shrink-0 font-mono text-[10.5px] leading-none">
    <span className="text-emerald-600">+{insertions ?? '?'}</span>{' '}
    <span className="text-red-600">-{deletions ?? '?'}</span>
  </span>
);

const Note: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div className="mx-2 my-2 rounded-control border border-dashed border-line px-2.5 py-2 text-[11px] leading-relaxed text-gray-500">
    {children}
  </div>
);

const Group: React.FC<{
  label: string;
  count: number;
  open: boolean;
  onToggle: () => void;
}> = ({ label, count, open, onToggle }) => (
  <button
    type="button"
    onClick={onToggle}
    className="flex w-full items-center gap-1 px-2.5 pb-1 pt-2 text-[10.5px] font-bold uppercase tracking-wider text-gray-400 hover:text-gray-600"
  >
    {open ? <ChevronDown16Regular aria-hidden="true" /> : <ChevronRight16Regular aria-hidden="true" />}
    <span>{label}</span>
    <span className="rounded border border-line px-1 font-mono text-[9.5px] font-semibold text-gray-500">
      {count}
    </span>
  </button>
);

export const GitContent: React.FC<{ context: RightDockContext }> = () => {
  const threadId = useConsoleStore((state) => state.currentSession.thread_id);
  const {
    refs,
    refsLoading,
    refsError,
    level,
    rev,
    commits,
    more,
    logLoading,
    logError,
    firstParent,
    pathFilter,
    appliedPathFilter,
    sha,
    detail,
    detailLoading,
    detailError,
    openFile,
    fileDiffs,
    openStash,
    stashDiffs,
    loadRefs,
    refresh,
    openRev,
    backToRefs,
    loadMore,
    toggleFirstParent,
    setPathFilter,
    applyPathFilter,
    openCommit,
    backToCommits,
    toggleFile,
    toggleStash,
    reset,
  } = useGitHistoryStore(
    useShallow((state) => ({
      refs: state.refs,
      refsLoading: state.refsLoading,
      refsError: state.refsError,
      level: state.level,
      rev: state.rev,
      commits: state.commits,
      more: state.more,
      logLoading: state.logLoading,
      logError: state.logError,
      firstParent: state.firstParent,
      pathFilter: state.pathFilter,
      appliedPathFilter: state.appliedPathFilter,
      sha: state.sha,
      detail: state.detail,
      detailLoading: state.detailLoading,
      detailError: state.detailError,
      openFile: state.openFile,
      fileDiffs: state.fileDiffs,
      openStash: state.openStash,
      stashDiffs: state.stashDiffs,
      loadRefs: state.loadRefs,
      refresh: state.refresh,
      openRev: state.openRev,
      backToRefs: state.backToRefs,
      loadMore: state.loadMore,
      toggleFirstParent: state.toggleFirstParent,
      setPathFilter: state.setPathFilter,
      applyPathFilter: state.applyPathFilter,
      openCommit: state.openCommit,
      backToCommits: state.backToCommits,
      toggleFile: state.toggleFile,
      toggleStash: state.toggleStash,
      reset: state.reset,
    })),
  );

  const [openGroups, setOpenGroups] = useState({
    locals: true,
    remotes: true,
    tags: true,
    stashes: true,
    worktrees: true,
  });
  const toggleGroup = useCallback((key: keyof typeof openGroups) => {
    setOpenGroups((current) => ({ ...current, [key]: !current[key] }));
  }, []);

  // A session switch starts over: another session runs in another workspace.
  useEffect(() => {
    reset();
    void loadRefs();
  }, [threadId, reset, loadRefs]);

  const locals = useMemo(() => refs?.branches.filter((b) => b.kind === 'local') ?? [], [refs]);
  const remotes = useMemo(() => refs?.branches.filter((b) => b.kind === 'remote') ?? [], [refs]);
  const current = useMemo(() => locals.find((branch) => branch.isHead) ?? null, [locals]);

  /**
   * The refs a commit carries, derived from the refs already read rather than
   * from a second `git log --decorate`: a branch or a tag that points at this
   * commit is a badge on it.
   */
  const refLabels = useMemo(() => {
    const labels = new Map<string, string[]>();
    const push = (sha: string, label: string) => {
      const existing = labels.get(sha) ?? [];
      existing.push(label);
      labels.set(sha, existing);
    };
    for (const branch of refs?.branches ?? []) {
      if (branch.isHead) push(branch.tipSha, 'HEAD');
      push(branch.tipSha, branch.name);
    }
    for (const tag of refs?.tags ?? []) push(tag.targetSha, tag.name);
    return labels;
  }, [refs]);

  const renderBranchRow = (branch: GitBranchView) => (
    <button
      key={`${branch.kind}:${branch.name}`}
      type="button"
      onClick={() => void openRev(branch.name)}
      className="flex w-full items-start gap-2 rounded-control px-2 py-1.5 text-left hover:bg-surface-hover"
    >
      {branch.kind === 'local' ? (
        <BranchFork16Regular aria-hidden="true" className="mt-0.5 shrink-0 text-gray-400" />
      ) : (
        <CloudArrowDown16Regular aria-hidden="true" className="mt-0.5 shrink-0 text-gray-400" />
      )}
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="truncate font-mono text-[11.5px] text-gray-900">{branch.name}</span>
          {branch.isHead && (
            <span className="shrink-0 rounded border border-accent bg-accent/10 px-1 font-mono text-[9.5px] font-bold text-accent">
              HEAD
            </span>
          )}
          {branch.ahead > 0 && (
            <span className="shrink-0 font-mono text-[10.5px] text-emerald-600">↑{branch.ahead}</span>
          )}
          {branch.behind > 0 && (
            <span className="shrink-0 font-mono text-[10.5px] text-red-600">↓{branch.behind}</span>
          )}
        </div>
        <div className="truncate text-[11px] text-gray-500">{branch.tipSubject}</div>
        <div className="mt-0.5 truncate text-[10.5px] text-gray-400">
          {branch.tipSha} · {calendarDay(branch.tipDate)}
          {branch.upstream ? ` · 跟踪 ${branch.upstream}` : ' · 无上游'}
        </div>
      </div>
      <ChevronRight16Regular aria-hidden="true" className="mt-1 shrink-0 text-gray-300" />
    </button>
  );

  const renderTagRow = (tag: GitTagView) => (
    <button
      key={tag.name}
      type="button"
      onClick={() => void openCommit(tag.targetSha, 'refs')}
      className="flex w-full items-start gap-2 rounded-control px-2 py-1.5 text-left hover:bg-surface-hover"
    >
      <Tag16Regular aria-hidden="true" className="mt-0.5 shrink-0 text-gray-400" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="truncate rounded border border-line bg-surface-hover px-1 font-mono text-[10.5px] text-gray-700">
            {tag.name}
          </span>
          <span className="shrink-0 text-[10px] text-gray-400">
            {tag.annotated ? '附注标签' : '轻量标签'}
          </span>
        </div>
        <div className="truncate text-[11px] text-gray-500">{tag.subject}</div>
        <div className="mt-0.5 truncate text-[10.5px] text-gray-400">
          指向 {tag.targetSha} · {calendarDay(tag.date)}
        </div>
      </div>
      <ChevronRight16Regular aria-hidden="true" className="mt-1 shrink-0 text-gray-300" />
    </button>
  );

  const renderStashRow = (index: number, name: string, message: string) => {
    const lazy = stashDiffs[index];
    return (
      <div key={name} className="mx-0.5">
        <button
          type="button"
          onClick={() => void toggleStash(index)}
          className="flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-left hover:bg-surface-hover"
        >
          <Clock16Regular aria-hidden="true" className="shrink-0 text-gray-400" />
          <span className="shrink-0 font-mono text-[11px] text-gray-700">{name}</span>
          <span className="min-w-0 flex-1 truncate text-[11px] text-gray-500">{message}</span>
          {openStash === index ? (
            <ChevronDown16Regular aria-hidden="true" className="shrink-0 text-gray-400" />
          ) : (
            <ChevronRight16Regular aria-hidden="true" className="shrink-0 text-gray-300" />
          )}
        </button>
        {openStash === index && (
          <div className="mt-1 rounded-control border border-line/70 bg-surface">
            {lazy?.loading ? (
              <div className="flex items-center justify-center gap-1.5 py-4 text-[11px] text-gray-400">
                <ArrowClockwise16Regular aria-hidden="true" className="animate-spin" />
                正在读取 stash 差异…
              </div>
            ) : (
              <GitDiffView
                diff={lazy?.diff ?? null}
                diffError={lazy?.error ?? null}
                selectedPath={name}
                fitContent={true}
              />
            )}
          </div>
        )}
      </div>
    );
  };

  const renderRefs = () => {
    if (refsLoading && refs === null) {
      return (
        <div className="flex items-center justify-center gap-1.5 py-8 text-[11.5px] text-gray-400">
          <ArrowClockwise16Regular aria-hidden="true" className="animate-spin" />
          正在读取分支…
        </div>
      );
    }
    if (refsError !== null) {
      return (
        <Note>
          <div className="text-red-600">读取分支失败：{refsError}</div>
          <button
            type="button"
            onClick={() => void loadRefs()}
            className="mt-1.5 rounded-control border border-line px-2 py-0.5 text-gray-700 hover:bg-surface-hover"
          >
            重试
          </button>
        </Note>
      );
    }
    if (refs === null) return <Note>没有可读取的仓库。</Note>;

    const others = locals.filter((branch) => !branch.isHead);

    return (
      <div className="pb-3">
        {current ? (
          <div className="mx-2 mt-2 rounded-control border border-accent/60 bg-accent/5 px-2.5 py-2">
            <div className="flex items-center gap-1.5">
              <span className="shrink-0 rounded border border-accent bg-accent/10 px-1 font-mono text-[9.5px] font-bold text-accent">
                HEAD
              </span>
              <span className="truncate font-mono text-[11.5px] font-semibold text-gray-900">
                {current.name}
              </span>
            </div>
            <div className="mt-1 truncate text-[11px] text-gray-500">{current.tipSubject}</div>
            <div className="mt-0.5 truncate text-[10.5px] text-gray-400">
              {current.tipSha} · 最新提交 {relativeTime(current.tipDate)}
              {current.upstream
                ? ` · 领先 ${current.ahead} / 落后 ${current.behind}`
                : ' · 无上游'}
            </div>
            <button
              type="button"
              onClick={() => void openRev(current.name)}
              className="mt-1.5 rounded-control border border-line px-2 py-0.5 text-[11px] text-gray-700 hover:bg-surface-hover"
            >
              查看历史
            </button>
          </div>
        ) : (
          <Note>
            没有分支指向 HEAD（游离 HEAD 或空仓库）。
            <button
              type="button"
              onClick={() => void openRev('HEAD')}
              className="ml-1 rounded-control border border-line px-1.5 py-0.5 text-gray-700 hover:bg-surface-hover"
            >
              查看 HEAD 历史
            </button>
          </Note>
        )}

        <Group
          label="本地分支"
          count={others.length}
          open={openGroups.locals}
          onToggle={() => toggleGroup('locals')}
        />
        {openGroups.locals && others.map(renderBranchRow)}

        <Group
          label="远程分支"
          count={remotes.length}
          open={openGroups.remotes}
          onToggle={() => toggleGroup('remotes')}
        />
        {openGroups.remotes && remotes.map(renderBranchRow)}

        <Group
          label="标签"
          count={refs.tags.length}
          open={openGroups.tags}
          onToggle={() => toggleGroup('tags')}
        />
        {openGroups.tags && refs.tags.map(renderTagRow)}

        <Group
          label="暂存"
          count={refs.stashes.length}
          open={openGroups.stashes}
          onToggle={() => toggleGroup('stashes')}
        />
        {openGroups.stashes &&
          (refs.stashes.length === 0 ? (
            <Note>当前仓库没有 stash。</Note>
          ) : (
            refs.stashes.map((stash) => renderStashRow(stash.index, stash.name, stash.message))
          ))}

        <Group
          label="工作树"
          count={refs.worktrees.length}
          open={openGroups.worktrees}
          onToggle={() => toggleGroup('worktrees')}
        />
        {openGroups.worktrees &&
          refs.worktrees.map((worktree) => (
            <div key={worktree.path} className="flex items-start gap-2 px-2 py-1.5">
              <Folder16Regular aria-hidden="true" className="mt-0.5 shrink-0 text-gray-400" />
              <div className="min-w-0 flex-1">
                <div className="truncate font-mono text-[10.5px] text-gray-700" title={worktree.path}>
                  {worktree.path}
                </div>
                <div className="mt-0.5 truncate text-[10.5px] text-gray-400">
                  {worktree.headSha} · {worktree.branch ?? '游离 HEAD'}
                  {worktree.isMain ? ' · 主工作树' : ''}
                </div>
              </div>
            </div>
          ))}

        {refs.truncated && <Note>分支或标签超过上限，列表已截断。</Note>}
      </div>
    );
  };

  const renderCommits = () => (
    <div className="pb-3">
      <div className="flex items-center gap-1.5 border-b border-line/60 px-2 py-1.5">
        <input
          value={pathFilter}
          onChange={(event) => setPathFilter(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') void applyPathFilter();
          }}
          placeholder="按路径过滤（回车应用）"
          className="h-7 min-w-0 flex-1 rounded-control border border-line bg-surface px-2 font-mono text-[11px] text-gray-900 outline-none focus:border-accent"
        />
        <button
          type="button"
          onClick={() => void toggleFirstParent()}
          title="只显示这个分支自身的提交，不展开被合并进来的历史"
          className={`h-7 shrink-0 rounded-control border px-2 text-[11px] ${
            firstParent
              ? 'border-accent bg-accent/10 font-semibold text-accent'
              : 'border-line text-gray-600 hover:bg-surface-hover'
          }`}
        >
          只看本分支
        </button>
      </div>

      {appliedPathFilter !== '' && (
        <Note>
          只显示改动过 <span className="font-mono">{appliedPathFilter}</span> 的提交。
        </Note>
      )}

      {logLoading && commits.length === 0 && (
        <div className="flex items-center justify-center gap-1.5 py-8 text-[11.5px] text-gray-400">
          <ArrowClockwise16Regular aria-hidden="true" className="animate-spin" />
          正在读取历史…
        </div>
      )}

      {logError !== null && <Note>读取历史失败：{logError}</Note>}

      {!logLoading && logError === null && commits.length === 0 && (
        <Note>这个版本没有提交（空仓库，或过滤条件没有命中）。</Note>
      )}

      {commits.map((commit: GitCommitView) => (
        <button
          key={commit.sha}
          type="button"
          onClick={() => void openCommit(commit.sha, 'commits')}
          className="flex w-full items-start gap-2 rounded-control px-2 py-1.5 text-left hover:bg-surface-hover"
        >
          <span className="mt-0.5 shrink-0 font-mono text-[11px] text-gray-400">
            {commit.shortSha}
          </span>
          <div className="min-w-0 flex-1">
            <div className="truncate text-[12px] text-gray-900">{commit.subject}</div>
            <div className="mt-0.5 truncate text-[10.5px] text-gray-400">
              {commit.author} · {relativeTime(commit.authoredAt)}
              {commit.parents.length > 1 ? ` · ${commit.parents.length} 个父提交` : ''}
            </div>
            {(refLabels.get(commit.shortSha)?.length ?? 0) > 0 && (
              <div className="mt-1 flex flex-wrap gap-1">
                {refLabels.get(commit.shortSha)?.map((label) => (
                  <span
                    key={label}
                    className={`rounded border px-1 font-mono text-[9.5px] ${
                      label === 'HEAD'
                        ? 'border-accent bg-accent/10 font-bold text-accent'
                        : 'border-line bg-surface-hover text-gray-600'
                    }`}
                  >
                    {label}
                  </span>
                ))}
              </div>
            )}
          </div>
        </button>
      ))}

      {more && (
        <button
          type="button"
          onClick={() => void loadMore()}
          disabled={logLoading}
          className="mx-2 mt-2 w-[calc(100%-1rem)] rounded-control border border-line py-1.5 text-[11.5px] text-gray-600 hover:bg-surface-hover disabled:opacity-60"
        >
          {logLoading ? '正在加载…' : `加载更多（已载入 ${commits.length} 条）`}
        </button>
      )}
    </div>
  );

  const renderCommit = () => {
    if (detailLoading && detail === null) {
      return (
        <div className="flex items-center justify-center gap-1.5 py-8 text-[11.5px] text-gray-400">
          <ArrowClockwise16Regular aria-hidden="true" className="animate-spin" />
          正在读取提交…
        </div>
      );
    }
    if (detailError !== null) return <Note>读取提交失败：{detailError}</Note>;
    if (detail === null) return <Note>没有可显示的提交。</Note>;

    return (
      <div className="pb-3">
        <div className="border-b border-line/60 px-2.5 py-2">
          <div className="text-[12.5px] font-semibold leading-snug text-gray-900">
            {detail.subject}
          </div>
          <div className="mt-1.5 space-y-0.5 text-[10.5px] text-gray-500">
            <div className="font-mono break-all">{detail.sha}</div>
            <div>
              {detail.author} · {relativeTime(detail.authoredAt)}
            </div>
            <div className="truncate">
              父提交{' '}
              <span className="font-mono">
                {detail.parents.length === 0
                  ? '（首个提交，与空树比较）'
                  : detail.parents.map((parent) => parent.slice(0, 7)).join(' ')}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <span>{detail.files.length} 个文件</span>
              <LineCounts insertions={detail.insertions} deletions={detail.deletions} />
            </div>
          </div>
          {detail.body !== '' && (
            <div className="mt-2 whitespace-pre-wrap border-l-2 border-line pl-2 text-[11px] leading-relaxed text-gray-500">
              {detail.body}
            </div>
          )}
        </div>

        {detail.files.length === 0 && (
          <Note>这个提交相对第一父没有文件改动（合并提交常见如此）。</Note>
        )}

        {detail.files.map((file) => {
          const expanded = openFile === file.path;
          const lazy = fileDiffs[file.path];
          return (
            <div key={`${file.status}:${file.path}`}>
              <button
                type="button"
                onClick={() => void toggleFile(file.path)}
                className={`flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-left hover:bg-surface-hover ${
                  expanded ? 'bg-surface-hover' : ''
                }`}
              >
                <StatusBadge status={file.status} />
                <div className="min-w-0 flex-1">
                  <div className="truncate font-mono text-[11px] text-gray-800" title={file.path}>
                    {file.path}
                  </div>
                  {file.oldPath !== null && (
                    <div className="truncate font-mono text-[10px] text-gray-400">
                      ← {file.oldPath}
                    </div>
                  )}
                </div>
                {file.binary ? (
                  <span className="shrink-0 text-[10px] text-gray-400">二进制</span>
                ) : (
                  <LineCounts insertions={file.insertions} deletions={file.deletions} />
                )}
              </button>
              {expanded && (
                <div className="mt-1 rounded-control border border-line/70 bg-surface">
                  {lazy?.loading ? (
                    <div className="flex items-center justify-center gap-1.5 py-4 text-[11px] text-gray-400">
                      <ArrowClockwise16Regular aria-hidden="true" className="animate-spin" />
                      正在读取差异…
                    </div>
                  ) : (
                    <GitDiffView
                      diff={lazy?.diff ?? null}
                      diffError={lazy?.error ?? null}
                      selectedPath={file.path}
                      fitContent={true}
                    />
                  )}
                </div>
              )}
            </div>
          );
        })}

        {detail.truncated && <Note>改动文件超过上限，列表已截断。</Note>}
      </div>
    );
  };

  const crumb = () => {
    if (level === 'refs') {
      return (
        <div className="flex h-8 shrink-0 items-center gap-1 border-b border-line/60 px-1.5">
          <span className="px-1 text-[11.5px] font-semibold text-gray-900">分支</span>
          <span className="flex-1" />
          <button
            type="button"
            title="重新读取分支、标签与工作树"
            aria-label="刷新"
            onClick={() => void refresh()}
            className="ui-icon-button ui-compact text-gray-500 hover:bg-surface-hover hover:text-gray-900"
          >
            <ArrowClockwise16Regular aria-hidden="true" />
          </button>
        </div>
      );
    }
    return (
      <div className="flex h-8 shrink-0 items-center gap-0.5 border-b border-line/60 px-1">
        <button
          type="button"
          title={level === 'commits' ? '返回分支列表' : '返回提交列表'}
          aria-label="返回"
          onClick={() => (level === 'commits' ? backToRefs() : backToCommits())}
          className="ui-icon-button ui-compact text-gray-500 hover:bg-surface-hover hover:text-gray-900"
        >
          <ChevronLeft16Regular aria-hidden="true" />
        </button>
        <button
          type="button"
          onClick={backToRefs}
          className="rounded-control px-1 text-[11.5px] text-gray-600 hover:bg-surface-hover"
        >
          分支
        </button>
        <ChevronRight16Regular aria-hidden="true" className="shrink-0 text-gray-300" />
        <button
          type="button"
          onClick={() => (level === 'commit' ? backToCommits() : undefined)}
          className="max-w-[45%] truncate rounded-control px-1 font-mono text-[11.5px] text-gray-600 hover:bg-surface-hover"
          title={rev ?? ''}
        >
          {rev ?? 'HEAD'}
        </button>
        {level === 'commit' && (
          <>
            <ChevronRight16Regular aria-hidden="true" className="shrink-0 text-gray-300" />
            <span className="max-w-[35%] truncate px-1 font-mono text-[11.5px] font-semibold text-gray-900">
              {sha?.slice(0, 7) ?? ''}
            </span>
          </>
        )}
        <span className="flex-1" />
        <button
          type="button"
          title="重新读取"
          aria-label="刷新"
          onClick={() => void refresh()}
          className="ui-icon-button ui-compact text-gray-500 hover:bg-surface-hover hover:text-gray-900"
        >
          <ArrowClockwise16Regular aria-hidden="true" />
        </button>
      </div>
    );
  };

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {crumb()}
      <div className="fluent-scrollbar flex-1 overflow-y-auto overflow-x-hidden">
        {level === 'refs' && renderRefs()}
        {level === 'commits' && renderCommits()}
        {level === 'commit' && renderCommit()}
      </div>
    </div>
  );
};

export const gitTab: RightDockTabDefinition = {
  id: 'git',
  label: '分支',
  order: 22,
  Icon: BranchFork16Regular,
  visible: (context) => context.sessionOpen,
  // Native-only: the desktop shell answers the history reads, so a plain browser
  // must not paint a tab that could never load.
  availability: gitHistoryAvailability,
  Content: GitContent,
};
