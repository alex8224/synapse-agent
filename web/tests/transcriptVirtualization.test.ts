/**
 * Guards for the transcript's virtual window.
 *
 * A long session projects well over a thousand rows, and mounting them all in one
 * commit is what froze the console.  The transcript therefore mounts only the rows
 * near the viewport -- but the scroll rules themselves (the follow, the pin latch,
 * the fold guard, the prepend) stay the transcript's own, so they are pinned here
 * by source as well as by the rules below.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import {
  ESTIMATED_ROW_PX,
  OVERSCAN_ROWS,
  anchoredScrollTop,
} from '../src/components/transcriptViewport.ts';

const here = dirname(fileURLToPath(import.meta.url));
const transcript = readFileSync(
  join(here, '..', 'src', 'components', 'Transcript.tsx'),
  'utf8',
);

test('only the rows near the viewport are mounted', () => {
  assert.ok(
    transcript.includes('virtualizer.getVirtualItems()'),
    'the list must render the virtualizer window, not every message',
  );
  assert.equal(
    /messages\.map\(\(m\)/.test(transcript),
    false,
    'mapping every message is what froze a long session',
  );
  assert.ok(
    transcript.includes('virtualizer.measureElement'),
    'each mounted row must report its real height back',
  );
  assert.ok(
    transcript.includes('height: totalSize'),
    'the list must reserve the full scroll height of every row',
  );
});

test('row identity is the message id, so a prepend keeps its measurements', () => {
  assert.ok(
    /getItemKey: \(index\) => messages\[index\]\?\.id/.test(transcript),
    'measurements keyed by index would be re-labelled by a prepended page',
  );
});

test('the transcript still owns its scrollport and its scroll rules', () => {
  // The virtualizer only decides *which* rows exist: the scroller, the follow, the
  // pin latch and the prepend anchor stay ours.
  for (const owned of [
    'scrollerRef',
    'pinnedToBottom',
    'userScrolling',
    'skipAutoScroll',
    'prependAnchor',
  ]) {
    assert.ok(transcript.includes(owned), `the transcript must keep ${owned}`);
  }
  assert.ok(
    transcript.includes('scrollMargin'),
    'the list offset inside the scrollport must be declared to the virtualizer',
  );
});

test('a prepended page keeps the distance from the bottom', () => {
  // The prepend grows the content above the viewport, so the distance from the
  // bottom is the invariant: `scrollHeight - scrollTop` is captured before the
  // page arrives and re-applied until the new rows have been measured.
  assert.equal(anchoredScrollTop(1000, 200), 800);
  assert.equal(anchoredScrollTop(1500, 200), 1300, 'the added height shifts the viewport down');
  assert.equal(anchoredScrollTop(100, 200), 0, 'the result is never negative');
  assert.ok(
    /prependAnchor\.current = scroller === null \? null : scroller\.scrollHeight - scroller\.scrollTop/
      .test(transcript),
    'the anchor must be captured before the store prepends',
  );
  assert.ok(
    transcript.includes('anchoredScrollTop(scroller.scrollHeight, distance)'),
    'the captured distance must be re-applied from the new scroll height',
  );
});

test('the window is sized for a chat row, not a line', () => {
  assert.ok(ESTIMATED_ROW_PX >= 40, 'a row is a message, not a log line');
  assert.ok(OVERSCAN_ROWS >= 2, 'the wheel needs slack beyond each edge');
});
