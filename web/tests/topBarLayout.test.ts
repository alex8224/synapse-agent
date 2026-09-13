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
const sideBar = readFileSync(join(here, '..', 'src', 'components', 'SideBar.tsx'), 'utf8');

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

test('the top bar owns the sidebar toggle', () => {
  assert.ok(source.includes('dock_to_left'), 'the top bar must keep the toggle');
  // The collapsed rail used to carry a second control for the same action; the
  // rail now keeps only new-session / search / loaded count.
  assert.equal(
    sideBar.includes('dock_to_right'),
    false,
    'the collapsed rail must not repeat the sidebar toggle',
  );
  assert.equal(
    sideBar.includes('toggleSidebar'),
    false,
    'the sidebar must not toggle itself at all',
  );
});

test('the top bar toggle lines up with the collapsed rail', () => {
  // The rail centres its buttons in a 44px column, so the header's toggle must
  // be the same 28px box and be pulled back by the header padding (-ml-2 = 8px,
  // the difference between px-4 and the rail's centring) to share that axis.
  const toggle = classNamesOf('onClick={toggleSidebar}');
  assert.ok(toggle.includes('h-7 w-7'), 'the toggle must match the rail button box');
  assert.ok(toggle.includes('-ml-2'), 'the toggle must be pulled onto the rail axis');
  assert.ok(
    sideBar.includes('w-[44px]') && sideBar.includes('w-7 h-7'),
    'the rail must keep the geometry this alignment is measured against',
  );
});

test('the context actions and the settings entry live in the sidebar', () => {
  assert.ok(sideBar.includes('<ConsoleActions'), 'the sidebar must host the context actions');
  // The header keeps identity only: no file browser, no diagnostics, no logout.
  for (const trigger of ['folder_open', 'terminal', 'logoutConsole', '会话信息']) {
    assert.equal(
      source.includes(trigger),
      false,
      `the header must not keep ${trigger}`,
    );
  }
  // The version is shown inside the settings panel, not next to its label.
  assert.equal(
    sideBar.includes('CONSOLE_VERSION'),
    false,
    'the settings row must not print the version',
  );
});
