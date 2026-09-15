/**
 * A popover that belongs to the window, not to the surface that opened it.
 *
 * Rendering one where its trigger lives breaks it in two ways:
 *
 *  1. `backdrop-filter` establishes a *backdrop root*.  A panel nested inside a
 *     chrome surface (the rail, the status strip, the composer) can only blur what
 *     that surface painted itself, so the transcript behind it stayed sharp and the
 *     panel read as transparent instead of frosted.
 *  2. An `absolute` panel is laid out inside its anchor's box, so a tall one
 *     stretched that box: the sidebar's footer grew by the panel's height and
 *     pushed the session tree up.
 *
 * Portalling to the body and positioning `fixed` from the anchor's viewport rect
 * avoids both while keeping the caller's dismissal contract -- the panel is still
 * the caller's element in the React tree, so its own ref and its click-outside
 * checks keep working.
 */
import React, { useEffect, useState } from 'react';
import { Portal } from './Portal.tsx';

export interface FloatingPanelProps {
  /** The trigger box the panel hangs from.  `null` renders nothing. */
  anchor: HTMLElement | null;
  /** Which edge of the anchor the panel hangs from. */
  side?: 'top' | 'bottom';
  /** Which edge of the panel lines up with the anchor's. */
  align?: 'start' | 'end';
  /** Gap between the anchor and the panel, in px. */
  offset?: number;
  /** The panel's own classes: fill, width, padding. */
  className: string;
  /** Accessible name: makes the panel itself the dialog, not a box inside one. */
  label?: string;
  children: React.ReactNode;
  panelRef?: React.Ref<HTMLDivElement>;
}

/** Never let a panel hang off the left edge of the window. */
const EDGE = 8;

export const FloatingPanel: React.FC<FloatingPanelProps> = ({
  anchor,
  side = 'top',
  align = 'start',
  offset = 8,
  className,
  label,
  children,
  panelRef,
}) => {
  const [box, setBox] = useState<{ left: number; top: number; bottom: number } | null>(null);

  useEffect(() => {
    if (anchor === null) {
      setBox(null);
      return;
    }
    const measure = () => {
      const rect = anchor.getBoundingClientRect();
      setBox({
        left: Math.max(EDGE, align === 'end' ? rect.right : rect.left),
        // `top` for a panel that hangs below its anchor, `bottom` (measured from
        // the viewport's bottom edge, as `position: fixed` wants) for one above.
        top: rect.bottom + offset,
        bottom: Math.max(EDGE, window.innerHeight - (rect.top - offset)),
      });
    };
    measure();
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, [anchor, side, align, offset]);

  if (box === null) return null;
  return (
    <Portal>
      <div
        ref={panelRef}
        role={label === undefined ? undefined : 'dialog'}
        aria-label={label}
        style={{
          left: box.left,
          ...(side === 'top' ? { bottom: box.bottom } : { top: box.top }),
          ...(align === 'end' ? { transform: 'translateX(-100%)' } : null),
        }}
        className={`responsive-floating-panel fixed z-50 ${className}`}
      >
        {children}
      </div>
    </Portal>
  );
};
