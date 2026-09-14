/**
 * Source guards for the console shell (two columns) and the reading column.
 *
 * The window is a row of two columns, not three stacked full-width rows: the
 * navigation is a full-height column on the left, and the header chips,
 * transcript, composer and status strip all belong to the workspace column on the
 * right.  Two consequences are pinned here, because both are easy to undo by
 * accident:
 *
 *  1. Nothing that belongs to the session may span the window again: the header
 *     and the status strip have to stay inside the workspace column, otherwise
 *     they render underneath the sidebar.
 *  2. The transcript and the composer must share one reading geometry
 *     (`.console-gutter` + `.console-column` in `index.css`): the same gutters
 *     around the same 80%-of-workspace column, so the input card keeps the
 *     chat's left and right edges.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const read = (relative: string) => readFileSync(join(here, '..', 'src', relative), 'utf8');

const app = read('App.tsx');
const styles = read('index.css');
const sidebar = read('components/SideBar.tsx');
const transcript = read('components/Transcript.tsx');
const composer = read('components/CommandInput.tsx');
const banner = read('components/RuntimeDiagnosticsBanner.tsx');

test('the shell is a sidebar column plus a workspace column', () => {
  // The window root paints the window fill (`material-canvas`), which is the
  // backdrop's translucent layer in a theme that has one.
  const shell = app.slice(app.indexOf('<div className="material-canvas'));
  const sidebarAt = shell.indexOf('<SideBar />');
  const columnAt = shell.indexOf('flex min-w-0 flex-1 flex-col');
  assert.ok(sidebarAt >= 0, 'the sidebar must be a direct child of the shell');
  assert.ok(columnAt > sidebarAt, 'the workspace column must follow the sidebar');
  for (const child of ['<TopBar />', '<Transcript />', '<CommandInput />', '<BottomBar />']) {
    assert.ok(shell.indexOf(child) > columnAt, `${child} must live inside the workspace column`);
  }
  assert.equal(
    shell.includes('flex-col font-body-md'),
    false,
    'the shell must not stack full-width rows again',
  );
});

test('the sidebar runs the full height of the window', () => {
  // Both the expanded tree and the collapsed rail are `h-full` columns and the
  // shell is `h-screen`, so they reach from the top edge to the bottom one.
  assert.equal(
    (sidebar.match(/h-full/g) ?? []).length,
    2,
    'both sidebar states must be full-height columns',
  );
  assert.ok(sidebar.includes('border-r'), 'the sidebar must keep its own right edge');
  assert.ok(app.includes('h-screen'), 'the shell must stay viewport-height');
});

test('the sidebar tree scrolls with no visible scrollbar', () => {
  // The tree still scrolls (wheel / touch / keyboard), but the 240px rail keeps a
  // clean edge.  The class is applied to the scroll container and is focusable so
  // the keyboard scrolls it even before a row inside has focus.
  const tree = sidebar.slice(
    sidebar.indexOf('no-scrollbar'),
    sidebar.indexOf('{/* Foot of the sidebar'),
  );
  assert.ok(tree.includes('overflow-y-auto'), 'the tree must still scroll');
  assert.ok(tree.includes('tabIndex={0}'), 'the tree must be keyboard-focusable');
  // The rules live in index.css and cover the three engine families.
  assert.ok(styles.includes('.no-scrollbar'), 'the class lives in index.css');
  assert.ok(/scrollbar-width:\s*none/.test(styles), 'Firefox needs scrollbar-width: none');
  assert.ok(/-ms-overflow-style:\s*none/.test(styles), 'legacy Edge needs -ms-overflow-style');
  assert.ok(
    /\.no-scrollbar::-webkit-scrollbar\s*\{[^}]*display:\s*none/.test(styles),
    'Blink/WebKit need the scoped webkit pseudo-element',
  );
  // Scoped on purpose: a bare `::-webkit-scrollbar` rule would hide every
  // scrollbar in the app, including a nested block's own.
  assert.equal(
    /(^|\n)\s*::-webkit-scrollbar/.test(styles),
    false,
    'the scrollbar rule must stay scoped to the class',
  );
});

test('the transcript and the composer share one reading width', () => {
  assert.ok(styles.includes('.console-column'), 'the shared reading width lives in index.css');
  assert.ok(styles.includes('.console-gutter'), 'the shared gutters live in index.css');
  // Desktop: the gutters are 10% of the workspace each side, so the column is
  // exactly 80% of it.  No `rem` cap: a hard cap froze the column on a wider pane.
  assert.ok(
    /@media\s*\(min-width:\s*1024px\)\s*\{[^}]*\.console-gutter\s*\{[^}]*padding-left:\s*10%[^}]*padding-right:\s*10%/.test(
      styles,
    ),
    'on desktop the shared gutters must be 10% of the workspace each side',
  );
  assert.ok(
    /\.console-gutter\s*\{[^}]*padding-left:\s*2rem[^}]*padding-right:\s*2rem/.test(styles),
    'below the breakpoint the shared gutters must be a flat 2rem',
  );
  assert.equal(
    /\.console-column\s*\{[^}]*max-width:/.test(styles),
    false,
    'the shared column must not carry a hard width cap again',
  );
  assert.ok(
    /\.console-column\s*\{[^}]*width:\s*100%/.test(styles),
    'the column must fill the gutter-inset content box',
  );
  assert.ok(transcript.includes('console-column'), 'the chat column must use it');
  assert.ok(composer.includes('console-column'), 'the composer card must use it');
  assert.equal(
    /max-w-3xl/.test(composer),
    false,
    'the composer must not carry a width of its own next to the shared one',
  );
  // The same horizontal gutters on every reading wrapper, otherwise the columns
  // drift apart (and the diagnostics notice stops starting on the chat edge).
  assert.ok(transcript.includes('console-gutter'), 'the transcript wrapper must keep its gutters');
  assert.ok(composer.includes('console-gutter'), 'the composer wrapper must keep the same gutters');
  assert.ok(banner.includes('console-gutter'), 'the diagnostics notice must use the same gutters');
});

test('the composer is the last row of the workspace column, not a floating card', () => {
  // A floating composer covered the bottom of the transcript: the newest streamed
  // line ended up behind the input, so "scrolled to the bottom" did not show it.
  assert.equal(
    composer.includes('absolute bottom-0'),
    false,
    'the composer must not be positioned over the transcript',
  );
  assert.ok(
    composer.includes('shrink-0'),
    'the composer must take its own height instead of overlaying the scroller',
  );
  assert.equal(
    composer.includes('bottom-10'),
    false,
    'the composer must not float above a gap left by the old full-width footer',
  );
  // The padding that used to keep content clear of the floating card is gone, so
  // the scrollport's bottom edge is the last visible line.
  assert.equal(
    transcript.includes('pb-36'),
    false,
    'the transcript must not reserve room for a floating composer any more',
  );
});

test('the composer card lines up with the chat column', () => {
  // The transcript scrolls with no visible scrollbar, so nothing takes a bite out
  // of the reading column: the chat column and the composer card share both edges
  // (a visible scrollbar made the card one scrollbar wider than the text).
  assert.ok(
    transcript.includes('no-scrollbar'),
    'the transcript must scroll with no visible scrollbar',
  );
  assert.ok(transcript.includes('overflow-y-auto'), 'the transcript must still scroll');
  assert.equal(
    transcript.includes('scrollbar-gutter'),
    false,
    'with no scrollbar there is no gutter to reserve',
  );
  assert.equal(
    composer.includes('scrollbar-gutter'),
    false,
    'the composer must not reserve a gutter the transcript does not need',
  );
});

test('the diagnostics notice is aligned with the reading column', () => {
  assert.ok(banner.includes('console-column'), 'the notice must start on the chat edge');
});

test('the sidebar foot carries the workspace identity and the settings entry', () => {
  // Real data only: the label is the attached workspace path (or an explicit empty
  // state), never a placeholder project or account name.
  assert.ok(sidebar.includes('workspacePath'), 'the identity row must show the real path');
  assert.ok(sidebar.includes('未绑定工作区'), 'an unset path needs an explicit empty state');
  assert.ok(sidebar.includes('<ConsoleActions'), 'the app-level actions stay at the foot');
  assert.ok(sidebar.includes('打开设置'), 'the settings entry stays at the foot');
});

test('the sidebar nav names the two entries and their shortcuts', () => {
  // The rail and the expanded tree expose the same actions; the expanded one
  // spells the shortcuts out instead of relying on the help dialog.
  const nav = sidebar.slice(
    sidebar.indexOf('{/* Nav entry'),
    sidebar.indexOf('{/* Shortcut hint'),
  );
  assert.ok(nav.includes('新建任务'), 'the sidebar nav must name the new-task entry');
  assert.ok(nav.includes('Ctrl+N'), 'and show its shortcut');
  // New task creates in the current project, exactly like the rail's `+`.
  assert.ok(nav.includes('createNewSession()'), 'the nav entry must call the existing action');
  // The search entry carries its shortcut in the tooltip and as a visible badge.
  assert.ok(
    (sidebar.match(/Ctrl\+K/g) ?? []).length >= 2,
    'the search entry must show its shortcut too',
  );
});
