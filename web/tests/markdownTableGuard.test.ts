/**
 * Source guards for the Markdown table rendering.
 *
 * Tables used to render at `text-xs` (12px), which read as a footnote next to the
 * 16px chat prose.  They now use the body's reading scale (`text-sm`, 14px) with
 * the header at the same size (weight, not size, marks it), roomier cells, and an
 * independent horizontal scroller so a wide table cannot stretch the transcript.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const markdown = readFileSync(
  join(here, '..', 'src', 'components', 'Markdown.tsx'),
  'utf8',
);

/** The `if (block.type === 'table')` branch up to the final paragraph fallback. */
const table = markdown.slice(
  markdown.indexOf("if (block.type === 'table')"),
  markdown.indexOf('<p key={key}'),
);

test('the table body reads at the chat scale, not a footnote size', () => {
  assert.ok(table.includes('text-sm'), 'the table must render at the body reading size');
  assert.equal(table.includes('text-xs'), false, 'the old footnote size must be gone');
});

test('the header keeps the cell size and only differs by weight', () => {
  assert.ok(
    table.includes(
      'border border-gray-200 bg-[#f8f9fa] px-3 py-1.5 text-left font-semibold text-gray-800',
    ),
    'the header must share the cell padding and size, marked only by font-semibold',
  );
});

test('the body cells get roomier padding', () => {
  assert.ok(
    table.includes('border border-gray-200 px-3 py-1.5 align-top text-gray-700'),
    'the cells must use the roomier padding',
  );
});

test('a wide table scrolls on its own instead of stretching the column', () => {
  assert.ok(table.includes('overflow-x-auto'), 'the table needs its own horizontal scroller');
  assert.ok(table.includes('max-w-full'), 'the scroller must stay inside the reading column');
});