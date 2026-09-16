/**
 * The status strip: the host of the bottom-bar entry manifest.
 *
 * The strip itself owns only what is common to every entry:
 *
 *  - the three tracks (`BottomBarRegions`) and the strip's own chrome,
 *  - the **one** open overlay (`openId`): the same entry closes, another
 *    replaces it, so F1 / F5 / F6 are mutually exclusive by construction,
 *  - the advertised keys, resolved through `consoleShortcuts`: a key the strip
 *    claims is always `preventDefault`ed — the browser's own action for it (F5
 *    reloads the page) must never run, on a press *or* on any repeat of a held
 *    key — and the repeat itself is then ignored, so holding F5 must not flap
 *    the panel either,
 *  - the dismissal split: a *popover* is closed by a click outside its own
 *    trigger+panel and by Escape (both handled by the overlay host below), while
 *    a *modal* owns its own scrim, Escape and focus — the strip keeps no second
 *    listener for it,
 *  - the compact (phone) band: a narrow window moves the entries whose policy is
 *    `more` into the 更多 menu instead of clipping them, and the left track
 *    scrolls rather than cutting a control off,
 *  - the availability filter: an entry may declare a subscribable
 *    `availability` source (the Codex usage entry is only there for an enabled
 *    OAuth profile), and the strip reads the manifest through those sources with
 *    `useSyncExternalStore` *before* resolving the layout.  An entry that answers
 *    "no" is therefore not in any track, in the 更多 menu, or in the shortcut
 *    table, leaves no separator behind, and its open panel goes with it,
 *  - closing the overlay when the session switches or the entry stops being in
 *    the layout (a hidden entry must not keep a stale panel on screen).
 *
 * Everything an entry paints lives in its own module under `bottomBar/`, with
 * its own store subscription, so a reasoning delta re-renders one entry at most
 * and never the strip.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';
import { FloatingPanel } from './FloatingPanel.tsx';
import { BOTTOM_BAR_ITEMS } from './bottomBar/manifest.tsx';
import { MORE_ENTRY } from './bottomBar/moreEntry.tsx';
import { BottomBarRegions } from './bottomBar/regions.ts';
import {
  COMPACT_QUERY,
  MORE_ENTRY_ID,
  availabilityGate,
  itemById,
  isSessionSwitch,
  layoutEntries,
  resolveBottomBarLayout,
  shortcutEntries,
  toggleOpenId,
} from './bottomBar/contract.ts';
import type { BottomBarContext, BottomBarItemDefinition } from './bottomBar/contract.ts';

/*
  Layout decision (kept deliberately): three tracks `1fr auto 1fr` keep the
  telemetry block in the exact horizontal centre of the bar, because both
  flexible tracks resolve to the same leftover width.  The left column carries
  activity + MCP + goal, the centre carries the current turn's telemetry, and the
  right track stays an empty, symmetric spacer (F1 opens the full shortcut list,
  and its entry paints no control).  Do not switch the centre to a right-aligned
  column: the bar must stay centre-weighted.

  `whitespace-nowrap` is load-bearing: the bar is a fixed 28px strip, and a
  squeezed label that wraps would double a row's line box and push the whole bar
  out of alignment.  Labels that can grow truncate at their own `max-w`.

  Deliberately *not* `overflow-hidden`: it once hid an `absolute` popover.  The
  popovers are portalled `FloatingPanel`s now, and the compact track scrolls
  instead of clipping, so no control is cut off.

  It is a real flex child of the app column rather than an overlay: while it was
  `fixed`, the middle row still stretched to the viewport bottom and the bar
  covered the sidebar's own footer (its settings entry), leaving a strip of it
  unreachable.
*/
const STRIP_BASE =
  'material-strip relative z-40 h-status w-full whitespace-nowrap border-t border-line px-3 font-numeric text-[11px] text-gray-500 shrink-0 select-none';
/** Wide window: the symmetric three-track grid (see the layout note above). */
const DESKTOP_STRIP = `${STRIP_BASE} grid grid-cols-[1fr_auto_1fr] items-center gap-4`;
/** Phone band: one row; the left track scrolls, the centre/right stay pinned. */
const COMPACT_STRIP = `${STRIP_BASE} flex items-center gap-2`;

/** The strip's one open overlay: which entry, and the element it hangs from. */
interface OpenOverlay {
  id: string;
  /** The trigger that opened it; `null` for an entry that paints no control. */
  anchor: HTMLElement | null;
}

