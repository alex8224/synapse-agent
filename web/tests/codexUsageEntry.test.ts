/**
 * The Codex usage entry's browser-free surface: the pure presentation rules, the
 * availability gate the strip filters the manifest through, and the wiring that
 * cannot be observed without a DOM.
 *
 * Three groups of rules:
 *
 *  1. **presentation** — the window label comes from the window's *real*
 *     `window_minutes` (a 7-day window is `7d`, never the TUI's hard-coded `1d`),
 *     the countdown is a pure function of `reset_at` and a caller-supplied "now",
 *     a credit is redeemable only when it is `available`, has an id and has not
 *     expired, and an error line never echoes the raw RPC message;
 *  2. **availability** — the gate the host hands to `useSyncExternalStore`:
 *     filtering, identity stability, subscribe/unsubscribe fan-out, and the fact
 *     that an unavailable entry is in no track, in no 更多 menu, in no shortcut
 *     table, and leaves no separator behind;
 *  3. **wiring** — the entry is in the manifest, it declares the availability
 *     source, the source's `subscribe` is what starts and stops the controller
 *     (so a hidden entry can still discover OAuth), and the confirmation copy is
 *     rendered *before* the only call that can spend a credit.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

import {
  CODEX_USAGE_UNAVAILABLE,
  creditExpiryText,
  creditStatusLabel,
  creditTitle,
  codexUsageErrorText,
  formatResetCountdown,
  formatUsageLabel,
  isCreditRedeemable,
  lowestRemainingPercent,
  remainingPercent,
  windowLabel,
} from '../src/stores/codexUsageView.ts';
import {
  availabilityGate,
  resolveBottomBarLayout,
  shortcutEntries,
} from '../src/components/bottomBar/contract.ts';
import type {
  BottomBarAvailability,
  BottomBarItemDefinition,
} from '../src/components/bottomBar/contract.ts';
import { BottomBarRegions } from '../src/components/bottomBar/regions.ts';
import type { CodexResetCreditView, CodexUsageView } from '../src/runtime-client/codexUsage.ts';

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = join(here, '..');
const read = (relative: string): string => readFileSync(join(webRoot, relative), 'utf8');

const NOW = 1_700_000_000;

const WINDOW = (usedPercent: number | null, windowMinutes: number | null, resetAt: number | null) => ({
  used_percent: usedPercent,
  window_minutes: windowMinutes,
  reset_at: resetAt,
});

test('the entry paints the Codex mark, monochrome and at the strip\'s own size', () => {
  const item = read('src/components/bottomBar/codexUsageItem.tsx');
  const mark = read('src/components/bottomBar/CodexMark.tsx');
  // The product's own mark, inlined: a generic code glyph was there before, and a
  // future edit that drops it should be a deliberate one.
  assert.ok(item.includes('<CodexMark'), 'the entry paints the Codex mark');
  assert.equal(item.includes('Code20Regular'), false, 'not a generic code glyph');
  // One tint, from the caller: the strip colours the entry by state (red below 50%
  // remaining) and every neighbouring icon is a single-colour glyph, so a
  // brand-coloured or hard-coded fill would break both the bar and the dark theme.
  assert.ok(mark.includes('fill="currentColor"'), 'the mark inherits the caller\'s colour');
  assert.equal(
    /fill="(?!#?currentColor)[^"]*"/.test(mark.replace(/fill="currentColor"/g, '')),
    false,
    'no second, hard-coded fill',
  );
  assert.ok(/viewBox="0 0 24 24"/.test(mark), 'the mark keeps its own box');
  assert.ok(mark.includes('CODEX_MARK_PX = 15'), 'sized with the neighbouring 15px icons');
  assert.ok(mark.includes('aria-hidden="true"'), 'decorative: the trigger carries the name');
});

const USAGE: CodexUsageView = {
  session: { project_id: 'proj', thread_id: 'thr' },
  model: 'gpt-5-codex',
  primary: WINDOW(18, 300, NOW + 5400),
  secondary: WINDOW(40, 10080, NOW + 200_000),
  captured_at: NOW,
  available_reset_count: 1,
};

const CREDIT: CodexResetCreditView = {
  id: 'credit-a',
  reset_type: 'weekly',
  status: 'available',
  granted_at: NOW - 1000,
  expires_at: null,
  title: null,
  description: null,
};

// --- presentation -----------------------------------------------------------

test('the window label comes from the real window length, never a hard-coded guess', () => {
  assert.equal(windowLabel(300), '5h');
  assert.equal(windowLabel(1440), '1d');
  // The whole point: a 7-day window must not be labelled `1d`.
  assert.equal(windowLabel(10080), '7d');
  assert.equal(windowLabel(60), '1h');
  assert.equal(windowLabel(90), '1h30m');
  assert.equal(windowLabel(30), '30m');
  assert.equal(windowLabel(null), '', 'an unknown length is left unlabelled');
  assert.equal(windowLabel(0), '');
});

test('remaining percent is derived from the spent percent and clamped', () => {
  assert.equal(remainingPercent(WINDOW(18, 300, null)), 82);
  assert.equal(remainingPercent(WINDOW(null, 300, null)), null);
  assert.equal(remainingPercent(WINDOW(120, 300, null)), 0);
  assert.equal(remainingPercent(null), null);
  assert.equal(lowestRemainingPercent(USAGE), 60);
  assert.equal(lowestRemainingPercent(null), null);
});

test('the countdown rounds up to the next unit, like the TUI', () => {
  assert.equal(formatResetCountdown(NOW + 30, NOW), '30s');
  assert.equal(formatResetCountdown(NOW + 59, NOW), '59s');
  assert.equal(formatResetCountdown(NOW + 60, NOW), '1m');
  assert.equal(formatResetCountdown(NOW + 5399, NOW), '2h');
  assert.equal(formatResetCountdown(NOW + 90_000, NOW), '2d');
  assert.equal(formatResetCountdown(NOW - 10, NOW), '0s', 'a passed reset reads as zero, never negative');
  assert.equal(formatResetCountdown(null, NOW), '--');
});

test('the strip line joins the windows with their own labels and the credit count', () => {
  const line = formatUsageLabel(USAGE, NOW);
  assert.ok(line.includes('5h 82%/2h'), line);
  assert.ok(line.includes('7d 60%/'), line);
  assert.ok(line.includes('resets 1'), line);
  // The loaded credit rows are the fresher count when they are there.
  const withCredits = formatUsageLabel(USAGE, NOW, {
    session: USAGE.session,
    model: USAGE.model,
    available_count: 3,
    credits: [],
  });
  assert.ok(withCredits.includes('resets 3'), withCredits);
  assert.equal(formatUsageLabel(null, NOW), CODEX_USAGE_UNAVAILABLE);
  assert.equal(
    formatUsageLabel({ ...USAGE, primary: null, secondary: null, available_reset_count: null }, NOW),
    CODEX_USAGE_UNAVAILABLE,
  );
});

test('a credits count from another snapshot never overrides the newer usage summary', () => {
  const credits = { session: USAGE.session, model: USAGE.model, available_count: 3, credits: [] };
  const same = formatUsageLabel(USAGE, NOW, credits);
  assert.ok(same.includes('resets 3'), same);

  // The daemon can switch the effective model without the console's own context
  // moving, which leaves the rows loaded for the previous profile behind: they must
  // not pin `resets` to their own count next to the fresh summary.
  const otherModel = formatUsageLabel(USAGE, NOW, { ...credits, model: 'gpt-5.1-codex' });
  assert.ok(otherModel.includes('resets 1'), otherModel);
  assert.equal(otherModel.includes('resets 3'), false, otherModel);

  // Same rule for a list that belongs to another session.
  const otherSession = formatUsageLabel(USAGE, NOW, {
    ...credits,
    session: { project_id: 'proj', thread_id: 'other' },
  });
  assert.ok(otherSession.includes('resets 1'), otherSession);
  assert.equal(otherSession.includes('resets 3'), false, otherSession);
});

test('a credit is redeemable only when it is available, identified and unexpired', () => {
  assert.equal(isCreditRedeemable(CREDIT, NOW), true);
  assert.equal(isCreditRedeemable({ ...CREDIT, status: 'Available' }, NOW), true);
  assert.equal(isCreditRedeemable({ ...CREDIT, status: 'redeemed' }, NOW), false);
  assert.equal(isCreditRedeemable({ ...CREDIT, status: 'redeeming' }, NOW), false);
  assert.equal(isCreditRedeemable({ ...CREDIT, id: '' }, NOW), false);
  assert.equal(isCreditRedeemable({ ...CREDIT, expires_at: NOW - 1 }, NOW), false);
  assert.equal(isCreditRedeemable({ ...CREDIT, expires_at: NOW }, NOW), false, 'expiry is inclusive');
  assert.equal(isCreditRedeemable({ ...CREDIT, expires_at: NOW + 1 }, NOW), true);
});

test('credit labels name the status and fall back to the type for a title', () => {
  assert.equal(creditStatusLabel('available'), '可兑换');
  assert.equal(creditStatusLabel('REDEEMED'), '已兑换');
  assert.equal(creditStatusLabel('weird'), '不可用');
  assert.equal(creditTitle(CREDIT), 'weekly');
  assert.equal(creditTitle({ ...CREDIT, title: 'Weekly reset' }), 'Weekly reset');
  assert.equal(creditExpiryText(CREDIT), '长期有效');
  assert.match(creditExpiryText({ ...CREDIT, expires_at: NOW }), /\d/);
});

test('an error line is bounded and never echoes the raw RPC message', () => {
  const leaky = new Error('failed to POST https://chatgpt.com/backend-api/wham?token=abc');
  const text = codexUsageErrorText(leaky);
  assert.equal(text, '读取 Codex 用量失败');
  assert.equal(text.includes('token'), false);
  assert.equal(text.includes('chatgpt.com'), false);
  // A protocol-shaped service code is the only extra detail allowed through.
  const coded = Object.assign(new Error('x'), { service_code: 'codex_usage_unavailable' });
  assert.equal(codexUsageErrorText(coded), '读取 Codex 用量失败（codex_usage_unavailable）');
  const weird = Object.assign(new Error('x'), { service_code: 'not a code!' });
  assert.equal(codexUsageErrorText(weird), '读取 Codex 用量失败');
  const dropped = Object.assign(new Error('x'), { name: 'ConnectionLostError' });
  assert.equal(codexUsageErrorText(dropped), '与运行时连接已断开');
  assert.equal(codexUsageErrorText('a string error'), '读取 Codex 用量失败');
});

// --- the availability gate --------------------------------------------------

/** A source the test can flip, with its own notification fan-out. */
function source(initial: boolean): BottomBarAvailability & { set: (value: boolean) => void; listeners: number } {
  let value = initial;
  const listeners = new Set<() => void>();
  return {
    getSnapshot: () => value,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    set: (next) => {
      value = next;
      for (const listener of listeners) listener();
    },
    get listeners(): number {
      return listeners.size;
    },
  };
}

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
      createElement('span', { 'data-entry': id, 'data-open': String(open) }, context.openId ?? ''),
    ...overrides,
  };
}

