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
  /**
   * Where the panel lines up with the anchor: its start edge, its end edge, or the
   * anchor's own centre line.  A trigger that is itself centred (the header's
   * session title) wants `center`, or the panel reads as belonging to one half of it.
   */
  align?: 'start' | 'end' | 'center';
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

/**
 * The width of a panel the console centres, in px -- `w-96`, which is what every
 * caller of `align="center"`/`"end"` paints.
 *
 * The offset has to be known before the panel exists, and none of the alternatives
 * survives: `transform` and `translate` are both taken by the entrance animation
 * (an inline `transform` is interpolated through it -- the panel sliding in from the
 * side -- and the CSS build transpiles a `translate` back to `transform`), while a
 * box whose auto margins would centre the panel is over-constrained whenever the
 * panel is wider than the box, and the browser then ignores the second edge instead
 * of splitting the difference.  `tests/shellLayout.test.ts` pins this pairing.
 */
const CENTRED_PANEL_WIDTH = 384;

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
        // The anchor's start edge, or the panel's own start edge once it is aligned
        // to the anchor's end edge or centred on it.
        left: Math.max(
          EDGE,
          align === 'end'
            ? rect.right - CENTRED_PANEL_WIDTH
            : align === 'center'
              ? rect.left + rect.width / 2 - CENTRED_PANEL_WIDTH / 2
              : rect.left,
        ),
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
        }}
        className={`responsive-floating-panel fixed z-50 ${className}`}
      >
        {children}
      </div>
    </Portal>
  );
};
