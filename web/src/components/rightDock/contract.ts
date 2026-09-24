/**
 * The right auxiliary dock's static registration contract.
 *
 * Designed after the bottomBar contract:
 * A right-dock tab entry is a module exporting one `RightDockTabDefinition`,
 * plus one line in `manifest.tsx`. Nothing else changes: the dock is the host,
 * and it paints whatever the manifest declares -- clean plugin extension,
 * no mutable runtime registry, no user layout pollution.
 *
 * Free of React hooks and zustand store dependencies so the Node test runner
 * can exercise sorting, filtering, width constraints and availability rules directly.
 */
import type React from 'react';

/** Width boundaries for the dock in pixels. */
export const DEFAULT_DOCK_WIDTH = 400;
export const MIN_DOCK_WIDTH = 280;
export const MAX_DOCK_WIDTH = 760;

/** Visual intent for the tab's badge badge counter. */
export type RightDockBadgeVariant = 'default' | 'accent' | 'diff' | 'goal' | 'browser' | 'trace';

/** Subscribable dynamic availability source (same contract as bottomBar). */
export interface RightDockAvailability {
  getSnapshot: () => boolean;
  subscribe: (listener: () => void) => () => void;
}

/** Business facts a tab's visibility / badge rule may read. */
export interface RightDockVisibilityContext {
  sessionOpen: boolean;
  compact: boolean;
  activeProjectId?: string;
  hasUncommittedChanges?: boolean;
}

/** Runtime context handed down to tab contents and header controls. */
export interface RightDockContext extends RightDockVisibilityContext {
  activeTabId: string;
  setActiveTab: (id: string) => void;
  closeDock: () => void;
  insertMention?: (text: string) => void;
}

/** Static definition of one right-dock tab. */
export interface RightDockTabDefinition {
  /** Stable unique identifier (e.g. 'files', 'changes', 'goals', 'browser'). */
  id: string;
  /** Human-readable label displayed in the tab header. */
  label: string;
  /** Ordering weight in the tab bar. Lower numbers sort first. */
  order: number;
  /** Optional keyboard shortcut key (e.g. '1', '2', etc.). */
  shortcutKey?: string;
  /** Optional dynamic badge counter or status label. */
  badge?: (context: RightDockVisibilityContext) => string | number | null;
  /** Visual style variant for the badge. */
  badgeVariant?: RightDockBadgeVariant;
  /** Business visibility rule; absent = always shown. */
  visible?: (context: RightDockVisibilityContext) => boolean;
  /** Dynamic availability source. */
  availability?: RightDockAvailability;
  /** Icon component for the tab header. */
  Icon?: React.ComponentType<{ className?: string }>;
  /** Tab body content component rendered when this tab is active. */
  Content: React.ComponentType<{ context: RightDockContext }>;
  /** Optional extra action buttons displayed on the right of the dock header. */
  HeaderExtra?: React.ComponentType<{ context: RightDockContext }>;
}

/** Clamp dock width strictly within minimum and maximum bounds. */
export function clampDockWidth(
  width: number,
  minWidth = MIN_DOCK_WIDTH,
  maxWidth = MAX_DOCK_WIDTH,
): number {
  if (Number.isNaN(width)) return DEFAULT_DOCK_WIDTH;
  return Math.min(Math.max(width, minWidth), maxWidth);
}

/** Sort items by order, keeping manifest index on ties for stable order. */
export function inOrderTabs(
  items: readonly RightDockTabDefinition[],
): RightDockTabDefinition[] {
  return items
    .map((item, index) => ({ item, index }))
    .sort((a, b) => a.item.order - b.item.order || a.index - b.index)
    .map((entry) => entry.item);
}

/** Resolve visible tabs from manifest items and visibility context. */
export function resolveRightDockTabs(
  items: readonly RightDockTabDefinition[],
  context: RightDockVisibilityContext,
): RightDockTabDefinition[] {
  const ordered = inOrderTabs(items);
  return ordered.filter((tab) => {
    if (tab.visible !== undefined && !tab.visible(context)) {
      return false;
    }
    return true;
  });
}

/** Find a tab by id. */
export function tabById(
  items: readonly RightDockTabDefinition[],
  id: string,
): RightDockTabDefinition | undefined {
  return items.find((item) => item.id === id);
}

/** The useSyncExternalStore pair for reading dynamic tab availability. */
export interface RightDockAvailabilityGate {
  subscribe: (listener: () => void) => () => void;
  getSnapshot: () => readonly RightDockTabDefinition[];
}

/** Filter items through their availability sources. */
export function availabilityGate(
  items: readonly RightDockTabDefinition[],
): RightDockAvailabilityGate {
  let cached: readonly RightDockTabDefinition[] | null = null;
  const compute = (): readonly RightDockTabDefinition[] => {
    const next = items.filter(
      (item) => item.availability === undefined || item.availability.getSnapshot(),
    );
    if (
      cached !== null &&
      cached.length === next.length &&
      cached.every((item, index) => item === next[index])
    ) {
      return cached;
    }
    cached = next;
    return next;
  };
  return {
    subscribe: (listener) => {
      const unsubscribes = items
        .map((item) => item.availability?.subscribe(listener))
        .filter((unsubscribe): unsubscribe is () => void => unsubscribe !== undefined);
      return () => {
        for (const unsubscribe of unsubscribes) unsubscribe();
      };
    },
    getSnapshot: compute,
  };
}
