/**
 * Offline tests for the preview zoom arithmetic.  Pure functions only: no DOM,
 * no client, no socket.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  MAX_SCALE,
  MIN_PREVIEW_SCALE,
  MIN_SCALE,
  clampOffset,
  clampScale,
  fitScale,
  initialScale,
  panLimit,
  scaledSize,
  steppedScale,
  wheelSteps,
  zoomAround,
} from '../src/components/imageZoom.ts';

/** The chart from the token report: wide, so a fit-to-viewport would crush it. */
const CHART = { width: 2200, height: 1350 };
/** A 1920x1080 viewport with the lightbox's own padding removed. */
const STAGE = { width: 1856, height: 1016 };

test('the default preview scale is never below 75% of the image', () => {
  for (const image of [
    CHART,
    { width: 4000, height: 3000 },
    { width: 1200, height: 900 },
    { width: 200, height: 100 },
  ]) {
    const scale = initialScale(image, STAGE);
    assert.ok(
      scale >= MIN_PREVIEW_SCALE - 1e-9,
      `${image.width}x${image.height} opens at ${scale}, below the 75% floor`,
    );
  }
});

test('a huge image is not fitted down into a thumbnail', () => {
  const image = { width: 4000, height: 3000 };
  // Fitting it would be 0.46; the floor wins, so the image overflows the stage.
  assert.ok(fitScale(image, STAGE) < MIN_PREVIEW_SCALE);
  assert.equal(initialScale(image, STAGE), MIN_PREVIEW_SCALE);
  const scaled = scaledSize(image, initialScale(image, STAGE));
  assert.ok(scaled.width > STAGE.width, 'the reader pans rather than reading a 46% image');
});

test('a small image opens at 1:1 instead of being blown up', () => {
  assert.equal(initialScale({ width: 200, height: 100 }, STAGE), 1);
  assert.equal(initialScale({ width: 1200, height: 900 }, STAGE), 1);
});

test('an image that fits is shown as large as the viewport allows', () => {
  const scale = initialScale(CHART, STAGE);
  // The height is the binding axis for a 2200x1350 chart on a 1856x1016 stage.
  const fitted = Math.min(STAGE.width / CHART.width, STAGE.height / CHART.height);
  assert.ok(Math.abs(scale - fitted) < 1e-9, `expected the whole-image fit, got ${scale}`);
  const scaled = scaledSize(CHART, scale);
  assert.ok(scaled.width <= STAGE.width + 1e-9 && scaled.height <= STAGE.height + 1e-9);
  assert.ok(scale < 1, 'a 2200px image is still slightly reduced to fit');
  assert.ok(scaled.width > 1600, 'and it is shown far larger than the old 872px cap');
});

test('fitScale is the whole-image fit and stays inside the zoom range', () => {
  assert.equal(fitScale({ width: 100, height: 50 }, { width: 400, height: 400 }), MAX_SCALE);
  assert.equal(fitScale({ width: 1e6, height: 1e6 }, STAGE), MIN_SCALE);
  assert.equal(fitScale({ width: 0, height: 0 }, STAGE), 1, 'a missing size is not a crash');
});

test('the scale is clamped to the supported range', () => {
  assert.equal(clampScale(0), MIN_SCALE);
  assert.equal(clampScale(100), MAX_SCALE);
  assert.equal(clampScale(Number.NaN), 1);
  assert.equal(clampScale(1.5), 1.5);
});

test('a zoom step is five percentage points, added or subtracted', () => {
  assert.equal(steppedScale(1, 1), 1.05);
  assert.equal(steppedScale(1, -1), 0.95);
  assert.equal(steppedScale(0.75, 1), 0.8);
  assert.equal(steppedScale(0.8, -1), 0.75);
  assert.equal(steppedScale(1, 3), 1.15);
  assert.equal(steppedScale(1, 0), 1);
});

test('repeated steps land on the numbers the readout shows', () => {
  let scale = 0.75;
  for (let i = 0; i < 5; i += 1) scale = steppedScale(scale, 1);
  assert.equal(scale, 1, 'five 5% steps from 75% must be exactly 100%');
  for (let i = 0; i < 5; i += 1) scale = steppedScale(scale, -1);
  assert.equal(scale, 0.75, 'and five steps back must be exactly 75% again');
});

