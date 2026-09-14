/**
 * Guards for the theme contract.
 *
 * The console is painted through one set of CSS variables (`src/index.css`) that
 * `tailwind.config.js` points the palette steps at, so a theme is a block of
 * values and swapping it repaints everything.  These assertions keep that true:
 * a variable the config references must exist, a theme must replace the roles it
 * claims to, and no component may paint a literal colour the theme cannot reach.
 */
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = join(here, '..');
const styles = readFileSync(join(webRoot, 'src', 'index.css'), 'utf8');
const config = readFileSync(join(webRoot, 'tailwind.config.js'), 'utf8');

/** Variable names declared inside one CSS block (a theme). */
function declaredIn(block: string): Set<string> {
  const names = new Set<string>();
  for (const match of block.matchAll(/(--[a-z0-9-]+)\s*:/g)) names.add(match[1]);
  return names;
}

/** The `{ … }` body of the first block whose selector matches. */
function blockOf(selector: string): string {
  const at = styles.indexOf(selector);
  assert.ok(at >= 0, `index.css must contain ${selector}`);
  const open = styles.indexOf('{', at);
  let depth = 0;
  for (let i = open; i < styles.length; i += 1) {
    if (styles[i] === '{') depth += 1;
    else if (styles[i] === '}') {
      depth -= 1;
      if (depth === 0) return styles.slice(open + 1, i);
    }
  }
  throw new Error(`unterminated block for ${selector}`);
}

const root = declaredIn(blockOf(':root'));

test('the theme contract names roles, not shades', () => {
  for (const role of [
    '--surface',
    '--surface-canvas',
    '--surface-sunken',
    '--line',
    '--accent',
    '--on-accent',
    '--danger',
    '--math-surface',
    '--math-header',
    '--math-inline',
    '--radius-card',
    '--radius-control',
    '--font-ui',
    '--font-body',
    '--font-mono',
    '--shadow-card',
    '--shadow-flyout',
    '--material-chrome',
    '--material-flyout',
    '--material-blur',
    '--motion-fast',
    '--motion-normal',
    '--motion-ease',
    '--chrome-h',
    '--status-h',
  ]) {
    assert.ok(root.has(role), `:root must define ${role}`);
  }
  // The palette steps the components use live here too, so a theme can replace
  // them without the components knowing.
  for (const step of ['--gray-50', '--gray-900', '--blue-600', '--red-600', '--amber-700']) {
    assert.ok(root.has(step), `:root must define ${step}`);
  }
});

test('a modal is portaled, so an acrylic ancestor cannot anchor it', () => {
  // `backdrop-filter` on the chrome makes that element the containing block for
  // `fixed` descendants, so a dialog rendered inside the sidebar was positioned
  // over the rail (it read as "docked to the sidebar") in the acrylic themes.
  // Every modal therefore renders through `Portal`.
  for (const name of [
    'SettingsDialog.tsx',
    'GoalDialog.tsx',
    'GitExplorer.tsx',
    'ImageLightbox.tsx',
    'BottomBar.tsx',
  ]) {
    const source = readFileSync(join(webRoot, 'src', 'components', name), 'utf8');
    if (!source.includes('fixed inset-0')) continue;
    assert.ok(source.includes("from './Portal.tsx'"), `${name} renders a modal and must portal it`);
    assert.ok(source.includes('<Portal>'), `${name} must wrap its modal in <Portal>`);
  }
});

test('a window of its own is not a scroll box', () => {
  // A visible scrollbar inside a dialog is the one thing that reads as a web page;
  // the wheel and the keyboard still move the content.
  const settings = readFileSync(join(webRoot, 'src', 'components', 'SettingsDialog.tsx'), 'utf8');
  assert.ok(settings.includes('no-scrollbar'), 'the settings window must not show a scrollbar');
  assert.ok(settings.includes('overflow-y-auto'), 'its content must still scroll');
});

test('every variable Tailwind references is declared by a theme', () => {
  const referenced = new Set<string>();
  for (const match of config.matchAll(/var\((--[a-z0-9-]+)\)/g)) referenced.add(match[1]);
  assert.ok(referenced.size >= 20, `expected the config to reference the contract, saw ${referenced.size}`);
  const missing = [...referenced].filter((name) => !root.has(name));
  assert.deepEqual(missing, [], 'a referenced variable with no value renders as nothing');
});

test('a theme replaces the roles it claims to', () => {
  for (const theme of ['fluent-light', 'fluent-dark']) {
    const declared = declaredIn(blockOf(`[data-theme='${theme}']`));
    for (const role of [
      '--gray-900',
      '--gray-200',
      '--surface',
      '--surface-canvas',
      '--line',
      '--accent',
      // Shape, type, elevation, material and motion are part of a theme too:
      // Fluent is not just another palette.
      '--radius-control',
      '--font-ui',
      '--font-body',
      '--shadow-flyout',
      '--material-blur',
      '--motion-ease',
      '--chrome-h',
    ]) {
      assert.ok(declared.has(role), `${theme} must replace ${role}`);
    }
    assert.ok(declared.size >= 30, `${theme} looks like a partial theme (${declared.size} variables)`);
  }
});

test('shape, elevation and material are named by role, not by literal', () => {
  const offenders: string[] = [];
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(full);
        continue;
      }
      if (!/\.tsx?$/.test(entry.name)) continue;
      const text = readFileSync(full, 'utf8');
      const found = text.match(/rounded-(lg|md|xl|2xl)|shadow-(sm|lg|xl)/g);
      if (found) offenders.push(`${entry.name}: ${[...new Set(found)].join(', ')}`);
    }
  };
  walk(join(webRoot, 'src'));
  assert.deepEqual(offenders, [], 'use rounded-card / rounded-control and shadow-card / shadow-flyout');
});

test('the window chrome and the flyouts carry their material', () => {
  const read = (name: string): string => readFileSync(join(webRoot, 'src', 'components', name), 'utf8');
  // The chrome: the shell surfaces a theme can make acrylic.
  for (const name of ['SideBar.tsx', 'TopBar.tsx', 'BottomBar.tsx']) {
    assert.ok(read(name).includes('material-chrome'), `${name} must use the chrome material`);
  }
  // The flyouts: the surfaces where acrylic is actually visible (content passes
  // behind them), so they must not fall back to an opaque fill.
  for (const name of ['SettingsDialog.tsx', 'GoalDialog.tsx', 'TodoPanel.tsx']) {
    assert.ok(read(name).includes('material-flyout'), `${name} must use the flyout material`);
  }
});

test('the console draws one focus ring, from the accent role', () => {
  assert.ok(
    /:where\([^)]*\):focus-visible\s*\{[^}]*outline:[^}]*rgb\(var\(--accent\)\)/.test(styles),
    'the focus ring must be drawn once, globally, in the accent colour',
  );
});

test('no component paints a colour the theme cannot reach', () => {
  const offenders: string[] = [];
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(full);
        continue;
      }
      if (!/\.tsx?$/.test(entry.name)) continue;
      const text = readFileSync(full, 'utf8');
      const found = text.match(/bg-white|text-white|border-white|\[#[0-9a-fA-F]{3,8}\]/g);
      if (found) offenders.push(`${entry.name}: ${[...new Set(found)].join(', ')}`);
    }
  };
  walk(join(webRoot, 'src'));
  assert.deepEqual(offenders, [], 'paint through the theme roles instead of literals');
});