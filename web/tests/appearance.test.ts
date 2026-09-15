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
  FRAME_COLOR_DARK,
  FRAME_COLOR_LIGHT,
  LIGHT_THEME,
  applyFrameColor,
  applyTheme,
  frameColorFor,
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

test('the window frame color follows the theme, not the manifest', () => {
  // Chromium paints the installed window's title bar -- and, under the window
  // controls overlay, the strip behind the window buttons -- with `theme_color`.
  // The manifest is static, so the tag is what makes it follow the reader.
  assert.equal(frameColorFor(DARK_THEME), FRAME_COLOR_DARK);
  assert.equal(frameColorFor(LIGHT_THEME), FRAME_COLOR_LIGHT);
  assert.equal(frameColorFor(null), FRAME_COLOR_LIGHT, 'the shipped palette is light');
});

test('applying a frame color rewrites the theme-color tag in place', () => {
  const written: Record<string, string> = {};
  const meta = {
    setAttribute: (name: string, value: string) => {
      written[name] = value;
    },
  };
  const target = {
    querySelector: (selector: string) =>
      selector === 'meta[name="theme-color"]' ? meta : null,
  };

  applyFrameColor(DARK_THEME, target);
  assert.equal(written.content, FRAME_COLOR_DARK);
  applyFrameColor(LIGHT_THEME, target);
  assert.equal(written.content, FRAME_COLOR_LIGHT);
});

test('a document without the tag, or no document at all, is left alone', () => {
  assert.doesNotThrow(() => applyFrameColor(DARK_THEME, null));
  assert.doesNotThrow(() => applyFrameColor(DARK_THEME, { querySelector: () => null }));
});

test('the first paint, the tag and the manifest agree on one color', () => {
  const html = readFileSync(join(here, '..', 'index.html'), 'utf8');
  const manifest = JSON.parse(
    readFileSync(join(here, '..', 'public', 'manifest.webmanifest'), 'utf8'),
  ) as { theme_color: string };
  const tag = /<meta name="theme-color" content="(#[0-9a-fA-F]{6})"/.exec(html);
  assert.ok(tag, 'index.html must ship a theme-color tag for the first paint');
  assert.equal(tag[1], FRAME_COLOR_LIGHT);
  assert.equal(manifest.theme_color, FRAME_COLOR_LIGHT, 'the manifest and the tag must agree');
});