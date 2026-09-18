/**
 * The strip's Codex usage entry: window remaining percent, reset countdown and
 * the redeemable reset-credit count, between the activity and MCP entries (the
 * TUI registers the same component at the same `order`).
 *
 * Two things make this entry different from the others:
 *
 *  1. it may not exist at all.  It is painted only while the *server* says the
 *     session's effective model is an enabled Codex OAuth provider, so it declares
 *     an `availability` source instead of a `visible` rule: that source's
 *     `subscribe` starts the controller which discovers the verdict, and the host
 *     filters the manifest through it with `useSyncExternalStore`.  When the
 *     verdict is "no" the entry leaves the strip entirely — no wrapper, no
 *     separator, no 更多 row, no panel;
 *  2. redeeming a credit is a real write against the account's quota.  The panel
 *     therefore raises a confirmation first, and only that confirmation's own
 *     button sends anything.
 *
 * The countdown is drawn from the view's `reset_at` with a local 1s tick: it never
 * asks the runtime for a fresh snapshot just to tick, and the window length is
 * labelled from the window's *real* `window_minutes` (a 7-day window stays `7d`
 * instead of inheriting the TUI's hard-coded `1d`).
 *
 * This module exports the definition and nothing else: the panel's markup lives in
 * `../CodexUsagePanel.tsx`, like every other entry's content.
 */
import { useEffect, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { ChevronDown16Regular } from '@fluentui/react-icons';
import { CodexMark } from './CodexMark.tsx';
import { CodexUsagePanel } from '../CodexUsagePanel.tsx';
import {
  CODEX_USAGE_LOADING,
  formatUsageLabel,
  lowestRemainingPercent,
} from '../../stores/codexUsageView.ts';
import { codexUsageAvailability, useCodexUsageStore } from '../../stores/codexUsage.ts';
import type { BottomBarItemDefinition } from './contract.ts';

export const CODEX_USAGE_ITEM_ID = 'codex-usage';

/** The strip's own truncation bound: a long line never squeezes the telemetry. */
const LABEL_MAX_WIDTH = 'max-w-[15rem]';

/**
 * A local 1s clock.
 *
 * It only drives the countdown text, so a tick re-renders this entry and nothing
 * else — and never triggers a request.  The interval goes away with the hook (and
 * the hook with the entry), so an unmounted entry leaves no timer behind, and only
 * one of the two clocks runs at a time: the trigger ticks while the panel is shut,
 * the panel ticks while it is open.
 */
function useLocalSeconds(active: boolean): number {
  const [seconds, setSeconds] = useState(() => Math.floor(Date.now() / 1000));
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setSeconds(Math.floor(Date.now() / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return seconds;
}

/** The one line the trigger prints, plus the tint its lowest window deserves. */
function useCodexUsageLine(nowSeconds: number): { line: string; low: number | null } {
  const { usage, credits, available } = useCodexUsageStore(
    useShallow((state) => ({
      usage: state.usage,
      credits: state.credits,
      available: state.available,
    })),
  );
  if (!available) return { line: '', low: null };
  return {
    line: usage === null ? CODEX_USAGE_LOADING : formatUsageLabel(usage, nowSeconds, credits),
    low: lowestRemainingPercent(usage),
  };
}

export const codexUsageItem: BottomBarItemDefinition = {
  id: CODEX_USAGE_ITEM_ID,
  label: 'Codex 用量',
  region: 'left',
  // Between the run state (10) and MCP (20) — the TUI's own `order`.
  order: 15,
  // A phone window has no room for a second numeric label, and the entry does
  // have a panel to open, so the 更多 menu carries it (the existing entry point).
  compact: 'more',
  overlay: 'popover',
  panelLabel: 'Codex 用量与重置额度',
  panelClassName:
    'w-80 max-w-[calc(100vw-2rem)] max-h-[calc(100dvh-5rem)] overflow-y-auto no-scrollbar whitespace-normal break-words rounded-card border border-line/80 material-flyout flyout-in p-3 font-numeric text-left shadow-flyout',
  availability: codexUsageAvailability,
  Trigger: function CodexUsageTrigger({ context, open, anchorRef }) {
    const available = useCodexUsageStore((state) => state.available);
    const nowSeconds = useLocalSeconds(available && !open);
    const { line, low } = useCodexUsageLine(nowSeconds);
    if (!available) return null;
    const critical = low !== null && low < 50;
    return (
      <button
        ref={anchorRef}
        data-entry={CODEX_USAGE_ITEM_ID}
        type="button"
        onClick={(event) => context.toggle(CODEX_USAGE_ITEM_ID, event.currentTarget)}
        title="Codex 用量与重置额度"
        aria-haspopup="dialog"
        aria-expanded={open}
        className="flex min-w-0 cursor-pointer items-center gap-1 transition-colors hover:text-gray-900"
      >
        <CodexMark className={`shrink-0 ${critical ? 'text-red-500' : 'text-blue-500'}`} />
        <span
          className={`truncate ${LABEL_MAX_WIDTH} ${critical ? 'text-red-600' : 'text-gray-700'}`}
        >
          {line}
        </span>
        <ChevronDown16Regular aria-hidden="true" className="shrink-0 text-gray-400" />
      </button>
    );
  },
  Content: function CodexUsageContent({ context }) {
    const nowSeconds = useLocalSeconds(true);
    return <CodexUsagePanel onClose={context.close} nowSeconds={nowSeconds} />;
  },
};
