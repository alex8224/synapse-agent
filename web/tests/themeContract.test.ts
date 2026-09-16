
test('the composer carries focus on the card, not on the field inside it', () => {
  // The field is full width with square corners, so the global ring drew a square
  // rectangle: it overflowed the card's rounded corners and its bottom edge cut the
  // card in two.  The card's border is the indicator instead.
  assert.ok(
    /\.ui-composer:focus-within\s*\{[^}]*border-color:[^}]*rgb\(var\(--accent\)\)/.test(styles),
    'the card border must turn to the accent role while the composer is focused',
  );
  assert.ok(
    /\.ui-composer-input:focus-visible\s*\{\s*outline:\s*none/.test(styles),
    'the full-width field must not draw a ring of its own',
  );
  // Both must live in the utilities layer: the card also carries the `border-line`
  // utility, and a cascade layer beats a more specific selector from an earlier one.
  // Anchor on the at-rule: a later comment also mentions `@layer utilities`.
  const utilities = styles.slice(styles.indexOf('@layer utilities {'));
  for (const rule of ['.ui-composer:focus-within', '.ui-composer-input:focus-visible']) {
    assert.ok(utilities.includes(rule), `${rule} must sit in the utilities layer to win`);
  }
});
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
    '--surface-hover',
    '--surface-pressed',
    '--fg-disabled',
    '--line',
    '--accent',
    '--on-accent',
    '--danger',
    '--radius-card',
    '--radius-control',
    '--composer-max',
    '--font-ui',
    '--font-body',
    '--font-mono',
    '--font-numeric',
    '--focus-ring',
    '--shadow-card',
    '--shadow-flyout',
    '--material-chrome',
    '--material-flyout',
    '--material-card',
    '--material-blur',
    '--material-edge',
    '--mica-backdrop',
    '--material-canvas',
    '--material-pane',
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
    // The strip's modals are their own components now (its popovers are
    // portalled by `FloatingPanel`).
    'HelpDialog.tsx',
    // The rail's popovers are windows of their own too: `FloatingPanel` portals them
    // for the same reason (an acrylic ancestor caps their backdrop).
    'ConsoleActions.tsx',
    'ArtifactsPanel.tsx',
    // The movable file window owns a full-viewport scrim of its own.
    'FloatingWindow.tsx',
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

test('the material has something to show through it', () => {
  // A translucent fill over a flat, identical surface is invisible, and a blur with
  // nothing behind it blurs nothing: the window paints a backdrop and the two
  // window fills are translucent over it, so the acrylic reads at rest.
  const app = readFileSync(join(webRoot, 'src', 'App.tsx'), 'utf8');
  assert.ok(app.includes('material-canvas'), 'the window root must use the window fill');
  assert.ok(app.includes('material-pane'), 'the workspace pane must use the pane fill');
  assert.equal(/bg-background|bg-surface-container/.test(app), false, 'no opaque window fills left');
  assert.ok(styles.includes('background-image: var(--mica-backdrop)'), 'the body paints the backdrop');
  // The shipped palette keeps both opaque and the backdrop off, so it pays nothing.
  const root = blockOf(':root');
  assert.ok(root.includes('--mica-backdrop: none'), 'the shipped theme has no backdrop');
});

test('flyouts settle in, and reduced motion is respected', () => {
  assert.ok(/\.flyout-in\s*\{[^}]*animation: flyout-in var\(--motion-normal\) var\(--motion-ease\)/.test(styles), 'the flyout entrance must take its timing from the theme');
  assert.ok(styles.includes('.scrim-in'), 'the scrim fades with it');
  assert.ok(
    /@media \(prefers-reduced-motion: reduce\)\s*\{[^}]*\.flyout-in[^}]*animation: none/.test(styles),
    'a reader who asked for reduced motion must get none',
  );
  // The entrance is attached by material, so a new flyout cannot forget it.
  const offenders: string[] = [];
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(full);
        continue;
      }
      if (!/\.tsx$/.test(entry.name)) continue;
      const text = readFileSync(full, 'utf8');
      for (const line of text.split('\n')) {
        if (line.includes('material-flyout') && !line.includes('flyout-in')) offenders.push(`${entry.name}: ${line.trim().slice(0, 40)}`);
      }
    }
  };
  walk(join(webRoot, 'src'));
  assert.deepEqual(offenders, [], 'every flyout surface must carry its entrance');
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
      '--surface-hover',
      '--surface-pressed',
      '--fg-disabled',
      '--line',
      '--accent',
      // Shape, type, elevation, material and motion are part of a theme too:
      // Fluent is not just another palette.
      '--radius-control',
      '--font-ui',
      '--font-body',
      '--shadow-flyout',
      '--material-card',
      '--material-blur',
      '--material-edge',
      '--mica-backdrop',
      '--motion-ease',
      '--chrome-h',
      '--focus-ring',
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
  for (const name of ['SideBar.tsx', 'TopBar.tsx']) {
    assert.ok(read(name).includes('material-chrome'), `${name} must use the chrome material`);
  }
  // The status strip takes the chrome fill through `.material-strip`, which is the
  // same fill and grain without the blur: nothing scrolls behind the strip, and a
  // blur there would make it a backdrop root and cap the MCP popover inside it.
  assert.ok(
    read('BottomBar.tsx').includes('material-strip'),
    'BottomBar.tsx must use the chrome material (the unblurred strip variant)',
  );
  // The flyouts: the surfaces where acrylic is actually visible (content passes
  // behind them), so they must not fall back to an opaque fill.
  for (const name of ['SettingsDialog.tsx', 'GoalDialog.tsx', 'TodoPanel.tsx']) {
    assert.ok(read(name).includes('material-flyout'), `${name} must use the flyout material`);
  }
});

