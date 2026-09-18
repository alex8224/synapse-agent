/**
 * The strip's centre entry: this turn's telemetry, then the session's usage.
 *
 * Wide window: the plain segments, exactly as before (`尚无本轮指标` when there
 * is nothing to report).  Phone band: the entry paints a compact chip — the
 * metrics worth a glance — and its popover carries the *complete* list, so
 * compacting never hides a number.
 *
 * It is the centre track's only entry, and a centre entry overflows by default
 * (`compactPolicyOf`); this one declares `compact` instead, so the phone strip
 * keeps a simplified telemetry rather than losing it.
 */
import { DataUsage20Regular } from '@fluentui/react-icons';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import {
  compactSegments,
  contextOccupancy,
  sessionUsageSegments,
  turnStatSegments,
} from '../../stores/usageView.ts';
import type { UsageSegment } from '../../stores/usageView.ts';
import type { BottomBarItemDefinition } from './contract.ts';

export const TELEMETRY_ITEM_ID = 'telemetry';

/**
 * One segment, as the strip prints it: optional label, then the value.
 *
 * A plain render function rather than a component: the entry module exports a
 * definition object, and keeping components out of its top level keeps the
 * module's fast-refresh boundary intact.
 */
function segmentView(segment: UsageSegment) {
  return (
    <span className="flex items-baseline gap-1">
      {segment.label !== '' && (
        <span className="font-sans text-[10px] text-gray-400">{segment.label}</span>
      )}
      <span className={segment.emphasis ? 'font-medium text-gray-800' : 'text-gray-600'}>
        {segment.value}
      </span>
    </span>
  );
}

/** The strip's own read of the metrics it paints. */
function useTelemetry(): UsageSegment[] {
  const { usage, sessionUsage, contextWindow } = useConsoleStore(
    useShallow((state) => ({
      usage: state.usage,
      sessionUsage: state.sessionUsage,
      contextWindow: state.contextWindow,
    })),
  );
  // The bar reports this turn's speed/latency/steps, then the session's usage as
  // two raw groups (totals, then context/hit share).  No label and no tooltip:
  // the numbers are printed as the runtime reported them.
  return [
    ...turnStatSegments(usage),
    ...sessionUsageSegments(sessionUsage, contextOccupancy(usage), contextWindow),
  ];
}

export const telemetryItem: BottomBarItemDefinition = {
  id: TELEMETRY_ITEM_ID,
  label: '本轮指标',
  region: 'center',
  order: 10,
  compact: 'compact',
  overlay: 'popover',
  panelLabel: '本轮与会话指标',
  panelClassName:
    'w-72 max-w-[calc(100vw-2rem)] rounded-card border border-line/80 material-flyout flyout-in p-3 text-left shadow-flyout',
  Trigger: function TelemetryTrigger({ context, open, anchorRef }) {
    const telemetry = useTelemetry();
    if (!context.compact) {
      if (telemetry.length === 0) {
        return (
          <span data-entry={TELEMETRY_ITEM_ID} className="font-sans text-[11px] text-gray-300">
            尚无本轮指标
          </span>
        );
      }
      return (
        <span data-entry={TELEMETRY_ITEM_ID} className="flex items-center gap-2">
          {telemetry.map((segment, index) => (
            <span key={segment.key} className="flex items-center gap-2">
              {index > 0 && <span className="text-gray-200">|</span>}
              {segmentView(segment)}
            </span>
          ))}
        </span>
      );
    }
    const compact = compactSegments(telemetry);
    return (
      <button
        ref={anchorRef}
        data-entry={TELEMETRY_ITEM_ID}
        type="button"
        onClick={(event) => context.toggle(TELEMETRY_ITEM_ID, event.currentTarget)}
        title="本轮与会话指标（点开看完整分段）"
        aria-haspopup="dialog"
        aria-expanded={open}
        className="flex cursor-pointer items-center gap-1 transition-colors hover:text-gray-900"
      >
        <DataUsage20Regular
          aria-hidden="true"
          className="shrink-0 text-gray-400"
          style={{ fontSize: '15px' }}
        />
        {compact.length === 0 ? (
          <span className="text-gray-300">-</span>
        ) : (
          compact.map((segment, index) => (
            <span key={segment.key} className="flex items-center gap-1.5">
              {index > 0 && <span className="text-gray-200">|</span>}
              {segmentView(segment)}
            </span>
          ))
        )}
      </button>
    );
  },
  Content: function TelemetryContent() {
    const telemetry = useTelemetry();
    return (
      <div className="space-y-1.5">
        <div className="border-b border-line/60 pb-1.5 text-xs font-bold text-gray-900">
          本轮与会话指标
        </div>
        {telemetry.length === 0 ? (
          <div className="font-sans text-[11px] text-gray-400">尚无本轮指标</div>
        ) : (
          telemetry.map((segment) => (
            <div
              key={segment.key}
              className="flex items-baseline justify-between gap-3 text-[11px]"
            >
              <span className="font-sans text-gray-400">{segment.label === '' ? segment.key : segment.label}</span>
              <span className={segment.emphasis ? 'font-medium text-gray-900' : 'text-gray-700'}>
                {segment.value}
              </span>
            </div>
          ))
        )}
      </div>
    );
  },
};
