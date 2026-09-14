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
    '--font-mono',
    '--shadow-card',
  ]) {
    assert.ok(root.has(role), `:root must define ${role}`);
  }
  // The palette steps the components use live here too, so a theme can replace
  // them without the components knowing.
  for (const step of ['--gray-50', '--gray-900', '--blue-600', '--red-600', '--amber-700']) {
    assert.ok(root.has(step), `:root must define ${step}`);
  }
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
    for (const role of ['--gray-900', '--gray-200', '--surface', '--surface-canvas', '--line', '--accent']) {
      assert.ok(declared.has(role), `${theme} must replace ${role}`);
    }
    assert.ok(declared.size >= 30, `${theme} looks like a partial theme (${declared.size} variables)`);
  }
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