/**
 * Source guards for the "open with" control.
 *
 * The decoders and the presentation helpers are pinned in `externalApps.test.ts`;
 * what is left is the *wiring*, which is a JSX fact and cannot be exercised without a
 * DOM.  These are the rules a launch depends on:
 *
 * - the menu is portaled out of the window's title bar.  Both hosts are windows whose
 *   header is a drag handle (a pointerdown that is not inside a `<button>` starts a
 *   drag) and whose box clips its overflow, so a menu inside that subtree would drag
 *   the window from its own search field;
 * - `Escape` is taken in the capture phase while the menu is open, because every
 *   window closes itself on a `window` keydown: the first `Escape` closes the menu,
 *   the next one closes the window;
 * - a click outside the trigger and the menu closes it;
 * - the trigger is a real split button: a disabled-when-nothing-is-selected main half
 *   and a caret that carries `aria-expanded` / `aria-controls`;
 * - the rows are keyboard buttons with radio semantics, and the menu starts its focus
 *   on the current one;
 * - the console never offers a directory to an external program, and it never
 *   advertises an application the host did not publish.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = join(here, '..');
const read = (name: string) =>
  readFileSync(join(webRoot, 'src', 'components', name), 'utf8');

const menu = read('OpenWithMenu.tsx');
const gitExplorer = read('GitExplorer.tsx');
const artifactsPanel = read('ArtifactsPanel.tsx');

test('the menu is portaled, and it carries the flyout material and its animation', () => {
  assert.ok(menu.includes("from './Portal.tsx'"), 'the menu must portal out of the window');
  assert.ok(menu.includes('<Portal>'));
  const material = menu.split('\n').filter((line) => line.includes('material-flyout'));
  assert.equal(material.length, 1, 'exactly one flyout surface');
  assert.ok(
    material[0].includes('flyout-in'),
    'a material-flyout surface must animate in with flyout-in',
  );
  // An absolutely positioned menu inside the title bar would be clipped by the
  // window and would sit inside the drag handle.
  assert.equal(menu.includes('absolute bottom-full'), false);
  assert.ok(menu.includes('fixed z-50'), 'the portaled menu positions itself fixed');
});

test('escape belongs to the menu while it is open', () => {
  assert.ok(menu.includes("window.addEventListener('keydown', onKeyDown, { capture: true })"));
  assert.ok(menu.includes('event.stopImmediatePropagation()'));
  assert.ok(
    menu.includes("if (event.key !== 'Escape') return;"),
    'only Escape is intercepted',
  );
});

test('a click outside the trigger and the menu closes it', () => {
  assert.ok(menu.includes("document.addEventListener('mousedown', onPointerDown)"));
  assert.ok(menu.includes('triggerRef.current?.contains(target)'));
  assert.ok(menu.includes('menuRef.current?.contains(target)'));
});

test('the trigger is a split button that says what it is', () => {
  assert.ok(menu.includes('aria-haspopup="menu"'));
  assert.ok(menu.includes('aria-expanded={open}'));
  assert.ok(menu.includes('aria-controls="open-with-menu"'));
  assert.ok(menu.includes('id="open-with-menu"'));
  assert.ok(menu.includes('disabled={disabled}'), 'the main half is disabled without a file');
  assert.ok(
    menu.includes('disabled={path === null}'),
    'the caret is disabled without a file',
  );
  assert.ok(menu.includes('disabledReason'), 'a disabled control explains itself');
  assert.ok(menu.includes('ui-button ui-compact'), 'the halves are shared controls');
  assert.ok(menu.includes('ui-icon-button ui-compact'));
});

test('the rows are keyboard buttons with radio semantics and a filter field', () => {
  assert.ok(menu.includes('role="menuitemradio"'));
  assert.ok(menu.includes('aria-checked={selected}'));
  assert.ok(menu.includes('<button'));
  assert.ok(menu.includes('ui-menu-item'), 'the rows take the shared menu tokens');
  assert.ok(menu.includes('id="open-with-filter"'), 'the filter field is reachable');
  assert.ok(
    menu.includes('useDialogKeyboardNav(') && menu.includes('\'[aria-checked="true"]\''),
    'the arrows must walk the rows and the focus must start on the current one',
  );
  assert.ok(menu.includes('fluent-scrollbar'), 'a long catalog scrolls inside the menu');
});

test('the catalog is read once and only published ids are offered', () => {
  assert.ok(menu.includes('loadExternalApps'));
  assert.ok(menu.includes('appsError'), 'a failed read is shown, not shown as an empty host');
  assert.ok(
    menu.includes('recommendedApps(catalog, path)'),
    'the recommended group comes from the host-claimed extensions',
  );
  assert.ok(
    menu.includes('preferredApp(catalog, path, rememberedId, lastOpenWithAppId)'),
    'the trigger follows the remembered choice, then the application used last',
  );
  // No command line, no path: the console can only send an id the host published.
  assert.equal(menu.includes('spawn'), false);
  assert.equal(menu.includes('exec'), false);
});

test('the remembered choice is what makes an application the default', () => {
  assert.ok(menu.includes('useAppearanceStore'));
  assert.ok(menu.includes('rememberOpenWith(extension,'));
  assert.ok(menu.includes('open-with-remember'), 'the footer checkbox is addressable');
  assert.ok(
    menu.includes('始终用'),
    'the checkbox says which application and which extension it remembers',
  );
});

test('a file is what gets opened, and a refusal is visible', () => {
  assert.ok(gitExplorer.includes('<OpenWithMenu'), 'the Git Explorer title bar offers it');
  assert.ok(gitExplorer.includes('path={selected}'));
  assert.ok(gitExplorer.includes('openExternalError'), 'a refused launch is rendered');
  assert.ok(
    artifactsPanel.includes('<OpenWithMenu'),
    'the file window title bar offers it',
  );
  assert.ok(
    artifactsPanel.includes("shownEntry.kind === 'file'"),
    'only a file is offered to an external program',
  );
  assert.ok(artifactsPanel.includes('openExternalError'));
});
