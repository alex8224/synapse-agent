/**
 * Source guards for the "已工作 N 秒" header of a fold.
 *
 * The header is the top of its fold, and the transcript keeps following the newest
 * line while that fold grows.  It stays a line of the reading column: it is neither
 * pinned to the top of the visible area nor painted as a bar, so no part of the fold
 * is masked while the column scrolls.  That -- no pin, no paint, and the rule still
 * owned by the header -- is what these guards pin down.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = join(here, '..');
const transcript = readFileSync(join(webRoot, 'src', 'components', 'Transcript.tsx'), 'utf8');
const styles = readFileSync(join(webRoot, 'src', 'index.css'), 'utf8');

/** The `{ ... }` body of the first block whose selector matches. */
function blockOf(selector: string): string {
  const at = styles.indexOf(selector);
  assert.ok(at >= 0, `index.css must contain ${selector}`);
  const open = styles.indexOf('{', at);
  let depth = 0;
  for (let i = open; i < styles.length; i += 1) {
    if (styles[i] === '{') depth += 1;
    else if (styles[i] === '}') {
      depth -= 1;
      if (depth === 0) return styles.slice(open, i);
    }
  }
  throw new Error(`unclosed block for ${selector}`);
}

test('every "已工作" header is the fold strip', () => {
  // Five headers print the strip: the thought and the tool fold in both states, and
  // the pending row.  A header without the strip loses the rule that belongs to it.
  assert.equal(
    (transcript.match(/className="transcript-fold-header"/g) ?? []).length,
    5,
    'every header must print the fold strip',
  );
  assert.equal(
    (transcript.match(/<span>已工作 /g) ?? []).length,
    (transcript.match(/className="transcript-fold-header"/g) ?? []).length,
    'no header may be printed outside the strip',
  );
  assert.equal(
    transcript.includes('transcript-fold-header material-titlebar'),
    false,
    'the strip must not take the chrome material: it is a line, not a bar',
  );
});

test('the strip wraps the header and its rule', () => {
  // The strip keeps the button and the rule together, so a fold can never end up with
  // a header whose rule drifted away from it.
  const strips =
    transcript.match(
      /<div className="transcript-fold-header">[\s\S]*?<\/button>[\s\S]*?border-b border-line\/60 my-2\.5" \/>\s*\n\s*<\/div>/g,
    ) ?? [];
  assert.equal(strips.length, 5, 'each strip must hold its button and the rule under it');
});

test('the header stays in flow instead of pinning over the column', () => {
  // A pinned header paints over the fold it names, so the reader scrolling a long
  // thought sees its text cut by a bar mid-column.  The header is a line of the
  // column: no pin, no layer of its own, no offset into the scrollport.
  const strip = blockOf('.transcript-fold-header');
  assert.equal(/position:\s*sticky/.test(strip), false, 'the header must not be pinned');
  assert.equal(/position:\s*fixed/.test(strip), false, 'the header must not float');
  assert.equal(/(^|[^-])top:\s/.test(strip), false, 'the header must take no scrollport offset');
  assert.equal(/z-index:\s*[1-9]/.test(strip), false, 'the header must take no layer of its own');
});

test('the strip paints nothing and keeps the rule margins inside it', () => {
  const strip = blockOf('.transcript-fold-header');
  // A strip that is not a formatting context lets the rule's margins collapse out
  // through it, which opens a gap between the header and the rule that belongs to it.
  assert.ok(/display:\s*flow-root/.test(strip), 'the strip must keep the rule margins inside it');
  assert.equal(
    /background(-color)?:\s*(#|rgb|hsl)/.test(strip),
    false,
    'the strip must not paint a colour of its own',
  );
  assert.equal(/background/.test(strip), false, 'the strip must carry no fill at all');
});
