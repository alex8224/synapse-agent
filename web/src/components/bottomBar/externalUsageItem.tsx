/**
 * The strip's external ACP (`agy`) usage entry: active model group's remaining
 * quota percentages and reset countdowns, positioned right next to the Codex
 * usage entry (they are mutually exclusive: a session is either native or
 * external ACP).
 *
 * Mirrors `codexUsageItem.tsx`:
 * - declares an `availability` source so the entry disappears completely when
 *   not applicable (native sessions or ACP agents without a usage surface),
 * - drives the countdown from a local 1s clock without re-querying the daemon,
 * - renders its popover content via `../ExternalUsagePanel.tsx`.
 */
import { useEffect, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { ChevronDown16Regular } from '@fluentui/react-icons';
import { AgyMark } from './AgyMark.tsx';
import { ExternalUsagePanel } from '../ExternalUsagePanel.tsx';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import {
  EXTERNAL_USAGE_LOADING,
  activeExternalModel,
  formatExternalUsageLabel,
  lowestExternalRemainingPercent,
} from '../../stores/externalUsageView.ts';
import {
  externalUsageAvailability,
  useExternalUsageStore,
} from '../../stores/externalUsage.ts';
import type { BottomBarItemDefinition } from './contract.ts';

export const EXTERNAL_USAGE_ITEM_ID = 'external-usage';

const LABEL_MAX_WIDTH = 'max-w-[15rem]';

function useLocalSeconds(active: boolean): number {
  const [seconds, setSeconds] = useState(() => Math.floor(Date.now() / 1000));
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setSeconds(Math.floor(Date.now() / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return seconds;
}

function useExternalUsageLine(nowSeconds: number): { line: string; low: number | null } {
  const { usage, available } = useExternalUsageStore(
    useShallow((state) => ({
      usage: state.usage,
      available: state.available,
    })),
  );
  const externalOptions = useConsoleStore((state) => state.externalSession?.options);
  if (!available) return { line: '', low: null };
  const activeModel = activeExternalModel(externalOptions);
  return {
    line:
      usage === null
        ? EXTERNAL_USAGE_LOADING
        : formatExternalUsageLabel(usage, activeModel, nowSeconds),
    low: lowestExternalRemainingPercent(usage, activeModel),
  };
}

export const externalUsageItem: BottomBarItemDefinition = {
  id: EXTERNAL_USAGE_ITEM_ID,
  label: 'Antigravity 用量',
  region: 'left',
  // Right next to Codex usage (15) and before MCP (20); native Codex and
  // external ACP are mutually exclusive per session.
  order: 16,
  compact: 'more',
  overlay: 'popover',
  panelLabel: 'Antigravity 用量',
  panelClassName:
    'w-80 max-w-[calc(100vw-2rem)] max-h-[calc(100dvh-5rem)] overflow-y-auto no-scrollbar whitespace-normal break-words rounded-card border border-line/80 material-flyout flyout-in p-3 font-numeric text-left shadow-flyout',
  availability: externalUsageAvailability,
  Trigger: function ExternalUsageTrigger({ context, open, anchorRef }) {
    const available = useExternalUsageStore((state) => state.available);
    const nowSeconds = useLocalSeconds(available && !open);
    const { line, low } = useExternalUsageLine(nowSeconds);
    if (!available) return null;
    const critical = low !== null && low < 50;
    return (
      <button
        ref={anchorRef}
        data-entry={EXTERNAL_USAGE_ITEM_ID}
        type="button"
        onClick={(event) => context.toggle(EXTERNAL_USAGE_ITEM_ID, event.currentTarget)}
        title="Antigravity 用量"
        aria-haspopup="dialog"
        aria-expanded={open}
        className="flex min-w-0 cursor-pointer items-center gap-1 transition-colors hover:text-gray-900"
      >
        <AgyMark className={`shrink-0 ${critical ? 'text-red-500' : 'text-blue-500'}`} />
        <span
          className={`truncate ${LABEL_MAX_WIDTH} ${critical ? 'text-red-600' : 'text-gray-700'}`}
        >
          {line}
        </span>
        <ChevronDown16Regular aria-hidden="true" className="shrink-0 text-gray-400" />
      </button>
    );
  },
  Content: function ExternalUsageContent({ context }) {
    const nowSeconds = useLocalSeconds(true);
    return <ExternalUsagePanel onClose={context.close} nowSeconds={nowSeconds} />;
  },
};
