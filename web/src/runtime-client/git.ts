/**
 * Strict decoders for the read-only git surface: the runtime wire's
 * `runtime.git.status` / `runtime.git.diff`, and the desktop shell's own history
 * reads (branches, tags, stashes, worktrees, a revision's commits, one commit's
 * files).
 *
 * Both surfaces are decoded here because they share the rule: only the declared
 * keys are accepted, every value is type-checked, and anything else raises
 * instead of reaching the UI as a half-shaped object.  Pure and dependency-free
 * so it runs under `node --test`.
 */

export interface GitFileChangeView {
  path: string;
  /** git's staged column (`M`, `A`, `?`, …), verbatim. */
  indexStatus: string;
  /** git's unstaged column, verbatim. */
  worktreeStatus: string;
}

export interface GitStatusView {
  branch: string | null;
  upstream: string | null;
  ahead: number;
  behind: number;
  dirty: boolean;
  files: GitFileChangeView[];
  truncated: boolean;
  /** Tracked added lines vs HEAD; null when git could not answer. */
  insertions: number | null;
  /** Tracked removed lines vs HEAD; null when git could not answer. */
  deletions: number | null;
}

export interface GitDiffView {
  path: string;
  text: string;
  binary: boolean;
  truncated: boolean;
  empty: boolean;
}

export class MalformedGitPayloadError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'MalformedGitPayloadError';
  }
}

function asRecord(value: unknown, what: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new MalformedGitPayloadError(`${what} must be an object`);
  }
  return value as Record<string, unknown>;
}

function exactKeys(record: Record<string, unknown>, keys: readonly string[], what: string): void {
  const actual = Object.keys(record).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, i) => key !== expected[i])) {
    throw new MalformedGitPayloadError(`${what} has unexpected keys`);
  }
}

function text(value: unknown, what: string): string {
  if (typeof value !== 'string') throw new MalformedGitPayloadError(`${what} must be a string`);
  return value;
}

function nullableText(value: unknown, what: string): string | null {
  if (value === null) return null;
  return text(value, what);
}

function count(value: unknown, what: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) {
    throw new MalformedGitPayloadError(`${what} must be a non-negative integer`);
  }
  return value;
}

/** A line count that is null when git could not answer, never a fabricated 0. */
function nullableCount(value: unknown, what: string): number | null {
  if (value === null) return null;
  return count(value, what);
}

function flag(value: unknown, what: string): boolean {
  if (typeof value !== 'boolean') throw new MalformedGitPayloadError(`${what} must be a boolean`);
  return value;
}

const FILE_KEYS = ['path', 'index_status', 'worktree_status'] as const;
const STATUS_KEYS = [
  'branch',
  'upstream',
  'ahead',
  'behind',
  'dirty',
  'files',
  'truncated',
  'insertions',
  'deletions',
] as const;
const DIFF_KEYS = ['path', 'text', 'binary', 'truncated', 'empty'] as const;

/** Decode one `runtime.git.status` result. */
export function parseGitStatus(payload: unknown): GitStatusView {
  const record = asRecord(payload, 'git status');
  exactKeys(record, STATUS_KEYS, 'git status');
  if (!Array.isArray(record['files'])) {
    throw new MalformedGitPayloadError('git status files must be an array');
  }
  const files = record['files'].map((entry) => {
    const file = asRecord(entry, 'git file change');
    exactKeys(file, FILE_KEYS, 'git file change');
    return {
      path: text(file['path'], 'git file path'),
      indexStatus: text(file['index_status'], 'git index status'),
      worktreeStatus: text(file['worktree_status'], 'git worktree status'),
    };
  });
  return {
    branch: nullableText(record['branch'], 'git branch'),
    upstream: nullableText(record['upstream'], 'git upstream'),
    ahead: count(record['ahead'], 'git ahead'),
    behind: count(record['behind'], 'git behind'),
    dirty: flag(record['dirty'], 'git dirty'),
    files,
    truncated: flag(record['truncated'], 'git truncated'),
    insertions: nullableCount(record['insertions'], 'git insertions'),
    deletions: nullableCount(record['deletions'], 'git deletions'),
  };
}