const WIDE = { sessionOpen: true, compact: false };
const PHONE = { sessionOpen: true, compact: true };

test('an entry without a source is always available, and the snapshot is the manifest', () => {
  const items = [entry('a'), entry('b')];
  const gate = availabilityGate(items);
  assert.deepEqual(gate.getSnapshot().map((item) => item.id), ['a', 'b']);
});

test('a source that answers no removes its entry, and yes brings it back', () => {
  const codex = source(false);
  const items = [entry('activity'), entry('codex', { availability: codex }), entry('mcp')];
  const gate = availabilityGate(items);
  assert.deepEqual(gate.getSnapshot().map((item) => item.id), ['activity', 'mcp']);
  codex.set(true);
  assert.deepEqual(gate.getSnapshot().map((item) => item.id), ['activity', 'codex', 'mcp']);
  codex.set(false);
  assert.deepEqual(gate.getSnapshot().map((item) => item.id), ['activity', 'mcp']);
});

test('the snapshot is referentially stable while the answer is unchanged', () => {
  const codex = source(true);
  const items = [entry('activity'), entry('codex', { availability: codex })];
  const gate = availabilityGate(items);
  const first = gate.getSnapshot();
  assert.equal(gate.getSnapshot(), first, 'a repeated read is the same array');
  // A source may notify for its own reasons (a usage refresh); the strip must not
  // re-render unless the answer actually changed.
  codex.set(true);
  assert.equal(gate.getSnapshot(), first);
  codex.set(false);
  assert.notEqual(gate.getSnapshot(), first);
});

