/**
 * The right dock's git history panel: its own store, and the availability source
 * the tab reads.
 *
 * Git history deliberately does **not** live in `useConsoleStore`: it is a second
 * view with its own lifecycle, and a page of commits, one commit's file list and
 * a lazily-read diff would otherwise be candidates for rebuilding the console
 * store that the transcript subscribes to.  The main store is read *from* here
 * (the workspace the session runs in) and never written.
 *
 * The drill-down position (`level`, `rev`, `sha`, `openFile`) lives here rather
 * than in the tab component on purpose: switching right-dock tabs unmounts the
 * panel, and a reader who comes back should land where they left.  The same
 * reason makes this the mobile-ready shape -- a narrow layout is another way to
 * paint this state, not another state.
 *
 * Every read is native-only.  There is no runtime RPC behind it (the daemon
 * serves `git.status` / `git.diff`, not history), so a plain browser hides the
 * tab instead of showing one that could never load; `hasNativeGitHistory()` is
 * what both this store and the tab's availability source ask.
 */
import { create } from 'zustand';
import {
  addGitWorktree,
  fetchGitCommit,
  fetchGitLog,
  fetchGitRefs,
  fetchGitStashDiff,
  hasNativeGitHistory,
  removeGitWorktree,
  switchGitBranch,
} from '../client/tauriGitFs.ts';
import {
  GIT_LOG_PAGE_SIZE,
  type GitCommitDetailView,
  type GitCommitView,
  type GitDiffView,
  type GitRefsView,
} from '../runtime-client/git.ts';
import { useConsoleStore } from './useConsoleStore.ts';

/** Which of the panel's three levels is painted. */
export type GitHistoryLevel = 'refs' | 'commits' | 'commit';

/** One lazily-read diff, keyed by the path (or stash index) it belongs to. */
export interface GitLazyDiff {
  diff: GitDiffView | null;
  loading: boolean;
  error: string | null;
}

/** Where a commit detail was opened from, so "back" returns there. */
export type GitDetailOrigin = 'refs' | 'commits';

export interface GitHistoryStoreState {
  // Level 1: branches, tags, stashes, worktrees.
  refs: GitRefsView | null;
  refsLoading: boolean;
  refsError: string | null;
  // Where we are, and the revision whose history is open.
  level: GitHistoryLevel;
  rev: string | null;
  // Level 2: one revision's commits.
  commits: GitCommitView[];
  more: boolean;
  logLoading: boolean;
  logError: string | null;
  firstParent: boolean;
  /** What the reader typed; applied on Enter. */
  pathFilter: string;
  /** What the loaded page was actually read with. */
  appliedPathFilter: string;
  // Level 3: one commit.
  sha: string | null;
  detailOrigin: GitDetailOrigin;
  detail: GitCommitDetailView | null;
  detailLoading: boolean;
  detailError: string | null;
  openFile: string | null;
  fileDiffs: Record<string, GitLazyDiff>;
  // Stashes are read on demand, from the list.
  openStash: number | null;
  stashDiffs: Record<number, GitLazyDiff>;
  // Writes (desktop only): one branch switch, one worktree add, one worktree
  // remove.  Everything else in this store reads.
  /** True while a write is in flight, so the menu can disable its rows. */
  actionBusy: boolean;
  /** The last write's refusal, verbatim from git. */
  actionError: string | null;
  clearActionError: () => void;
  /** Switch the workspace to `branch`; false when git refused. */
  switchBranch: (branch: string) => Promise<boolean>;
  /**
   * Create the worktree for `branch` and open it as the console's project.
   * `createBranch` makes the branch first when it does not exist yet.
   */
  createWorktree: (branch: string, createBranch: boolean) => Promise<boolean>;
  /** Remove one worktree; false when git refused (uncommitted work needs `force`). */
  removeWorktree: (path: string, force: boolean) => Promise<boolean>;

  loadRefs: () => Promise<void>;
  refresh: () => Promise<void>;
  openRev: (rev: string) => Promise<void>;
  backToRefs: () => void;
  loadMore: () => Promise<void>;
  /** Re-read the open revision's first page with the current filters. */
  reloadLog: () => Promise<void>;
  toggleFirstParent: () => Promise<void>;
  setPathFilter: (value: string) => void;
  applyPathFilter: () => Promise<void>;
  openCommit: (sha: string, origin: GitDetailOrigin) => Promise<void>;
  backToCommits: () => void;
  toggleFile: (path: string) => Promise<void>;
  /** Expand one stash and read its diff the first time it is expanded. */
  toggleStash: (index: number) => Promise<void>;
  closeStash: () => void;
  reset: () => void;
}

