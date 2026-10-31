/**
 * Source guard for the sidebar row's overflow menu.
 *
 * A row used to paint four hover actions -- copy, fork, rename, delete -- which cost
 * the session title about a third of the 240px rail for controls reached for rarely.
 * Copy and fork stay on the row (they are what a reader uses while reading the list,
 * and neither rewrites the session); rename and delete moved into a menu that hangs
 * off a "more" trigger.
 *
 * What is pinned here is the split itself, the menu's contract (a portalled menu with
 * real menu semantics that closes before it acts), and the one geometry fact that is
 * easy to break silently: the width passed for end-alignment must be the width the
 * panel paints, because the offset is computed before the panel exists.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const read = (relative: string) => readFileSync(join(here, '..', 'src', relative), 'utf8');

const sidebar = read('components/SideBar.tsx');
const menu = read('components/SessionRowMenu.tsx');

test('the row keeps copy and fork, and hides the two writing actions behind one trigger', () => {
  assert.ok(sidebar.includes('copyThreadId(sess.thread_id)'), 'copy stays on the row');
  assert.ok(
    /void forkRow\(project\.project_id, sess\.thread_id\)/.test(sidebar),
    'fork stays on the row',
  );
  assert.ok(
    sidebar.includes('MoreHorizontal20Regular'),
    'the remaining actions need one trigger, not one control each',
  );
  // Rename and delete are no longer row controls: their handlers live in the menu the
  // row renders, and nothing in the row itself may paint them again.  (The inline
  // rename *field* keeps its own label; it is what the menu opens, not a row action.)
  assert.equal(
    sidebar.includes('title="重命名会话"'),
    false,
    'the rename action must not sit on the row any more',
  );
  assert.equal(
    /aria-label=\{`删除会话记录/.test(sidebar),
    false,
    'the delete action must not sit on the row any more',
  );
  assert.ok(
    sidebar.includes('aria-haspopup="menu"') && sidebar.includes('aria-expanded={rowMenu?.threadId === sess.thread_id}'),
    'the trigger must announce the menu it opens and whether it is open',
  );
  assert.ok(
    sidebar.includes('{rowMenu?.threadId === sess.thread_id && ('),
    'only the row whose menu is open may render one',
  );
});

test('the menu carries the two actions and closes before it acts', () => {
  assert.ok(menu.includes('role="menu"'), 'it must be a menu');
  assert.ok(menu.includes('role="menuitem"'), 'and its rows must be menu items');
  assert.ok(menu.includes("import { FloatingPanel }"), 'it must float: the rail is a backdrop root');
  assert.ok(
    menu.includes('aria-haspopup="dialog"'),
    'the delete row opens a confirmation, and says so',
  );
  // Rename replaces the row with an input that takes the focus itself; delete opens a
  // modal that must own it.  Either way the menu is gone first.
  assert.ok(
    /onClose\(\);\s*\n\s*onRename\(\);/.test(menu) && /onClose\(\);\s*\n\s*onDelete\(\);/.test(menu),
    'a row must close the menu before it acts',
  );
  // A menu claims Escape in the capture phase: every console surface listens for it
  // on `window`, and the first press must close the menu, not the panel below it.
  assert.ok(
    /addEventListener\('keydown', onKeyDown, true\)/.test(menu),
    'Escape must be claimed before the console hears it',
  );
  assert.ok(
    /addEventListener\('pointerdown', onPointerDown, true\)/.test(menu),
    'clicking away must dismiss it',
  );
});

test('the menu is aligned to its own width, not to the console panels’ 384px', () => {
  assert.ok(menu.includes('align="end"'), 'a row menu opens towards the rail, not over the transcript');
  assert.ok(menu.includes('alignWidth={ROW_MENU_WIDTH}'), 'the alignment must be given the real width');
  assert.ok(menu.includes('const ROW_MENU_WIDTH = 176'), 'and that width must be a named constant');
  // `w-44` is 11rem = 176px: the class and the constant are one fact in two places.
  assert.ok(menu.includes('className="w-44'), 'the panel must paint the width it aligns to');
});
