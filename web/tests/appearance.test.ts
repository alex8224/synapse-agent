/**
 * Offline tests for the appearance preference (no DOM, no React).
 *
 * The theme itself is CSS; what is testable here is the mapping from a preference
 * to a theme id, and that the module keeps its hands off browser storage (the
 * console's C-12 invariant, which `sourceGuard.test.ts` also enforces).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  APPEARANCE_OPTIONS,
  DARK_THEME,
  LIGHT_THEME,
  applyTheme,
  themeFor,
} from '../src/stores/appearance.ts';

const here = dirname(fileURLToPath(import.meta.url));

test('an explicit choice beats the operating system', () => {
  assert.equal(themeFor('light', true), LIGHT_THEME, 'light stays Fluent light on a dark system');
  assert.equal(themeFor('dark', false), DARK_THEME, 'dark stays dark on a light system');
});

test('system follows the operating system', () => {
  assert.equal(themeFor('system', true), DARK_THEME);
  assert.equal(themeFor('system', false), LIGHT_THEME, 'system light uses Fluent too');
});

test('applying a theme writes the document attribute, and clears it for the default', () => {
  const root = { dataset: {} as DOMStringMap };
  applyTheme(root, DARK_THEME);
  assert.equal(root.dataset.theme, DARK_THEME);
  applyTheme(root, LIGHT_THEME);
  assert.equal(root.dataset.theme, LIGHT_THEME);
  applyTheme(root, null);
  assert.equal('theme' in root.dataset, false, 'the shipped palette carries no attribute');
  assert.doesNotThrow(() => applyTheme(null, DARK_THEME));
});

test('the control offers exactly the three appearances', () => {
  assert.deepEqual(
    APPEARANCE_OPTIONS.map((option) => option.value),
    ['system', 'light', 'dark'],
  );
  for (const option of APPEARANCE_OPTIONS) {
    assert.ok(option.label.length > 0, 'every option needs a label');
  }
});

test('the appearance preference is never written to browser storage', () => {
  // C-12: the console keeps no browser storage; the only state it holds is the
  // host's HttpOnly session cookie.  A theme preference is therefore session-only.
  const source = readFileSync(join(here, '..', 'src', 'stores', 'appearance.ts'), 'utf8');
  assert.equal(/localStorage|sessionStorage|indexedDB|document\.cookie/.test(source), false);
});