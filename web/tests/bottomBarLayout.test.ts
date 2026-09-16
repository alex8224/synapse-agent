/**
 * Source guards for the status strip's *host* wiring.
 *
 * The strip is a host: it paints the tracks of a static manifest
 * (`src/components/bottomBar/`) and owns only the rules common to every entry.
 * The contract itself is exercised in `bottomBarContract.test.ts`; what is left
 * here is the wiring that only exists in the JSX:
 *
 *  1. the strip's chrome — the symmetric three-track grid of a wide window, the
 *     unblurred `material-strip` fill, and no clipping of its own content,
 *  2. the subscription budget — the strip reads one store field (the session
 *     flag) and every entry owns its own subscription, so a reasoning delta
 *     re-renders one entry at most and never the strip,
 *  3. the entry modules, not the host, render the panels,
 *  4. the phone band is the strip's own row (and its track scrolls), not the
 *     positional `display: none` that used to clip the centre track away.
 */
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const components = join(here, '..', 'src', 'components');
const read = (relative: string): string => readFileSync(join(components, relative), 'utf8');

const host = read('BottomBar.tsx');
const regions = read('bottomBar/regions.ts');
const styles = readFileSync(join(here, '..', 'src', 'index.css'), 'utf8');

test('the wide strip keeps the symmetric three-track grid', () => {
  // The three tracks `1fr auto 1fr` keep the telemetry in the exact horizontal
  // centre of the bar, because both flexible tracks resolve to the same leftover
  // width.  The right track is the empty, symmetric spacer (F1's entry paints no
  // control), so it must not become a right-aligned column.
  const strip = host.slice(host.indexOf('const STRIP_BASE'), host.indexOf('const COMPACT_STRIP'));
  assert.ok(strip.includes('grid-cols-[1fr_auto_1fr]'), 'the footer must keep 1fr auto 1fr');
  assert.ok(strip.includes('material-strip'), 'the strip keeps the chrome material');
  assert.ok(strip.includes('whitespace-nowrap'), 'a squeezed label must not wrap the 28px strip');
  assert.equal(strip.includes('justify-end'), false, 'the bar must not be right-aligned');
  assert.equal(strip.includes('overflow-hidden'), false, 'the bar must not clip its own content');
  // The centre track self-centres, from the one place that owns the tracks.
  assert.ok(regions.includes('justify-self-center'), 'the centre track must self-centre');
  assert.equal(regions.includes('ml-auto'), false);
  assert.equal(regions.includes('justify-end'), false);
});

test('the strip paints the manifest tracks instead of hand-writing them', () => {
  assert.ok(host.includes('<BottomBarRegions'), 'the host must render the resolved tracks');
  assert.ok(host.includes('resolveBottomBarLayout('), 'and take its layout from the resolver');
  // The panels belong to the entry modules: the host must not grow a second,
  // hand-wired copy of an entry (that is what "one module + one manifest line"
  // means).
  for (const panel of ['<McpPanel', '<GoalDialog', '<HelpDialog', 'turnStatSegments', 'goalLabel']) {
    assert.equal(host.includes(panel), false, `the host must not render ${panel} itself`);
  }
  for (const [module, panel] of [
    ['bottomBar/mcpItem.tsx', '<McpPanel'],
    ['bottomBar/goalItem.tsx', '<GoalDialog'],
    ['bottomBar/helpItem.tsx', '<HelpDialog'],
    ['bottomBar/telemetryItem.tsx', 'turnStatSegments'],
  ] as const) {
    assert.ok(read(module).includes(panel), `${module} owns ${panel}`);
  }
});

