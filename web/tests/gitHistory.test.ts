/**
 * Tests for the desktop shell's git history surface.
 *
 * Verifies:
 * - the native payload decoders (branches/tags/stashes/worktrees, a log page,
 *   one commit) accept what the shell sends and refuse everything else;
 * - the history fetchers are native-only: outside the desktop shell they raise a
 *   typed error instead of reaching for a runtime RPC that does not exist, and
 *   the availability source the tab reads says the same thing;
 * - the panel's store pages a history, re-reads page 0 when a filter changes,
 *   and reads one file's diff once;
 * - the panel stays read-only, and never derives a commit's files from the
 *   workspace's standing `git status`.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  GitHistoryUnavailableError,
  fetchGitCommit,
  fetchGitLog,
  fetchGitRefs,
  fetchGitStashDiff,
  hasNativeGitHistory,
} from '../src/client/tauriGitFs.ts';
import { parseGitCommitDetail, parseGitLog, parseGitRefs } from '../src/runtime-client/git.ts';
import { gitHistoryAvailability, useGitHistoryStore } from '../src/stores/gitHistoryStore.ts';
import { useConsoleStore } from '../src/stores/useConsoleStore.ts';

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = join(here, '..');

const REFS_PAYLOAD = {
  current: 'main',
  branches: [
    {
      name: 'main',
      kind: 'local',
      tip_sha: '0e2541a',
      upstream: 'origin/main',
      ahead: 1,
      behind: 2,
      tip_date: '2026-09-27T22:30:47+08:00',
      tip_subject: 'fold the boundary',
      is_head: true,
    },
    {
      name: 'origin/main',
      kind: 'remote',
      tip_sha: '0e2541a',
      upstream: null,
      ahead: 0,
      behind: 0,
      tip_date: '2026-09-27T22:30:47+08:00',
      tip_subject: 'fold the boundary',
      is_head: false,
    },
  ],
  tags: [
    {
      name: 'v1.0.0',
      target_sha: '13b42d3',
      annotated: true,
      date: '2026-09-24T10:21:17+08:00',
      subject: 'release 1.0.0',
    },
  ],
  stashes: [{ index: 0, name: 'stash@{0}', message: 'On main: wip' }],
  worktrees: [{ path: 'F:/w', head_sha: '0e2541a', branch: 'main', is_main: true }],
  truncated: false,
};

const COMMIT_PAYLOAD = {
  sha: 'abc1234def',
  short_sha: 'abc1234',
  parents: ['def5678'],
  author: 'alex',
  authored_at: '2026-09-27T22:30:47+08:00',
  subject: 'a commit',
  body: '',
  files: [
    {
      path: 'new/a.ts',
      status: 'R100',
      old_path: 'old/a.ts',
      insertions: 2,
      deletions: 1,
      binary: false,
    },
  ],
  insertions: 2,
  deletions: 1,
  truncated: false,
  diff: null,
};

const LOG_PAYLOAD = {
  rev: 'main',
  commits: [
    {
      sha: 'abc1234def',
      short_sha: 'abc1234',
      parents: ['def5678'],
      author: 'alex',
      authored_at: '2026-09-27T22:30:47+08:00',
      subject: 'a commit',
    },
  ],
  more: true,
};

const DIFF_PAYLOAD = {
  path: 'stash@{0}',
  text: '@@ -1 +1 @@\n-one\n+two\n',
  binary: false,
  truncated: false,
  empty: false,
};

interface Invocation {
  command: string;
  args: Record<string, unknown>;
}

/**
 * Install a fake `__TAURI_INTERNALS__` and collect what the panel asks for.
 *
 * `isTauri()` reads `window`, so the global is replaced for the duration of one
 * test and restored afterwards; nothing here touches the real shell.
 */
