import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Dismiss16Regular,
  ScaleFit16Regular,
  ZoomIn16Regular,
  ZoomOut16Regular,
} from '@fluentui/react-icons';
import { Portal } from './Portal.tsx';
import {
  clampOffset,
  clampScale,
  fitScale,
  initialScale,
  panLimit,
  scaledSize,
  steppedScale,
  wheelSteps,
  zoomAround,
  type Offset,
  type Size,
} from './imageZoom.ts';

/**
 * Full-size view of one transcript image, with zoom and pan.
 *
 * It renders the object URL the thumbnail already resolved, so opening it costs
 * no extra read and the blob stays owned (and revoked) by the thumbnail's
 * loader.  The overlay is `fixed`, like the goal dialog, so the transcript's
 * scroll container cannot clip it; Escape, the close button and a click on the
 * backdrop all dismiss it, and a click inside the panel does not.
 *
 * Sizing (`imageZoom.ts` holds the arithmetic):
 *
 * - it opens at the whole-image fit, but never below 75% of the image's own
 *   pixels and never above 1:1.  A wide chart therefore stays legible instead
 *   of being shrunk into the panel, and a small screenshot is not blown up;
 * - when that leaves the image larger than the stage, the stage pans (drag) --
 *   the offset is clamped so the picture can never be dragged out of view;
 * - the wheel zooms around the pointer, the header has fit / 1:1 / -/+ controls,
 *   `+` `-` `0` `1` work from the keyboard, and a double click toggles between
 *   the fit and 1:1.
 */
