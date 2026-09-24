/**
 * Behaviour tests for the status strip's registration contract.
 *
 * The strip is meant to grow by *adding a module and a manifest line*, so the
 * rules that make that safe are exercised here rather than only guarded in the
 * source:
 *
 *  - the resolver's tracks, order, business visibility and compact policy,
 *  - the rendered DOM of those tracks (`react-dom/server`): a hidden entry
 *    leaves no wrapper and no separator behind, an empty track paints nothing,
 *    and a keyboard-only entry is not in a track at all,
 *  - the one-open-overlay rule and the shortcut table the strip answers to,
 *  - the manifest's own shape (unique ids, one module per entry, declared
 *    overlays that have content).
 *
 * `regions.ts` and `contract.ts` are plain `.ts` (no JSX) so this file can be run
 * by `node --test` with type stripping only.
 */
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

import {
  COMPACT_QUERY,
  MORE_ENTRY_ID,
  compactPolicyOf,
  isSessionSwitch,
  itemById,
  layoutEntries,
  resolveBottomBarLayout,
  shortcutEntries,
  toggleOpenId,
} from '../src/components/bottomBar/contract.ts';
import type {
  BottomBarItemDefinition,
  BottomBarVisibilityContext,
} from '../src/components/bottomBar/contract.ts';
import { BottomBarRegions, REGION_SEPARATOR, regionClass } from '../src/components/bottomBar/regions.ts';
import {
  CONSOLE_SHORTCUTS,
  HELP_SHORTCUT_KEY,
  helpRows,
  shortcutByKey,
  withChord,
  THEME_SHORTCUT_CHORD,
} from '../src/components/consoleShortcuts.ts';

const here = dirname(fileURLToPath(import.meta.url));
const bottomBarDir = join(here, '..', 'src', 'components', 'bottomBar');

const WIDE: BottomBarVisibilityContext = { sessionOpen: true, compact: false };
const PHONE: BottomBarVisibilityContext = { sessionOpen: true, compact: true };

/** An entry that paints nothing but a marker, so the DOM is easy to assert. */
function entry(
  id: string,
  overrides: Partial<BottomBarItemDefinition> = {},
): BottomBarItemDefinition {
  return {
    id,
    label: id,
    region: 'left',
    order: 0,
    Trigger: ({ context, open }) =>
      createElement('span', { 'data-entry': id, 'data-open': String(open) }, `${id}:${String(context.openId)}`),
    ...overrides,
  };
}

const OVERLAY: Pick<BottomBarItemDefinition, 'overlay' | 'Content'> = {
  overlay: 'popover',
  Content: () => createElement('div', null, 'panel'),
};

const MORE = entry(MORE_ENTRY_ID, { region: 'right', order: Number.MAX_SAFE_INTEGER, ...OVERLAY });

// --- the resolver -----------------------------------------------------------

test('the tracks are ordered by `order`, ties keeping the manifest order', () => {
  const layout = resolveBottomBarLayout(
    [
      entry('goal', { region: 'left', order: 30 }),
      entry('activity', { region: 'left', order: 10 }),
      entry('mcp', { region: 'left', order: 20 }),
      entry('late-tie', { region: 'left', order: 30 }),
      entry('telemetry', { region: 'center', order: 10 }),
    ],
    WIDE,
  );
  assert.deepEqual(layout.regions.left.map((item) => item.id), [
    'activity',
    'mcp',
    'goal',
    'late-tie',
  ]);
  assert.deepEqual(layout.regions.center.map((item) => item.id), ['telemetry']);
  assert.deepEqual(layout.regions.right, []);
});

test('a hidden entry is in no track at all, so it leaves nothing behind', () => {
  const hidden = entry('hidden', {
    visible: (context) => context.sessionOpen,
  });
  const open = resolveBottomBarLayout([entry('a'), hidden], WIDE);
  assert.deepEqual(open.regions.left.map((item) => item.id), ['a', 'hidden']);
  // No session: the entry disappears from every list the renderer reads — that
  // is what makes "hidden" mean "no wrapper, no separator", not "renders null".
  const closed = resolveBottomBarLayout([entry('a'), hidden], { sessionOpen: false, compact: false });
  assert.deepEqual(closed.regions.left.map((item) => item.id), ['a']);
  assert.deepEqual(closed.keyboard, []);
  assert.deepEqual(closed.overflow, []);
});

