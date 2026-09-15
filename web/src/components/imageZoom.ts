/**
 * Zoom arithmetic for the image preview.
 *
 * Pure functions: the component owns the DOM, this owns the numbers.  The rule
 * they encode is that a preview must never quietly shrink a picture into
 * illegibility -- the scale it opens at is at least `MIN_PREVIEW_SCALE` of the
 * image's own pixels, even when that means the image is larger than the
 * viewport and has to be panned (or zoomed out on purpose).
 *
 * Offsets are measured from the *centre* of the stage, so a centred image is
 * `{x: 0, y: 0}` at any scale and `panLimit` is symmetric.
 */

/** Smallest share of the image's own pixels a preview opens at. */
export const MIN_PREVIEW_SCALE = 0.75;
/** Zoom-out floor and zoom-in ceiling. */
export const MIN_SCALE = 0.1;
export const MAX_SCALE = 4;
/**
 * One zoom step: five percentage points, added or subtracted.
 *
 * The step is additive rather than multiplicative so the readout moves in the
 * numbers the reader sees (75% -> 80% -> 85%) at every scale, instead of the
 * big jumps a ratio produces once the image is already large.
 */
export const ZOOM_STEP = 0.05;
/** One physical wheel notch, in normalized pixels. */
export const WHEEL_NOTCH_PX = 100;
/** A `deltaMode: 1` line, in pixels (three lines add up to one notch). */
const LINE_PX = 40;

export interface Size {
  width: number;
  height: number;
}

export interface Offset {
  x: number;
  y: number;
}

function usable(size: Size): boolean {
  return (
    Number.isFinite(size.width) &&
    Number.isFinite(size.height) &&
    size.width > 0 &&
    size.height > 0
  );
}

/** Keep a scale inside the supported range; a non-finite value falls back to 1. */
export function clampScale(scale: number): number {
  if (!Number.isFinite(scale)) return 1;
  return Math.min(Math.max(scale, MIN_SCALE), MAX_SCALE);
}

/** The scale that shows the whole image inside `viewport`. */
export function fitScale(image: Size, viewport: Size): number {
  if (!usable(image) || !usable(viewport)) return 1;
  return clampScale(Math.min(viewport.width / image.width, viewport.height / image.height));
}

/**
 * The scale a preview opens at: fill the viewport, but never below
 * `MIN_PREVIEW_SCALE` and never above 1:1 -- a small image is shown as it is
 * rather than blown up, and a large one stays legible instead of being fitted
 * down to a thumbnail.
 */
export function initialScale(image: Size, viewport: Size): number {
  if (!usable(image) || !usable(viewport)) return 1;
  const fitted = Math.min(viewport.width / image.width, viewport.height / image.height);
  return clampScale(Math.min(Math.max(fitted, MIN_PREVIEW_SCALE), 1));
}

/** The rendered size of the image at `scale`. */
export function scaledSize(image: Size, scale: number): Size {
  return { width: image.width * scale, height: image.height * scale };
}

/**
 * How far the image may be moved before a gap would show: zero on an axis the
 * image fits in, half the overflow otherwise.
 */
export function panLimit(scaled: Size, viewport: Size): Offset {
  if (!usable(scaled) || !usable(viewport)) return { x: 0, y: 0 };
  return {
    x: Math.max(0, (scaled.width - viewport.width) / 2),
    y: Math.max(0, (scaled.height - viewport.height) / 2),
  };
}

/** Keep the image from being dragged out of the stage. */
export function clampOffset(offset: Offset, scaled: Size, viewport: Size): Offset {
  if (!Number.isFinite(offset.x) || !Number.isFinite(offset.y)) return { x: 0, y: 0 };
  const limit = panLimit(scaled, viewport);
  return {
    x: Math.min(Math.max(offset.x, -limit.x), limit.x),
    y: Math.min(Math.max(offset.y, -limit.y), limit.y),
  };
}

/**
 * Keep the point under `anchor` where it is while the scale changes, so a wheel
 * zoom reads as "zoom where the pointer is" instead of "zoom at the centre".
 */
export function zoomAround(offset: Offset, scale: number, next: number, anchor: Offset): Offset {
  if (!Number.isFinite(scale) || scale <= 0 || !Number.isFinite(next)) return offset;
  const ratio = next / scale;
  return {
    x: anchor.x - (anchor.x - offset.x) * ratio,
    y: anchor.y - (anchor.y - offset.y) * ratio,
  };
}

/** Round to a millionth: repeated 5% steps must not drift into 85.00000000001%. */
function trim(value: number): number {
  return Math.round(value * 1e6) / 1e6;
}

/** `scale` moved by `steps` zoom steps (positive zooms in, negative out). */
export function steppedScale(scale: number, steps: number): number {
  if (!Number.isFinite(steps) || steps === 0) return clampScale(scale);
  return clampScale(trim(scale + steps * ZOOM_STEP));
}

/**
 * A wheel delta in pixels, whatever unit the browser reported it in.
 *
 * Chrome reports pixels (`deltaMode: 0`), Firefox reports lines (`1`), and a
 * page-scrolling device reports pages (`2`); a step must mean the same notch on
 * all three.
 */
export function normalizeWheelDelta(deltaY: number, deltaMode: number): number {
  if (!Number.isFinite(deltaY)) return 0;
  if (deltaMode === 1) return deltaY * LINE_PX;
  if (deltaMode === 2) return deltaY * WHEEL_NOTCH_PX;
  return deltaY;
}

/**
 * How many whole zoom steps one wheel event is worth, and what to carry over.
 *
 * A trackpad sends dozens of small deltas per gesture; stepping on every event
 * would cross the whole range in one flick, so the leftover distance is carried
 * until it adds up to a notch.  Positive steps zoom in.
 */
export function wheelSteps(
  carry: number,
  deltaY: number,
  deltaMode = 0,
): { steps: number; carry: number } {
  const pixels = normalizeWheelDelta(deltaY, deltaMode);
  if (pixels === 0) return { steps: 0, carry };
  const total = (Number.isFinite(carry) ? carry : 0) - pixels;
  const steps = Math.trunc(total / WHEEL_NOTCH_PX);
  return { steps, carry: trim(total - steps * WHEEL_NOTCH_PX) };
}
