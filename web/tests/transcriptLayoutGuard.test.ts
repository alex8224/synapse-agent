/**
 * Source guards for the transcript's *dispatcher* layout.
 *
 * What a single row paints is pinned by `transcriptRowContract.test.ts`, which reads
 * the row modules; what is left here is the column, the scroller and the gap between
 * rows -- the parts every kind shares.  Geometry only a browser can measure (where a
 * rule sits, how far apart two rows land) lives in `transcriptFoldSpace.verify.ts`.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { rowPaints } from '../src/stores/turnWork.ts';
import { dispatcherSource } from './helpers/transcriptSource.ts';

const dispatcher = dispatcherSource();

test('the column is the shared reading width', () => {
  assert.ok(
    dispatcher.includes('console-column'),
    'the transcript column must use the shared reading width',
  );
});

test('the scroller scrolls with no visible scrollbar', () => {
  // The column keeps both its edges -- and so stays on the composer card's -- only if
  // no scrollbar takes a bite out of it, the way the sidebar tree already works.
  // Scrolling itself (wheel, touch, keyboard) must stay.
  assert.ok(dispatcher.includes('no-scrollbar'), 'the scroller must hide its scrollbar');
  assert.ok(dispatcher.includes('overflow-y-auto'), 'the transcript must still scroll');
  assert.equal(
    dispatcher.includes('scrollbar-gutter'),
    false,
    'a hidden scrollbar leaves nothing to reserve',
  );
});

test('a row the fold hides gets no gap either', () => {
  // The wrapper exists per index -- it is what the virtualizer positions and measures --
  // so an unconditional row gap put 20px of blank in place of every step a collapsed
  // turn hides, and the dead space grew with each step it took.
  assert.ok(
    /className=\{paints\s*\?[\s\S]{0,160}?: 'absolute left-0 top-0 w-full'\}/.test(dispatcher),
    'the row gap must be conditional on the row painting',
  );
  assert.ok(dispatcher.includes('rowPaints(m, meta)'), 'the wrapper must ask the same rule the row does');
});

test('a batch boundary is not a paragraph break', () => {
  // Two calls of one batch and the first call of the next batch are the same distance
  // apart: a batch is no longer a container on screen, so its edge must not space rows
  // differently -- which read as "parallel calls are closer together".
  assert.ok(
    dispatcher.includes("continuesCalls ? 'pb-0.5' : 'pb-5'"),
    'a batch boundary must not open a gap between two calls',
  );
  assert.ok(
    dispatcher.includes("next.type === 'tool_group'"),
    'the tighter gap is for a following batch, not for any following row',
  );
});

test('a step with nothing in it is not a step', () => {
  // The rule lives in the policy table, so a kind declares its own behaviour and the
  // fold reads one table instead of a chain of type checks.  An empty batch would
  // otherwise be a blank row of its own: the TUI never paints an empty "0 tools".
  const collapsed = { isFirst: false, isExpanded: false };
  assert.equal(rowPaints({ type: 'tool_group', tools: [] } as never), false);
  assert.equal(rowPaints({ type: 'tool_group', tools: [{}] } as never, collapsed), false);
  assert.equal(
    rowPaints({ type: 'tool_group', tools: [{}] } as never, { isFirst: true, isExpanded: false }),
    true,
  );
  assert.equal(rowPaints({ type: 'thought' } as never, collapsed), false);
  assert.equal(rowPaints({ type: 'thought' } as never, { isFirst: false, isExpanded: true }), true);
  // A row that is not a step is not hidden by the fold at all.
  assert.equal(rowPaints({ type: 'assistant' } as never, collapsed), true);
  assert.equal(rowPaints({ type: 'user' } as never, collapsed), true);
  assert.equal(rowPaints({ type: 'info' } as never, collapsed), true);
});