test('a keyboard-only entry is not painted in a track', () => {
  const help = entry('help', { region: 'right', Trigger: undefined, ...OVERLAY });
  const layout = resolveBottomBarLayout([entry('activity'), help], WIDE);
  assert.deepEqual(layout.regions.right, []);
  assert.deepEqual(layout.keyboard.map((item) => item.id), ['help']);
  // It is still reachable: a hidden entry closes its overlay, a keyboard-only
  // one must not (F1 would close its own dialog the moment it opened).
  assert.ok(layoutEntries(layout).some((item) => item.id === 'help'));
});

test('the compact band moves a `more` entry into the overflow, not out of reach', () => {
  const layout = resolveBottomBarLayout(
    [
      entry('activity'),
      entry('telemetry', { region: 'center', order: 10 }),
      entry('aside', { region: 'center', order: 20, compact: 'more', ...OVERLAY }),
    ],
    PHONE,
    MORE,
  );
  // A centre entry overflows by default; this one declares `compact` instead and
  // keeps a simplified place in the strip.
  assert.deepEqual(layout.regions.center.map((item) => item.id), ['telemetry']);
  assert.deepEqual(layout.overflow.map((item) => item.id), ['aside']);
  assert.deepEqual(layout.regions.right.map((item) => item.id), [MORE_ENTRY_ID]);
  assert.ok(layoutEntries(layout).some((item) => item.id === 'aside'));
  // A wide window keeps everything in its own track and no 更多 entry at all.
  const wide = resolveBottomBarLayout(
    [entry('activity'), entry('telemetry', { region: 'center' }), entry('aside', { region: 'center', ...OVERLAY })],
    WIDE,
    MORE,
  );
  assert.deepEqual(wide.overflow, []);
  assert.deepEqual(wide.regions.right, []);
});

test('an entry with nothing to open stays in the strip instead of the 更多 menu', () => {
  // The 更多 menu can only toggle an overlay, so a `more` entry without one would
  // be unreachable: it is kept instead of dropped.
  const plain = entry('plain', { region: 'center', compact: 'more' });
  assert.equal(compactPolicyOf(plain), 'more');
  const layout = resolveBottomBarLayout([plain], PHONE, MORE);
  assert.deepEqual(layout.regions.center.map((item) => item.id), ['plain']);
  assert.deepEqual(layout.overflow, []);
  // ...and with nothing overflowed the 更多 entry is not painted either.
  assert.deepEqual(layout.regions.right, []);
});

test('an entry may declare its compact form', () => {
  const layout = resolveBottomBarLayout(
    [entry('telemetry', { region: 'center', compact: 'compact' })],
    PHONE,
    MORE,
  );
  assert.deepEqual(layout.regions.center.map((item) => item.id), ['telemetry']);
  assert.deepEqual(layout.overflow, []);
});

// --- the one-open-overlay rule ---------------------------------------------

test('the same id closes the open overlay and another replaces it', () => {
  assert.equal(toggleOpenId(null, 'mcp'), 'mcp');
  assert.equal(toggleOpenId('mcp', 'mcp'), null);
  assert.equal(toggleOpenId('mcp', 'goal'), 'goal');
  assert.equal(toggleOpenId('goal', 'help'), 'help');
});

test('another session (or project) is what closes an open panel', () => {
  const here = { thread_id: 'thr-a', project_id: 'proj-a' };
  assert.equal(isSessionSwitch(here, { ...here }), false);
  // A repaint that only reorders the same session is not a switch.
  assert.equal(isSessionSwitch({ ...here }, here), false);
  assert.equal(isSessionSwitch({ ...here, thread_id: 'thr-b' }, here), true);
  assert.equal(isSessionSwitch({ ...here, project_id: 'proj-b' }, here), true);
  // The draft session (no thread yet) counts as a switch away from a real one.
  assert.equal(isSessionSwitch({ project_id: 'proj-a', thread_id: '' }, here), true);
});