function withTauri(handler: (command: string, args: Record<string, unknown>) => unknown): {
  calls: Invocation[];
  restore: () => void;
} {
  const calls: Invocation[] = [];
  const global = globalThis as unknown as { window?: unknown };
  const original = global.window;
  global.window = {
    __TAURI_INTERNALS__: {
      invoke: async (command: string, args: Record<string, unknown> = {}) => {
        calls.push({ command, args });
        return handler(command, args);
      },
    },
  };
  return {
    calls,
    restore: () => {
      global.window = original;
    },
  };
}

/** Drop the desktop shell, so a test starts from the browser's point of view. */
function withoutTauri(): () => void {
  const global = globalThis as unknown as { window?: unknown };
  const original = global.window;
  global.window = {};
  return () => {
    global.window = original;
  };
}

test('a refs payload decodes into camelCase fields', () => {
  const refs = parseGitRefs(REFS_PAYLOAD);
  assert.equal(refs.current, 'main');
  assert.equal(refs.branches.length, 2);
  assert.equal(refs.branches[0]?.kind, 'local');
  assert.equal(refs.branches[0]?.isHead, true);
  assert.equal(refs.branches[0]?.upstream, 'origin/main');
  assert.equal(refs.branches[0]?.ahead, 1);
  assert.equal(refs.branches[0]?.behind, 2);
  assert.equal(refs.branches[1]?.upstream, null, 'a remote branch has no upstream');
  assert.equal(refs.tags[0]?.targetSha, '13b42d3');
  assert.equal(refs.tags[0]?.annotated, true);
  assert.equal(refs.stashes[0]?.index, 0);
  assert.equal(refs.worktrees[0]?.isMain, true);
  assert.equal(refs.truncated, false);
});

test('a malformed refs payload is refused, never half-read', () => {
  assert.throws(() => parseGitRefs({ ...REFS_PAYLOAD, extra: 1 }), /unexpected keys/);
  assert.throws(() => parseGitRefs({ ...REFS_PAYLOAD, branches: 'main' }), /must be an array/);
  assert.throws(
    () =>
      parseGitRefs({
        ...REFS_PAYLOAD,
        branches: [{ ...REFS_PAYLOAD.branches[0], kind: 'tag' }],
      }),
    /"local" or "remote"/,
  );
  assert.throws(
    () => parseGitRefs({ ...REFS_PAYLOAD, tags: [{ name: 'v1' }] }),
    /unexpected keys/,
  );
  // A negative or fractional count is not a count.
  assert.throws(
    () =>
      parseGitRefs({
        ...REFS_PAYLOAD,
        branches: [{ ...REFS_PAYLOAD.branches[0], ahead: -1 }],
      }),
    /non-negative integer/,
  );
});

test('a log page decodes, and its shape is pinned', () => {
  const page = parseGitLog(LOG_PAYLOAD);
  assert.equal(page.rev, 'main');
  assert.equal(page.more, true);
  assert.equal(page.commits[0]?.shortSha, 'abc1234');
  assert.deepEqual(page.commits[0]?.parents, ['def5678']);

  assert.throws(() => parseGitLog({ ...LOG_PAYLOAD, more: 'yes' }), /must be a boolean/);
  assert.throws(() => parseGitLog({ ...LOG_PAYLOAD, rev: 7 }), /must be a string/);
  assert.throws(() => parseGitLog({ ...LOG_PAYLOAD, commits: [{}] }), /unexpected keys/);
});

test('a commit detail decodes its files, and its diff only when one was read', () => {
  const detail = parseGitCommitDetail(COMMIT_PAYLOAD);
  assert.equal(detail.subject, 'a commit');
  assert.equal(detail.diff, null, 'no path was asked for, so there is no diff');
  assert.equal(detail.files[0]?.status, 'R100');
  assert.equal(detail.files[0]?.oldPath, 'old/a.ts');
  assert.equal(detail.files[0]?.insertions, 2);

  const withDiff = parseGitCommitDetail({ ...COMMIT_PAYLOAD, diff: DIFF_PAYLOAD });
  assert.equal(withDiff.diff?.text.includes('+two'), true);
  assert.equal(withDiff.diff?.binary, false);

  // A binary change carries no line counts: null, never a fabricated zero.
  const binary = parseGitCommitDetail({
    ...COMMIT_PAYLOAD,
    files: [{ ...COMMIT_PAYLOAD.files[0], insertions: null, deletions: null, binary: true }],
  });
  assert.equal(binary.files[0]?.insertions, null);
  assert.equal(binary.files[0]?.binary, true);

  assert.throws(
    () => parseGitCommitDetail({ ...COMMIT_PAYLOAD, diff: { path: 'a.ts' } }),
    /unexpected keys/,
  );
  assert.throws(() => parseGitCommitDetail({ ...COMMIT_PAYLOAD, insertions: null }), /non-negative/);
});

