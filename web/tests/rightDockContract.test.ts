/**
 * Contract tests for the right auxiliary dock registration and layout rules.
 *
 * Verifies tab ordering, visibility resolution, availability gates and width clamping
 * in isolation from the DOM / React runtime.
 */
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import {
  clampDockWidth,
  DEFAULT_DOCK_WIDTH,
  inOrderTabs,
  resolveRightDockTabs,
  tabById,
  availabilityGate,
  type RightDockTabDefinition,
  type RightDockVisibilityContext,
} from '../src/components/rightDock/contract.ts';

const here = dirname(fileURLToPath(import.meta.url));
const rightDockDir = join(here, '..', 'src', 'components', 'rightDock');

const dummyContent = () => null;

function makeTab(overrides: Partial<RightDockTabDefinition>): RightDockTabDefinition {
  return {
    id: overrides.id ?? 'tab',
    label: overrides.label ?? 'Tab',
    order: overrides.order ?? 50,
    Content: dummyContent,
    ...overrides,
  };
}

test('clampDockWidth strictly bounds pixel width', () => {
  assert.equal(clampDockWidth(400), 400);
  assert.equal(clampDockWidth(100), 280); // clamp min
  assert.equal(clampDockWidth(1200), 760); // clamp max
  assert.equal(clampDockWidth(Number.NaN), DEFAULT_DOCK_WIDTH);
});

test('manifest.tsx registers all shipped tab modules in rightDock', () => {
  const modules = readdirSync(rightDockDir)
    .filter((name) => name.endsWith('Tab.tsx'))
    .sort();
  const manifest = readFileSync(join(rightDockDir, 'manifest.tsx'), 'utf8');
  const registered = [...manifest.matchAll(/from '\.\/([A-Za-z0-9]+Tab\.tsx)'/g)]
    .map((match) => match[1])
    .sort();

  assert.deepEqual(registered, modules);
  assert.deepEqual(modules, ['changesTab.tsx', 'filesTab.tsx', 'goalsTab.tsx', 'previewTab.tsx', 'trajectoryTab.tsx']);
});

test('every tab module exports a valid RightDockTabDefinition', () => {
  const manifest = readFileSync(join(rightDockDir, 'manifest.tsx'), 'utf8');
  for (const file of readdirSync(rightDockDir).filter((name) => name.endsWith('Tab.tsx'))) {
    const source = readFileSync(join(rightDockDir, file), 'utf8');
    const idMatch = /id: '([a-z0-9_-]+)'/.exec(source);
    assert.ok(idMatch, `${file} must declare an id`);
    const labelMatch = /label: '([^']+)'/.exec(source);
    assert.ok(labelMatch, `${file} must declare a label`);
    const orderMatch = /order: (\d+)/.exec(source);
    assert.ok(orderMatch, `${file} must declare a numeric order`);
    const exportMatch = /export const ([A-Za-z0-9]+Tab): RightDockTabDefinition/.exec(source);
    assert.ok(exportMatch, `${file} must export its tab definition`);
    assert.ok(manifest.includes(exportMatch[1]), `${file} must be exported in manifest.tsx`);
  }
});

test('inOrderTabs sorts strictly by order with stable index tie-breaking', () => {
  const items = [
    makeTab({ id: 'preview', order: 40 }),
    makeTab({ id: 'files', order: 10 }),
    makeTab({ id: 'changes', order: 20 }),
    makeTab({ id: 'goals', order: 20 }),
  ];
  const sorted = inOrderTabs(items);
  assert.deepEqual(
    sorted.map((t) => t.id),
    ['files', 'changes', 'goals', 'preview'],
  );
});

test('resolveRightDockTabs filters based on business visibility context', () => {
  const items = [
    makeTab({ id: 'always', order: 10 }),
    makeTab({
      id: 'session-only',
      order: 20,
      visible: (ctx) => ctx.sessionOpen,
    }),
    makeTab({
      id: 'non-compact-only',
      order: 30,
      visible: (ctx) => !ctx.compact,
    }),
  ];

  const ctxOpenWide: RightDockVisibilityContext = { sessionOpen: true, compact: false };
  assert.deepEqual(
    resolveRightDockTabs(items, ctxOpenWide).map((t) => t.id),
    ['always', 'session-only', 'non-compact-only'],
  );

  const ctxClosedCompact: RightDockVisibilityContext = { sessionOpen: false, compact: true };
  assert.deepEqual(
    resolveRightDockTabs(items, ctxClosedCompact).map((t) => t.id),
    ['always'],
  );
});

test('tabById finds item or returns undefined', () => {
  const items = [makeTab({ id: 'files' }), makeTab({ id: 'changes' })];
  assert.equal(tabById(items, 'files')?.id, 'files');
  assert.equal(tabById(items, 'unknown'), undefined);
});

test('availabilityGate dynamically includes or excludes tabs', () => {
  let isAvailable = false;
  const listeners: Array<() => void> = [];

  const dynamicTab = makeTab({
    id: 'dynamic',
    availability: {
      getSnapshot: () => isAvailable,
      subscribe: (fn) => {
        listeners.push(fn);
        return () => {
          const idx = listeners.indexOf(fn);
          if (idx >= 0) listeners.splice(idx, 1);
        };
      },
    },
  });

  const gate = availabilityGate([makeTab({ id: 'static' }), dynamicTab]);

  // Initially dynamic is not available
  assert.deepEqual(
    gate.getSnapshot().map((t) => t.id),
    ['static'],
  );

  // Turn on
  isAvailable = true;
  listeners.forEach((fn) => fn());
  assert.deepEqual(
    gate.getSnapshot().map((t) => t.id),
    ['static', 'dynamic'],
  );
});