test('the shortcuts come from the layout, so a hidden entry stops answering', () => {
  const mcp = entry('mcp', { shortcutKey: 'F5', ...OVERLAY });
  const goal = entry('goal', {
    shortcutKey: 'F6',
    overlay: 'modal',
    Content: () => createElement('div', null, 'modal'),
    visible: (context) => context.sessionOpen,
  });
  const open = shortcutEntries(resolveBottomBarLayout([mcp, goal], WIDE));
  assert.deepEqual([...open.keys()].sort(), ['F5', 'F6']);
  assert.equal(open.get('F6')?.id, 'goal');
  // A hidden entry is out of the layout, so F6 no longer opens it.
  const closed = shortcutEntries(
    resolveBottomBarLayout([mcp, goal], { sessionOpen: false, compact: false }),
  );
  assert.deepEqual([...closed.keys()], ['F5']);
});

test('an entry with a key but no overlay is not bound to it', () => {
  const bound = shortcutEntries(resolveBottomBarLayout([entry('plain', { shortcutKey: 'F5' })], WIDE));
  assert.equal(bound.size, 0);
});

test('an entry is found by id, including the host 更多 entry', () => {
  assert.equal(itemById([entry('a'), MORE], MORE_ENTRY_ID)?.id, MORE_ENTRY_ID);
  assert.equal(itemById([entry('a')], 'missing'), undefined);
});

// --- the rendered tracks ----------------------------------------------------

function render(layout: Parameters<typeof BottomBarRegions>[0]['layout'], compact = false): string {
  return renderToStaticMarkup(
    createElement(BottomBarRegions, {
      layout,
      compact,
      slot: (item: BottomBarItemDefinition) => createElement('span', { 'data-entry': item.id }),
    }),
  );
}

/** The markup of one track, so a stray separator in another track is visible. */
function track(markup: string, region: 'left' | 'center' | 'right'): string {
  const start = markup.indexOf(`data-region="${region}"`);
  assert.ok(start >= 0, `the strip must render a ${region} track`);
  const end = markup.indexOf('data-region="', start + 1);
  return markup.slice(start, end === -1 ? markup.length : end);
}

test('a hidden entry leaves no wrapper and no separator in its track', () => {
  const items = [
    entry('activity'),
    entry('mcp', { order: 20 }),
    entry('goal', { order: 30, visible: () => false }),
    entry('late', { order: 40 }),
  ];
  const markup = render(resolveBottomBarLayout(items, WIDE));
  const left = track(markup, 'left');
  assert.equal((left.match(/data-entry=/g) ?? []).length, 3, 'three entries are painted');
  // Two separators for three entries: the hidden one leaves no third, and the
  // entries around it do not get a trailing/leading one either.
  assert.equal((left.match(/data-separator/g) ?? []).length, 2);
  assert.equal(left.includes('data-entry="goal"'), false);
  assert.equal(left.includes('undefined'), false);
});

test('the separators sit between entries, never before the first or after the last', () => {
  const markup = render(resolveBottomBarLayout([entry('a'), entry('b', { order: 1 })], WIDE));
  const left = track(markup, 'left');
  assert.ok(left.includes('data-entry="a"'));
  assert.ok(left.indexOf('data-entry="a"') < left.indexOf('data-separator'));
  assert.ok(left.indexOf('data-separator') < left.indexOf('data-entry="b"'));
});

test('an empty track renders nothing to paint', () => {
  const markup = render(resolveBottomBarLayout([entry('activity')], WIDE));
  const right = track(markup, 'right');
  // No children at all: the track's `empty:hidden` class keeps it out of the
  // flex flow, so an unused track cannot leave a phantom gap.
  assert.equal(/data-entry=|data-separator/.test(right), false);
  assert.ok(right.includes('empty:hidden'));
  const center = track(markup, 'center');
  assert.equal(/data-entry=|data-separator/.test(center), false);
});