test('the history fetchers are native-only and name their commands', async () => {
  const restore = withoutTauri();
  try {
    assert.equal(hasNativeGitHistory(), false);
    assert.equal(gitHistoryAvailability.getSnapshot(), false);
    await assert.rejects(() => fetchGitRefs('/w'), GitHistoryUnavailableError);
    await assert.rejects(() => fetchGitLog({ rev: 'main' }, '/w'), GitHistoryUnavailableError);
    await assert.rejects(() => fetchGitCommit('abc', undefined, '/w'), GitHistoryUnavailableError);
    await assert.rejects(() => fetchGitStashDiff(0, '/w'), GitHistoryUnavailableError);
  } finally {
    restore();
  }

  const shell = withTauri((command) => {
    if (command === 'tauri_git_refs') return REFS_PAYLOAD;
    if (command === 'tauri_git_log') return LOG_PAYLOAD;
    if (command === 'tauri_git_commit') return COMMIT_PAYLOAD;
    if (command === 'tauri_git_stash') return DIFF_PAYLOAD;
    throw new Error(`unexpected command ${command}`);
  });
  try {
    assert.equal(hasNativeGitHistory(), true);
    assert.equal(gitHistoryAvailability.getSnapshot(), true);

    const refs = await fetchGitRefs('/w');
    assert.equal(refs.current, 'main');

    await fetchGitLog({ rev: 'main', skip: 50, firstParent: true, path: 'a.ts' }, '/w');
    await fetchGitCommit('abc', 'a.ts', '/w');
    const stashDiff = await fetchGitStashDiff(0, '/w');
    assert.equal(stashDiff.path, 'stash@{0}');

    assert.deepEqual(
      shell.calls.map((call) => call.command),
      ['tauri_git_refs', 'tauri_git_log', 'tauri_git_commit', 'tauri_git_stash'],
    );
    assert.deepEqual(shell.calls[1]?.args, {
      workspace: '/w',
      rev: 'main',
      limit: 50,
      skip: 50,
      // The shell's command is `rename_all = "snake_case"`, so this key is not
      // camelCased on the way out.
      first_parent: true,
      path: 'a.ts',
    });
    assert.deepEqual(shell.calls[2]?.args, { workspace: '/w', sha: 'abc', path: 'a.ts' });
    assert.deepEqual(shell.calls[3]?.args, { workspace: '/w', index: 0 });
  } finally {
    shell.restore();
  }
});

