/**
 * Offline tests for the git surface's decoders and the explorer's wiring.
 *
 * The decoders guard the UI from a half-shaped payload, so the important cases
 * are the ones that must *throw*; the wiring guards keep the read-only promise
 * (no write path) and the chip/explorer pair in step.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  MalformedGitPayloadError,
  changeStatusCode,
  changeStatusLabel,
  diffLineClass,
  parseGitDiff,
  parseGitStatus,
} from '../src/runtime-client/git.ts';

const here = dirname(fileURLToPath(import.meta.url));
const read = (relative: string) => readFileSync(join(here, '..', 'src', relative), 'utf8');

const STATUS = {
  branch: 'feature/x',
  upstream: 'origin/feature/x',
  ahead: 2,
  behind: 1,
  dirty: true,
  files: [{ path: 'a.ts', index_status: ' ', worktree_status: 'M' }],
  truncated: false,
  insertions: 17,
  deletions: 890,
};

test('a git status payload decodes into camelCase fields', () => {
  const status = parseGitStatus(STATUS);
  assert.equal(status.branch, 'feature/x');
  assert.equal(status.ahead, 2);
  assert.equal(status.behind, 1);
  assert.equal(status.insertions, 17);
  assert.equal(status.deletions, 890);
  assert.deepEqual(status.files, [
    { path: 'a.ts', indexStatus: ' ', worktreeStatus: 'M' },
  ]);
});

test('a detached HEAD decodes as a null branch', () => {
  const status = parseGitStatus({ ...STATUS, branch: null, upstream: null });
  assert.equal(status.branch, null);
  assert.equal(status.upstream, null);
});

test('unknown line counts decode as null, never a fabricated zero', () => {
  const status = parseGitStatus({ ...STATUS, insertions: null, deletions: null });
  assert.equal(status.insertions, null);
  assert.equal(status.deletions, null);
  // A real zero is a number, distinct from the null "git could not answer".
  const clean = parseGitStatus({ ...STATUS, insertions: 0, deletions: 0 });
  assert.equal(clean.insertions, 0);
  assert.equal(clean.deletions, 0);
});

test('malformed git payloads are refused, never half-read', () => {
  const bad = [
    null,
    'nope',
    { ...STATUS, extra: 1 },
    { ...STATUS, ahead: -1 },
    { ...STATUS, ahead: 1.5 },
    { ...STATUS, dirty: 'yes' },
    { ...STATUS, insertions: -1 },
    { ...STATUS, insertions: 1.5 },
    { ...STATUS, insertions: '17' },
    { ...STATUS, deletions: -1 },
    { ...STATUS, files: [{ path: 'a.ts', index_status: ' ' }] },
    { ...STATUS, files: [{ path: 'a.ts', index_status: 1, worktree_status: 'M' }] },
    { ...STATUS, files: 'a.ts' },
  ];
  for (const payload of bad) {
    assert.throws(() => parseGitStatus(payload), MalformedGitPayloadError);
  }
});

test('a git diff payload decodes, and rejects unknown shapes', () => {
  const diff = parseGitDiff({
    path: 'a.ts',
    text: '@@ -1 +1 @@\n-a\n+b\n',
    binary: false,
    truncated: false,
    empty: false,
  });
  assert.equal(diff.path, 'a.ts');
  assert.equal(diff.truncated, false);
  assert.throws(() => parseGitDiff({ path: 'a.ts' }), MalformedGitPayloadError);
  assert.throws(
    () => parseGitDiff({ path: 'a.ts', text: 'x', binary: false, truncated: false, empty: 1 }),
    MalformedGitPayloadError,
  );
});

test('status codes and labels follow git', () => {
  const change = (index: string, worktree: string) => ({
    path: 'p',
    indexStatus: index,
    worktreeStatus: worktree,
  });
  assert.equal(changeStatusCode(change(' ', 'M')), ' M');
  assert.equal(changeStatusLabel(change(' ', 'M')), '已修改');
  assert.equal(changeStatusLabel(change('M', ' ')), '已暂存');
  assert.equal(changeStatusLabel(change('M', 'M')), '已暂存+已修改');
  assert.equal(changeStatusLabel(change('?', '?')), '未跟踪');
  assert.equal(changeStatusLabel(change(' ', 'D')), '已删除');
});

test('diff lines are coloured by role', () => {
  assert.equal(diffLineClass('+added'), 'text-green-700');
  assert.equal(diffLineClass('-removed'), 'text-red-700');
  assert.equal(diffLineClass('@@ -1,2 +1,2 @@'), 'text-blue-700');
  assert.equal(diffLineClass('--- a/x'), 'text-gray-500');
  assert.equal(diffLineClass(' context'), 'text-gray-700');
});

test('the explorer stays read-only and is reachable from the branch chip', () => {
  const explorer = read('components/GitExplorer.tsx');
  const topBar = read('components/TopBar.tsx');
  // Read-only: the only wire calls are the two read methods.
  assert.ok(explorer.includes('client.gitStatus('), 'the explorer must read status');
  assert.ok(explorer.includes('client.gitDiff('), 'the explorer must read a diff');
  for (const write of ['gitAdd', 'gitCommit', 'gitCheckout', 'gitStage']) {
    assert.equal(explorer.includes(write), false, `the explorer must not call ${write}`);
  }
  // The header chip opens it, and shows the tracking counts the status carries.
  assert.ok(topBar.includes('<GitExplorer'), 'the header must render the explorer');
  assert.ok(topBar.includes('setExplorerOpen(true)'), 'the chip must open it');
  assert.ok(topBar.includes('gitStatus.ahead'), 'the chip must show the ahead count');
  assert.ok(topBar.includes('gitStatus.behind'), 'the chip must show the behind count');
  // The change statistics are the real tracked added/removed lines, not the file
  // count, and they are shown as `+N -M`.
  assert.ok(topBar.includes('gitStatus.insertions'), 'the chip must read the added lines');
  assert.ok(topBar.includes('gitStatus.deletions'), 'the chip must read the removed lines');
  assert.ok(topBar.includes('+{insertions}'), 'the added lines render as +N');
  assert.ok(topBar.includes('-{deletions}'), 'the removed lines render as -M');
  assert.equal(
    topBar.includes('files.length'),
    false,
    'the chip must not fall back to the changed-file count',
  );
});