test('the gate subscribes to every source and unsubscribes from all of them', () => {
  const a = source(true);
  const b = source(true);
  const gate = availabilityGate([entry('a', { availability: a }), entry('b', { availability: b })]);
  const seen: number[] = [];
  const unsubscribe = gate.subscribe(() => seen.push(seen.length));
  assert.equal(a.listeners, 1);
  assert.equal(b.listeners, 1);
  a.set(false);
  b.set(false);
  assert.equal(seen.length, 2);
  unsubscribe();
  assert.equal(a.listeners, 0);
  assert.equal(b.listeners, 0);
});

test('an unavailable entry is in no track, leaves no separator and loses its 更多 row', () => {
  const codex = source(true);
  const items = [
    entry('activity'),
    entry('codex', { order: 15, compact: 'more', overlay: 'popover', Content: () => null, availability: codex }),
    entry('mcp', { order: 20 }),
  ];
  const gate = availabilityGate(items);
  const wide = resolveBottomBarLayout(gate.getSnapshot(), WIDE);
  assert.deepEqual(wide.regions.left.map((item) => item.id), ['activity', 'codex', 'mcp']);

  codex.set(false);
  const hidden = resolveBottomBarLayout(gate.getSnapshot(), WIDE);
  assert.deepEqual(hidden.regions.left.map((item) => item.id), ['activity', 'mcp']);
  assert.deepEqual(hidden.overflow, []);
  assert.deepEqual(hidden.keyboard, []);

  // The phone band: no row in the 更多 menu either, so the host's own 更多 entry
  // is not painted (nothing overflowed).
  const phone = resolveBottomBarLayout(gate.getSnapshot(), PHONE);
  assert.deepEqual(phone.overflow, []);
  assert.deepEqual(phone.regions.right, []);

  codex.set(true);
  const phoneAgain = resolveBottomBarLayout(gate.getSnapshot(), PHONE);
  assert.deepEqual(phoneAgain.overflow.map((item) => item.id), ['codex']);
});

