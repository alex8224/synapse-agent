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

/** Index just past the closing brace of `@layer utilities { … }`. */
function utilitiesLayerEnd(): number {
  const start = stylesheet.indexOf('@layer utilities {');
  assert.ok(start >= 0, 'index.css must keep its utilities layer');
  let depth = 0;
  for (let i = stylesheet.indexOf('{', start); i < stylesheet.length; i += 1) {
    if (stylesheet[i] === '{') depth += 1;
    else if (stylesheet[i] === '}') {
      depth -= 1;
      if (depth === 0) return i;
    }
  }
  throw new Error('unterminated @layer utilities block');
}

test('the floor outranks the Tailwind utilities on the cells', () => {
  // A rule inside `@layer utilities` loses to a later utility of equal
  // specificity, so the floor has to be declared outside the layer.
  assert.ok(
    stylesheet.indexOf('min-width: 8rem') > utilitiesLayerEnd(),
    'the cell floor must be declared outside @layer utilities',
  );
});

test('body rows alternate so a wide row can be followed across the grid', () => {
  // The grid is deliberately quiet (Stroke2, the same pair every prose rule uses),
  // which in the light theme left a table of wrapped paths as a grey smear.  The
  // stripe is derived from the *foreground* role, so it is a dark band on light
  // and a light band on dark; at 6% it lands on a clear ~#F0F2F5 band on light,
  // strong enough to track a wrapped row across the grid.
  const rule = /\.markdown-body tbody tr:nth-child\(even\) > td \{\s*background-color:\s*rgb\(var\(--gray-900\) \/ 0\.06\);/;
  assert.ok(rule.test(stylesheet), 'even body rows must carry the stripe');
  assert.ok(
    stylesheet.indexOf('.markdown-body tbody tr:nth-child(even)') > utilitiesLayerEnd(),
    'the stripe must outrank the cell utilities',
  );
});

test('the table paints an opaque surface of its own', () => {
  // The transcript sits on a semi-transparent mica backdrop; without an opaque
  // surface role the cells would show it through and the grid would smear.
  const rule = /\.markdown-body table \{\s*background-color:\s*rgb\(var\(--surface\)\);/;
  assert.ok(rule.test(stylesheet), 'the table must paint the surface role');
  assert.ok(
    stylesheet.indexOf('.markdown-body table') > utilitiesLayerEnd(),
    'the table surface must outrank the cell utilities',
  );
});

test('body rows highlight on hover', () => {
  const rule = /\.markdown-body tbody tr:hover > td \{\s*background-color:\s*rgb\(var\(--surface-hover\)\);/;
  assert.ok(rule.test(stylesheet), 'hovering a body row must highlight its cells');
  assert.ok(
    stylesheet.indexOf('.markdown-body tbody tr:hover') > utilitiesLayerEnd(),
    'the hover fill must outrank the cell utilities',
  );
});

test('the frame is drawn once, by the scroll wrapper', () => {
  assert.ok(
    table.includes('rounded-control border border-line bg-surface'),
    'the wrapper must own the frame and the opaque surface',
  );
  assert.ok(
    table.includes('w-full border-collapse text-sm'),
    'the table must fill the wrapper it is framed by',
  );
});

test('inline code in a cell is outlined against the stripes', () => {
  const rule = /\.markdown-body td code \{\s*border:\s*1px solid rgb\(var\(--line\)\);/;
  assert.ok(rule.test(stylesheet), 'a code span must keep its own outline in a striped cell');
});

test('the header boundary is one step stronger than the grid', () => {
  // Head and body have to stay distinguishable even with striped rows below, and
  // the header fill alone (#F5F5F5 on a #FFFFFF card) is not enough in the light
  // theme.  `--gray-300` is Fluent's `colorNeutralStroke1`, one step above the
  // `colorNeutralStroke2` grid the cells keep.
  const rule = /\.markdown-body thead th \{[^}]*border-bottom-width:\s*2px;[^}]*border-bottom-color:\s*rgb\(var\(--gray-300\)\);/;
  assert.ok(rule.test(stylesheet), 'the header needs a stronger bottom rule than the grid');
  assert.ok(
    stylesheet.indexOf('.markdown-body thead th') > utilitiesLayerEnd(),
    'the header rule must outrank the cell utilities',
  );
});