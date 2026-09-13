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

test('the column is centred and the message text is larger', () => {
  assert.ok(transcript.includes('mx-auto max-w-4xl'), 'the transcript column must be centred');
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
