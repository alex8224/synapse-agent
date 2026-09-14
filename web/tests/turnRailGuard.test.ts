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

test('a rail bar lengthens under the pointer and settles back', () => {
  assert.ok(
    /group-hover:w-\d/.test(rail),
    'a hovered bar must lengthen through the group-hover width',
  );
  assert.ok(
    rail.includes('transition-[width,background-color]'),
    'the lengthening must be animated, not a jump',
  );
  // The bar must not carry two conflicting widths at once: the animated row
  // takes the lengthened width outright instead of relying on stylesheet order.
  assert.ok(
    /const resting = .*\n/.test(rail) && rail.includes('onScreen ? BAR_LENGTHENED : resting'),
    'the resting and lengthened widths must be exclusive',
  );
});

test('the rail follows the transcript and animates the turn on screen', () => {
  assert.ok(
    rail.includes("addEventListener('scroll'"),
    'the rail must follow the transcript port, not only the pointer',
  );
  assert.ok(
    rail.includes('requestAnimationFrame'),
    'the follow must be coalesced onto a frame',
  );
  assert.ok(
    rail.includes('activeTurnIndex') && rail.includes('turnRailRowFor'),
    'the on-screen turn must come from the shared rules',
  );
  // The transcript owns the port the rail listens to.
  assert.ok(
    transcript.includes('scrollerRef'),
    'the transcript must keep its own scroller handle',
  );
});
