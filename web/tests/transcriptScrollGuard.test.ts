/**
 * Source guards for the transcript's auto-scroll.
 *
 * Streaming must keep following the newest content, but a *view* change — opening
 * a thought or a tool group — must not: the store replaces the `messages` array
 * to flip the flag, and the auto-scroll effect keys off that array identity, so
 * without the guard the fold opened off-screen at the bottom of the page.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const transcript = readFileSync(
  join(here, '..', 'src', 'components', 'Transcript.tsx'),
  'utf8',
);

test('new content still scrolls the transcript to the bottom', () => {
  assert.ok(transcript.includes('scrollIntoView'), 'the transcript must follow new content');
  assert.ok(/}, \[messages\]\)/.test(transcript), 'the effect must still key off the messages array');
});

test('opening a fold never scrolls the transcript to the bottom', () => {
  assert.ok(transcript.includes('skipAutoScroll'), 'the transcript must have a scroll guard');
  assert.ok(
    /handleToggleExpand = \(id: string\) => \{\s*skipAutoScroll\.current = true;/.test(transcript),
    'an expand toggle must raise the guard before it mutates the messages array',
  );
  // Every fold toggle goes through the guarded handler; a direct call would
  // reintroduce the jump.
  assert.ok(
    !transcript.includes('onClick={() => toggleMessageExpand'),
    'no fold may call toggleMessageExpand directly',
  );
  assert.equal(
    (transcript.match(/onClick=\{\(\) => handleToggleExpand\(m\.id\)\}/g) ?? []).length,
    2,
    'both the thought fold and the tool group must use the guarded handler',
  );
});

test('prepending an earlier page keeps its own guard', () => {
  assert.ok(
    /handleLoadEarlier = \(\) => \{\s*skipAutoScroll\.current = true;/.test(transcript),
    'loading an earlier page must not yank the view to the bottom either',
  );
});