test('a keyboard-only entry renders no control in any track', () => {
  const help = entry('help', { region: 'right', Trigger: undefined, ...OVERLAY });
  const markup = render(resolveBottomBarLayout([entry('activity'), help], WIDE));
  assert.equal(markup.includes('data-entry="help"'), false);
  assert.equal(/data-entry=|data-separator/.test(track(markup, 'right')), false);
});

test('the phone band scrolls the left track instead of clipping it', () => {
  const layout = resolveBottomBarLayout([entry('activity'), entry('mcp', { order: 20 })], PHONE);
  const compact = track(render(layout, true), 'left');
  assert.ok(compact.includes('overflow-x-auto'));
  assert.ok(compact.includes('no-scrollbar'));
  // The wide band keeps the plain track: nothing scrolls on a desktop window.
  const wide = track(render(layout, false), 'left');
  assert.equal(wide.includes('overflow-x-auto'), false);
  assert.equal(regionClass('left', false).includes('overflow-x-auto'), false);
  assert.equal(regionClass('left', true).includes('flex-1'), true);
});

test('the separator is a sibling, not a wrapper around an entry', () => {
  const element = REGION_SEPARATOR as { type?: unknown; props?: { children?: unknown } };
  assert.equal(element.type, 'span');
  assert.equal(element.props?.children, '|');
  const markup = render(resolveBottomBarLayout([entry('a'), entry('b', { order: 1 })], WIDE));
  assert.equal(/<span[^>]*data-separator="true"[^>]*>\|<\/span>/.test(markup), true);
  assert.ok(markup.includes('class="text-gray-200"'), 'the separator keeps the strip\u2019s tint');
});

test('the tracks render in reading order', () => {
  const markup = render(resolveBottomBarLayout([entry('a')], WIDE));
  assert.ok(markup.indexOf('data-region="left"') < markup.indexOf('data-region="center"'));
  assert.ok(markup.indexOf('data-region="center"') < markup.indexOf('data-region="right"'));
});

// --- the shortcut table -----------------------------------------------------

test('the help list is the table, in order, and F1 is one of its rows', () => {
  const rows = helpRows();
  assert.deepEqual(rows.map((row) => row.keys), CONSOLE_SHORTCUTS.map((shortcut) => shortcut.chord));
  assert.ok(rows.some((row) => row.keys === 'F1' && row.label === '打开快捷键帮助'));
  assert.ok(rows.some((row) => row.keys === 'F5'));
  assert.ok(rows.some((row) => row.keys === 'F6'));
  assert.equal(shortcutByKey(HELP_SHORTCUT_KEY)?.key, 'F1');
  assert.equal(shortcutByKey('F7'), undefined);
});

test('a trigger tooltip takes its chord from the table', () => {
  assert.equal(withChord('管理 MCP 服务器', 'F5'), '管理 MCP 服务器 (F5)');
  assert.equal(withChord('设置目标', 'F6'), '设置目标 (F6)');
  // An unregistered key loses the chord instead of printing `undefined`.
  assert.equal(withChord('随便', 'F9'), '随便');
});

test('only the strip keys are claimed, and each by exactly one row', () => {
  const keys = CONSOLE_SHORTCUTS.filter((shortcut) => shortcut.key !== undefined).map(
    (shortcut) => shortcut.key,
  );
  assert.deepEqual([...keys].sort(), ['F1', 'F5', 'F6']);
  assert.equal(new Set(keys).size, keys.length, 'a key must not be advertised twice');
  // Copy-only rows: the composer and the shell own those bindings (F2 is the
  // model picker's), so the strip must not answer them either.
  const copyOnly = CONSOLE_SHORTCUTS.filter((shortcut) => shortcut.key === undefined).map(
    (shortcut) => shortcut.chord,
  );
  assert.deepEqual(copyOnly, [
    'Enter', 'Ctrl + C', 'Ctrl + B', 'Ctrl + J', 'Ctrl + `', 'Ctrl + N', 'Ctrl + K',
    THEME_SHORTCUT_CHORD, 'F2',
  ]);
  for (const chord of copyOnly) {
    assert.equal(shortcutByKey(chord), undefined, `${chord} must not be a strip binding`);
  }
});

