/**
 * Strict decoders for the read-only git surface (`runtime.git.status` /
 * `runtime.git.diff`).
 *
 * Same rule as the artifact decoders: only the declared keys are accepted, every
 * value is type-checked, and anything else raises instead of reaching the UI as
 * a half-shaped object.  Pure and dependency-free so it runs under `node --test`.
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
  return `${change.indexStatus}${change.worktreeStatus}`;
}

/** Human label for one change code. */
export function changeStatusLabel(change: GitFileChangeView): string {
  const code = changeStatusCode(change);
  if (code === '??') return '未跟踪';
  if (change.indexStatus !== ' ' && change.worktreeStatus !== ' ') return '已暂存+已修改';
  if (change.indexStatus !== ' ') return '已暂存';
  if (change.worktreeStatus === 'D') return '已删除';
  return '已修改';
}