export const ImageLightbox: React.FC<{
  src: string;
  label: string;
  meta: string;
  onClose: () => void;
}> = ({ src, label, meta, onClose }) => {
  const stageRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{ fromX: number; fromY: number; offset: Offset } | null>(null);
  /** Wheel distance not yet worth a whole zoom step (see `wheelSteps`). */
  const wheelCarryRef = useRef(0);

  const [natural, setNatural] = useState<Size | null>(null);
  const [stage, setStage] = useState<Size | null>(null);
  /** `null` until the reader zooms by hand: the opening scale is derived. */
  const [manualScale, setManualScale] = useState<number | null>(null);
  const [rawOffset, setRawOffset] = useState<Offset>({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);

  // The stage is the measuring stick: `clientWidth/Height` is what the image is
  // fitted into, and it changes with the window and with the header's controls.
  useEffect(() => {
    const node = stageRef.current;
    if (node === null) return;
    const measure = (): void => setStage({ width: node.clientWidth, height: node.clientHeight });
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  /**
   * The scale in force: the reader's own choice once there is one, and the
   * opening scale otherwise.  Deriving it keeps the first paint already correct
   * (no flash at 1:1) and means a resize re-fits an untouched preview while
   * leaving a hand-zoomed one alone.
   */
  const autoScale = natural !== null && stage !== null ? initialScale(natural, stage) : 1;
  const scale = manualScale ?? autoScale;
  const rendered = natural === null ? null : scaledSize(natural, scale);
  /** Panning is clamped at render time too, so a resize cannot leave a gap. */
  const offset =
    rendered === null || stage === null ? rawOffset : clampOffset(rawOffset, rendered, stage);

  const applyScale = useCallback(
    (next: number, anchor?: Offset) => {
      if (natural === null || stage === null) return;
      const bounded = clampScale(next);
      const moved =
        anchor === undefined ? offset : zoomAround(offset, scale, bounded, anchor);
      setManualScale(bounded);
      setRawOffset(clampOffset(moved, scaledSize(natural, bounded), stage));
    },
    [natural, stage, scale, offset],
  );

  const fitNow = useCallback(() => {
    if (natural === null || stage === null) return;
    applyScale(fitScale(natural, stage));
  }, [applyScale, natural, stage]);

  // The wheel must be able to `preventDefault`, so it is registered by hand:
  // React's `onWheel` is passive at the root and cannot stop the page scroll.
  useEffect(() => {
    const node = stageRef.current;
    if (node === null) return;
    const onWheel = (event: WheelEvent): void => {
      if (natural === null || stage === null) return;
      event.preventDefault();
      const { steps, carry } = wheelSteps(wheelCarryRef.current, event.deltaY, event.deltaMode);
      wheelCarryRef.current = carry;
      if (steps === 0) return;
      const rect = node.getBoundingClientRect();
      applyScale(steppedScale(scale, steps), {
        x: event.clientX - (rect.left + rect.width / 2),
        y: event.clientY - (rect.top + rect.height / 2),
      });
    };
    node.addEventListener('wheel', onWheel, { passive: false });
    return () => node.removeEventListener('wheel', onWheel);
  }, [applyScale, natural, stage, scale]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        onClose();
      } else if (event.key === '+' || event.key === '=') {
        applyScale(steppedScale(scale, 1));
      } else if (event.key === '-' || event.key === '_') {
        applyScale(steppedScale(scale, -1));
      } else if (event.key === '0') {
        fitNow();
      } else if (event.key === '1') {
        applyScale(1);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [applyScale, fitNow, onClose, scale]);

  const limit =
    natural === null || stage === null ? { x: 0, y: 0 } : panLimit(scaledSize(natural, scale), stage);
  const pannable = limit.x > 0.5 || limit.y > 0.5;

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>): void => {
    if (!pannable) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = { fromX: event.clientX, fromY: event.clientY, offset };
    setDragging(true);
  };

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>): void => {
    const drag = dragRef.current;
    if (drag === null || natural === null || stage === null) return;
    setRawOffset(
      clampOffset(
        {
          x: drag.offset.x + (event.clientX - drag.fromX),
          y: drag.offset.y + (event.clientY - drag.fromY),
        },
        scaledSize(natural, scale),
        stage,
      ),
    );
  };

  const endDrag = (): void => {
    dragRef.current = null;
    setDragging(false);
  };

  const onDoubleClick = (): void => {
    if (natural === null || stage === null) return;
    const fitted = fitScale(natural, stage);
    applyScale(Math.abs(scale - fitted) < 1e-6 ? 1 : fitted);
  };

  const control = 'ui-icon-button ui-compact text-gray-500 hover:text-gray-800';

  return (
    // `Portal`: the lightbox belongs to the window, not to the transcript row that
    // opened it.
    <Portal>
      <div
        role="dialog"
        aria-modal="true"
        aria-label="图片预览"
        onClick={onClose}
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-md p-4 scrim-in"
      >
        <div
          onClick={(event) => event.stopPropagation()}
          className="flex h-full max-h-full w-full max-w-[95vw] flex-col rounded-card border border-line/80 material-flyout flyout-in p-3 shadow-flyout"
        >
          <div className="mb-2 flex items-center gap-3 border-b border-gray-100 pb-1.5">
            <span className="truncate font-mono text-[11px] font-semibold text-gray-900">
              {label}
            </span>
            <span className="shrink-0 font-mono text-[10px] text-gray-400">{meta}</span>
            <div className="ml-auto flex shrink-0 items-center gap-1">
              <span className="font-mono text-[10px] text-gray-500" data-image-zoom>
                {Math.round(scale * 100)}%
              </span>
              <button type="button" onClick={fitNow} title="适应窗口 (0)" className={control}>
                <ScaleFit16Regular aria-hidden="true" />
              </button>
              <button
                type="button"
                onClick={() => applyScale(steppedScale(scale, -1))}
                title="缩小 (-)"
                className={control}
              >
                <ZoomOut16Regular aria-hidden="true" />
              </button>
              <button
                type="button"
                onClick={() => applyScale(steppedScale(scale, 1))}
                title="放大 (+)"
                className={control}
              >
                <ZoomIn16Regular aria-hidden="true" />
              </button>
              <button
                type="button"
                onClick={() => applyScale(1)}
                title="原始大小 (1)"
                className="rounded-control px-1.5 py-0.5 font-mono text-[10px] text-gray-500 hover:bg-sunken hover:text-gray-800"
              >
                1:1
              </button>
              <button
                type="button"
                onClick={onClose}
                title="关闭 (Esc)"
                className="ui-icon-button ui-compact ml-1 text-gray-400 hover:text-gray-700"
              >
                <Dismiss16Regular aria-hidden="true" />
              </button>
            </div>
          </div>
          <div
            ref={stageRef}
            data-image-stage
            data-image-scale={scale.toFixed(4)}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
            onDoubleClick={onDoubleClick}
            className={`relative min-h-0 flex-1 touch-none select-none overflow-hidden rounded-control bg-sunken ${
              !pannable ? 'cursor-zoom-in' : dragging ? 'cursor-grabbing' : 'cursor-grab'
            }`}
          >
            <img
              src={src}
              alt={label}
              draggable={false}
              onLoad={(event) =>
                setNatural({
                  width: event.currentTarget.naturalWidth,
                  height: event.currentTarget.naturalHeight,
                })
              }
              // `max-w-none` matters: Tailwind's preflight caps every image at
              // `max-width: 100%`, which would silently clamp the zoomed size.
              style={
                rendered === null
                  ? { visibility: 'hidden' }
                  : {
                      width: rendered.width,
                      height: rendered.height,
                      transform: `translate(calc(-50% + ${offset.x}px), calc(-50% + ${offset.y}px))`,
                    }
              }
              className="absolute left-1/2 top-1/2 max-w-none"
            />
            {natural === null && (
              <span className="absolute inset-0 flex items-center justify-center text-[13px] text-gray-500">
                读取图片…
              </span>
            )}
          </div>
        </div>
      </div>
    </Portal>
  );
};
