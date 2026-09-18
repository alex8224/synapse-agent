/**
 * The status strip's static registration contract.
 *
 * A bottom-bar entry is a module exporting one `BottomBarItemDefinition`, plus
 * one line in `manifest.tsx`.  Nothing else changes: the strip is the host, and
 * it paints whatever the manifest declares — no mutable registry, no runtime
 * registration, no user layout file, and no plugin surface.
 *
 * The contract itself lives here, deliberately free of React and of the store,
 * so the Node test runner can exercise the layout rules directly (the region
 * renderer in `regions.ts` renders the same rules into real DOM through
 * `react-dom/server`).  The rules are:
 *
 *  - **region + order**: `left` / `center` / `right`, ordered inside each track;
 *    ties keep the manifest's order.
 *  - **visibility**: an entry may declare a business `visible` rule.  A hidden
 *    entry is *not* in the layout at all, so it leaves no wrapper and no
 *    separator behind — the renderer only ever sees entries it paints.
 *  - **availability**: an entry whose existence depends on an answer only the
 *    runtime has (an OAuth verdict, a capability) declares an `availability`
 *    source instead of a `visible` rule, because the strip has to *subscribe* to
 *    it.  The host filters the manifest through those sources with
 *    `useSyncExternalStore` *before* resolving the layout, so an entry that
 *    becomes unavailable leaves no wrapper, no separator and no 更多 row behind,
 *    and its open panel is closed by the strip's own "no longer reachable" rule.
 *  - **overlay kind**: `popover` (hangs from its own trigger) or `modal` (owns
 *    its own scrim, Escape and focus).  An entry may also declare none, and
 *    then it is a plain control.
 *  - **keyboard-only entries**: an entry with no `Trigger` paints no control at
 *    all (F1 help) — the right track stays the symmetric spacer.
 *  - **compact policy**: on a narrow window an entry is `keep`t, painted by its
 *    own `compact` form, or moved into the 更多 menu (`more`).  An entry with
 *    `more` but nothing to open stays in the strip: a control is never dropped
 *    without a way back to it.
 */
import type React from 'react';

/** The strip's three tracks, left to right. */
export type BottomBarRegion = 'left' | 'center' | 'right';

/** What activating an entry's trigger opens. */
export type BottomBarOverlayKind = 'popover' | 'modal';

/**
 * How an entry behaves on a narrow window.
 *
 *  - `keep`: painted as usual.
 *  - `compact`: painted by its own narrow form (the entry decides what that is,
 *    and offers the full view through its overlay).
 *  - `more`: moved into the 更多 menu, so it stays reachable without the width.
 */
export type BottomBarCompactPolicy = 'keep' | 'compact' | 'more';

/**
 * Whether an entry may exist right now, as a subscribable source.
 *
 * `subscribe` runs when the strip mounts and the returned function when it
 * unmounts — which is the hook an entry uses to *start and stop* whatever
 * discovers the answer.  A source that could only be discovered while its Trigger
 * was painted could never flip from hidden to painted, so the discovery must not
 * hang off the control's mount.
 */
export interface BottomBarAvailability {
  /** The current answer; must be cheap and referentially stable. */
  getSnapshot: () => boolean;
  /** Notify on every change of that answer.  Returns the unsubscribe. */
  subscribe: (listener: () => void) => () => void;
}

/** The phone band the strip compacts in — the shell's own mobile breakpoint. */
export const COMPACT_QUERY = '(max-width: 767px)';

/** The id of the host's 更多 entry (not a manifest entry). */
export const MORE_ENTRY_ID = 'more';

/** Business facts a `visible` rule may read. */
export interface BottomBarVisibilityContext {
  /** A session is attached (`currentSession.thread_id !== ''`). */
  sessionOpen: boolean;
  /** The window is in the compact (phone) band. */
  compact: boolean;
}

/** The strip's runtime state, handed to every trigger and overlay. */
export interface BottomBarContext extends BottomBarVisibilityContext {
  /** The one overlay the strip has open, or `null`. */
  openId: string | null;
  /**
   * The element the open overlay hangs from — the trigger that opened it.  The
   * 更多 menu reuses it, because the entry it opens is not painted itself.
   */
  anchor: HTMLElement | null;
  /**
   * Open this entry, or close it when it is already the open one.
   *
   * A trigger passes its own element (`event.currentTarget`), which is what a
   * popover hangs from and what keeps a click on it from closing the panel.
   */
  toggle: (id: string, anchor?: HTMLElement | null) => void;
  /** Close whatever is open. */
  close: () => void;
  /** Entries the 更多 menu carries (empty on a wide window). */
  overflow: readonly BottomBarItemDefinition[];
}

