/**
 * Source guards for the pinned "已工作 N 秒" header of an opened fold.
 *
 * A turn's header is the top of its fold, and the transcript keeps following the
 * newest line while that fold grows.  Left in flow the header is carried off the
 * top edge, so the reader ends up watching the tail of a block they can no longer
 * identify.  The header is therefore a pinned strip: it sticks to the top of the
 * visible area and the fold scrolls *under* it.  Both halves of that -- the pin and
 * the paint that hides what slides beneath it -- are pinned here.
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

test('every "已工作" header is the pinned strip', () => {
  // Five headers print the strip: the thought and the tool fold in both states, and
  // the pending row.  A header left in flow is the one that gets pushed off the top.
  assert.equal(
    (transcript.match(/className="transcript-fold-header material-titlebar"/g) ?? []).length,
    5,
    'every header must be the pinned strip',
  );
  assert.equal(
    (transcript.match(/<span>已工作 /g) ?? []).length,
    (transcript.match(/className="transcript-fold-header material-titlebar"/g) ?? []).length,
    'no header may be printed outside the strip',
  );
});

test('the strip wraps the header and its rule', () => {
  // The strip is what `sticky` is applied to, so the button and the rule have to be
  // *inside* it: pinned alone, the button would leave its rule behind, and a strip
  // that stopped before the rule would let the fold scroll between the two.
  const strips =
    transcript.match(
      /<div className="transcript-fold-header material-titlebar">[\s\S]*?<\/button>[\s\S]*?border-b border-line\/60 my-2\.5" \/>\s*\n\s*<\/div>/g,
    ) ?? [];
  assert.equal(strips.length, 5, 'each strip must hold its button and the rule under it');
});

test('the pin clears the chrome bar and lands on the pane inset', () => {
  // The scroller runs up behind the chrome bar (`.console-pane-inset`), so `top: 0`
  // would pin the header *behind* it.  The offset has to clear the bar by the same
  // inset the pane reserves for `scroll-padding-top`, which is what puts the pinned
  // strip exactly where the pane's content starts -- the two must move together.
  const strip = blockOf('.transcript-fold-header');
  assert.ok(/position:\s*sticky/.test(strip), 'the header strip must be sticky');
  assert.ok(
    strip.includes('top: calc(var(--chrome-h) + 1.5rem);'),
    'the pin must clear the chrome bar by the pane inset',
  );
  assert.ok(
    blockOf('.console-pane-inset').includes('scroll-padding-top: calc(var(--chrome-h) + 1.5rem);'),
    'the pin and the pane inset must be the same offset',
  );
  // The fold is painted after the header, so without a layer of its own it would
  // slide *over* the pinned strip instead of under it.
  assert.ok(/z-index:\s*[1-9]/.test(strip), 'the strip must paint above the fold it pins');
});

test('the strip takes the theme material and keeps the rule margins inside it', () => {
  const strip = blockOf('.transcript-fold-header');
  // A pinned strip that paints nothing lets the fold show through it, and one that
  // is not a formatting context lets the rule's margins collapse out through it,
  // which opens a transparent band at its foot -- the same leak, 10px lower.
  assert.ok(/display:\s*flow-root/.test(strip), 'the strip must keep the rule margins inside it');
  assert.equal(
    /background(-color)?:\s*(#|rgb|hsl)/.test(strip),
    false,
    'the strip must take a material role, not a colour of its own',
  );
  assert.ok(
    transcript.includes('transcript-fold-header material-titlebar'),
    'the strip must name its material role so the theme still decides the fill',
  );
});
