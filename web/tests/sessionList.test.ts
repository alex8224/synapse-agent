/**
 * Offline tests for the sidebar session grouping and search helpers.
 * Pure functions only: no DOM, no host, no socket.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import type { SessionItem } from '../src/stores/historyMapper.ts';
import {
  filterSessions,
  groupSessionsByTime,
  sessionGroupKey,
} from '../src/stores/sessionList.ts';

/** Local 2026-09-11 12:00, so day boundaries are unambiguous. */
const NOW = new Date(2026, 8, 11, 12, 0, 0);

function item(threadId: string, title: string, updatedAt: string): SessionItem {
  return { thread_id: threadId, title, updated_at: updatedAt, time_label: '' };
}

/** ISO string for a local wall-clock time (the daemon sends ISO with an offset). */
function at(year: number, month: number, day: number, hour = 12): string {
  return new Date(year, month - 1, day, hour, 0, 0).toISOString();
}

test('sessionGroupKey buckets timestamps relative to the local day', () => {
  assert.equal(sessionGroupKey(at(2026, 9, 11, 9), NOW), 'today');
  assert.equal(sessionGroupKey(at(2026, 9, 10, 23), NOW), 'yesterday');
  assert.equal(sessionGroupKey(at(2026, 9, 8, 12), NOW), 'last7');
  assert.equal(sessionGroupKey(at(2026, 8, 20, 12), NOW), 'last30');
  assert.equal(sessionGroupKey(at(2026, 7, 1, 12), NOW), 'older');
});

test('sessionGroupKey treats the exact start of today as today and one ms earlier as yesterday', () => {
  const todayStart = new Date(2026, 8, 11, 0, 0, 0);
  assert.equal(sessionGroupKey(todayStart.toISOString(), NOW), 'today');
  assert.equal(sessionGroupKey(new Date(todayStart.getTime() - 1).toISOString(), NOW), 'yesterday');
});

test('sessionGroupKey keeps an unparseable timestamp visible as older', () => {
  assert.equal(sessionGroupKey('not-a-timestamp', NOW), 'older');
  assert.equal(sessionGroupKey('', NOW), 'older');
});

test('groupSessionsByTime omits empty buckets and keeps the newest bucket first', () => {
  const groups = groupSessionsByTime(
    [
      item('t1', 'today one', at(2026, 9, 11, 9)),
      item('o1', 'old one', at(2026, 6, 1)),
      item('y1', 'yesterday one', at(2026, 9, 10, 20)),
      item('t2', 'today two', at(2026, 9, 11, 10)),
    ],
    NOW,
  );
  assert.deepEqual(
    groups.map((g) => g.key),
    ['today', 'yesterday', 'older'],
  );
  assert.deepEqual(
    groups.map((g) => g.label),
    ['今天', '昨天', '更早'],
  );
  assert.deepEqual(
    groups[0].items.map((i) => i.thread_id),
    ['t1', 't2'],
  );
  assert.equal(groups[1].items[0].thread_id, 'y1');
  assert.equal(groups[2].items[0].thread_id, 'o1');
});

test('groupSessionsByTime returns nothing for an empty list', () => {
  assert.deepEqual(groupSessionsByTime([], NOW), []);
});

test('filterSessions returns the input untouched for a blank query', () => {
  const items = [item('a', 'alpha', at(2026, 9, 11)), item('b', 'beta', at(2026, 9, 11))];
  assert.equal(filterSessions(items, ''), items);
  assert.equal(filterSessions(items, '   '), items);
});

test('filterSessions matches the title case-insensitively', () => {
  const items = [
    item('a', 'Fix the Web UI', at(2026, 9, 11)),
    item('b', 'unrelated', at(2026, 9, 11)),
  ];
  assert.deepEqual(
    filterSessions(items, 'web ui').map((i) => i.thread_id),
    ['a'],
  );
  assert.deepEqual(
    filterSessions(items, 'WEB').map((i) => i.thread_id),
    ['a'],
  );
});

test('filterSessions also matches a pasted thread id', () => {
  const items = [
    item('tmp-phase5-t2', 'phase task', at(2026, 9, 11)),
    item('abc123', 'other', at(2026, 9, 11)),
  ];
  assert.deepEqual(
    filterSessions(items, 'abc1').map((i) => i.thread_id),
    ['abc123'],
  );
});

test('filterSessions returns nothing when nothing matches', () => {
  const items = [item('a', 'alpha', at(2026, 9, 11))];
  assert.deepEqual(filterSessions(items, 'zzz'), []);
});
