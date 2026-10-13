import assert from 'node:assert/strict';
import test from 'node:test';
import { projectLabel } from '../src/stores/sessionList.ts';
import { sessionKey } from '../src/stores/sessionViews.ts';
import {
  syncSessionOrder,
  recordEndedSession,
  MAX_RECENT_ENDED,
  type RecentEndedSession,
  getSessionHolderPalette,
} from '../src/components/runningSessionHolders.ts';

test('syncSessionOrder keeps original position when selecting another session without jumping to top', () => {
  // Initially, session 1 and session 2 start in sequence
  let order: string[] = [];
  order = syncSessionOrder(order, ['p1:t1']);
  assert.deepEqual(order, ['p1:t1']);

  order = syncSessionOrder(order, ['p1:t1', 'p1:t2']);
  assert.deepEqual(order, ['p1:t1', 'p1:t2']);

  // When user clicks session 2 (selecting it), active candidate keys might have session 2 first or second
  // But syncSessionOrder MUST keep session 1 at index 0 and session 2 at index 1!
  const reorderedCandidates = ['p1:t2', 'p1:t1'];
  order = syncSessionOrder(order, reorderedCandidates);
  assert.deepEqual(order, ['p1:t1', 'p1:t2'], 'Selected session must NOT jump to the top');

  // Appending session 3
  order = syncSessionOrder(order, ['p1:t2', 'p1:t1', 'p2:t3']);
  assert.deepEqual(order, ['p1:t1', 'p1:t2', 'p2:t3'], 'New session must be appended to end');

  // Session 1 completes and disappears
  order = syncSessionOrder(order, ['p1:t2', 'p2:t3']);
  assert.deepEqual(order, ['p1:t2', 'p2:t3'], 'Completed session must be removed');
});

test('recordEndedSession retains up to 5 recently ended sessions with FIFO eviction', () => {
  let recent: RecentEndedSession[] = [];

  // Add 5 sessions
  for (let i = 1; i <= 5; i++) {
    recent = recordEndedSession(recent, { projectId: 'p1', threadId: `t${i}` });
  }
  assert.equal(recent.length, 5);
  assert.equal(recent[0].threadId, 't5');
  assert.equal(recent[4].threadId, 't1');

  // Add a 6th session -> t1 is evicted
  recent = recordEndedSession(recent, { projectId: 'p1', threadId: 't6' });
  assert.equal(recent.length, 5);
  assert.equal(recent[0].threadId, 't6');
  assert.equal(recent[4].threadId, 't2');
  assert.ok(!recent.some((r) => r.threadId === 't1'), 't1 should be evicted');

  // Re-recording an existing session (t3 finishes again) moves it to front without duplicates
  recent = recordEndedSession(recent, { projectId: 'p1', threadId: 't3' });
  assert.equal(recent.length, 5);
  assert.equal(recent[0].threadId, 't3');
  assert.equal(
    recent.filter((r) => r.threadId === 't3').length,
    1,
    'No duplicates allowed',
  );
});

test('getSessionHolderPalette uses theme-adaptive semantic tokens', () => {
  const normal = getSessionHolderPalette('p1:t1', false, 'running');
  assert.ok(normal.bg.startsWith('bg-'), 'Should use theme-aware semantic token');
  assert.ok(normal.border.includes('border'), 'Should have a border');
  assert.ok(normal.text.startsWith('text-'), 'Should have high contrast text');
});
