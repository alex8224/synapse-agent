import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Dismiss16Regular,
  ScaleFit16Regular,
  ZoomIn16Regular,
  ZoomOut16Regular,
} from '@fluentui/react-icons';
import { GeneratedHtml } from './GeneratedHtml.tsx';
import { Portal } from './Portal.tsx';
import type { DiagramSize } from '../markdown/mermaid.ts';
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
 * Full-size view of one transcript diagram, with zoom and pan.
 *
 * The inline card in `MermaidBlock.tsx` has to fit the reading column, which
 * means a wide or tall diagram is either scaled down or clipped; this is where
 * it is actually readable.  It reuses the image preview's arithmetic
 * (`imageZoom.ts`) and its visual language on purpose -- the rule "a preview
 * must never quietly shrink a drawing into illegibility" is the same one, and a
 * second implementation of it would be a second thing to keep correct.
 *
 * Sizing:
 *
 * - it opens at the whole-diagram fit, but never below 75% of the diagram's own
 *   `viewBox` pixels and never above 1:1, so a huge diagram opens legible and
 *   pannable instead of fitted down to a thumbnail;
 * - the wheel zooms around the pointer, dragging pans (clamped, so the drawing
 *   can never be dragged out of the stage), the header has fit / 1:1 / -/+
 *   controls, `+` `-` `0` `1` work from the keyboard, a double click toggles
 *   between the fit and 1:1, and Escape, the close button and the backdrop all
 *   dismiss it.
 *
 * The markup is the *same* sanitized SVG the card injected, and it is the only
 * copy in the document: mermaid's ids are not namespaced (a sequence diagram
 * carries `actor0`, `root-0`, ... besides the diagram's own id, and its theme
 * CSS is scoped by `#<svgId>`), so a second copy would duplicate them.  The
 * card empties its stage while this is open instead.
 */
export interface MermaidLightboxProps {
  /** Sanitized, size-frozen SVG markup, exactly as the card injected it. */
  svg: string;
  /** The diagram's intrinsic size, in `viewBox` units. */
  size: DiagramSize;
  onClose: () => void;
}