test('the rendered tracks carry no separator for an entry the gate removed', () => {
  const codex = source(false);
  const items = [
    entry('activity'),
    entry('codex', { order: 15, availability: codex }),
    entry('mcp', { order: 20 }),
  ];
  const markup = renderToStaticMarkup(
    createElement(BottomBarRegions, {
      layout: resolveBottomBarLayout(availabilityGate(items).getSnapshot(), WIDE),
      compact: false,
      slot: (item: BottomBarItemDefinition) => createElement('span', { 'data-entry': item.id }),
    }),
  );
  const left = markup.slice(markup.indexOf('data-region="left"'), markup.indexOf('data-region="center"'));
  assert.equal((left.match(/data-entry=/g) ?? []).length, 2);
  assert.equal((left.match(/data-separator/g) ?? []).length, 1, 'two entries, one separator');
  assert.equal(left.includes('codex'), false);
});

test('an unavailable entry stops answering its shortcut', () => {
  const codex = source(true);
  const items = [
    entry('mcp', { shortcutKey: 'F5', overlay: 'popover', Content: () => null }),
    entry('codex', { shortcutKey: 'F7', overlay: 'popover', Content: () => null, availability: codex }),
  ];
  const gate = availabilityGate(items);
  assert.deepEqual(
    [...shortcutEntries(resolveBottomBarLayout(gate.getSnapshot(), WIDE)).keys()].sort(),
    ['F5', 'F7'],
  );
  codex.set(false);
  assert.deepEqual(
    [...shortcutEntries(resolveBottomBarLayout(gate.getSnapshot(), WIDE)).keys()],
    ['F5'],
  );
});