// --- the manifest -----------------------------------------------------------

test('every entry module is registered, and the manifest holds no stranger', () => {
  const modules = readdirSync(bottomBarDir)
    .filter((name) => name.endsWith('Item.tsx'))
    .sort();
  const manifest = readFileSync(join(bottomBarDir, 'manifest.tsx'), 'utf8');
  const registered = [...manifest.matchAll(/from '\.\/([A-Za-z0-9]+Item\.tsx)'/g)]
    .map((match) => match[1])
    .sort();
  // Adding an entry is one module plus one manifest line: a module that is not
  // registered (or a line pointing at nothing) fails here.
  assert.deepEqual(registered, modules);
  assert.ok(modules.length >= 5, `expected the shipped entries, saw ${modules.join(', ')}`);
});

test('the shipped entries declare a track, an order and a unique id', () => {
  const manifest = readFileSync(join(bottomBarDir, 'manifest.tsx'), 'utf8');
  const ids = new Set<string>();
  for (const file of readdirSync(bottomBarDir).filter((name) => name.endsWith('Item.tsx'))) {
    const source = readFileSync(join(bottomBarDir, file), 'utf8');
    const id = /id: ([A-Z_]+_ID)/.exec(source);
    assert.ok(id, `${file} must declare its id through a constant`);
    const value = new RegExp(`const ${id[1]} = '([^']+)'`).exec(source);
    assert.ok(value, `${file} must define ${id[1]}`);
    assert.equal(ids.has(value[1]), false, `${value[1]} is declared twice`);
    ids.add(value[1]);
    assert.ok(/region: '(left|center|right)'/.test(source), `${file} must declare a track`);
    assert.ok(/order: \d+/.test(source), `${file} must declare an order`);
    const exported = /export const ([A-Za-z0-9]+): BottomBarItemDefinition/.exec(source);
    assert.ok(exported, `${file} must export its definition`);
    assert.ok(manifest.includes(exported[1]), `${file}'s entry must be in the manifest`);
  }
});

test('every declared overlay has content, and every shortcut a table row', () => {
  for (const file of readdirSync(bottomBarDir).filter((name) => name.endsWith('Item.tsx'))) {
    const source = readFileSync(join(bottomBarDir, file), 'utf8');
    const overlay = /overlay: '(popover|modal)'/.exec(source);
    if (overlay !== null) {
      assert.ok(/Content:/.test(source), `${file} declares an overlay and must render content`);
      if (overlay[1] === 'popover') {
        assert.ok(/panelClassName:/.test(source), `${file}'s popover needs its own box classes`);
      }
    }
    const shortcut = /shortcutKey: ([A-Za-z_]+|'[^']+')/.exec(source);
    if (shortcut === null) continue;
    const key = /^'/.test(shortcut[1])
      ? shortcut[1].slice(1, -1)
      : new RegExp(`const ${shortcut[1]} = '([^']+)'`).exec(
          readFileSync(join(here, '..', 'src', 'components', 'consoleShortcuts.ts'), 'utf8'),
        )?.[1];
    assert.ok(
      key !== undefined && shortcutByKey(key) !== undefined,
      `${file} claims ${shortcut[1]}, which the shortcut table does not know`,
    );
  }
});

test('the strip compacts on the shell\u2019s own mobile breakpoint', () => {
  const app = readFileSync(join(here, '..', 'src', 'App.tsx'), 'utf8');
  assert.ok(app.includes(`matchMedia('${COMPACT_QUERY}')`), 'the band must match the shell');
  assert.equal(COMPACT_QUERY, '(max-width: 767px)');
});
