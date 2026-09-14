/**
 * Source guards for the transcript's auto-scroll.
 *
 * Streaming must keep following the newest content, but only while the reader is
 * already at the bottom: a *view* change (opening a fold) must never scroll, and
 * neither must a stream the reader has deliberately scrolled away from.  The
 * follow is instant rather than smooth, because a smooth animation whose target
 * moves every few milliseconds is restarted (and never finishes) once per chunk.
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

test('the follow is instant, never an animation restarted per chunk', () => {
  assert.equal(
    transcript.includes("behavior: 'smooth'"),
    false,
    'a smooth follow is restarted on every chunk while a turn streams',
  );
});

test('only the reader can end the follow', () => {
  // Our own `scrollIntoView` and the browser's scroll anchoring also fire
  // `scroll`, and a layout change above the viewport (a streamed thought settling,
  // a tool row appearing) moves the scroll position on its own.  Reading the
  // "am I at the bottom?" latch from every scroll event ended the follow for the
  // rest of the turn -- the reasoning streamed into view, the tool call after it
  // did not.
  assert.ok(transcript.includes('userScrolling'), 'the follow latch needs a user-gesture flag');
  assert.ok(
    /if \(!userScrolling\.current\) return;/.test(transcript),
    'a scroll event the reader did not cause must not end the follow',
  );
  for (const gesture of ['wheel', 'touchstart', 'touchmove']) {
    assert.ok(
      transcript.includes(`addEventListener('${gesture}'`),
      `${gesture} must arm the follow latch`,
    );
  }
  assert.ok(
    transcript.includes('USER_SCROLL_SETTLE_MS'),
    'a gesture needs an end, so the latch settles shortly after its last event',
  );
  // Following re-asserts the latch, so the next update keeps following.
  assert.ok(
    /scrollIntoView\(\{ block: 'end' \}\);\s*\n\s*\/\/[^\n]*\n\s*pinnedToBottom\.current = true;/.test(
      transcript,
    ),
    'the follow must re-arm the latch after it scrolls',
  );
});

test('only a view that is already at the bottom follows the stream', () => {
  assert.ok(transcript.includes('pinnedToBottom'), 'the transcript must track whether it is pinned');
  assert.ok(
    transcript.includes('PINNED_TO_BOTTOM_PX'),
    'the pin threshold must be a named constant, not a literal',
  );
  assert.ok(
    /if \(!pinnedToBottom\.current/.test(transcript),
    'an unpinned view must not be yanked to the bottom by a stream',
  );
  assert.ok(
    transcript.includes("addEventListener('scroll'"),
    'the pin state must follow real scrolling',
  );
});

test('opening a fold never scrolls the transcript to the bottom', () => {
  assert.ok(transcript.includes('skipAutoScroll'), 'the transcript must have a scroll guard');
  assert.ok(
    /handleToggleExpand = [\s\S]{0,80}skipAutoScroll\.current = true;/.test(transcript),
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