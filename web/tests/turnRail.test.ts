/**
 * Offline tests for the turn rail's pure rules (no DOM, no socket).
 *
 * The rules mirror the TUI's `ui/turn_rail.py`, so the same properties are
 * pinned here: one-line previews, centred ticks while they fit, proportional
 * buckets past that, and a hover label that names a bucket by its turn range.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  TURN_RAIL_PREVIEW_MAX,
  formatTurnRailPreview,
  transcriptTurns,
  turnRailHoverText,
  turnRailSlotLabel,
  turnRailTickSlots,
} from '../src/stores/turnRail.ts';
import type { TranscriptMessage } from '../src/stores/historyMapper.ts';

function message(type: TranscriptMessage['type'], content: string, id = `${type}-1`): TranscriptMessage {
  return { id, type, timestamp: 'Turn 1', content };
}

test('a rail preview is one collapsed line with an ellipsis', () => {
  assert.equal(formatTurnRailPreview('  hello   world  '), 'hello world');
  assert.equal(formatTurnRailPreview(''), '(empty)');
  assert.equal(formatTurnRailPreview('   '), '(empty)');
  const long = 'x'.repeat(TURN_RAIL_PREVIEW_MAX + 10);
  const clipped = formatTurnRailPreview(long);
  assert.equal(clipped.length, TURN_RAIL_PREVIEW_MAX);
  assert.ok(clipped.endsWith('…'));
  // Newlines collapse too, so a multi-line paste cannot break the rail.
  assert.equal(formatTurnRailPreview('a\n\nb\tc'), 'a b c');
});

test('ticks are centred while the turns fit', () => {
  const slots = turnRailTickSlots(3, 7);
  assert.deepEqual(
    slots.map((slot) => slot.length),
    [0, 0, 1, 1, 1, 0, 0],
  );
  // Every turn appears exactly once, in order.
  assert.deepEqual(slots.flat(), [0, 1, 2]);
});

test('past the available rows the turns merge into buckets', () => {
  const slots = turnRailTickSlots(6, 3);
  assert.equal(slots.length, 3);
  assert.deepEqual(slots.flat(), [0, 1, 2, 3, 4, 5]);
  // Each row covers a contiguous range and every turn is placed.
  for (const slot of slots) {
    assert.deepEqual(slot, [...slot].sort((a, b) => a - b));
  }
  assert.deepEqual(turnRailTickSlots(0, 4).flat(), []);
});

test('a bucket label names the range it covers', () => {
  const previews = ['first turn', 'second', 'third'];
  assert.equal(turnRailSlotLabel([1], [previews[1]]), 'second');
  assert.equal(turnRailSlotLabel([0, 1, 2], previews), '#1-3 first turn');
  assert.equal(turnRailSlotLabel([], []), '');
});

test('a transcript splits into turns with their conclusions', () => {
  const messages: TranscriptMessage[] = [
    message('user', 'do the thing', 'u1'),
    message('thought', 'reasoning', 't1'),
    message('assistant', 'first draft', 'a1'),
    message('tool_group', '', 'g1'),
    message('assistant', 'final answer', 'a2'),
    message('user', 'and more', 'u2'),
  ];
  const turns = transcriptTurns(messages);
  assert.equal(turns.length, 2);
  assert.deepEqual(
    turns.map((turn) => [turn.index, turn.anchorId, turn.user, turn.conclusion]),
    [
      [1, 'u1', 'do the thing', 'final answer'],
      [2, 'u2', 'and more', ''],
    ],
  );
});

test('the hover text carries the user message and the conclusion', () => {
  const [turn] = transcriptTurns([
    message('user', 'what is the version?', 'u1'),
    message('assistant', '0.1.44', 'a1'),
  ]);
  assert.equal(turnRailHoverText(turn), '#1 what is the version?\n→ 0.1.44');
  // A turn with no answer yet shows the user line only.
  const [pending] = transcriptTurns([message('user', 'still running', 'u2')]);
  assert.equal(turnRailHoverText(pending), '#1 still running');
});
