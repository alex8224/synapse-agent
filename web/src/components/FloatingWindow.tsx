import React, { useEffect, useRef, useState } from 'react';
import {
  Dismiss16Regular,
  FullScreenMaximize16Regular,
  FullScreenMinimize16Regular,
} from '@fluentui/react-icons';
import { Portal } from './Portal.tsx';

/**
 * A centered, movable, resizable modal window.
 *
 * The file manager opened from a transcript path can be dragged by its header,
 * resized from the bottom-right grip, maximized to the viewport, and closed with
 * Escape / the close button / a click on the scrim.  Position and size are plain
 * state (no layout thrash): the drag/resize handlers update one rect, and the
 * window is a single `fixed` box inside a full-viewport scrim.
 *
 * The header is the drag handle.  A pointerdown that lands on a control is left
 * alone, so the buttons keep working; the drag also stops while maximized.
 */

/** Smallest a window may be resized to. */
const MIN_WIDTH = 360;
const MIN_HEIGHT = 240;
/** Gap kept between a maximized window and the viewport edge. */
const EDGE = 8;
/** How much of the window must stay on screen while dragging. */
const KEEP_X = 48;
const KEEP_Y = 40;
/** Matches `--motion-normal`: how long the maximize/restore transition runs. */
const MOTION_MS = 200;

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), Math.max(min, max));
}

function centeredRect(width: number, height: number): Rect {
  if (typeof window === 'undefined') return { x: 0, y: 0, w: width, h: height };
  const w = Math.min(width, window.innerWidth - 2 * EDGE);
  const h = Math.min(height, window.innerHeight - 2 * EDGE);
  return { x: Math.round((window.innerWidth - w) / 2), y: Math.round((window.innerHeight - h) / 2), w, h };
}

function maximizedRect(): Rect {
  if (typeof window === 'undefined') return { x: EDGE, y: EDGE, w: 0, h: 0 };
  return { x: EDGE, y: EDGE, w: window.innerWidth - 2 * EDGE, h: window.innerHeight - 2 * EDGE };
}

export interface FloatingWindowProps {
  /** Accessible dialog name. */
  label: string;
  /**
   * Scrim weight.  `dim` (the default) is the image-lightbox scrim: the window
   * content *is* the thing being looked at, so everything behind it steps back.
   * `soft` is the dialog scrim every other console window uses -- the right
   * choice for a document window, where the reader still wants the console
   * behind it to stay legible (and where a 60% black veil reads far too heavy
   * in the light theme).
   */
  scrim?: 'dim' | 'soft';
  /** Header title, part of the drag handle. */
  title: React.ReactNode;
  /** Extra header controls, placed before the maximize / close buttons. */
  actions?: React.ReactNode;
  initialWidth?: number;
  initialHeight?: number;
  onClose: () => void;
  children: React.ReactNode;
}