// --- wiring -----------------------------------------------------------------

test('the entry is registered in the manifest and declares its availability source', () => {
  const manifest = read('src/components/bottomBar/manifest.tsx');
  assert.ok(manifest.includes("from './codexUsageItem.tsx'"), 'one line registers the entry');
  assert.ok(manifest.includes('codexUsageItem'));

  const item = read('src/components/bottomBar/codexUsageItem.tsx');
  assert.ok(item.includes('availability: codexUsageAvailability'), 'the entry answers its own existence');
  assert.ok(item.includes("compact: 'more'"), 'the phone band reuses the 更多 entry point');
  assert.ok(item.includes('order: 15'), 'between the run state and MCP, like the TUI');
  assert.ok(item.includes('truncate'), 'a long line truncates instead of squeezing the telemetry');
  assert.ok(item.includes('max-w-['));
  assert.ok(item.includes('<CodexUsagePanel'), 'the panel is the entry\'s own content module');
  // The entry never talks to the runtime itself: every request goes through the
  // controller, which is what enforces the confirmation.
  for (const file of ['src/components/bottomBar/codexUsageItem.tsx', 'src/components/CodexUsagePanel.tsx']) {
    const source = read(file);
    for (const forbidden of ['consumeCodexResetCredit', 'getCodexUsage', 'getRuntimeConfig', 'client.']) {
      assert.equal(source.includes(forbidden), false, `${file} must not call ${forbidden}`);
    }
  }
});

test('the availability source starts and stops the controller, not a painted Trigger', () => {
  const store = read('src/stores/codexUsage.ts');
  const subscribe = store.slice(store.indexOf('export const codexUsageAvailability'));
  assert.ok(subscribe.includes('controller.start()'), 'subscribing is what starts the discovery');
  assert.ok(subscribe.includes('controller.stop()'), 'and unsubscribing is what stops it');
  // The main store is read, never written: Codex state has its own home.
  assert.equal(store.includes('useConsoleStore.setState'), false);
  assert.equal(store.includes('useConsoleStore.getState()'), true);
});

test('the host filters the manifest through the gate before resolving the layout', () => {
  const host = read('src/components/BottomBar.tsx');
  assert.ok(host.includes('availabilityGate(BOTTOM_BAR_ITEMS)'));
  assert.ok(host.includes('useSyncExternalStore(gate.subscribe, gate.getSnapshot)'));
  assert.ok(host.includes('resolveBottomBarLayout(availableItems, visibility, MORE_ENTRY)'));
});

test('the confirmation copy is painted before the only control that can spend a credit', () => {
  const panel = read('src/components/CodexUsagePanel.tsx');
  assert.ok(/pending !== null[\s\S]{0,1200}CODEX_USAGE_CONFIRM_TEXT/.test(panel));
  assert.ok(/CODEX_USAGE_CONFIRM_TEXT[\s\S]{0,900}confirmResetCredit\(\)/.test(panel));
  // ...and the plain row control only raises it.
  assert.ok(panel.includes('requestResetCredit(credit.id)'));
  assert.ok(panel.includes('cancelResetCredit()'));
});
