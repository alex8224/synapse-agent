/**
 * Behaviour tests for the composer action menu's registration contract.
 *
 * The composer's bottom-left control is a menu that grows by *adding a module
 * and a manifest line*, so the rules that make that safe are exercised here
 * rather than only guarded in the source:
 *
 *  - the registry's ids and order, and which rows are runnable,
 *  - "unavailable" is real: a screenshot row carries no `run` and explains
 *    itself, so it can never fake a capture,
 *  - the resolver rejects a malformed registry (blank / duplicate ids),
 *  - the row navigation (`nextActionIndex`) wraps the arrows and honours
 *    Home / End, and every row — a disabled one included — stays reachable,
 *  - the manifest's own shape (one module per entry, a declared id per module),
 *  - each row owns its mark: the host paints `action.icon` and holds no glyph
 *    switch, so a new icon never reaches the generic host,
 *  - the card hosts the generic menu with no screenshot branch of its own.
 *
 * `contract.ts`, `manifest.ts` and the `*Action.ts` modules are plain `.ts` (no
 * JSX) so this file runs under `node --test` with type stripping only; the host
 * (`ActionMenu.tsx`) is exercised in the browser by `composerActions.verify.ts`.
 */
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import {
  actionById,
  isComposerActionNavKey,
  isRunnable,
  nextActionIndex,
  resolveComposerActions,
} from '../src/components/composer/actions/contract.ts';
import type { ComposerActionDefinition } from '../src/components/composer/actions/contract.ts';
import { COMPOSER_ACTIONS } from '../src/components/composer/actions/manifest.ts';
import {
  ADD_IMAGE_ACTION_ID,
  addImageAction,
} from '../src/components/composer/actions/addImageAction.ts';
import {
  WINDOW_SCREENSHOT_ACTION_ID,
  windowScreenshotAction,
} from '../src/components/composer/actions/windowScreenshotAction.ts';
import {
  SCREENSHOT_SETTINGS_ACTION_ID,
  screenshotSettingsAction,
} from '../src/components/composer/actions/screenshotSettingsAction.ts';

const here = dirname(fileURLToPath(import.meta.url));
const actionsDir = join(here, '..', 'src', 'components', 'composer', 'actions');
const card = readFileSync(join(here, '..', 'src', 'components', 'CommandInput.tsx'), 'utf8');

// --- the registry -----------------------------------------------------------

test('the registry ships the three actions in order', () => {
  assert.deepEqual(
    COMPOSER_ACTIONS.map((action) => action.id),
    ['add-image', 'window-screenshot', 'screenshot-settings'],
  );
});

test('every shipped row is runnable and carries a run', () => {
  assert.deepEqual(
    COMPOSER_ACTIONS.filter(isRunnable).map((action) => action.id),
    [ADD_IMAGE_ACTION_ID, WINDOW_SCREENSHOT_ACTION_ID, SCREENSHOT_SETTINGS_ACTION_ID],
  );
  for (const id of [WINDOW_SCREENSHOT_ACTION_ID, SCREENSHOT_SETTINGS_ACTION_ID]) {
    const action = actionById(COMPOSER_ACTIONS, id);
    assert.ok(action, `${id} must be registered`);
    assert.equal(typeof action.run, 'function', `${id} must carry a run`);
    assert.equal(isRunnable(action), true, `${id} must be runnable`);
  }
});