const EMPTY = {
  refs: null,
  refsLoading: false,
  refsError: null,
  level: 'refs' as GitHistoryLevel,
  rev: null,
  commits: [] as GitCommitView[],
  more: false,
  logLoading: false,
  logError: null,
  firstParent: false,
  pathFilter: '',
  appliedPathFilter: '',
  sha: null,
  detailOrigin: 'commits' as GitDetailOrigin,
  detail: null,
  detailLoading: false,
  detailError: null,
  openFile: null,
  fileDiffs: {} as Record<string, GitLazyDiff>,
  openStash: null,
  stashDiffs: {} as Record<number, GitLazyDiff>,
  actionBusy: false,
  actionError: null,
};

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** The workspace this session runs in, read from the console store. */
function workspacePathOf(): string {
  const state = useConsoleStore.getState();
  const activeProject = state.projects.find(
    (project) => project.project_id === state.activeProjectId,
  );
  return activeProject?.workspace_path || state.workspacePath || '';
}

/**
 * Reads are token-fenced: a page that arrives after the reader moved on (another
 * branch, another commit, another session) is dropped instead of repainting the
 * panel with the previous question's answer.
 */
let refsToken = 0;
let logToken = 0;
let detailToken = 0;

export const useGitHistoryStore = create<GitHistoryStoreState>((set, get) => ({
  ...EMPTY,

  loadRefs: async () => {
    if (!hasNativeGitHistory()) {
      // Not a git failure: this window has no desktop bridge, so there is no
      // native reader to ask.  Saying so is the point -- an empty inventory looks
      // exactly like a repository with no branches, and the reader cannot tell
      // "nothing to show" from "cannot look".
      set({
        refs: null,
        refsLoading: false,
        refsError: '当前窗口没有桌面壳桥接（__TAURI_INTERNALS__ 不可用），读不到分支与工作树',
      });
      return;
    }
    const token = ++refsToken;
    set({ refsLoading: true, refsError: null });
    try {
      const refs = await fetchGitRefs(workspacePathOf());
      if (token !== refsToken) return;
      set({ refs, refsLoading: false });
    } catch (error) {
      if (token !== refsToken) return;
      set({ refsLoading: false, refsError: messageOf(error) });
    }
  },

  refresh: async () => {
    await get().loadRefs();
    // HEAD may have moved (the agent commits, or a branch was switched), so the
    // open level is re-read rather than left showing the previous state.
    if (get().level === 'commits' && get().rev !== null) {
      await get().reloadLog();
    }
    if (get().level === 'commit' && get().sha !== null) {
      await get().openCommit(get().sha as string, get().detailOrigin);
    }
  },

  openRev: async (rev) => {
    set({
      level: 'commits',
      rev,
      commits: [],
      more: false,
      logError: null,
      logLoading: false,
      pathFilter: '',
      appliedPathFilter: '',
      firstParent: false,
    });
    await get().reloadLog();
  },

  backToRefs: () => {
    set({ level: 'refs', rev: null, sha: null, detail: null, detailError: null, openFile: null });
  },

  loadMore: async () => {
    const { rev, commits, more, logLoading } = get();
    if (rev === null || !more || logLoading) return;
    const token = ++logToken;
    set({ logLoading: true, logError: null });
    try {
      const page = await fetchGitLog(
        {
          rev,
          skip: commits.length,
          limit: GIT_LOG_PAGE_SIZE,
          firstParent: get().firstParent,
          path: get().appliedPathFilter || null,
        },
        workspacePathOf(),
      );
      if (token !== logToken) return;
      set((state) => ({
        commits: [...state.commits, ...page.commits],
        more: page.more,
        logLoading: false,
      }));
    } catch (error) {
      if (token !== logToken) return;
      set({ logLoading: false, logError: messageOf(error) });
    }
  },

  toggleFirstParent: async () => {
    set((state) => ({ firstParent: !state.firstParent }));
    await get().reloadLog();
  },

  reloadLog: async () => {
    const { rev } = get();
    if (rev === null) return;
    const token = ++logToken;
    set({ logLoading: true, logError: null, commits: [], more: false });
    try {
      const page = await fetchGitLog(
        {
          rev,
          skip: 0,
          limit: GIT_LOG_PAGE_SIZE,
          firstParent: get().firstParent,
          path: get().appliedPathFilter || null,
        },
        workspacePathOf(),
      );
      if (token !== logToken) return;
      set({ commits: page.commits, more: page.more, logLoading: false });
    } catch (error) {
      if (token !== logToken) return;
      set({ logLoading: false, logError: messageOf(error) });
    }
  },

  setPathFilter: (value) => set({ pathFilter: value }),

  applyPathFilter: async () => {
    set((state) => ({ appliedPathFilter: state.pathFilter }));
    await get().reloadLog();
  },

  openCommit: async (sha, origin) => {
    const token = ++detailToken;
    set({
      level: 'commit',
      sha,
      detailOrigin: origin,
      detail: null,
      detailError: null,
      detailLoading: true,
      openFile: null,
      fileDiffs: {},
    });
    try {
      const detail = await fetchGitCommit(sha, undefined, workspacePathOf());
      if (token !== detailToken) return;
      set({ detail, detailLoading: false });
    } catch (error) {
      if (token !== detailToken) return;
      set({ detailLoading: false, detailError: messageOf(error) });
    }
  },

  backToCommits: () => {
    set({ level: 'commits', sha: null, detail: null, detailError: null, openFile: null, fileDiffs: {} });
  },

  toggleFile: async (path) => {
    const { openFile, sha, fileDiffs } = get();
    if (openFile === path) {
      set({ openFile: null });
      return;
    }
    set({ openFile: path });
    // One file's diff is read the first time it is expanded, and kept: collapsing
    // and re-expanding it must not shell out to git again.
    if (sha === null || fileDiffs[path]?.diff) return;
    set((state) => ({
      fileDiffs: { ...state.fileDiffs, [path]: { diff: null, loading: true, error: null } },
    }));
    try {
      // The same command answers the file list again; the list is left as it is,
      // so a truncation notice cannot flicker away on one file's read.
      const detail = await fetchGitCommit(sha, path, workspacePathOf());
      set((state) => ({
        fileDiffs: {
          ...state.fileDiffs,
          [path]: { diff: detail.diff, loading: false, error: null },
        },
      }));
    } catch (error) {
      set((state) => ({
        fileDiffs: {
          ...state.fileDiffs,
          [path]: { diff: null, loading: false, error: messageOf(error) },
        },
      }));
    }
  },

  toggleStash: async (index) => {
    if (get().openStash === index) {
      set({ openStash: null });
      return;
    }
    set({ openStash: index });
    if (get().stashDiffs[index]?.diff) return;
    set((state) => ({
      stashDiffs: { ...state.stashDiffs, [index]: { diff: null, loading: true, error: null } },
    }));
    try {
      const diff = await fetchGitStashDiff(index, workspacePathOf());
      set((state) => ({
        stashDiffs: { ...state.stashDiffs, [index]: { diff, loading: false, error: null } },
      }));
    } catch (error) {
      set((state) => ({
        stashDiffs: {
          ...state.stashDiffs,
          [index]: { diff: null, loading: false, error: messageOf(error) },
        },
      }));
    }
  },

  closeStash: () => set({ openStash: null }),

  clearActionError: () => set({ actionError: null }),

  switchBranch: async (branch) => {
    const workspace = workspacePathOf();
    set({ actionBusy: true, actionError: null });
    try {
      await switchGitBranch(branch, false, workspace);
      // HEAD moved under both this panel and the header's chrome, so both are
      // re-read instead of being left describing the branch that is gone.
      await get().refresh();
      await useConsoleStore.getState().loadGitStatus();
      return true;
    } catch (error) {
      set({ actionError: messageOf(error) });
      return false;
    } finally {
      // Held until the reads settle: a second click while the refs still describe
      // the branch just left would ask git for it again.
      set({ actionBusy: false });
    }
  },

  createWorktree: async (branch, createBranch) => {
    const workspace = workspacePathOf();
    set({ actionBusy: true, actionError: null });
    try {
      const added = await addGitWorktree(branch, createBranch, workspace);
      // A project *is* a directory, so the new worktree becomes one and the
      // console moves there: its agent root, its own `.synapse/` state and every
      // later read belong to that worktree without a second mechanism.
      const failure = await useConsoleStore.getState().openWorkspaceProject(added.path);
      if (failure === null) return true;
      // The worktree exists but could not be opened.  Re-read the inventory so
      // its row is there to click, instead of leaving the reader to retry
      // "create" and be told the directory already exists.
      set({ actionError: failure });
      await get().refresh();
      return false;
    } catch (error) {
      set({ actionError: messageOf(error) });
      return false;
    } finally {
      set({ actionBusy: false });
    }
  },

  removeWorktree: async (path, force) => {
    const workspace = workspacePathOf();
    set({ actionBusy: true, actionError: null });
    try {
      await removeGitWorktree(path, force, workspace);
      await get().refresh();
      // The removed directory may still be a registered project: there is no
      // `runtime.project.remove` contract yet, so its row survives.  Re-creating
      // a worktree for the same branch restores the same fixed path, which makes
      // that row valid again -- the row is stale, never wrong.
      await useConsoleStore.getState().loadProjects();
      return true;
    } catch (error) {
      set({ actionError: messageOf(error) });
      return false;
    } finally {
      set({ actionBusy: false });
    }
  },

  reset: () => {
    refsToken += 1;
    logToken += 1;
    detailToken += 1;
    set({ ...EMPTY });
  },
}));

/**
 * The dynamic availability source the right dock's tab reads.
 *
 * The desktop shell injects `__TAURI_INTERNALS__` before the console's own
 * scripts run, so the verdict does not change while the page is open and the
 * subscription is a no-op rather than a poll.
 */
export const gitHistoryAvailability = {
  getSnapshot: () => hasNativeGitHistory(),
  subscribe: () => () => {},
};
