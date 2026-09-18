/**
 * The status strip's three tracks, rendered from a resolved layout.
 *
 * This module is plain `.ts` on purpose: it renders through `createElement`
 * instead of JSX so the Node test runner — which strips types but cannot compile
 * JSX — can render it with `react-dom/server` and assert the *DOM* contract:
 * a hidden entry leaves no wrapper and no separator behind, order is respected,
 * a keyboard-only entry paints nothing at all, and an empty track leaves no box
 * (which is what keeps a compact strip from growing phantom gaps).
 *
 * The strip's own chrome (the `<footer>`, the open overlay, the keyboard) lives
 * in `BottomBar.tsx`; this file only lays the entries out.
 */
import React, { Fragment, createElement } from 'react';
import type {
  BottomBarItemDefinition,
  BottomBarLayout,
  BottomBarRegion,
} from './contract.ts';

/** Classes of each track.  `empty:hidden` keeps an unused track out of the flow. */
export const REGION_CLASS: Record<BottomBarRegion, string> = {
  left: 'flex min-w-0 items-center gap-2.5 empty:hidden',
  center: 'flex shrink-0 items-center justify-self-center gap-2 tabular-nums empty:hidden',
  right: 'flex min-w-0 items-center gap-2 empty:hidden',
};

/**
 * The phone band's tracks.
 *
 * The left track takes the leftover width and *scrolls* instead of shrinking its
 * entries: a shrink would let a label overlap its neighbour, and clipping would
 * put a control out of reach.  The centre and the right (the 更多 entry) stay
 * pinned, so the way to a compacted entry never scrolls away.
 */
export const COMPACT_REGION_CLASS: Record<BottomBarRegion, string> = {
  left: 'flex min-w-0 flex-1 items-center gap-2.5 overflow-x-auto no-scrollbar empty:hidden',
  center: 'flex shrink-0 items-center gap-2 tabular-nums empty:hidden',
  right: 'flex shrink-0 items-center gap-2 empty:hidden',
};

/** The classes of one track, for the band the strip is in. */
export function regionClass(region: BottomBarRegion, compact: boolean): string {
  return (compact ? COMPACT_REGION_CLASS : REGION_CLASS)[region];
}

/** Reading order of the tracks, which is also the DOM order. */
export const REGION_ORDER: readonly BottomBarRegion[] = ['left', 'center', 'right'];

/**
 * Painted between two entries of one track.
 *
 * It is a sibling of the entries, never a wrapper around them, and the renderer
 * only emits it for entries it actually paints: that is what makes "a hidden
 * entry leaves no separator" a property of the layout rather than of a filter
 * applied afterwards.
 */
export const REGION_SEPARATOR = createElement(
  'span',
  { className: 'text-gray-200', 'data-separator': 'true' },
  '|',
);

export interface BottomBarRegionsProps {
  layout: BottomBarLayout;
  /** Phone band: the left track scrolls, the side tracks stay pinned. */
  compact: boolean;
  /** Renders one entry's own control (the host adds refs and overlays). */
  slot: (item: BottomBarItemDefinition, index: number) => React.ReactNode;
}

export const BottomBarRegions: React.FC<BottomBarRegionsProps> = ({ layout, compact, slot }) =>
  createElement(
    Fragment,
    null,
    REGION_ORDER.map((region) =>
      createElement(
        'div',
        { key: region, 'data-region': region, className: regionClass(region, compact) },
        layout.regions[region].map((item, index) =>
          createElement(
            Fragment,
            { key: item.id },
            index === 0 ? null : REGION_SEPARATOR,
            slot(item, index),
          ),
        ),
      ),
    ),
  );