export const MermaidLightbox: React.FC<MermaidLightboxProps> = ({ svg, size, onClose }) => {
  const stageRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{ fromX: number; fromY: number; offset: Offset } | null>(null);
  /** Wheel distance not yet worth a whole zoom step (see `wheelSteps`). */
  const wheelCarryRef = useRef(0);

  const [stage, setStage] = useState<Size | null>(null);
  /** `null` until the reader zooms by hand: the opening scale is derived. */
  const [manualScale, setManualScale] = useState<number | null>(null);
  const [rawOffset, setRawOffset] = useState<Offset>({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  /**
   * The diagram's own size, snapshotted on mount: it is what every zoom and pan
   * calculation is relative to, and the viewer is mounted per opening.
   */
  const [natural] = useState<Size>(size);

  // The stage is the measuring stick: `clientWidth/Height` is what the diagram
  // is fitted into, and it changes with the window and with the header's controls.
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
   * (no flash at 1:1) and means a resize re-fits an untouched diagram while
   * leaving a hand-zoomed one alone.
   */
  const autoScale = stage === null ? 1 : initialScale(natural, stage);
  const scale = manualScale ?? autoScale;
  const rendered = stage === null ? null : scaledSize(natural, scale);
  /** Panning is clamped at render time too, so a resize cannot leave a gap. */
  const offset =
    rendered === null || stage === null ? rawOffset : clampOffset(rawOffset, rendered, stage);

  const applyScale = useCallback(
    (next: number, anchor?: Offset) => {
      if (rendered === null || stage === null) return;
      const bounded = clampScale(next);
      const moved = anchor === undefined ? offset : zoomAround(offset, scale, bounded, anchor);
      setManualScale(bounded);
      setRawOffset(clampOffset(moved, scaledSize(natural, bounded), stage));
    },
    [natural, rendered, stage, scale, offset],
  );

  const fitNow = useCallback(() => {
    if (rendered === null || stage === null) return;
    applyScale(fitScale(natural, stage));
  }, [applyScale, natural, rendered, stage]);

  // The wheel must be able to `preventDefault`, so it is registered by hand:
  // React's `onWheel` is passive at the root and cannot stop the page scroll.
  useEffect(() => {
    const node = stageRef.current;
    if (node === null) return;
    const onWheel = (event: WheelEvent): void => {
      if (rendered === null || stage === null) return;
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
  }, [applyScale, rendered, stage, scale]);

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

  const limit = rendered === null || stage === null ? { x: 0, y: 0 } : panLimit(rendered, stage);
  const pannable = limit.x > 0.5 || limit.y > 0.5;

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>): void => {
    if (!pannable) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = { fromX: event.clientX, fromY: event.clientY, offset };
    setDragging(true);
  };

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>): void => {
    const drag = dragRef.current;
    if (drag === null || rendered === null || stage === null) return;
    setRawOffset(
      clampOffset(
        {
          x: drag.offset.x + (event.clientX - drag.fromX),
          y: drag.offset.y + (event.clientY - drag.fromY),
        },
        rendered,
        stage,
      ),
    );
  };

  const endDrag = (): void => {
    dragRef.current = null;
    setDragging(false);
  };

  const onDoubleClick = (): void => {
    if (rendered === null || stage === null) return;
    const fitted = fitScale(natural, stage);
    applyScale(Math.abs(scale - fitted) < 1e-6 ? 1 : fitted);
  };

  const control = 'ui-icon-button ui-compact text-gray-500 hover:text-gray-800';

  return (
    // `Portal`: the viewer belongs to the window, not to the transcript row that
    // opened it (an ancestor with `backdrop-filter` would become the containing
    // block for `fixed`).
    <Portal>
      <div
        role="dialog"
        aria-modal="true"
        aria-label="图形预览"
        onClick={onClose}
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-md p-4 scrim-in"
      >
        <div
          onClick={(event) => event.stopPropagation()}
          className="flex h-full max-h-full w-full max-w-[95vw] flex-col rounded-card border border-line/80 material-flyout flyout-in p-3 shadow-flyout"
        >
          <div className="mb-2 flex items-center gap-3 border-b border-gray-100 pb-1.5">
            <span className="truncate font-mono text-[11px] font-semibold text-gray-900">
              mermaid 图形
            </span>
            <span className="shrink-0 font-mono text-[10px] text-gray-400">
              {Math.round(size.width)} × {Math.round(size.height)}
            </span>
            <div className="ml-auto flex shrink-0 items-center gap-1">
              <span className="font-mono text-[10px] text-gray-500" data-mermaid-zoom>
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
            data-mermaid-stage
            data-mermaid-scale={scale.toFixed(4)}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
            onDoubleClick={onDoubleClick}
            className={`relative min-h-0 flex-1 touch-none select-none overflow-hidden rounded-control bg-sunken ${
              !pannable ? 'cursor-zoom-in' : dragging ? 'cursor-grabbing' : 'cursor-grab'
            }`}
          >
            {/*
              The box carries the scaled pixel size and the SVG fills it
              (`.mermaid-zoom-box svg`), so the drawing is never clamped back to
              the stage width by the size attributes mermaid wrote.
            */}
            {rendered !== null && (
              <div
                style={{
                  width: rendered.width,
                  height: rendered.height,
                  transform: `translate(calc(-50% + ${offset.x}px), calc(-50% + ${offset.y}px))`,
                }}
                className="mermaid-zoom-box absolute left-1/2 top-1/2"
              >
                <GeneratedHtml html={svg} className="mermaid-diagram mermaid-actual" />
              </div>
            )}
            {rendered === null && (
              <span className="absolute inset-0 flex items-center justify-center text-[13px] text-gray-500">
                测量视口…
              </span>
            )}
          </div>
        </div>
      </div>
    </Portal>
  );
};
