/**
 * Source guards for the Markdown table rendering.
 *
 * Tables used to render at `text-xs` (12px), which read as a footnote next to the
 * 16px chat prose.  They now use the body's reading scale (`text-sm`, 14px) with
 * the header at the same size (weight, not size, marks it), roomier cells, and an
 * independent horizontal scroller so a wide table cannot stretch the transcript.
 *
 * The column floor is guarded too: automatic table layout squeezes a short
 * column down to its minimum content width, which for Chinese is one character,
 * so a label such as `B. 新功能文件` used to render as a four-line ribbon next to
 * a wide neighbour.  The floor lives in `index.css` (an arbitrary Tailwind
 * `min-w-[8rem]` class is not emitted unless it is used in a scanned file, and
 * the cells carry no such class).
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
const stylesheet = readFileSync(join(here, '..', 'src', 'index.css'), 'utf8');

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
  // The header row is a layer, not a colour of its own: Fluent's table header is
  // `colorNeutralBackground3` over `colorNeutralStroke2` borders, the same pair the
  // code blocks use.
  assert.ok(
    table.includes(
      'border border-line bg-sunken px-3 py-1.5 text-left font-semibold text-gray-800',
    ),
    'the header must share the cell padding and size, marked only by font-semibold',
  );
});

test('the body cells get roomier padding', () => {
  assert.ok(
    table.includes('border border-line px-3 py-1.5 align-top text-gray-700'),
    'the cells must use the roomier padding',
  );
});

test('a wide table scrolls on its own instead of stretching the column', () => {
  assert.ok(table.includes('overflow-x-auto'), 'the table needs its own horizontal scroller');
  assert.ok(table.includes('max-w-full'), 'the scroller must stay inside the reading column');
});

test('no column can be squeezed into a character-wide ribbon', () => {
  const rule = /\.markdown-body th,\s*\n\.markdown-body td\s*\{[^}]*min-width:\s*([\d.]+)rem/;
  const match = rule.exec(stylesheet);
  assert.ok(match, 'the cells need a min-width floor declared on .markdown-body');
  assert.ok(
    Number(match[1]) >= 6,
    `the floor must leave room for a short label, got ${match?.[1]}rem`,
  );
});

test('the floor outranks the Tailwind utilities on the cells', () => {
  // A rule inside `@layer utilities` loses to a later utility of equal
  // specificity, so the floor has to be declared outside the layer.
  const start = stylesheet.indexOf('@layer utilities {');
  assert.ok(start >= 0, 'index.css must keep its utilities layer');
  let depth = 0;
  let end = -1;
  for (let i = stylesheet.indexOf('{', start); i < stylesheet.length; i += 1) {
    if (stylesheet[i] === '{') depth += 1;
    else if (stylesheet[i] === '}') {
      depth -= 1;
      if (depth === 0) {
        end = i;
        break;
      }
    }
  }
  assert.ok(end > start, 'the utilities layer must be closed');
  assert.ok(
    stylesheet.indexOf('min-width: 8rem') > end,
    'the cell floor must be declared outside @layer utilities',
  );
});