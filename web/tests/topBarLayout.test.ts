/**
 * Source guard for the header (top bar) layout decision.
 *
 * Two invariants:
 *  1. The header is a symmetric three-track grid (`minmax(0,1fr) auto minmax(0,1fr)`)
 *     so the session label sits in the exact horizontal centre instead of hanging
 *     off the branch chip.
 *  2. The branch chip (dot + fork icon + branch name) and its separator render
 *     only when the host reported a branch, so a workspace that is not under git
 *     shows no orphan branch widget.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '..', 'src', 'components', 'TopBar.tsx'), 'utf8');

function classNamesOf(anchor: string): string {
  const index = source.indexOf(anchor);
  assert.ok(index >= 0, `TopBar must contain ${anchor}`);
  const match = /className="([^"]*)"/.exec(source.slice(index));
  assert.ok(match, `no className found after ${anchor}`);
  return match[1];
}

test('the header keeps the symmetric three-track grid', () => {
  const header = classNamesOf('<header');
  assert.ok(
    header.includes('grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]'),
    'the header must keep a symmetric grid so the centre track stays centred',
  );
  assert.equal(
    header.includes('justify-between'),
    false,
    'the header must not be a space-between flex row',
  );
});

test('the session label is centred in the header', () => {
  const label = classNamesOf('{/* Session label');
  assert.ok(label.includes('justify-self-center'), 'the session label must self-centre');
  assert.equal(label.includes('ml-auto'), false);
});

test('the branch chip is rendered only for a git-managed workspace', () => {
  const guard = source.indexOf("gitBranch !== ''");
  assert.ok(guard >= 0, 'the branch chip must be guarded by a non-empty branch');
  const chip = source.indexOf('fork_right');
  assert.ok(chip > guard, 'the branch chip must sit inside the non-empty-branch guard');
  const separator = source.indexOf('text-gray-300 mx-1');
  assert.ok(separator > guard, 'the branch separator must sit inside the same guard');
});