test('the strip reads one store field; every entry owns its own subscription', () => {
  // A reasoning delta re-renders the entry that paints it, never the strip: the
  // host subscribes to the session flag only (for `visible` and for closing an
  // overlay when the session switches).
  assert.equal((host.match(/useConsoleStore\(/g) ?? []).length, 1, 'one selector, the session flag');
  assert.ok(host.includes('state.currentSession.thread_id !== \'\''));
  for (const field of ['state.usage', 'state.sessionUsage', 'state.goal', 'state.mcpStatus', 'state.runtimeStatus']) {
    assert.equal(host.includes(field), false, `the strip must not subscribe to ${field}`);
  }
  assert.ok(host.includes('useConsoleStore.subscribe('), 'a session switch must close the open overlay');
  // The entries that paint store data subscribe on their own.
  for (const module of ['activityItem.tsx', 'mcpItem.tsx', 'goalItem.tsx', 'telemetryItem.tsx']) {
    assert.ok(
      read(`bottomBar/${module}`).includes('useConsoleStore('),
      `${module} must subscribe to what it paints`,
    );
  }
  // ...and the help entry paints no store data at all.
  assert.equal(read('bottomBar/helpItem.tsx').includes('useConsoleStore'), false);
});

test('a popover is portalled from its own trigger, never boxed inside the strip', () => {
  // An `absolute` panel inside the strip inherited the strip's material as its
  // backdrop root and stretched the strip's box; the popover is a `FloatingPanel`
  // anchored to the entry's own element instead.
  assert.ok(host.includes('<FloatingPanel'), 'the overlay host must portal the popover');
  assert.ok(host.includes('anchor={anchor}'), 'and hang it from the entry trigger');
  const panel = read('McpPanel.tsx');
  assert.equal(panel.includes('absolute bottom-'), false, 'the MCP panel must not position itself');
  assert.equal(panel.includes('role="dialog"'), false, 'the box (and its dialog role) is the host\'s');
  assert.ok(panel.includes('useDialogKeyboardNav('), 'the panel keeps its own keyboard navigation');
});

test('the 更多 menu owns its keyboard navigation and hands focus back to its trigger', () => {
  const more = read('bottomBar/moreEntry.tsx');
  // The menu is opened by a trigger that keeps the focus, so a keydown listener on
  // the menu box never fires: it must take the shared roving-navigation hook, which
  // focuses the first row on open, walks the rows with the arrows and hands the
  // focus back to the trigger on close.
  assert.ok(
    more.includes('useDialogKeyboardNav('),
    'the 更多 menu must use the shared keyboard navigation',
  );
  assert.ok(more.includes('ref={menuRef}'), 'the hook needs the menu box');
  assert.ok(more.includes('onKeyDown={onKeyDown}'), 'and the arrows need to reach the box');
  assert.ok(more.includes('role="menuitem"'), 'its rows stay real menu items');
  // A row that opens a modal is unmounted with the menu while the modal records
  // the element to restore the focus to.  The row must hand the focus back to the
  // 更多 trigger (which stays mounted) first, or the modal would remember a
  // detached row and leave the focus on `<body>` on close.
  assert.ok(
    /context\.anchor\?\.focus\(\)[\s\S]{0,400}context\.toggle\(item\.id, context\.anchor\)/.test(more),
    'the row must return the focus to the 更多 trigger before opening its overlay',
  );
});

test('the phone band is a row the strip owns, not positional clipping', () => {
  const compact = host.slice(host.indexOf('const COMPACT_STRIP'));
  assert.ok(compact.slice(0, 200).includes('flex items-center'), 'the phone strip is one row');
  // The band used to hide the centre and right tracks by `nth-child`: that
  // clipped the telemetry and the 更多 entry out of reach.
  assert.equal(styles.includes('> footer > div:nth-child(2)'), false);
  assert.equal(styles.includes('> footer > div:last-child'), false);
  // The left track scrolls instead of shrinking its entries (a shrink would let
  // a label overlap its neighbour, and clipping would hide a control).
  assert.ok(regions.includes('overflow-x-auto'), 'the compact left track scrolls');
  assert.ok(regions.includes('no-scrollbar'), 'without painting a scrollbar in the strip');
  // Every track stays out of the flow while it is empty.
  for (const track of ['left', 'center', 'right']) {
    const wide = new RegExp(`^  ${track}: '([^']*)'`, 'm').exec(regions);
    assert.ok(wide?.[1].includes('empty:hidden'), `${track} must hide while it is empty`);
  }
  assert.ok(
    (regions.match(/empty:hidden/g) ?? []).length >= 6,
    'every track, in both bands',
  );
});

test('every entry module is a definition, and none of them registers itself', () => {
  const modules = readdirSync(join(components, 'bottomBar')).filter((name) => name.endsWith('Item.tsx'));
  assert.ok(modules.length >= 5);
  for (const module of modules) {
    const source = read(`bottomBar/${module}`);
    assert.ok(
      /export const [A-Za-z0-9]+: BottomBarItemDefinition/.test(source),
      `${module} must export one definition`,
    );
    // No mutable registry: the manifest is the only place entries are listed.
    for (const forbidden of ['new Map(', 'new Set(', '.push(', 'register(']) {
      assert.equal(source.includes(forbidden), false, `${module} must not keep a registry (${forbidden})`);
    }
  }
});