/** Decode one `runtime.git.diff` result. */
export function parseGitDiff(payload: unknown): GitDiffView {
  const record = asRecord(payload, 'git diff');
  exactKeys(record, DIFF_KEYS, 'git diff');
  return {
    path: text(record['path'], 'git diff path'),
    text: text(record['text'], 'git diff text'),
    binary: flag(record['binary'], 'git diff binary'),
    truncated: flag(record['truncated'], 'git diff truncated'),
    empty: flag(record['empty'], 'git diff empty'),
  };
}

/**
 * Tailwind class for one unified-diff line, by its role.
 *
 * Lives here rather than in the component so the explorer file exports only
 * components (fast refresh) and the rule stays testable without a DOM.
 */
export function diffLineClass(line: string): string {
  if (line.startsWith('+++') || line.startsWith('---')) return 'text-gray-500';
  if (line.startsWith('@@')) return 'text-blue-700';
  if (line.startsWith('+')) return 'text-green-700';
  if (line.startsWith('-')) return 'text-red-700';
  return 'text-gray-700';
}


/**
 * Two-letter porcelain status for one change, the way `git status --short`
 * prints it (`M ` staged, ` M` worktree, `??` untracked).
 */
export function changeStatusCode(change: GitFileChangeView): string {
  const index =
    change.indexStatus ??
    (change as unknown as { index_status?: string }).index_status ??
    ' ';
  const worktree =
    change.worktreeStatus ??
    (change as unknown as { worktree_status?: string }).worktree_status ??
    ' ';
  return `${index}${worktree}`;
}

/** Human label for one change code. */
export function changeStatusLabel(change: GitFileChangeView): string {
  const code = changeStatusCode(change);
  if (code.includes('?')) return '未跟踪';
  const index = code[0] ?? ' ';
  const worktree = code[1] ?? ' ';
  if (index !== ' ' && worktree !== ' ') return '已暂存+已修改';
  if (index !== ' ') return '已暂存';
  if (worktree === 'D') return '已删除';
  if (worktree === 'A') return '新文件';
  return '已修改';
}

/* ---------------------------------------------------------------------------
 * The desktop shell's history surface.
 *
 * These payloads never cross the runtime wire: they are the answer to the
 * shell's own `git` calls (`rust/synapse-gui/src/git_fs.rs`, exposed as the
 * `tauri_git_*` commands). They are decoded with the same strictness as the wire
 * ones, and the shell adds one rule of its own -- a revision and a path are
 * validated before git sees them -- so a shape decoded here can only carry what
 * the caller asked for.
 * ------------------------------------------------------------------------- */

/** The page size the history reader asks for when the caller does not choose. */
export const GIT_LOG_PAGE_SIZE = 50;

export interface GitBranchView {
  name: string;
  /** `local` or `remote`. */
  kind: 'local' | 'remote';
  tipSha: string;
  /** The tracking branch, or null when this branch has no upstream. */
  upstream: string | null;
  ahead: number;
  behind: number;
  tipDate: string;
  tipSubject: string;
  /** True for the branch `HEAD` currently points at. */
  isHead: boolean;
}

export interface GitTagView {
  name: string;
  /** The commit the tag points at, never the annotated tag object itself. */
  targetSha: string;
  annotated: boolean;
  date: string;
  subject: string;
}

export interface GitStashView {
  index: number;
  name: string;
  message: string;
}

export interface GitWorktreeView {
  /** Absolute path, exactly as `git worktree list` reports it. */
  path: string;
  headSha: string;
  branch: string | null;
  isMain: boolean;
}

export interface GitRefsView {
  current: string | null;
  branches: GitBranchView[];
  tags: GitTagView[];
  stashes: GitStashView[];
  worktrees: GitWorktreeView[];
  truncated: boolean;
}