test('the panel pages a history and reads one file diff once', async () => {
  useConsoleStore.setState({ workspacePath: '/w' });
  useGitHistoryStore.getState().reset();

  const secondPage = {
    rev: 'main',
    commits: [{ ...LOG_PAYLOAD.commits[0], sha: 'fff9999', short_sha: 'fff9999' }],
    more: false,
  };
  const shell = withTauri((command, args) => {
    if (command === 'tauri_git_refs') return REFS_PAYLOAD;
    if (command === 'tauri_git_log') return args.skip === 0 ? LOG_PAYLOAD : secondPage;
    if (command === 'tauri_git_commit') {
      return { ...COMMIT_PAYLOAD, diff: args.path === undefined ? null : DIFF_PAYLOAD };
    }
    throw new Error(`unexpected command ${command}`);
  });
  try {
    const store = useGitHistoryStore.getState();
    await store.loadRefs();
    assert.equal(useGitHistoryStore.getState().refs?.current, 'main');

    await useGitHistoryStore.getState().openRev('main');
    let state = useGitHistoryStore.getState();
    assert.equal(state.level, 'commits');
    assert.equal(state.commits.length, 1);
    assert.equal(state.more, true);

    await state.loadMore();
    state = useGitHistoryStore.getState();
    assert.equal(state.commits.length, 2, 'the second page is appended');
    assert.equal(state.more, false);
    assert.equal(shell.calls.filter((call) => call.command === 'tauri_git_log')[1]?.args.skip, 1);

    // A commit is opened from the list, then one file is expanded twice.
    await useGitHistoryStore.getState().openCommit('abc1234def', 'commits');
    await useGitHistoryStore.getState().toggleFile('new/a.ts');
    state = useGitHistoryStore.getState();
    assert.equal(state.openFile, 'new/a.ts');
    assert.equal(state.fileDiffs['new/a.ts']?.diff?.path, 'stash@{0}');

    await useGitHistoryStore.getState().toggleFile('new/a.ts');
    await useGitHistoryStore.getState().toggleFile('new/a.ts');
    assert.equal(
      shell.calls.filter((call) => call.command === 'tauri_git_commit').length,
      2,
      'one read for the commit and one for its file, never one per expand',
    );

    // The toggle re-reads page 0 with `--first-parent`, not the next page.
    useGitHistoryStore.getState().backToCommits();
    await useGitHistoryStore.getState().toggleFirstParent();
    state = useGitHistoryStore.getState();
    assert.equal(state.firstParent, true);
    const lastLog = shell.calls.filter((call) => call.command === 'tauri_git_log').at(-1);
    assert.equal(lastLog?.args.skip, 0);
    assert.equal(lastLog?.args.first_parent, true);
  } finally {
    shell.restore();
    useGitHistoryStore.getState().reset();
  }
});

test('a session switch starts the panel over', async () => {
  useConsoleStore.setState({ workspacePath: '/w' });
  const shell = withTauri((command) => {
    if (command === 'tauri_git_refs') return REFS_PAYLOAD;
    if (command === 'tauri_git_log') return LOG_PAYLOAD;
    throw new Error(`unexpected command ${command}`);
  });
  try {
    await useGitHistoryStore.getState().loadRefs();
    await useGitHistoryStore.getState().openRev('main');
    assert.equal(useGitHistoryStore.getState().level, 'commits');

    useGitHistoryStore.getState().reset();
    const state = useGitHistoryStore.getState();
    assert.equal(state.level, 'refs');
    assert.equal(state.refs, null);
    assert.deepEqual(state.commits, []);
    assert.equal(state.rev, null);
  } finally {
    shell.restore();
  }
});

test('the panel stays read-only and never derives a commit from git status', () => {
  const tab = readFileSync(join(webRoot, 'src', 'components', 'rightDock', 'gitTab.tsx'), 'utf8');
  for (const write of ['gitAdd', 'gitCommit', 'gitCheckout', 'gitStage', 'gitPush', 'gitReset']) {
    assert.ok(!tab.includes(write), `the git panel must not write: ${write}`);
  }
  // The turn's change cards are the runtime's; this panel shows a commit's own
  // files, and `gitStatus` is the workspace's standing delta against HEAD, which
  // counts every earlier turn again.
  assert.ok(!tab.includes('gitStatus'), 'a commit must not be derived from the working tree');
  assert.ok(tab.includes('availability: gitHistoryAvailability'), 'the tab is native-gated');
  assert.ok(tab.includes("id: 'git'"), 'the tab registers as `git`');
  assert.ok(tab.includes('order: 22'), 'the tab sits between 审查 and 轨迹');

  const manifest = readFileSync(
    join(webRoot, 'src', 'components', 'rightDock', 'manifest.tsx'),
    'utf8',
  );
  assert.ok(manifest.includes('gitTab'), 'the manifest must register the git tab');
});
