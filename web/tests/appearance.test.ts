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
  oppositeAppearance,
  readStoredAppearance,
  readStoredOpenWith,
  themeFor,
  useAppearanceStore,
  OPEN_WITH_STORAGE_KEY,
} from '../src/stores/appearance.ts';
import { THEME_SHORTCUT_CHORD } from '../src/components/consoleShortcuts.ts';

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

test('the remembered "open with" choice round-trips per extension', () => {
  const store = memoryStorage();
  withStorage(store, () => {
    assert.deepEqual(readStoredOpenWith(), {}, 'nothing is remembered yet');
    useAppearanceStore.getState().rememberOpenWith('.tsx', 'vscode');
    assert.equal(useAppearanceStore.getState().openWith['.tsx'], 'vscode');
    assert.equal(store.getItem(OPEN_WITH_STORAGE_KEY), '{".tsx":"vscode"}');
    assert.deepEqual(readStoredOpenWith(), { '.tsx': 'vscode' }, 'it survives a reload');

    // Unticking the checkbox forgets exactly that extension.
    useAppearanceStore.getState().rememberOpenWith('.tsx', null);
    assert.equal(useAppearanceStore.getState().openWith['.tsx'], undefined);
    assert.deepEqual(readStoredOpenWith(), {});
  });
});

test('a stored "open with" entry that is not an application id is dropped', () => {
  // The value has to look like an id the host published: a hand-edited path, a
  // command line or a credential-shaped string must never become a menu entry.
  withStorage(
    memoryStorage({
      [OPEN_WITH_STORAGE_KEY]: JSON.stringify({
        '.tsx': 'vscode',
        '.py': 'C:/tools/python.exe',
        '.sh': 'sh -c "rm -rf /"',
        'not-an-extension': 'vscode',
        '.md': 'Zed',
      }),
    }),
    () => {
      assert.deepEqual(readStoredOpenWith(), { '.tsx': 'vscode' });
    },
  );
  withStorage(memoryStorage({ [OPEN_WITH_STORAGE_KEY]: 'not json' }), () => {
    assert.deepEqual(readStoredOpenWith(), {}, 'a malformed blob is not an error');
  });
  withStorage(undefined, () => {
    assert.deepEqual(readStoredOpenWith(), {}, 'a browser without storage still works');
  });
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

test('one click flips the palette on screen, the system preference included', () => {
  assert.equal(oppositeAppearance('light', false), 'dark');
  assert.equal(oppositeAppearance('dark', false), 'light');
  // While the preference is "system" the click resolves the operating system first,
  // so it always changes what the reader is looking at instead of storing the mode
  // the OS already paints.
  assert.equal(oppositeAppearance('system', true), 'light');
  assert.equal(oppositeAppearance('system', false), 'dark');
});

test('the toggle stores an explicit choice, so the system stops overriding it', () => {
  const store = memoryStorage();
  withStorage(store, () => {
    useAppearanceStore.setState({ appearance: 'system' });
    useAppearanceStore.getState().toggleAppearance();
    // Node has no `matchMedia`, so "system" resolves to light here: the toggle must
    // come out the other way, and it must be the *stored* choice from now on.
    assert.equal(useAppearanceStore.getState().appearance, 'dark');
    assert.equal(store.getItem(APPEARANCE_STORAGE_KEY), 'dark');
    useAppearanceStore.getState().toggleAppearance();
    assert.equal(useAppearanceStore.getState().appearance, 'light');
    assert.equal(store.getItem(APPEARANCE_STORAGE_KEY), 'light');
    useAppearanceStore.setState({ appearance: 'system' });
  });
});

test('the sidebar row carries the toggle, and the shell answers its chord', () => {
  const actions = readFileSync(join(here, '..', 'src', 'components', 'ConsoleActions.tsx'), 'utf8');
  const app = readFileSync(join(here, '..', 'src', 'App.tsx'), 'utf8');
  const shortcuts = readFileSync(
    join(here, '..', 'src', 'components', 'consoleShortcuts.ts'),
    'utf8',
  );
  // One button, two icons: the one shown is the theme the click switches *to*.
  assert.ok(actions.includes('WeatherMoon20Regular'), 'the row must offer the moon');
  assert.ok(actions.includes('WeatherSunny20Regular'), 'and the sun');
  assert.ok(actions.includes('toggleAppearance'), 'and it must flip the appearance');
  assert.ok(
    actions.includes('THEME_SHORTCUT_CHORD'),
    'its tooltip must take the chord from the one table',
  );
  assert.ok(shortcuts.includes(THEME_SHORTCUT_CHORD), 'the F1 list must carry the chord');
  assert.ok(shortcuts.includes('切换浅色 / 深色主题'), 'with its own label');
  // The shell answers the same chord from anywhere.  Ctrl+Shift+L is not claimed by
  // Chrome or Edge, and it does not collide with the console's other bindings.
  assert.ok(
    /\(e\.ctrlKey \|\| e\.metaKey\) && e\.shiftKey && e\.key\.toLowerCase\(\) === 'l'/.test(app),
    'the shell must answer Ctrl+Shift+L',
  );
  assert.ok(app.includes('toggleAppearance'), 'through the same store action as the button');
});