export interface BottomBarTriggerProps {
  context: BottomBarContext;
  /** True while this entry owns the open overlay (for `aria-expanded`). */
  open: boolean;
  /**
   * Attach this to the entry's own root element: it is the element the strip
   * anchors a `popover` to, and the one a click must land on to keep it open.
   * An entry that forgets it still works — the popover falls back to the strip.
   */
  anchorRef: React.RefCallback<HTMLElement>;
}

export interface BottomBarItemDefinition {
  /** Stable id: the entry's `openId` and its React key. */
  id: string;
  /** Short name for the 更多 menu row. */
  label: string;
  region: BottomBarRegion;
  /** Order inside the region; ties keep the manifest's order. */
  order: number;
  /** `KeyboardEvent.key` that opens this entry (see `consoleShortcuts`). */
  shortcutKey?: string;
  /** What activating the trigger opens; absent = a plain control. */
  overlay?: BottomBarOverlayKind;
  /** Narrow-window policy; defaults to `keep` (`more` in the centre track). */
  compact?: BottomBarCompactPolicy;
  /** Business visibility; absent = always painted. */
  visible?: (context: BottomBarVisibilityContext) => boolean;
  /**
   * Dynamic existence; absent = always available.  Read through
   * `useSyncExternalStore` by the host, so the entry is filtered out of the
   * manifest *before* the layout is resolved and its source can start/stop the
   * discovery that answers it.
   */
  availability?: BottomBarAvailability;
  /**
   * The strip's control for this entry.  Absent = a keyboard-only entry: the
   * strip paints no control for it and leaves no wrapper behind.
   *
   * A painted trigger carries `data-entry={id}` on its root element, which is
   * what makes an entry addressable in the rendered strip (the browser
   * verification script reads exactly that), and it attaches `anchorRef` to the
   * same element when it opens a popover.
   */
  Trigger?: React.FC<BottomBarTriggerProps>;
  /** The overlay body; rendered only while this entry is the open one. */
  Content?: React.FC<{ context: BottomBarContext }>;
  /** Classes for a `popover` overlay's box (width, padding, material). */
  panelClassName?: string;
  /** Accessible name of a `popover` overlay. */
  panelLabel?: string;
}

export interface BottomBarLayout {
  /** The entries each track paints, in order (trigger-bearing entries only). */
  regions: Record<BottomBarRegion, BottomBarItemDefinition[]>;
  /** Visible entries the strip paints no control for (F1 help). */
  keyboard: BottomBarItemDefinition[];
  /** Entries the strip does not paint on a narrow window (the 更多 menu). */
  overflow: BottomBarItemDefinition[];
}

/**
 * The narrow-window policy of an entry.
 *
 * A centre entry defaults to `more`: the centre track is the first thing a phone
 * window cannot afford, and overflowing it keeps it reachable instead of
 * clipping it.  The side tracks default to `keep`.
 */
export function compactPolicyOf(item: BottomBarItemDefinition): BottomBarCompactPolicy {
  if (item.compact !== undefined) return item.compact;
  return item.region === 'center' ? 'more' : 'keep';
}

/**
 * Whether an entry leaves the strip on a narrow window.
 *
 * An entry with nothing to open has no 更多 row that could reach it, so it stays
 * in the strip: a control is never dropped without a way back to it.
 */
function overflows(item: BottomBarItemDefinition): boolean {
  return (
    compactPolicyOf(item) === 'more' &&
    item.overlay !== undefined &&
    item.Content !== undefined
  );
}

/**
 * Split the manifest into the tracks the strip paints, the keyboard-only
 * entries, and the 更多 overflow.
 *
 * `moreEntry` is the host's own 更多 entry: it is appended to its region only
 * while something actually overflowed, so the menu never appears empty.
 */
