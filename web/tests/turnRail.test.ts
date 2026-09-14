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
  TURN_RAIL_ACTIVE_SLACK_PX,
  TURN_RAIL_PREVIEW_MAX,
  activeTurnIndex,
  formatTurnRailPreview,
  transcriptTurns,
  turnRailHoverText,
  turnRailRowFor,
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

test('the active turn is the last anchor the viewport has passed', () => {
  const offsets = [0, 400, 900, 1500];
  assert.equal(activeTurnIndex(offsets, 0), 0);
  assert.equal(activeTurnIndex(offsets, 100), 0);
  assert.equal(activeTurnIndex(offsets, 400), 1);
  // Still short of the next anchor once the slack is taken into account.
  assert.equal(activeTurnIndex(offsets, 880), 1);
  assert.equal(activeTurnIndex(offsets, 899), 2);
  assert.equal(activeTurnIndex(offsets, 1500), 3);
  assert.equal(activeTurnIndex(offsets, 99_999), 3);
});

test('a scroll position inside the slack still counts as at the anchor', () => {
  const offsets = [0, 400];
  // A turn scrolled to `block: 'start'` lands a pixel or two off the edge.
  assert.equal(activeTurnIndex(offsets, 400 - TURN_RAIL_ACTIVE_SLACK_PX + 1), 1);
  assert.equal(activeTurnIndex(offsets, 400 - TURN_RAIL_ACTIVE_SLACK_PX - 1), 0);
});

test('the first turn stays active while the viewport is above every anchor', () => {
  assert.equal(activeTurnIndex([], 0), -1);
  // The port's own top padding puts the first anchor below the top edge; the
  // turn is still the one on screen.
  assert.equal(activeTurnIndex([50], 0), 0);
  assert.equal(activeTurnIndex([50], 30), 0);
});

test('an anchor the rail cannot find does not shadow the ones above it', () => {
  // A missing anchor is measured as +Infinity: the turns above it stay active
  // rather than the rail going dark.
  const offsets = [0, Number.POSITIVE_INFINITY];
  assert.equal(activeTurnIndex(offsets, 10), 0);
  assert.equal(activeTurnIndex(offsets, 99_999), 0);
});

test('the last turn is the one on screen at the bottom', () => {
  // A short last turn: its anchor sits *inside* the viewport, so the anchor rule
  // alone marked the second-to-last bar while the reader was on the last turn.
  const offsets = [0, 400, 900, 1400];
  const maxScroll = 1500;
  assert.equal(activeTurnIndex(offsets, maxScroll, TURN_RAIL_ACTIVE_SLACK_PX, maxScroll), 3);
  // Within the slack of the bottom counts as the bottom too.
  assert.equal(
    activeTurnIndex(offsets, maxScroll - TURN_RAIL_ACTIVE_SLACK_PX, TURN_RAIL_ACTIVE_SLACK_PX, maxScroll),
    3,
  );
  // Away from the bottom the anchor rule still decides.
  assert.equal(
    activeTurnIndex(offsets, maxScroll - 200, TURN_RAIL_ACTIVE_SLACK_PX, maxScroll),
    2,
  );
  // A transcript that fits: the last turn is on screen.
  assert.equal(activeTurnIndex(offsets, 0, TURN_RAIL_ACTIVE_SLACK_PX, 0), 3);
  // No turns at all stays -1.
  assert.equal(activeTurnIndex([], 0, TURN_RAIL_ACTIVE_SLACK_PX, 0), -1);
});

test('the animated row is the one whose range holds the turn', () => {
  const slots = turnRailTickSlots(3, 5);
  assert.equal(slots[1][0], 0);
  assert.equal(turnRailRowFor(slots, 0), 1);
  assert.equal(turnRailRowFor(slots, 2), 3);
  assert.equal(turnRailRowFor(slots, -1), -1);
  // Past RAIL_ROWS turns a row stands for a range: the turn maps to that row.
  const merged = turnRailTickSlots(60, 4);
  assert.deepEqual(merged.map((row) => row.length), [15, 15, 15, 15]);
  assert.equal(turnRailRowFor(merged, 0), 0);
  assert.equal(turnRailRowFor(merged, 20), 1);
  assert.equal(turnRailRowFor(merged, 59), 3);
  // A gap row draws nothing, so it can never be the animated row.
  assert.equal(turnRailRowFor(turnRailTickSlots(2, 5), 1), 2);
});