export const FloatingWindow: React.FC<FloatingWindowProps> = ({
  label,
  scrim = 'dim',
  title,
  actions,
  initialWidth = 896,
  initialHeight = 576,
  onClose,
  children,
}) => {
  const [rect, setRect] = useState<Rect>(() => centeredRect(initialWidth, initialHeight));
  const [maximized, setMaximized] = useState(false);
  const [animating, setAnimating] = useState(false);
  const restoreRef = useRef<Rect | null>(null);
  const motionTimer = useRef<number | null>(null);
  const dragRef = useRef<{ dx: number; dy: number } | null>(null);
  const resizeRef = useRef<{ sx: number; sy: number; w: number; h: number } | null>(null);

  useEffect(
    () => () => {
      if (motionTimer.current !== null) window.clearTimeout(motionTimer.current);
    },
    [],
  );

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  // A maximized window tracks the viewport; a restored one keeps its rect.
  useEffect(() => {
    if (!maximized) return;
    const onResize = () => setRect(maximizedRect());
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [maximized]);

  /** Run the rect transition for one step only (maximize / restore). */
  const animateOnce = (): void => {
    setAnimating(true);
    if (motionTimer.current !== null) window.clearTimeout(motionTimer.current);
    motionTimer.current = window.setTimeout(() => setAnimating(false), MOTION_MS);
  };

  const toggleMaximize = (): void => {
    animateOnce();
    if (maximized) {
      setRect(restoreRef.current ?? centeredRect(initialWidth, initialHeight));
      setMaximized(false);
    } else {
      restoreRef.current = rect;
      setRect(maximizedRect());
      setMaximized(true);
    }
  };

  const onHeaderPointerDown = (event: React.PointerEvent<HTMLDivElement>): void => {
    if (maximized) return;
    if ((event.target as HTMLElement).closest('button') !== null) return;
    dragRef.current = { dx: event.clientX - rect.x, dy: event.clientY - rect.y };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const onHeaderPointerMove = (event: React.PointerEvent<HTMLDivElement>): void => {
    const drag = dragRef.current;
    if (drag === null) return;
    setRect((r) => ({
      ...r,
      x: clamp(event.clientX - drag.dx, KEEP_X - r.w, window.innerWidth - KEEP_X),
      y: clamp(event.clientY - drag.dy, 0, window.innerHeight - KEEP_Y),
    }));
  };

  const onHeaderPointerUp = (event: React.PointerEvent<HTMLDivElement>): void => {
    dragRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  const onGripPointerDown = (event: React.PointerEvent<HTMLDivElement>): void => {
    if (maximized) return;
    event.stopPropagation();
    resizeRef.current = { sx: event.clientX, sy: event.clientY, w: rect.w, h: rect.h };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const onGripPointerMove = (event: React.PointerEvent<HTMLDivElement>): void => {
    const start = resizeRef.current;
    if (start === null) return;
    setRect((r) => ({
      ...r,
      w: clamp(start.w + (event.clientX - start.sx), MIN_WIDTH, window.innerWidth - r.x - EDGE),
      h: clamp(start.h + (event.clientY - start.sy), MIN_HEIGHT, window.innerHeight - r.y - EDGE),
    }));
  };

  const onGripPointerUp = (event: React.PointerEvent<HTMLDivElement>): void => {
    resizeRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  return (
    <Portal>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={label}
        onClick={onClose}
        className={`fixed inset-0 z-50 scrim-in ${
          scrim === 'dim' ? 'bg-black/60 backdrop-blur-md' : 'bg-black/25 backdrop-blur-sm'
        }`}
      >
        <div
          onClick={(event) => event.stopPropagation()}
          style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h }}
          className={`responsive-file-window fixed flex flex-col overflow-hidden rounded-card border border-line/80 material-flyout flyout-in text-left shadow-flyout ${
            animating ? 'fluent-window-motion' : ''
          }`}
        >
          <div
            onPointerDown={onHeaderPointerDown}
            onPointerMove={onHeaderPointerMove}
            onPointerUp={onHeaderPointerUp}
            onPointerCancel={onHeaderPointerUp}
            onDoubleClick={toggleMaximize}
            className={`material-titlebar flex select-none items-center gap-2 border-b border-line/60 px-3 py-1.5 ${
              maximized ? '' : 'cursor-move'
            }`}
          >
            {title}
            <div className="ml-auto flex items-center gap-1">
              {actions}
              <button
                type="button"
                onClick={toggleMaximize}
                title={maximized ? '还原窗口' : '最大化窗口'}
                aria-label={maximized ? '还原窗口' : '最大化窗口'}
                className="ui-icon-button ui-compact text-gray-400 hover:text-gray-700"
              >
                {maximized ? (
                  <FullScreenMinimize16Regular aria-hidden="true" />
                ) : (
                  <FullScreenMaximize16Regular aria-hidden="true" />
                )}
              </button>
              <button
                type="button"
                onClick={onClose}
                title="关闭 (Esc)"
                aria-label="关闭"
                className="ui-icon-button ui-compact text-gray-400 hover:text-gray-700"
              >
                <Dismiss16Regular aria-hidden="true" />
              </button>
            </div>
          </div>
          <div className="flex min-h-0 flex-1">{children}</div>
          {!maximized && (
            <div
              onPointerDown={onGripPointerDown}
              onPointerMove={onGripPointerMove}
              onPointerUp={onGripPointerUp}
              onPointerCancel={onGripPointerUp}
              title="拖动调整窗口大小"
              className="absolute bottom-0 right-0 h-4 w-4 cursor-nwse-resize"
            />
          )}
        </div>
      </div>
    </Portal>
  );
};
