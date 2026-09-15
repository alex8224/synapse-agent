/**
 * Source guards for the image preview's zoom.
 *
 * `imageZoom.test.ts` proves the arithmetic; these guards pin the wiring, which
 * is where the requirement can silently regress: a preview that opens below 75%
 * of the image, a wheel listener React registers as passive (so `preventDefault`
 * is ignored and the page scrolls instead), or a Tailwind preflight rule quietly
 * clamping the zoomed size back to the stage width.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const read = (relative: string): string => readFileSync(join(here, '..', relative), 'utf8');

const lightbox = read('src/components/ImageLightbox.tsx');
const zoom = read('src/components/imageZoom.ts');

test('the 75% floor is a named constant, not a magic number in the component', () => {
  assert.ok(
    zoom.includes('export const MIN_PREVIEW_SCALE = 0.75'),
    'imageZoom.ts must define the preview floor',
  );
  assert.ok(
    lightbox.includes("from './imageZoom.ts'"),
    'ImageLightbox.tsx must use the shared arithmetic',
  );
  assert.ok(
    lightbox.includes('initialScale(natural, stage)'),
    'the opening scale must come from initialScale',
  );
});

test('the preview no longer caps the image at a fixed panel size', () => {
  assert.ok(!lightbox.includes('max-w-4xl'), 'the 896px panel cap must be gone');
  assert.ok(!lightbox.includes('max-h-[75vh]'), 'the 75vh cap must be gone');
  assert.ok(
    lightbox.includes('max-w-none'),
    'Tailwind preflight caps every image at max-width:100%, which would clamp a zoomed image',
  );
});

test('zoom and pan are applied as explicit pixels, not by CSS class', () => {
  assert.ok(lightbox.includes('width: rendered.width'), 'the rendered width must be set per scale');
  assert.ok(lightbox.includes('height: rendered.height'), 'the rendered height must be set per scale');
  assert.ok(lightbox.includes('clampOffset('), 'panning must go through the clamp');
  assert.ok(lightbox.includes('scaledSize('), 'the pan limit must use the scaled size');
});

test('the wheel listener is registered non-passively', () => {
  assert.ok(
    lightbox.includes("addEventListener('wheel', onWheel, { passive: false })"),
    'a passive wheel listener cannot preventDefault, so the transcript would scroll instead',
  );
  assert.ok(
    lightbox.includes("removeEventListener('wheel', onWheel)"),
    'the wheel listener must be removed on unmount',
  );
  assert.ok(lightbox.includes('event.preventDefault()'), 'the wheel must not scroll the page');
});

test('zoom is reachable from the pointer and from the keyboard', () => {
  assert.ok(lightbox.includes('steppedScale(scale, 1)'), 'there must be a zoom-in step');
  assert.ok(lightbox.includes('steppedScale(scale, -1)'), 'there must be a zoom-out step');
  assert.ok(lightbox.includes('zoomAround('), 'a wheel zoom must be anchored at the pointer');
  assert.ok(lightbox.includes('fitScale(natural, stage)'), 'there must be a whole-image fit');
  assert.ok(lightbox.includes("event.key === '0'"), 'the keyboard must reach the fit');
  assert.ok(lightbox.includes("event.key === '1'"), 'the keyboard must reach 1:1');
  assert.ok(lightbox.includes('data-image-zoom'), 'the current scale must be readable');
});

test('the zoom step is a fixed five percentage points', () => {
  assert.ok(
    zoom.includes('export const ZOOM_STEP = 0.05'),
    'imageZoom.ts must define the step',
  );
  assert.ok(
    zoom.includes('scale + steps * ZOOM_STEP'),
    'a step must be additive, so the readout moves 75% -> 80% -> 85%',
  );
  assert.ok(!zoom.includes('WHEEL_STEP'), 'the old multiplicative step must be gone');
  assert.ok(
    lightbox.includes('wheelSteps('),
    'the wheel must accumulate small deltas into whole notches',
  );
  assert.ok(
    lightbox.includes('event.deltaMode'),
    'the wheel must respect the unit the browser reports',
  );
});

test('imageZoom.ts stays pure', () => {
  const code = zoom.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  assert.ok(!/\b(document|window)\s*\./.test(code), 'imageZoom.ts must stay DOM-free');
  assert.ok(!/from '/.test(code), 'imageZoom.ts must have no imports');
  assert.ok(!/\bfetch\s*\(/.test(code), 'imageZoom.ts must never fetch');
});
