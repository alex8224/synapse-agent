/**
 * Offline tests for the appearance preference (no DOM, no React).
 *
 * The theme itself is CSS; what is testable here is the mapping from a preference
 * to a theme id, and that the reader's choice round-trips through the one
 * non-secret browser store the console is allowed to keep (C-12 still forbids
 * *credential* persistence; `sourceGuard.test.ts` enforces the boundary).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  APPEARANCE_OPTIONS,
  APPEARANCE_STORAGE_KEY,
  DARK_THEME,
  FRAME_COLOR_DARK,
  FRAME_COLOR_LIGHT,
  LIGHT_THEME,
  applyFrameColor,
  applyTheme,
  frameColorFor,
  initAppearance,
  readStoredAppearance,
  themeFor,
  useAppearanceStore,
} from '../src/stores/appearance.ts';

const here = dirname(fileURLToPath(import.meta.url));

/** A minimal in-memory `localStorage` stand-in. */
function memoryStorage(initial: Record<string, string> = {}) {
  const data = new Map(Object.entries(initial));
  return {
    getItem: (key: string): string | null => data.get(key) ?? null,
    setItem: (key: string, value: string): void => {
      data.set(key, value);
    },
  };
}

/**
 * Run `body` with `globalThis.localStorage` swapped for `store`.
 *
 * Node has no DOM, so the module's storage lookup is exercised explicitly; the
 * original property descriptor (absent on most Node versions) is restored
 * afterwards so the swap never leaks into another test.
 */
function withStorage<T>(store: unknown, body: () => T): T {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  Object.defineProperty(globalThis, 'localStorage', {
    value: store,
    configurable: true,
    writable: true,
  });
  try {
    return body();
  } finally {
    if (original) Object.defineProperty(globalThis, 'localStorage', original);
    else delete (globalThis as { localStorage?: unknown }).localStorage;
  }
}

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

test('a stored choice is restored on the next load', () => {
  const storage = memoryStorage({ [APPEARANCE_STORAGE_KEY]: 'dark' });
  withStorage(storage, () => {
    assert.equal(readStoredAppearance(), 'dark');
    initAppearance();
    assert.equal(useAppearanceStore.getState().appearance, 'dark');
  });
});

test('choosing an appearance writes it for the next load', () => {
  const storage = memoryStorage();
  withStorage(storage, () => {
    initAppearance();
    assert.equal(
      useAppearanceStore.getState().appearance,
      'system',
      'a console with nothing stored follows the system',
    );
    useAppearanceStore.getState().setAppearance('light');
    assert.equal(storage.getItem(APPEARANCE_STORAGE_KEY), 'light');
    assert.equal(useAppearanceStore.getState().appearance, 'light');
  });
});

test('a missing, unknown or unusable stored value falls back to the system', () => {
  withStorage(memoryStorage(), () => {
    assert.equal(readStoredAppearance(), 'system');
    initAppearance();
    assert.equal(useAppearanceStore.getState().appearance, 'system');
  });
  withStorage(memoryStorage({ [APPEARANCE_STORAGE_KEY]: 'midnight' }), () => {
    assert.equal(readStoredAppearance(), 'system', 'an unknown name is not an appearance');
  });
  withStorage(undefined, () => {
    assert.equal(readStoredAppearance(), 'system', 'a browser without storage still starts');
  });
});

test('a blocked storage never breaks the theme switch', () => {
  const blocked = {
    getItem: () => {
      throw new Error('storage is blocked');
    },
    setItem: () => {
      throw new Error('storage is blocked');
    },
  };
  withStorage(blocked, () => {
    initAppearance();
    assert.equal(useAppearanceStore.getState().appearance, 'system');
    assert.doesNotThrow(() => useAppearanceStore.getState().setAppearance('dark'));
    assert.equal(useAppearanceStore.getState().appearance, 'dark');
  });
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

test('the pre-paint script reads the same key and names the same themes', () => {
  // The bundle only runs after the HTML has been parsed, so without this script a
  // stored dark choice would paint the light palette for a frame on every reload.
  const html = readFileSync(join(here, '..', 'index.html'), 'utf8');
  assert.ok(html.includes(APPEARANCE_STORAGE_KEY), 'index.html must read the stored key');
  for (const theme of [LIGHT_THEME, DARK_THEME]) {
    assert.ok(html.includes(theme), `index.html must name ${theme}`);
  }
});