export function resolveBottomBarLayout(
  items: readonly BottomBarItemDefinition[],
  context: BottomBarVisibilityContext,
  moreEntry?: BottomBarItemDefinition,
): BottomBarLayout {
  const regions: Record<BottomBarRegion, BottomBarItemDefinition[]> = {
    left: [],
    center: [],
    right: [],
  };
  const keyboard: BottomBarItemDefinition[] = [];
  const overflow: BottomBarItemDefinition[] = [];
  for (const item of inOrder(items)) {
    if (item.visible !== undefined && !item.visible(context)) continue;
    if (context.compact && overflows(item)) {
      overflow.push(item);
      continue;
    }
    if (item.Trigger === undefined) {
      keyboard.push(item);
      continue;
    }
    regions[item.region].push(item);
  }
  if (overflow.length > 0 && moreEntry !== undefined) regions[moreEntry.region].push(moreEntry);
  return { regions, keyboard, overflow };
}

/** Entries sorted by `order`, ties keeping the manifest's order (stable). */
function inOrder(items: readonly BottomBarItemDefinition[]): BottomBarItemDefinition[] {
  return items
    .map((item, index) => ({ item, index }))
    .sort((a, b) => a.item.order - b.item.order || a.index - b.index)
    .map((entry) => entry.item);
}

/** Every entry the layout keeps reachable, painted or not. */
export function layoutEntries(layout: BottomBarLayout): BottomBarItemDefinition[] {
  return [
    ...layout.regions.left,
    ...layout.regions.center,
    ...layout.regions.right,
    ...layout.keyboard,
    ...layout.overflow,
  ];
}

/**
 * The entries a keypress may open, by `KeyboardEvent.key`.
 *
 * Built from the *layout*, so an entry that is hidden (or whose overlay is gone)
 * stops answering its shortcut at the same moment it stops being painted.
 */
export function shortcutEntries(layout: BottomBarLayout): Map<string, BottomBarItemDefinition> {
  const found = new Map<string, BottomBarItemDefinition>();
  for (const item of layoutEntries(layout)) {
    if (item.shortcutKey === undefined || item.overlay === undefined) continue;
    found.set(item.shortcutKey, item);
  }
  return found;
}

/**
 * The strip's one-open-overlay rule: the same entry closes, another replaces.
 *
 * Keeping a single id (rather than one boolean per panel) is what makes F1 / F5
 * / F6 mutually exclusive, and what lets a modal and a popover share the same
 * dismissal path.
 */
export function toggleOpenId(current: string | null, id: string): string | null {
  return current === id ? null : id;
}

/** What identifies the session a panel belongs to. */
export interface SessionIdentity {
  thread_id: string;
  project_id: string;
}

/**
 * Whether the console moved to another session (or another project).
 *
 * An open panel belongs to the session that opened it — its goal, its MCP
 * attachment, its telemetry — so a switch closes it rather than leaving one
 * session's panel on screen for another.
 */
export function isSessionSwitch(current: SessionIdentity, previous: SessionIdentity): boolean {
  return (
    current.thread_id !== previous.thread_id || current.project_id !== previous.project_id
  );
}

/** The entry a layout holds for an id, or `undefined` when it is not in it. */
export function itemById(
  items: readonly BottomBarItemDefinition[],
  id: string,
): BottomBarItemDefinition | undefined {
  return items.find((item) => item.id === id);
}

/** The `useSyncExternalStore` pair the strip reads the manifest through. */
export interface BottomBarAvailabilityGate {
  /** Subscribe to every entry's source; returns one unsubscribe for all of them. */
  subscribe: (listener: () => void) => () => void;
  /**
   * The entries whose source currently says yes, in manifest order.
   *
   * Referentially stable while the answer is unchanged (the host compares it by
   * identity), so a source that merely re-notifies cannot re-render the strip.
   */
  getSnapshot: () => readonly BottomBarItemDefinition[];
}

/**
 * Filter a manifest through its entries' own `availability` sources.
 *
 * An entry without a source is always available, so a manifest of plain entries
 * resolves to itself.  This is a *pure* function of the sources' current answers:
 * the strip only has to feed the pair to `useSyncExternalStore` and hand the
 * snapshot to `resolveBottomBarLayout`, which is what makes "the entry is gone"
 * mean "no wrapper, no separator, no 更多 row, no shortcut, and its open panel
 * closes".
 */
export function availabilityGate(
  items: readonly BottomBarItemDefinition[],
): BottomBarAvailabilityGate {
  let cached: readonly BottomBarItemDefinition[] | null = null;
  const compute = (): readonly BottomBarItemDefinition[] => {
    const next = items.filter(
      (item) => item.availability === undefined || item.availability.getSnapshot(),
    );
    // Identity-stable while the visible set is unchanged: a source may notify for
    // its own reasons, and the strip must not re-render for that.
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
