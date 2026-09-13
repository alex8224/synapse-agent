/**
 * Source guards for the transcript's chat layout.
 *
 * The side a turn is on *is* the role, so neither side carries a "User" /
 * "Assistant" heading, and every turn is framed the same way.
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

test('the user turn sits on the right, the assistant turn on the left', () => {
  assert.ok(
    transcript.includes('className="flex justify-end"'),
    'the user turn must be pushed to the right',
  );
  assert.ok(
    transcript.includes('flex max-w-[80%] flex-col items-end'),
    'the user content must hug the right edge',
  );
  assert.ok(
    transcript.includes('flex max-w-[80%] flex-col items-start'),
    'the assistant turn must hug the left edge',
  );
});

test('the side a turn is on replaces the role heading', () => {
  assert.ok(!transcript.includes('>User</span>'), 'the user turn must not print a role heading');
  assert.ok(
    !transcript.includes('>Assistant</span>'),
    'the assistant turn must not print a role heading',
  );
});

test('the conversation carries no bubble frame', () => {
  assert.ok(
    !transcript.includes('bg-gray-50/70 px-3 py-2'),
    'the user turn must not be boxed',
  );
  assert.ok(
    !transcript.includes('bg-white p-3.5'),
    'the assistant turn must not be boxed',
  );
});

test('the column is the shared reading width and the message text is larger', () => {
  // The width itself lives in `index.css` (`shellLayout.test.ts` pins the pairing
  // with the composer); here it only has to be that shared column, not a literal.
  assert.ok(
    transcript.includes('console-column'),
    'the transcript column must use the shared reading width',
  );
  assert.equal(
    (transcript.match(/text-base leading-relaxed/g) ?? []).length,
    2,
    'both sides must render at the larger size',
  );
});

test('assistant-side activity stays inside the left column', () => {
  // Thoughts, tools and info stay bounded instead of running the full width.
  assert.equal(
    (transcript.match(/max-w-\[85%\]/g) ?? []).length,
    3,
    'the thought, tool group and info rows must be bounded to the left column',
  );
});

test('the run log stays subordinate to the answer', () => {
  // Thought and tool rows are log lines: no filled chip, no box of their own, and
  // a smaller size than the answer they precede.
  const thought = transcript.slice(
    transcript.indexOf("if (m.type === 'thought')"),
    transcript.indexOf("if (m.type === 'tool_group')"),
  );
  assert.equal(thought.includes('bg-[#f3f4f5]'), false, 'a thought must not be a filled chip');
  assert.ok(thought.includes('text-[11px]'), 'a thought line must be smaller than the answer');
  const tools = transcript.slice(
    transcript.indexOf("if (m.type === 'tool_group')"),
    transcript.indexOf("if (m.type === 'assistant')"),
  );
  assert.equal(
    tools.includes('bg-[#f3f4f5]'),
    false,
    'a collapsed tool group must not be a filled chip either',
  );
  assert.ok(tools.includes('text-[11px]'), 'a tool line must be smaller than the answer');
});

test('the scroller keeps the reading column centred when a scrollbar appears', () => {
  // A one-sided scrollbar narrows the scroll port, which would shift the column
  // left of the composer that shares its width; symmetric gutters keep the
  // centring, and therefore the alignment, intact.
  assert.ok(
    transcript.includes('[scrollbar-gutter:stable_both-edges]'),
    'the transcript scroller must reserve symmetric scrollbar gutters',
  );
});