test('a step stops at the supported limits', () => {
  assert.equal(steppedScale(MAX_SCALE, 1), MAX_SCALE);
  assert.equal(steppedScale(MIN_SCALE, -1), MIN_SCALE);
  assert.equal(steppedScale(0.12, -1), MIN_SCALE, '7% would be below the floor');
  assert.equal(steppedScale(3.98, 1), MAX_SCALE, '403% would be above the ceiling');
});

test('one wheel notch is one step, whatever unit the browser reports', () => {
  assert.deepEqual(wheelSteps(0, -100, 0), { steps: 1, carry: 0 }, 'a pixel notch up zooms in');
  assert.deepEqual(wheelSteps(0, 100, 0), { steps: -1, carry: 0 }, 'down zooms out');
  assert.deepEqual(wheelSteps(0, -3, 1), { steps: 1, carry: 20 }, 'three lines are a notch');
  assert.deepEqual(wheelSteps(0, -1, 2), { steps: 1, carry: 0 }, 'a page is a notch');
});

test('a trackpad flick does not cross the whole range', () => {
  // Thirty 4px deltas add up to 120px: one step and a 20px carry, not 30 steps.
  let carry = 0;
  let steps = 0;
  for (let i = 0; i < 30; i += 1) {
    const result = wheelSteps(carry, -4, 0);
    carry = result.carry;
    steps += result.steps;
  }
  assert.equal(steps, 1);
  assert.equal(carry, 20);
});

test('a carried distance is spent by the next event', () => {
  const first = wheelSteps(0, -80, 0);
  assert.deepEqual(first, { steps: 0, carry: 80 }, 'under a notch, nothing happens yet');
  assert.deepEqual(wheelSteps(first.carry, -40, 0), { steps: 1, carry: 20 });
});

test('a zero or unusable delta is not a step', () => {
  assert.deepEqual(wheelSteps(50, 0, 0), { steps: 0, carry: 50 });
  assert.deepEqual(wheelSteps(0, Number.NaN, 0), { steps: 0, carry: 0 });
});

test('zooming keeps the point under the pointer still', () => {
  // Anchor 100px right of the centre, image offset 0: after doubling, the same
  // image point must still sit at +100, which means the offset moves to -100.
  const next = zoomAround({ x: 0, y: 0 }, 1, 2, { x: 100, y: 0 });
  assert.deepEqual(next, { x: -100, y: 0 });
  // And the invariant holds generally: anchor = offset + (anchor - offset) * ratio
  // is preserved, i.e. the image coordinate under the anchor is unchanged.
  const ratio = 2;
  const imageUnderAnchorBefore = 100 - 0;
  const imageUnderAnchorAfter = (100 - next.x) / ratio;
  assert.equal(imageUnderAnchorBefore, imageUnderAnchorAfter);
});

test('an offset is clamped so the image cannot be dragged out of view', () => {
  const scaled = { width: 3000, height: 1500 };
  const limit = panLimit(scaled, STAGE);
  assert.equal(limit.x, (3000 - STAGE.width) / 2);
  assert.equal(limit.y, (1500 - STAGE.height) / 2);
  assert.deepEqual(clampOffset({ x: 99999, y: -99999 }, scaled, STAGE), {
    x: limit.x,
    y: -limit.y,
  });
  // While the image fits, there is nothing to pan: it stays centred.
  const small = { width: 100, height: 100 };
  assert.deepEqual(panLimit(small, STAGE), { x: 0, y: 0 });
  assert.deepEqual(clampOffset({ x: 50, y: 50 }, small, STAGE), { x: 0, y: 0 });
});

test('a degenerate size never produces a NaN offset', () => {
  assert.deepEqual(clampOffset({ x: Number.NaN, y: 0 }, CHART, STAGE), { x: 0, y: 0 });
  assert.deepEqual(panLimit({ width: 0, height: 0 }, STAGE), { x: 0, y: 0 });
});
