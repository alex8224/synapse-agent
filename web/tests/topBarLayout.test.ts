/**
 * Source guard for the header (top bar) layout decision.
 *
 * The header is the chip row of the *workspace column* (the sidebar is a
 * full-height sibling column, see `shellLayout.test.ts`).  Invariants:
 *  1. It is a three-track grid with two equal `1fr` sides, so the session title
 *     sits on the centre line of the workspace column and cannot drift with the
 *     width of the chips on either side; separate tracks also mean a narrow
 *     window truncates instead of overlapping the title.
 *  2. The git-only chips (branch name with its tracking counts, and the change
 *     statistics) render only when the host reported a branch, so a workspace
 *     that is not under git shows no orphan branch widget.
 *  3. The branch chip and the statistics chip are both buttons that re-read
 *     `runtime.git.status` and then open the read-only git explorer: either one
 *     is enough, so the explorer stays reachable while the status is unread, and
 *     a change made outside the console (a commit) is picked up on click.
 *  4. The change statistics are the real tracked added/removed line counts
 *     (`+N -M`), never the changed-file count; while they are unknown the chip
 *     shows no number instead of a fabricated zero.
 *  5. On a narrow window the secondary project chip hides instead of squeezing
 *     the centre title or the branch out.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '..', 'src', 'components', 'TopBar.tsx'), 'utf8');
const sideBar = readFileSync(join(here, '..', 'src', 'components', 'SideBar.tsx'), 'utf8');
const sessionInfo = readFileSync(join(here, '..', 'src', 'components', 'SessionInfoPanel.tsx'), 'utf8');

function classNamesOf(anchor: string): string {
  const index = source.indexOf(anchor);
  assert.ok(index >= 0, `TopBar must contain ${anchor}`);
  const match = /className="([^"]*)"/.exec(source.slice(index));
  assert.ok(match, `no className found after ${anchor}`);
  return match[1];
}

test('the header centres the session title between two equal tracks', () => {
  const header = classNamesOf('<header');
  assert.ok(header.includes('grid'), 'the header must lay its chips out in a grid');
  assert.ok(
    header.includes('grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]'),
    'the two equal `1fr` side tracks are what centre the middle track',
  );
});

test('the session title is the centred middle track', () => {
  const start = source.indexOf('{/* Centre track');
  assert.ok(start >= 0, 'the header must render the session as the middle track');
  const tab = source.slice(start, source.indexOf('{/* Right track'));
  assert.ok(tab.includes('{sessionTitle}'), 'the track must show the real session title');
  assert.ok(
    tab.includes('truncate'),
    'a long title must truncate instead of pushing the other chips out',
  );
  assert.ok(tab.includes('max-w-[32rem]'), 'the title track must be capped so it cannot drift');
  assert.equal(
    tab.includes('justify-self'),
    false,
    'the title is centred by the equal side tracks, not by a self-alignment',
  );
});

test('the change statistics hang off the branch chip', () => {
  const branch = source.indexOf('{/* Branch context only exists for a git-managed workspace');
  const centre = source.indexOf('{/* Centre track');
  assert.ok(branch >= 0 && centre > branch, 'the branch chip must precede the centre track');
  const chip = source.slice(branch, centre);
  assert.ok(
    chip.indexOf('{gitBranch}') < chip.indexOf('+{insertions}'),
    'the statistics must follow the branch name they describe',
  );
  assert.ok(
    chip.includes('refreshAndOpenExplorer'),
    'the statistics chip must refresh and open the explorer',
  );
  assert.ok(chip.includes('+{insertions}'), 'the added lines render as +N');
  assert.ok(chip.includes('-{deletions}'), 'the removed lines render as -M');
  // The dirty marker stays; the file-with-plus/minus glyph does not.
  assert.ok(chip.includes('bg-amber-500'), 'a dirty workspace still shows its marker');
  assert.equal(
    /\bdifference\b/.test(source),
    false,
    'the statistics chip must not carry the file-difference icon',
  );
  assert.equal(
    /files\.length/.test(chip),
    false,
    'the chip must not fall back to the changed-file count',
  );
});

test('the branch chip is rendered only for a git-managed workspace', () => {
  const guard = source.indexOf("gitBranch !== ''");
  assert.ok(guard >= 0, 'the branch chip must be guarded by a non-empty branch');
  const chip = source.indexOf('<Branch20Regular');
  assert.ok(chip > guard, 'the branch chip must sit inside the non-empty-branch guard');
  // The project chip carries a separator of its own *before* this guard, so the
  // search has to start at the guard.
  const separator = source.indexOf('h-3.5 w-px bg-line/80', guard);
  assert.ok(separator > guard, 'the branch separator must sit inside the same guard');
  assert.ok(
    source.indexOf('{/* Project context') < guard,
    'the project chip must come before the git-only group',
  );
});

test('the top bar owns the sidebar toggle', () => {
  assert.ok(source.includes('<PanelLeft20Regular'), 'the top bar must keep the toggle');
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

test('the toggle is the first control of the workspace column', () => {
  // The sidebar is a full-height column to the left of the header, so the header
  // is no longer pulled back onto the collapsed rail's axis: the toggle is simply
  // the leading control of the left track, in the same themed box as the rail
  // buttons.
  const toggle = classNamesOf('onClick={onToggleNavigation ?? toggleSidebar}');
  assert.ok(toggle.includes('ui-icon-button'), 'the toggle must match the rail button box');
  assert.equal(
    toggle.includes('-ml-'),
    false,
    'the header must not offset a control towards the rail it no longer spans',
  );
  assert.ok(
    sideBar.includes('w-[44px]') && sideBar.includes('ui-icon-button'),
    'the rail must keep the geometry this alignment is measured against',
  );
});

test('the sidebar keeps its actions and the header owns the session info', () => {
  assert.ok(sideBar.includes('<ConsoleActions'), 'the sidebar must host the context actions');
  // The header keeps identity plus the session's own details: no file browser, no
  // diagnostics, no logout.
  for (const trigger of ['folder_open', 'terminal', 'logoutConsole']) {
    assert.equal(
      source.includes(trigger),
      false,
      `the header must not keep ${trigger}`,
    );
  }
  // The session info hangs off the title, which is the session's identity, and the
  // sidebar's footer no longer carries an info icon of its own: two triggers for one
  // panel only left the reader guessing which one to use.
  assert.ok(source.includes('<SessionInfoPanel'), 'the title must open the session info');
  // It hangs from the title: below it (the default opens upwards, which is where a
  // footer trigger lives) and centred on it, because the title is itself centred and
  // a panel aligned to one of its edges reads as belonging to that half.
  assert.ok(
    sessionInfo.includes('side="bottom"'),
    'the panel must open below the title, not above it',
  );
  assert.ok(
    sessionInfo.includes('align="center"'),
    'the panel must be centred under the title',
  );
  assert.equal(
    sideBar.includes('会话信息'),
    false,
    'the sidebar must not keep a session-info trigger',
  );
  // The version is shown inside the settings panel, not next to its label.
  assert.equal(
    sideBar.includes('CONSOLE_VERSION'),
    false,
    'the settings row must not print the version',
  );
});

test('the combined branch and statistics chip opens the git explorer', () => {
  const opens = source.match(/onClick=\{refreshAndOpenExplorer\}/g) ?? [];
  assert.equal(opens.length, 1, 'the combined chip must call the refresh-and-open handler');
  assert.ok(source.includes('openGitExplorer()'), 'the handler must open the explorer');
  assert.ok(
    /refreshAndOpenExplorer[\s\S]*?loadGitStatus\(\)/.test(source),
    'the handler must re-read runtime.git.status before opening',
  );
  const branch = source.slice(
    source.indexOf("gitBranch !== ''"),
    source.indexOf('{/* Centre track'),
  );
  assert.ok(branch.includes('<button'), 'the branch chip must be a button, not a div');
  assert.ok(
    branch.includes('refreshAndOpenExplorer'),
    'the branch chip must refresh and open the explorer',
  );
  // Statistics share the branch button rather than adding another narrow-screen control.
  assert.ok(
    (branch.match(/<button/g) ?? []).length === 1,
    'the combined chip must stay a single button',
  );
});

test('a narrow window drops the secondary chip, not the title or the branch', () => {
  // The project chip is secondary context: below the `lg` breakpoint it is
  // hidden, so the centre title and the branch chip keep their room instead of
  // being squeezed out.
  const project = source.slice(
    source.indexOf('{/* Project context'),
    source.indexOf("gitBranch !== ''"),
  );
  assert.ok(project.includes('hidden'), 'the project chip must be hideable');
  assert.ok(project.includes('lg:flex'), 'the project chip must come back at lg');
  assert.ok(project.includes('lg:inline'), 'its separator must hide with the chip');
  // The identity that must survive: the centre title and the branch chip carry no
  // base `hidden` class of their own.
  const title = source.slice(
    source.indexOf('{/* Centre track'),
    source.indexOf('{/* Right track'),
  );
  assert.equal(/\bhidden\b/.test(title.replaceAll('aria-hidden', 'ariaHidden')), false, 'the session title must stay visible');
  const branch = source.slice(
    source.indexOf("gitBranch !== ''"),
    source.indexOf('{/* Centre track'),
  );
  assert.equal(/\bhidden\b/.test(classNamesOf('onClick={refreshAndOpenExplorer}')), false, 'the branch button must stay visible');
});