test('the screenshot rows describe themselves and reach only the typed context', () => {
  for (const id of [WINDOW_SCREENSHOT_ACTION_ID, SCREENSHOT_SETTINGS_ACTION_ID]) {
    const action = actionById(COMPOSER_ACTIONS, id);
    assert.ok(action, `${id} must be registered`);
    assert.ok(action.detail.length > 0, `${id} must explain itself`);
    assert.equal(/未接入/.test(action.detail), false, `${id} is wired now`);
  }
  // The rows ask the host through the one narrow capability each needs, and the
  // context is otherwise ignored: no row captures or opens anything itself.
  let starts = 0;
  let opens = 0;
  const context = {
    pickImages: () => {},
    startWindowScreenshot: () => {
      starts += 1;
    },
    openScreenshotSettings: () => {
      opens += 1;
    },
  };
  windowScreenshotAction.run?.(context);
  screenshotSettingsAction.run?.(context);
  assert.equal(starts, 1, 'the window row must ask the host to start a capture');
  assert.equal(opens, 1, 'the settings row must ask the host to open the tool');
});

test('the add-image action only asks the host to open its picker', () => {
  let picks = 0;
  addImageAction.run?.({ pickImages: () => (picks += 1) });
  assert.equal(picks, 1, 'the action must go through the one narrow capability');
});

// --- the resolver -----------------------------------------------------------

test('resolveComposerActions rejects a malformed registry', () => {
  const ok: ComposerActionDefinition = { id: 'a', label: 'A', detail: 'd', icon: null };
  assert.deepEqual(resolveComposerActions([ok]), [ok]);
  // A registry is a set: a duplicate id (or a blank one) fails where it is written.
  assert.throws(() => resolveComposerActions([ok, { ...ok }]), /unique/);
  assert.throws(() => resolveComposerActions([{ ...ok, id: '' }]), /unique and non-empty/);
  assert.throws(() => resolveComposerActions([{ ...ok, label: '' }]), /label and a detail/);
  assert.throws(() => resolveComposerActions([{ ...ok, detail: '' }]), /label and a detail/);
});

// --- row navigation ---------------------------------------------------------

test('the arrows wrap the rows and Home / End jump to the ends', () => {
  // `-1` is focus still on the trigger / the menu box: enter from the matching end.
  assert.equal(nextActionIndex(3, -1, 'ArrowDown'), 0);
  assert.equal(nextActionIndex(3, -1, 'ArrowUp'), 2);
  assert.equal(nextActionIndex(3, 0, 'ArrowDown'), 1);
  assert.equal(nextActionIndex(3, 2, 'ArrowDown'), 0);
  assert.equal(nextActionIndex(3, 0, 'ArrowUp'), 2);
  assert.equal(nextActionIndex(3, 1, 'Home'), 0);
  assert.equal(nextActionIndex(3, 1, 'End'), 2);
  // Nothing to move to.
  assert.equal(nextActionIndex(0, -1, 'ArrowDown'), -1);
});

test('only the four navigation keys are claimed', () => {
  for (const key of ['ArrowDown', 'ArrowUp', 'Home', 'End']) {
    assert.equal(isComposerActionNavKey(key), true, `${key} must be a nav key`);
  }
  for (const key of ['Enter', 'Escape', 'Tab', 'PageDown', 'a']) {
    assert.equal(isComposerActionNavKey(key), false, `${key} must not be a nav key`);
  }
});

// --- the manifest -----------------------------------------------------------

test('every action module is registered, and the manifest holds no stranger', () => {
  const modules = readdirSync(actionsDir)
    .filter((name) => name.endsWith('Action.ts'))
    .sort();
  const manifest = readFileSync(join(actionsDir, 'manifest.ts'), 'utf8');
  const registered = [...manifest.matchAll(/from '\.\/([A-Za-z0-9]+Action\.ts)'/g)]
    .map((match) => match[1])
    .sort();
  // Adding an action is one module plus one manifest line: a module that is not
  // registered (or a line pointing at nothing) fails here.
  assert.deepEqual(registered, modules);
  assert.ok(modules.length >= 3, `expected the shipped actions, saw ${modules.join(', ')}`);
});

