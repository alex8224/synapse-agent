/**
 * Source guards for the turn rail's wiring.
 *
 * The rail is only useful if the transcript can be scrolled to a turn, so the
 * anchor and the jump have to stay in step, and the rail must keep using the
 * shared rules rather than growing its own copy.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const read = (name: string) => readFileSync(join(here, '..', 'src', 'components', name), 'utf8');

const rail = read('TurnRail.tsx');
const transcript = read('Transcript.tsx');

test('the rail maps turns with the shared rules', () => {
  for (const helper of ['transcriptTurns', 'turnRailTickSlots', 'turnRailSlotLabel', 'turnRailHoverText']) {
    assert.ok(rail.includes(helper), `the rail must use ${helper}`);
  }
  // A single-turn transcript has nothing to navigate.
  assert.ok(rail.includes('turns.length < 2'), 'the rail must hide itself for one turn');
});

test('clicking a rail row scrolls to that turn', () => {
  assert.ok(rail.includes('scrollIntoView'), 'a rail row must jump to its turn');
  assert.ok(rail.includes('data-turn-id='), 'the rail must look the anchor up by turn id');
  assert.ok(
    transcript.includes('data-turn-id={m.id}'),
    'the transcript must anchor each user turn with its id',
  );
  assert.ok(transcript.includes('<TurnRail />'), 'the transcript must render the rail');
});
