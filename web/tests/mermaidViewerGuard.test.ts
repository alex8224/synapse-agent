/**
 * Source guards for the diagram card and its full-screen viewer.
 *
 * `mermaidPolicy.test.ts` proves the sizing arithmetic; these guards pin the
 * wiring, which is where the requirement silently regresses:
 *
 * - mermaid sizes its own root SVG (`width="100%"` plus an inline `max-width`,
 *   which no stylesheet rule can outrank), so a card that forgets to freeze the
 *   diagram back to its `viewBox` size pins every drawing to the reading column
 *   and can never scroll;
 * - a stage without a height cap lets one drawing stretch a transcript row to
 *   its full height, which is the "cannot see it all" half of the problem;
 * - a viewer that reimplements zoom/pan instead of sharing `imageZoom.ts` is a
 *   second copy of the same rule to keep correct, and a wheel listener React
 *   registers as passive cannot stop the page scroll.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const read = (relative: string): string => readFileSync(join(here, '..', relative), 'utf8');

const block = read('src/components/MermaidBlock.tsx');
const lightbox = read('src/components/MermaidLightbox.tsx');
const policy = read('src/markdown/mermaid.ts');
const styles = read('src/index.css');

test('the card bounds a tall diagram instead of stretching the row', () => {
  assert.ok(block.includes('max-h-[28rem]'), 'the stage must cap its height');
  assert.ok(block.includes('overflow-auto'), 'the stage must scroll rather than clip');
  assert.ok(block.includes('fluent-scrollbar'), 'the stage must use the shared scrollbar');
});

test('the diagram is never pinned to the column by mermaid own attributes', () => {
  assert.ok(
    policy.includes('export function freezeSvgSize'),
    'the frozen size must live in the pure policy module',
  );
  assert.ok(block.includes('freezeSvgSize('), 'the card must freeze what mermaid rendered');
  assert.ok(block.includes('mermaid-fit'), 'the default view must be the column fit');
  assert.ok(block.includes('mermaid-actual'), 'the card must be able to show 1:1');
  assert.ok(
    styles.includes('.mermaid-diagram.mermaid-actual svg'),
    '1:1 must be a rule the stylesheet can win',
  );
});

test('the card reports the size and offers the viewer', () => {
  assert.ok(block.includes('data-diagram-size'), 'the measured size must be readable');
  assert.ok(block.includes('原始大小'), 'the 1:1 toggle must be reachable from the card');
  assert.ok(block.includes('放大'), 'the viewer must be reachable from the card');
  assert.ok(block.includes('onClick={openViewer}'), 'the enlarge button must open the viewer');
  assert.ok(block.includes('<MermaidLightbox'), 'the card must mount the viewer');
});

test('a new diagram starts from the default view', () => {
  // The view flags are tagged with the source they were chosen for and derived
  // during render, so a redrawn diagram cannot inherit a stale zoom or 1:1.
  assert.ok(block.includes('view.source === source'), 'the view flags must be source-tagged');
  assert.ok(block.includes('zoomed = mine?.zoomed ?? false'), 'the viewer flag is derived');
  assert.ok(block.includes('actualSize = mine?.actual ?? false'), 'the 1:1 flag is derived');
});

test('the viewer is the shared zoom arithmetic, not a second implementation', () => {
  assert.ok(lightbox.includes("from './imageZoom.ts'"), 'the viewer must share the arithmetic');
  for (const call of ['initialScale(', 'fitScale(', 'clampOffset(', 'scaledSize(', 'zoomAround(']) {
    assert.ok(lightbox.includes(call), `MermaidLightbox.tsx must use ${call}`);
  }
  assert.ok(lightbox.includes('wheelSteps('), 'small wheel deltas must accumulate');
  assert.ok(lightbox.includes('steppedScale(scale, 1)'), 'there must be a zoom-in step');
  assert.ok(lightbox.includes('steppedScale(scale, -1)'), 'there must be a zoom-out step');
  assert.ok(lightbox.includes("event.key === '0'"), 'the keyboard must reach the fit');
  assert.ok(lightbox.includes("event.key === '1'"), 'the keyboard must reach 1:1');
  assert.ok(lightbox.includes("event.key === 'Escape'"), 'Escape must dismiss the viewer');
  assert.ok(lightbox.includes('data-mermaid-scale'), 'the current scale must be readable');
});

test('the wheel zooms at the pointer and never scrolls the page', () => {
  assert.ok(
    lightbox.includes("addEventListener('wheel', onWheel, { passive: false })"),
    'a passive wheel listener cannot preventDefault, so the transcript would scroll instead',
  );
  assert.ok(lightbox.includes("removeEventListener('wheel', onWheel)"));
  assert.ok(lightbox.includes('event.preventDefault()'));
  assert.ok(lightbox.includes('event.deltaMode'), 'the wheel must respect the reported unit');
  assert.ok(lightbox.includes('setPointerCapture('), 'panning must survive the pointer leaving');
});

test('the viewer is a portal over the window, not part of the transcript row', () => {
  assert.ok(lightbox.includes('Portal'), 'the overlay belongs to the window');
  assert.ok(lightbox.includes('role="dialog"') && lightbox.includes('aria-modal="true"'));
  assert.ok(lightbox.includes('material-flyout flyout-in'), 'a flyout carries its entrance');
  // `markdownRenderGuard.test.ts` keeps `document.body` out of the card itself.
  assert.equal(/document\s*\.\s*body/.test(block.replace(/\/\*[\s\S]*?\*\//g, '')), false);
});

test('the diagram is rendered exactly once, so no mermaid id is duplicated', () => {
  // mermaid's ids are not namespaced (`actor0`, `root-0`, ... besides the
  // diagram's own id, and its theme CSS is scoped by `#<svgId>`), so a second
  // copy would duplicate them.  The card empties its stage while the viewer is
  // open instead, keeping the measured height so the row cannot collapse.
  const rendered = block.match(/<GeneratedHtml\b/g) ?? [];
  assert.equal(rendered.length, 1, 'the card injects the diagram exactly once');
  assert.ok(block.includes('{!zoomed && ('), 'the card must empty its stage while zoomed');
  assert.ok(block.includes('placeholder'), 'the emptied stage must keep its measured height');
  assert.ok(block.includes('getBoundingClientRect().height'), 'that height must be measured');
});

test('the zoomed SVG is not clamped back to the stage', () => {
  assert.ok(lightbox.includes('mermaid-zoom-box'), 'the scaled box must be nameable');
  assert.ok(
    styles.includes('.mermaid-zoom-box svg'),
    'the box must size the SVG, or the frozen attributes would win',
  );
  assert.ok(styles.includes('max-width: none'), 'the fit rule must not clamp the zoom');
});
