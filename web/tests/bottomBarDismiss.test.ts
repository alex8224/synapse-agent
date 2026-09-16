/**
 * Source guards for the strip's dismissal contract.
 *
 * The rule is one owner per overlay kind, and the overlay kind decides who owns
 * it:
 *
 *  - a **popover** is closed by a click outside *its own trigger and panel* — and
 *    by Escape — which the strip's overlay host handles.  The membership test is
 *    deliberately two elements, never the whole bar: a bar-wide wrapper swallowed
 *    the clicks that were supposed to close it.
 *  - a **modal** renders its own scrim and owns its own Escape and focus
 *    (`GoalDialog`, `HelpDialog`), so the strip keeps no second listener for it.
 *
 * The advertised keys are handled once, in one place, and key repeat is ignored
 * so that holding F5 cannot flap the panel.  The composer's pickers keep the
 * same popover contract inside `ModelControls`.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const components = join(here, '..', 'src', 'components');
const read = (relative: string): string => readFileSync(join(components, relative), 'utf8');

const host = read('BottomBar.tsx');
const controls = read('ModelControls.tsx');

test('a click outside the popover closes it, and only its own trigger+panel keeps it', () => {
  assert.ok(
    host.includes("document.addEventListener('mousedown'"),
    'the strip must listen for outside clicks while a popover is open',
  );
  assert.ok(
    host.includes('anchor?.contains(target)'),
    'the entry trigger is what keeps its popover open',
  );
  assert.ok(
    host.includes('panelRef.current?.contains(target)'),
    'and so is the panel itself',
  );
  assert.ok(
    host.includes('if (!inside) context.close()'),
    'a click anywhere else must close the popover',
  );
  // Not the whole bar: a single ref around the strip would swallow those clicks.
  assert.equal(
    host.includes('barRef.current?.contains'),
    false,
    'the bar-wide ref must not be part of the dismissal test',
  );
  // The popover is the only overlay kind the strip dismisses itself.
  assert.ok(
    /if \(!popover\) return;/.test(host),
    'a modal must not be dismissed by the strip as well',
  );
});

test('every popover hands the strip its own trigger element', () => {
  assert.ok(host.includes('anchorRef={anchorRef}'), 'the strip must ask for the trigger element');
  for (const module of ['mcpItem.tsx', 'telemetryItem.tsx']) {
    assert.ok(
      read(`bottomBar/${module}`).includes('ref={anchorRef}'),
      `${module} must anchor its popover to its own trigger`,
    );
  }
  // The 更多 menu opens other entries' overlays, so it is the fallback anchor.
  assert.ok(host.includes('anchors.current.get(MORE_ENTRY_ID)'), 'the 更多 trigger is the fallback');
});

test('a modal owns its scrim, its Escape and its focus', () => {
  for (const [name, file] of [
    ['GoalDialog.tsx', read('GoalDialog.tsx')],
    ['HelpDialog.tsx', read('HelpDialog.tsx')],
  ] as const) {
    assert.ok(file.includes('fixed inset-0'), `${name} must render its own scrim`);
    assert.ok(file.includes("event.key === 'Escape'"), `${name} must handle its own Escape`);
    assert.ok(file.includes('useDialogKeyboardNav('), `${name} must own its focus round trip`);
    assert.ok(file.includes('window.removeEventListener'), `${name} must clean its listener up`);
  }
  // ...which is exactly why the strip does not listen for a modal's Escape.
  assert.equal(
    /item\.overlay === 'modal'[\s\S]{0,200}Escape/.test(host),
    false,
    'the strip must not duplicate a modal\u2019s Escape',
  );
});

test('the composer pickers keep the same popover contract', () => {
  assert.ok(controls.includes("document.addEventListener('mousedown'"));
  assert.ok(controls.includes('ref.current?.contains(target)'));
  assert.ok(controls.includes('if (!inside) closeOthers()'));
  assert.ok(/key === 'Escape'/.test(controls), 'Escape must reach the composer popovers');
});

test('the advertised keys are handled once, and key repeat is ignored', () => {
  assert.ok(host.includes('if (event.repeat) return;'), 'holding a key must not flap the panel');
  assert.ok(host.includes('shortcuts.get(event.key)'), 'the layout decides which keys answer');
  assert.equal(
    (host.match(/window\.addEventListener\('keydown'/g) ?? []).length,
    1,
    'the strip registers exactly one listener for the advertised keys',
  );
  // The popover's own Escape listener lives with the popover (the overlay host);
  // a modal's lives in the modal.  Nothing is registered twice for one overlay.
  assert.equal((host.match(/document\.addEventListener\('keydown'/g) ?? []).length, 1);
  // F1 / F5 / F6 come from the shortcut table, so no component spells a key out.
  for (const key of ["'F1'", "'F5'", "'F6'"]) {
    assert.equal(host.includes(`event.key === ${key}`), false, `the strip must not hard-code ${key}`);
  }
});

test('a claimed key is prevented before the repeat check, so a held F5 cannot reload', () => {
  // The order is the fix.  A key the strip does not answer must return before
  // anything is prevented (it belongs to the browser and every other listener),
  // while a key it *does* answer must be `preventDefault`ed before the repeat is
  // ignored: with the repeat check first, a held F5 repeated a keydown whose
  // browser default — reload the page — was never suppressed.
  const at = host.indexOf('shortcuts.get(event.key)');
  assert.notEqual(at, -1, 'the strip resolves a keypress through the layout');
  const repeat = host.indexOf('if (event.repeat) return;', at);
  const prevent = host.indexOf('event.preventDefault()', at);
  assert.notEqual(repeat, -1, 'the repeat check must still exist');
  assert.ok(at < repeat, 'an unclaimed key is matched (and ignored) before the repeat check');
  assert.ok(
    prevent !== -1 && prevent < repeat,
    'a claimed key must be prevented even while it repeats',
  );
});