test('the console draws one focus ring, from the accent role', () => {
  assert.ok(
    /:where\([^)]*\):focus-visible\s*\{[^}]*outline:[^}]*rgb\(var\(--focus-ring\)\)/.test(styles),
    'the focus ring must be drawn once, globally, from its own role',
  );
});

test('a card in the page is painted, not blurred', () => {
  // Fluent reserves acrylic for the transient surfaces: a flyout, a dialog, a
  // teaching tip.  A card that is part of the page is a solid layer fill with a
  // stroke, so the reading column -- where the two look most alike -- must not
  // reach for `backdrop-blur-*` at all.  (The flyouts are covered by
  // `material-flyout`, which carries the blur in its own rule.)
  const offenders: string[] = [];
  for (const name of ['Transcript.tsx', 'CodeBlock.tsx', 'MathTex.tsx', 'Markdown.tsx', 'MermaidBlock.tsx']) {
    const source = readFileSync(join(webRoot, 'src', 'components', name), 'utf8');
    if (source.includes('backdrop-blur')) offenders.push(name);
  }
  assert.deepEqual(offenders, [], 'an in-page card must not blur the page behind it');
  // The expanded thought panel is the one deliberate exception, and it takes the
  // material *by name*: the fill, the blur radius and the grain stay the theme's
  // decision, so a component can never pick its own frosted look.
  const transcript = readFileSync(join(webRoot, 'src', 'components', 'Transcript.tsx'), 'utf8');
  assert.equal(
    (transcript.match(/material-card/g) ?? []).length,
    1,
    'the acrylic card must be the thought panel, through the card material',
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

test('the Fluent main surface uses shared controls and bundled SVG icons', () => {
  for (const name of ['SideBar', 'TopBar', 'CommandInput', 'ModelControls', 'ConsoleActions', 'SettingsDialog']) {
    const source = readFileSync(join(webRoot, 'src', 'components', `${name}.tsx`), 'utf8');
    assert.ok(source.includes("from '@fluentui/react-icons'"), `${name} uses bundled Fluent icons`);
    assert.equal(source.includes('material-symbols-outlined'), false, `${name} must not mix icon families`);
    assert.equal(source.includes('focus:outline-none'), false, `${name} must preserve keyboard focus`);
    assert.ok(source.includes('ui-button') || source.includes('ui-icon-button'), `${name} uses shared controls`);
  }
  assert.ok(/sans:\s*\["var\(--font-ui\)"\]/.test(config));
  assert.ok(/mono:\s*\["var\(--font-mono\)"\]/.test(config));
  for (const role of ['--control-h', '--control-compact-h', '--type-ui', '--type-caption', '--selection-fill']) {
    assert.ok(root.has(role), `the control contract defines ${role}`);
  }
});

test('navigation selection has a marker and picker choices are keyboard buttons', () => {
  const sidebar = readFileSync(join(webRoot, 'src', 'components', 'SideBar.tsx'), 'utf8');
  const pickers = readFileSync(join(webRoot, 'src', 'components', 'ModelControls.tsx'), 'utf8');
  assert.ok(sidebar.includes("aria-current={selected ? 'page' : undefined}"));
  assert.ok(styles.includes(".ui-nav-row[data-selected='true']::before"));
  assert.ok(/<button\s+key=\{m\}\s+type="button"/.test(pickers));
  assert.ok(/<button\s+key=\{lvl\}\s+type="button"\s+disabled=\{!canSetThinking\}/.test(pickers));
  const collapsed = sidebar.slice(sidebar.indexOf('{/* Minimal Rail View'), sidebar.indexOf('{/* Expanded Sidebar View'));
  assert.ok(collapsed.includes('setSettingsOpen(true)'), 'collapsed rail must open settings');
  assert.ok(sidebar.includes('{settingsOpen && <SettingsDialog'), 'shared settings dialog must remain mounted');
});