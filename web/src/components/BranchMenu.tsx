import {
  Add20Regular,
  Branch20Regular,
  CheckmarkCircle20Regular,
  FolderLink20Regular,
  History20Regular,
} from '@fluentui/react-icons';
import React, { useEffect, useRef, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../stores/useConsoleStore';
import { gitHistoryAvailability, useGitHistoryStore } from '../stores/gitHistoryStore';
import type { GitRefsView } from '../runtime-client/git.ts';
import { ConsolePanel } from './consolePanel.tsx';

/**
 * The header's branch chip menu: switch branch, and the two worktree operations.
 *
 * The branch chip used to open the read-only explorer directly.  Branch switching
 * and worktrees belong next to the branch name -- that is the context they act on
 * -- so the chip now opens this menu and the explorer is one row inside it.  The
 * menu is where the console's *only* git writes live; the history panel and the
 * explorer stay read-only, which is why the writes are named here and nowhere
 * else.
 *
 * Worktrees follow one fixed convention (the shell puts them next to the main
 * checkout as `<repo>@<branch>`), so this menu never asks for a path: it names a
 * branch, and the shell derives the directory.  A created worktree is then opened
 * as a project, because a project *is* a workspace directory -- that single
 * decision is what gives the worktree its own agent root, its own `.synapse/`
 * state and its own sessions.
 *
 * Every refusal is git's own sentence, printed as it came: a branch another
 * worktree already holds, a worktree with uncommitted work, a target directory
 * that already exists.  Nothing is pre-judged here.
 */
export const BranchMenu: React.FC<{ anchor: HTMLElement | null; onClose: () => void }> = ({
  anchor,
  onClose,
}) => {
  const {
    refs,
    refsLoading,
    refsError,
    actionBusy,
    actionError,
    loadRefs,
    clearActionError,
    switchBranch,
    createWorktree,
    removeWorktree,
  } = useGitHistoryStore(
    // Only the fields this menu paints: a commit page must not re-render it.
    useShallow((state) => ({
      refs: state.refs,
      refsLoading: state.refsLoading,
      refsError: state.refsError,
      actionBusy: state.actionBusy,
      actionError: state.actionError,
      loadRefs: state.loadRefs,
      clearActionError: state.clearActionError,
      switchBranch: state.switchBranch,
      createWorktree: state.createWorktree,
      removeWorktree: state.removeWorktree,
    })),
  );
  const openGitExplorer = useConsoleStore((state) => state.openGitExplorer);
  const loadGitStatus = useConsoleStore((state) => state.loadGitStatus);
  const openWorkspaceProject = useConsoleStore((state) => state.openWorkspaceProject);
  const gitBranch = useConsoleStore((state) => state.gitBranch);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [draft, setDraft] = useState('');
  // The path git just refused to remove, together with the inventory it was read
  // from, so the second click is the forced one.  Pairing the two makes "a re-read
  // disarms it" a render-time derivation instead of an effect: the row this
  // decision belonged to may be gone by then.
  const [armed, setArmed] = useState<{ path: string; refs: GitRefsView | null } | null>(null);
  const forcePath = armed !== null && armed.refs === refs ? armed.path : null;

  // Closing hands the focus back to the chip that opened the menu, so a keyboard
  // reader is not dropped at the top of the document.
  const close = () => {
    onClose();
    anchor?.focus();
  };

  useEffect(() => {
    // The menu is the only place that acts on branches, so it re-reads them
    // instead of trusting a page the history panel may have loaded long ago; the
    // header's chrome is re-read with them, because this is now its only entry.
    // A refusal from the last time the menu was open is stale, so it goes first.
    clearActionError();
    void loadRefs();
    void loadGitStatus();
  }, [clearActionError, loadRefs, loadGitStatus]);

  useEffect(() => {
    // The topmost surface claims Escape, so a panel underneath (the settings
    // dialog, the session info) does not close in the same press.
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.stopPropagation();
      onClose();
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [onClose]);

  useEffect(() => {
    // A menu, unlike the panels the console opens from a title, is dismissed by
    // clicking away from it.  The chip itself is exempt: its own click toggles,
    // and closing here would reopen it in the same gesture.
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (panelRef.current?.contains(target)) return;
      if (anchor !== null && anchor.contains(target)) return;
      onClose();
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    return () => document.removeEventListener('pointerdown', onPointerDown, true);
  }, [anchor, onClose]);

  const locals = refs === null ? [] : refs.branches.filter((branch) => branch.kind === 'local');
  // The branch the header already shows comes from `runtime.git.status`, which
  // works with and without the native bridge; the refs read only adds the *list*.
  // Without the fallback an unreadable inventory would report "游离 HEAD" for a
  // workspace that is plainly on a branch.
  const current = refs === null ? (gitBranch === '' ? null : gitBranch) : refs.current;
  const worktrees = refs === null ? [] : refs.worktrees;
  // Every action in this menu is a native write, so without the bridge there is
  // nothing to offer -- the sections say why instead of inviting a click that
  // cannot work.  The verdict comes from the same source the history tab's
  // availability uses, so this menu never touches the bridge module itself.
  const canWrite = gitHistoryAvailability.getSnapshot();

  const runSwitch = async (branch: string) => {
    if (await switchBranch(branch)) close();
  };

  const runCreate = async () => {
    const branch = draft.trim();
    if (branch === '') return;
    // The branch list decides "create the branch" from "check the existing one
    // out", so it has to be read before deciding: a menu opened a moment ago may
    // still be loading its refs, and guessing "new" for an existing branch makes
    // git refuse with "a branch named X already exists".
    if (useGitHistoryStore.getState().refs === null) await loadRefs();
    const known = useGitHistoryStore.getState().refs?.branches ?? [];
    const exists = known.some((entry) => entry.kind === 'local' && entry.name === branch);
    if (await createWorktree(branch, !exists)) {
      setDraft('');
      close();
    }
  };

  const runRemove = async (path: string) => {
    const forced = forcePath === path;
    if (await removeWorktree(path, forced)) {
      setArmed(null);
      close();
      return;
    }
    // Not forced and refused: git named the uncommitted work, so the next click
    // on this row is the forced removal -- and the label says so.
    if (!forced) setArmed({ path, refs });
  };

  const runOpen = async (path: string) => {
    if ((await openWorkspaceProject(path)) === null) close();
  };

  const section = (label: string) => (
    <div className="ui-section-label px-1 pb-1 pt-2">{label}</div>
  );

  return (
    <ConsolePanel
      title="分支与工作树"
      anchor={anchor}
      onClose={close}
      side="bottom"
      align="start"
      panelRef={panelRef}
    >
      <div className="flex items-center justify-between rounded-control border-b border-line/40 px-1 py-1.5">
        <span className="shrink-0 font-mono text-[10px] font-medium text-gray-500">当前分支</span>
        <span className="min-w-0 truncate font-mono text-[11px] text-gray-900">
          {current ?? '游离 HEAD'}
        </span>
      </div>

      {refsLoading && <div className="px-1 py-1 font-mono text-[10px] text-gray-400">读取中…</div>}
      {refsError !== null && (
        <div className="break-all px-1 py-1 text-[11px] text-red-600">{refsError}</div>
      )}
      {actionError !== null && (
        <div className="break-all rounded-control bg-red-50 px-1.5 py-1 text-[11px] text-red-700">
          {actionError}
        </div>
      )}

      {section('切换分支')}
      {refs === null && !refsLoading && (
        <div className="px-1 py-1 font-mono text-[10px] text-gray-400">读不到本地分支列表。</div>
      )}
      {refs !== null && locals.filter((branch) => branch.name !== current).length === 0 && (
        <div className="px-1 py-1 font-mono text-[10px] text-gray-400">没有其他本地分支。</div>
      )}
      {locals
        .filter((branch) => branch.name !== current)
        .map((branch) => (
          <button
            key={branch.name}
            type="button"
            onClick={() => void runSwitch(branch.name)}
            disabled={actionBusy || !canWrite}
            title={`切换到 ${branch.name}\n${branch.tipSubject}`}
            className="ui-nav-row flex w-full items-center gap-1.5 px-1 py-1.5 text-left disabled:opacity-50"
          >
            <Branch20Regular aria-hidden="true" className="shrink-0 text-gray-500" />
            <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-gray-800">
              {branch.name}
            </span>
            <span className="shrink-0 font-mono text-[10px] text-gray-400">切换</span>
          </button>
        ))}

      {section('工作树')}
      {refs === null && !refsLoading && (
        <div className="px-1 py-1 font-mono text-[10px] text-gray-400">读不到工作树列表。</div>
      )}
      {worktrees.map((worktree) => (
        <div key={worktree.path} className="flex items-start gap-1.5 rounded-control px-1 py-1.5">
          <FolderLink20Regular aria-hidden="true" className="mt-0.5 shrink-0 text-gray-500" />
          <div className="min-w-0 flex-1">
            <div className="truncate font-mono text-[10.5px] text-gray-800" title={worktree.path}>
              {worktree.path}
            </div>
            <div className="mt-0.5 truncate text-[10px] text-gray-400">
              {worktree.branch ?? '游离 HEAD'}
              {worktree.isMain ? ' · 主工作树' : ''}
              {forcePath === worktree.path ? ' · 再次点击将丢弃未提交改动' : ''}
            </div>
          </div>
          <button
            type="button"
            onClick={() => void runOpen(worktree.path)}
            disabled={actionBusy || !canWrite}
            title="把该工作树作为项目打开（会话、终端与 git 都跟随它）"
            className="ui-button ui-compact shrink-0"
          >
            打开
          </button>
          {!worktree.isMain && (
            <button
              type="button"
              onClick={() => void runRemove(worktree.path)}
              disabled={actionBusy || !canWrite}
              title={
                forcePath === worktree.path
                  ? '强制删除该工作树（未提交的改动会丢失）'
                  : '删除该工作树（有未提交改动时 git 会拒绝）'
              }
              className="ui-button ui-compact shrink-0 text-red-600"
            >
              {forcePath === worktree.path ? '强制删除' : '删除'}
            </button>
          )}
        </div>
      ))}

      {section('新建工作树')}
      <div className="flex items-center gap-1 px-1">
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') void runCreate();
          }}
          placeholder="分支名，不存在则新建"
          spellCheck={false}
          aria-label="工作树的分支名"
          className="ui-field min-w-0 flex-1 font-mono text-[11px]"
        />
        <button
          type="button"
          onClick={() => void runCreate()}
          disabled={actionBusy || !canWrite || draft.trim() === ''}
          title="在主仓库同级创建该分支的工作树，并切过去"
          className="ui-button ui-compact ui-primary shrink-0"
        >
          <Add20Regular aria-hidden="true" />
          创建
        </button>
      </div>
      <div className="px-1 pt-1 text-[10px] leading-snug text-gray-400">
        工作树固定在主仓库同级，目录名为{' '}
        <code className="font-mono text-gray-500">&lt;仓库&gt;@&lt;分支&gt;</code>
        （分支名中的 / 换成 -）。创建后作为项目打开。
      </div>

      <div className="mt-1 border-t border-line/60 pt-1">
        <button
          type="button"
          onClick={() => {
            clearActionError();
            // The explorer is the read-only view: it paints the changed files and
            // per-file diffs, never a write.
            openGitExplorer();
            close();
          }}
          className="ui-nav-row flex w-full items-center gap-1.5 px-1 py-1.5 text-left"
        >
          <History20Regular aria-hidden="true" className="shrink-0 text-gray-500" />
          <span className="min-w-0 flex-1 truncate text-[11px] text-gray-800">
            变更与历史（只读）
          </span>
        </button>
      </div>

      {current !== null && (
        <div className="flex items-center gap-1 px-1 pt-0.5 text-[10px] text-gray-400">
          <CheckmarkCircle20Regular aria-hidden="true" className="shrink-0 text-accent-fg" />
          <span className="truncate">切分支会改写工作区，未提交的改动由 git 决定是否允许。</span>
        </div>
      )}
    </ConsolePanel>
  );
};