export interface GitCommitView {
  sha: string;
  shortSha: string;
  parents: string[];
  author: string;
  authoredAt: string;
  subject: string;
}

export interface GitLogView {
  /** The revision the page was read for. */
  rev: string;
  commits: GitCommitView[];
  /** True when the page is not the end of the history. */
  more: boolean;
}

export interface GitCommitFileView {
  path: string;
  /** git's own status letter (`A`, `M`, `D`, `R100`, …), verbatim. */
  status: string;
  /** Set only for a rename or a copy. */
  oldPath: string | null;
  insertions: number | null;
  deletions: number | null;
  /** A binary change carries no line counts. */
  binary: boolean;
}

export interface GitCommitDetailView {
  sha: string;
  shortSha: string;
  parents: string[];
  author: string;
  authoredAt: string;
  subject: string;
  body: string;
  files: GitCommitFileView[];
  insertions: number;
  deletions: number;
  truncated: boolean;
  /** Present only when one path was asked for. */
  diff: GitDiffView | null;
}

const BRANCH_KEYS = [
  'name',
  'kind',
  'tip_sha',
  'upstream',
  'ahead',
  'behind',
  'tip_date',
  'tip_subject',
  'is_head',
] as const;
const TAG_KEYS = ['name', 'target_sha', 'annotated', 'date', 'subject'] as const;
const STASH_KEYS = ['index', 'name', 'message'] as const;
const WORKTREE_KEYS = ['path', 'head_sha', 'branch', 'is_main'] as const;
const REFS_KEYS = ['current', 'branches', 'tags', 'stashes', 'worktrees', 'truncated'] as const;
const COMMIT_KEYS = ['sha', 'short_sha', 'parents', 'author', 'authored_at', 'subject'] as const;
const LOG_KEYS = ['rev', 'commits', 'more'] as const;
const COMMIT_FILE_KEYS = [
  'path',
  'status',
  'old_path',
  'insertions',
  'deletions',
  'binary',
] as const;
const COMMIT_DETAIL_KEYS = [
  'sha',
  'short_sha',
  'parents',
  'author',
  'authored_at',
  'subject',
  'body',
  'files',
  'insertions',
  'deletions',
  'truncated',
  'diff',
] as const;

function entries(value: unknown, what: string): unknown[] {
  if (!Array.isArray(value)) throw new MalformedGitPayloadError(`${what} must be an array`);
  return value;
}

function texts(value: unknown, what: string): string[] {
  return entries(value, what).map((entry) => text(entry, what));
}

function branchKind(value: unknown, what: string): 'local' | 'remote' {
  const raw = text(value, what);
  if (raw !== 'local' && raw !== 'remote') {
    throw new MalformedGitPayloadError(`${what} must be "local" or "remote"`);
  }
  return raw;
}

function parseCommit(entry: unknown): GitCommitView {
  const commit = asRecord(entry, 'git commit');
  exactKeys(commit, COMMIT_KEYS, 'git commit');
  return {
    sha: text(commit['sha'], 'git commit sha'),
    shortSha: text(commit['short_sha'], 'git commit short sha'),
    parents: texts(commit['parents'], 'git commit parents'),
    author: text(commit['author'], 'git commit author'),
    authoredAt: text(commit['authored_at'], 'git commit date'),
    subject: text(commit['subject'], 'git commit subject'),
  };
}