/** The phone band, from the same breakpoint the shell lays out for. */
function useCompactStrip(): boolean {
  const [compact, setCompact] = useState(() => window.matchMedia(COMPACT_QUERY).matches);
  useEffect(() => {
    const query = window.matchMedia(COMPACT_QUERY);
    const update = () => setCompact(query.matches);
    update();
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);
  return compact;
}

/** One entry's control.  A keyboard-only entry paints nothing at all. */
const BottomBarEntry: React.FC<{
  item: BottomBarItemDefinition;
  context: BottomBarContext;
  register: (id: string, element: HTMLElement | null) => void;
}> = ({ item, context, register }) => {
  const { id } = item;
  // The entry hands the strip its own element, so a popover hangs from the
  // trigger it belongs to (and a click on that trigger never closes it).
  const anchorRef = useCallback(
    (element: HTMLElement | null) => register(id, element),
    [id, register],
  );
  const Trigger = item.Trigger;
  if (Trigger === undefined) return null;
  return <Trigger context={context} open={context.openId === id} anchorRef={anchorRef} />;
};

/** The open entry's overlay: a portalled popover, or a self-contained modal. */
const BottomBarOverlay: React.FC<{
  item: BottomBarItemDefinition;
  context: BottomBarContext;
  anchor: HTMLElement | null;
}> = ({ item, context, anchor }) => {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const popover = item.overlay === 'popover';

  useEffect(() => {
    // A modal is not this component's business: it renders its own scrim and
    // owns its own Escape and focus (see `GoalDialog` / `HelpDialog`), so the
    // strip keeps exactly one listener per overlay kind instead of two.
    if (!popover) return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (target === null) return;
      // Only *this* entry's trigger and panel keep the popover open: a click
      // anywhere else — the rest of the strip included — closes it.  A wrapper
      // around the whole bar would swallow those clicks instead.
      const inside =
        (anchor?.contains(target) ?? false) || (panelRef.current?.contains(target) ?? false);
      if (!inside) context.close();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') context.close();
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [anchor, context, popover]);

  const Content = item.Content;
  if (Content === undefined) return null;
  if (!popover) return <Content context={context} />;
  return (
    <FloatingPanel
      anchor={anchor}
      side="top"
      align="start"
      offset={8}
      label={item.panelLabel}
      className={item.panelClassName ?? ''}
      panelRef={panelRef}
    >
      <Content context={context} />
    </FloatingPanel>
  );
};

export const BottomBar: React.FC = () => {
  // Only the facts the strip itself decides on: what each entry paints is its
  // own subscription (a reasoning delta must not re-render the strip).
  const sessionOpen = useConsoleStore((state) => state.currentSession.thread_id !== '');
  const compact = useCompactStrip();
  const [open, setOpen] = useState<OpenOverlay | null>(null);
  const barRef = useRef<HTMLElement | null>(null);
  // Only the keyboard path needs this registry: a click hands the strip the
  // trigger element itself, and the element the open overlay hangs from is part
  // of the open state (never read from a ref while rendering).
  const anchors = useRef(new Map<string, HTMLElement>());
  const openId = open?.id ?? null;

  const toggle = useCallback((id: string, anchor: HTMLElement | null = null) => {
    setOpen((current) =>
      toggleOpenId(current?.id ?? null, id) === null ? null : { id, anchor },
    );
  }, []);
  const close = useCallback(() => setOpen(null), []);
  const registerAnchor = useCallback((id: string, element: HTMLElement | null) => {
    if (element === null) anchors.current.delete(id);
    else anchors.current.set(id, element);
  }, []);

  const visibility = useMemo(() => ({ sessionOpen, compact }), [sessionOpen, compact]);
  // Entries that answer their own existence at runtime (the Codex usage entry is
  // only there for an enabled OAuth profile) are filtered *before* the layout is
  // resolved.  The gate subscribes to each source, which is also what starts and
  // stops that source's discovery — a source started only by a painted Trigger
  // could never become painted in the first place.  Filtering here is what makes
  // "unavailable" mean no wrapper, no separator, no 更多 row, no shortcut, and no
  // stale panel: every one of those is derived from the resolved layout.
  const gate = useMemo(() => availabilityGate(BOTTOM_BAR_ITEMS), []);
  const availableItems = useSyncExternalStore(gate.subscribe, gate.getSnapshot);
  const layout = useMemo(
    () => resolveBottomBarLayout(availableItems, visibility, MORE_ENTRY),
    [availableItems, visibility],
  );
  const shortcuts = useMemo(() => shortcutEntries(layout), [layout]);
  const reachable = useMemo(() => layoutEntries(layout), [layout]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      // A key the strip does not answer is left alone entirely: it must reach the
      // browser and every other listener untouched.
      const item = shortcuts.get(event.key);
      if (item === undefined) return;
      // ...but a claimed key is the strip's own, so its browser default never
      // runs.  This is deliberately before the repeat check: a held F5 repeats,
      // and a repeat that returned first would let the browser reload the page.
      event.preventDefault();
      // Holding a key down repeats it; the strip answers the press once.
      if (event.repeat) return;
      // A keypress has no element of its own, so a popover hangs from the
      // trigger the entry paints (or from the 更多 entry / the strip itself when
      // the entry was compacted away).
      toggle(
        item.id,
        anchors.current.get(item.id) ?? anchors.current.get(MORE_ENTRY_ID) ?? barRef.current,
      );
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [shortcuts, toggle]);

  useEffect(
    () =>
      useConsoleStore.subscribe((state, previous) => {
        // A panel belongs to the session that opened it.
        if (isSessionSwitch(state.currentSession, previous.currentSession)) setOpen(null);
      }),
    [],
  );

  useEffect(() => {
    // A hidden entry (its business rule, or the compact band) closes its own
    // overlay rather than leaving it on screen with no trigger behind it.
    if (openId === null) return;
    if (!reachable.some((item) => item.id === openId)) setOpen(null);
  }, [openId, reachable]);

  const context: BottomBarContext = useMemo(
    () => ({
      openId,
      anchor: open?.anchor ?? null,
      toggle,
      close,
      compact,
      sessionOpen,
      overflow: layout.overflow,
    }),
    [open, openId, toggle, close, compact, sessionOpen, layout],
  );

  // Resolved from the *available* entries (plus the host's own 更多 entry): an
  // entry that stopped being available takes its overlay off screen in the same
  // render, instead of leaving one stale panel for a frame.
  const openItem =
    openId === null ? null : (itemById([...availableItems, MORE_ENTRY], openId) ?? null);

  return (
    <>
      <footer ref={barRef} className={compact ? COMPACT_STRIP : DESKTOP_STRIP}>
        <BottomBarRegions
          layout={layout}
          compact={compact}
          slot={(item) => (
            <BottomBarEntry key={item.id} item={item} context={context} register={registerAnchor} />
          )}
        />
      </footer>

      {openItem !== null && (
        <BottomBarOverlay item={openItem} context={context} anchor={open?.anchor ?? null} />
      )}
    </>
  );
};
