/**
 * Contract tests for the installable console surface (PWA).
 *
 * They run under the Node built-in test runner and read the real build inputs
 * from disk: `public/manifest.webmanifest`, the icon files it points at and
 * `index.html`.  The point is to fail here — in a plain unit test — rather than
 * in a browser that silently refuses to offer "安装应用" because an icon is
 * missing, the display mode is wrong, or a shortcut points at an action the
 * console does not implement.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { NEW_SESSION_ACTION, readShortcutAction } from '../src/client/deepLink.ts';

interface ManifestIcon {
  src: string;
  sizes: string;
  type?: string;
  purpose?: string;
}

interface ManifestShortcut {
  name: string;
  url: string;
  icons?: ManifestIcon[];
}

interface Manifest {
  id?: string;
  name: string;
  short_name: string;
  start_url: string;
  scope: string;
  display: string;
  display_override?: string[];
  theme_color: string;
  background_color: string;
  icons: ManifestIcon[];
  launch_handler?: { client_mode?: string };
  shortcuts?: ManifestShortcut[];
}

const publicFile = (name: string) => new URL(`../public/${name}`, import.meta.url);

const manifest = JSON.parse(
  readFileSync(publicFile('manifest.webmanifest'), 'utf8'),
) as Manifest;

const indexHtml = readFileSync(new URL('../index.html', import.meta.url), 'utf8');

/** PNG dimensions straight from IHDR, so the assertion does not trust `sizes`. */
function pngSize(name: string): { width: number; height: number } {
  const bytes = readFileSync(publicFile(name));
  assert.equal(bytes.subarray(1, 4).toString('ascii'), 'PNG', `${name} is not a PNG`);
  return { width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) };
}

function purposesOf(icon: ManifestIcon): string[] {
  return (icon.purpose ?? 'any').split(/\s+/).filter(Boolean);
}

test('manifest declares an installable standalone app on the console origin', () => {
  assert.equal(manifest.name, 'Synapse Console');
  assert.ok(manifest.short_name.length > 0 && manifest.short_name.length <= 12);
  assert.equal(manifest.start_url, '/');
  assert.equal(manifest.scope, '/');
  assert.equal(manifest.display, 'standalone');
  assert.equal(manifest.id, '/');
  assert.match(manifest.theme_color, /^#[0-9a-fA-F]{6}$/);
  assert.match(manifest.background_color, /^#[0-9a-fA-F]{6}$/);
});

test('every declared icon exists with the size it claims', () => {
  assert.ok(manifest.icons.length >= 3);
  for (const icon of manifest.icons) {
    assert.ok(icon.src.startsWith('/'), `${icon.src} must be an absolute root path`);
    assert.equal(icon.type, 'image/png');
    const name = icon.src.slice(1);
    const [declaredWidth, declaredHeight] = icon.sizes.split('x').map(Number);
    const size = pngSize(name);
    assert.deepEqual(size, { width: declaredWidth, height: declaredHeight }, `${name} size`);
  }
});

test('the icon set covers the any + maskable purposes Chrome requires', () => {
  const anySizes = new Set(
    manifest.icons.filter((i) => purposesOf(i).includes('any')).map((i) => i.sizes),
  );
  assert.ok(anySizes.has('192x192'), 'a 192x192 "any" icon is required');
  assert.ok(anySizes.has('512x512'), 'a 512x512 "any" icon is required');

  const maskable = manifest.icons.filter((i) => purposesOf(i).includes('maskable'));
  assert.ok(maskable.length >= 1, 'a maskable icon is required for Android launchers');
  assert.ok(maskable.some((i) => i.sizes === '512x512'));
});

test('launch_handler focuses the window that is already open', () => {
  // Without this a notification click (and a taskbar click) opens a second copy
  // of the console instead of returning to the running one.
  assert.equal(manifest.launch_handler?.client_mode, 'focus-existing');
});

test('the caption overlay is requested with standalone as the fallback', () => {
  // Order is the priority: the overlay wins where it exists (Chromium desktop,
  // installed only), and `display` still gives every other browser an app window.
  assert.deepEqual(manifest.display_override, ['window-controls-overlay', 'standalone']);
  assert.equal(manifest.display, 'standalone');
});

test('every manifest shortcut points at an action this build implements', () => {
  assert.ok(manifest.shortcuts && manifest.shortcuts.length >= 1);
  for (const shortcut of manifest.shortcuts) {
    assert.ok(shortcut.name.length > 0);
    assert.ok(shortcut.url.startsWith('/'), `${shortcut.url} must be an absolute root path`);
    const query = shortcut.url.slice(shortcut.url.indexOf('?'));
    assert.equal(
      readShortcutAction(query).action,
      NEW_SESSION_ACTION,
      `${shortcut.url} is not an action the console reads`,
    );
  }
});

test('index.html wires the manifest and the browser chrome', () => {
  assert.match(indexHtml, /<link rel="manifest" href="\/manifest\.webmanifest" \/>/);
  assert.match(indexHtml, /<meta name="theme-color" content="#[0-9a-fA-F]{6}" \/>/);
  assert.match(indexHtml, /<link rel="apple-touch-icon" href="\/([\w.-]+\.png)" \/>/);
  const appleIcon = /<link rel="apple-touch-icon" href="\/([\w.-]+\.png)" \/>/.exec(indexHtml);
  assert.ok(appleIcon, 'apple-touch-icon must reference a png in public/');
  assert.doesNotThrow(() => pngSize(appleIcon[1]));
});

test('readShortcutAction strips only the action it recognises', () => {
  assert.deepEqual(readShortcutAction('?action=new-session'), {
    action: NEW_SESSION_ACTION,
    remainingSearch: '',
  });
  assert.deepEqual(readShortcutAction('?action=new-session&project=demo'), {
    action: NEW_SESSION_ACTION,
    remainingSearch: 'project=demo',
  });
  assert.deepEqual(readShortcutAction('?action=delete-everything'), {
    action: null,
    remainingSearch: '',
  });
  assert.deepEqual(readShortcutAction(''), { action: null, remainingSearch: '' });
});