/** Decode one `tauri_git_refs` result. */
export function parseGitRefs(payload: unknown): GitRefsView {
  const record = asRecord(payload, 'git refs');
  exactKeys(record, REFS_KEYS, 'git refs');
  return {
    current: nullableText(record['current'], 'git current branch'),
    branches: entries(record['branches'], 'git branches').map((entry) => {
      const branch = asRecord(entry, 'git branch');
      exactKeys(branch, BRANCH_KEYS, 'git branch');
      return {
        name: text(branch['name'], 'git branch name'),
        kind: branchKind(branch['kind'], 'git branch kind'),
        tipSha: text(branch['tip_sha'], 'git branch tip sha'),
        upstream: nullableText(branch['upstream'], 'git branch upstream'),
        ahead: count(branch['ahead'], 'git branch ahead'),
        behind: count(branch['behind'], 'git branch behind'),
        tipDate: text(branch['tip_date'], 'git branch tip date'),
        tipSubject: text(branch['tip_subject'], 'git branch tip subject'),
        isHead: flag(branch['is_head'], 'git branch is_head'),
      };
    }),
    tags: entries(record['tags'], 'git tags').map((entry) => {
      const tag = asRecord(entry, 'git tag');
      exactKeys(tag, TAG_KEYS, 'git tag');
      return {
        name: text(tag['name'], 'git tag name'),
        targetSha: text(tag['target_sha'], 'git tag target'),
        annotated: flag(tag['annotated'], 'git tag annotated'),
        date: text(tag['date'], 'git tag date'),
        subject: text(tag['subject'], 'git tag subject'),
      };
    }),
    stashes: entries(record['stashes'], 'git stashes').map((entry) => {
      const stash = asRecord(entry, 'git stash');
      exactKeys(stash, STASH_KEYS, 'git stash');
      return {
        index: count(stash['index'], 'git stash index'),
        name: text(stash['name'], 'git stash name'),
        message: text(stash['message'], 'git stash message'),
      };
    }),
    worktrees: entries(record['worktrees'], 'git worktrees').map((entry) => {
      const worktree = asRecord(entry, 'git worktree');
      exactKeys(worktree, WORKTREE_KEYS, 'git worktree');
      return {
        path: text(worktree['path'], 'git worktree path'),
        headSha: text(worktree['head_sha'], 'git worktree head'),
        branch: nullableText(worktree['branch'], 'git worktree branch'),
        isMain: flag(worktree['is_main'], 'git worktree is_main'),
      };
    }),
    truncated: flag(record['truncated'], 'git refs truncated'),
  };
}

/** Decode one `tauri_git_log` result. */
export function parseGitLog(payload: unknown): GitLogView {
  const record = asRecord(payload, 'git log');
  exactKeys(record, LOG_KEYS, 'git log');
  return {
    rev: text(record['rev'], 'git log rev'),
    commits: entries(record['commits'], 'git log commits').map(parseCommit),
    more: flag(record['more'], 'git log more'),
  };
}

/** Decode one `tauri_git_commit` result. */
export function parseGitCommitDetail(payload: unknown): GitCommitDetailView {
  const record = asRecord(payload, 'git commit detail');
  exactKeys(record, COMMIT_DETAIL_KEYS, 'git commit detail');
  return {
    sha: text(record['sha'], 'git commit sha'),
    shortSha: text(record['short_sha'], 'git commit short sha'),
    parents: texts(record['parents'], 'git commit parents'),
    author: text(record['author'], 'git commit author'),
    authoredAt: text(record['authored_at'], 'git commit date'),
    subject: text(record['subject'], 'git commit subject'),
    body: text(record['body'], 'git commit body'),
    files: entries(record['files'], 'git commit files').map((entry) => {
      const file = asRecord(entry, 'git commit file');
      exactKeys(file, COMMIT_FILE_KEYS, 'git commit file');
      return {
        path: text(file['path'], 'git commit file path'),
        status: text(file['status'], 'git commit file status'),
        oldPath: nullableText(file['old_path'], 'git commit file old path'),
        insertions: nullableCount(file['insertions'], 'git commit file insertions'),
        deletions: nullableCount(file['deletions'], 'git commit file deletions'),
        binary: flag(file['binary'], 'git commit file binary'),
      };
    }),
    insertions: count(record['insertions'], 'git commit insertions'),
    deletions: count(record['deletions'], 'git commit deletions'),
    truncated: flag(record['truncated'], 'git commit truncated'),
    diff: record['diff'] === null ? null : parseGitDiff(record['diff']),
  };
}