test('the shipped action modules declare an id and export a definition', () => {
  const manifest = readFileSync(join(actionsDir, 'manifest.ts'), 'utf8');
  const ids = new Set<string>();
  for (const file of readdirSync(actionsDir).filter((name) => name.endsWith('Action.ts'))) {
    const source = readFileSync(join(actionsDir, file), 'utf8');
    const idConst = /id: ([A-Z_]+_ID)/.exec(source);
    assert.ok(idConst, `${file} must declare its id through a constant`);
    const value = new RegExp(`const ${idConst[1]} = '([^']+)'`).exec(source);
    assert.ok(value, `${file} must define ${idConst[1]}`);
    assert.equal(ids.has(value[1]), false, `${value[1]} is declared twice`);
    ids.add(value[1]);
    const exported = /export const ([A-Za-z0-9]+): ComposerActionDefinition/.exec(source);
    assert.ok(exported, `${file} must export its definition`);
    assert.ok(manifest.includes(exported[1]), `${file}'s action must be in the manifest`);
  }
});

test('the menu host paints each action\'s own mark and keeps no glyph switch', () => {
  const host = readFileSync(join(actionsDir, 'ActionMenu.tsx'), 'utf8');
  assert.ok(host.includes('{action.icon}'), 'the host must paint the mark the action carries');
  assert.equal(
    /actionGlyph|ComposerActionIcon/.test(host),
    false,
    'no per-icon switch may live in the host',
  );
  // The mark lives with the action, so a new icon is a change to one module.
  for (const file of readdirSync(actionsDir).filter((name) => name.endsWith('Action.ts'))) {
    const source = readFileSync(join(actionsDir, file), 'utf8');
    assert.match(source, /icon: React\.createElement\(/, `${file} must paint its own mark`);
  }
});

test('no action module or the menu host reaches the store', () => {
  const files = [
    ...readdirSync(actionsDir).filter((name) => name.endsWith('.ts')),
    'ActionMenu.tsx',
  ];
  for (const file of files) {
    const source = readFileSync(join(actionsDir, file), 'utf8');
    assert.equal(
      /useConsoleStore|zustand/.test(source),
      false,
      `${file} must take a typed context, never the store`,
    );
  }
});

// --- the card that hosts the menu -------------------------------------------

test('the card mounts the generic menu and keeps no screenshot branch', () => {
  assert.ok(
    card.includes('<ActionMenu'),
    'the card must mount the generic action menu',
  );
  assert.ok(
    card.includes('onPickImages={pickImages}'),
    'the card must hand the menu its one upload capability',
  );
  assert.ok(card.includes('import { ActionMenu }'), 'the menu must be imported');
  // The card *wires* the capture capabilities the menu declares, but the
  // screenshot surface itself (progress, cancel, the confirmable result) lives
  // in its own banner: no capture state or branch may live in the card.
  assert.equal(
    card.includes('ScreenshotTaskBanner'),
    false,
    'the capture surface must not be rendered by the card',
  );
  assert.equal(/status\.state|\.pending|useScreenshotStore\(\(s\) => s\.status/.test(card), false,
    'no capture state may be read by the card');
  assert.equal(card.includes('截图'), false, 'no screenshot copy may live in the card');
});

test('the image row still routes through the card\'s one upload path', () => {
  // The picker and its `handleFiles` route stay on the card; the menu only asks
  // the card to open it.
  assert.ok(card.includes('accept="image/*"'), 'the hidden picker stays on the card');
  assert.ok(
    /onChange=\{[\s\S]*?handleFiles\(e\.target\.files\)/.test(card),
    'the picker must still route to handleFiles',
  );
  assert.ok(
    card.includes('const pickImages = useCallback(() => fileInputRef.current?.click()'),
    'the card hands the menu a pickImages capability',
  );
});

test('add project stays its own control beside the menu', () => {
  assert.ok(card.includes('添加项目'), 'the add-project button must stay');
  assert.ok(card.includes('<AddProjectDialog'), 'and its dialog too');
  assert.ok(
    /title="添加项目（选择本地目录并新建会话）"/.test(card),
    'the add-project trigger keeps its label',
  );
});
