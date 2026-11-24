/**
 * The external ACP (Antigravity / `agy`) usage panel: quota groups, per-window
 * remaining percentages and reset countdowns, plus manual refresh.
 *
 * Modeled directly after `CodexUsagePanel.tsx`: the box, `dialog` role and
 * portal belong to `bottomBar/externalUsageItem.tsx`, while this module owns
 * only the panel's markup and keyboard navigation.
 */
import { useRef } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useDialogKeyboardNav } from './keyboardNav.ts';
import { useConsoleStore } from '../stores/useConsoleStore.ts';
import {
  EXTERNAL_USAGE_UNAVAILABLE,
  activeExternalModel,
  bucketRemainingPercent,
  bucketWindowLabel,
  formatResetCountdown,
  selectActiveGroup,
  sortBucketsForDisplay,
} from '../stores/externalUsageView.ts';
import { refreshExternalUsage, useExternalUsageStore } from '../stores/externalUsage.ts';

export interface ExternalUsagePanelProps {
  onClose: () => void;
  nowSeconds: number;
}

export const ExternalUsagePanel: React.FC<ExternalUsagePanelProps> = ({
  onClose,
  nowSeconds,
}) => {
  const { usage, loading, error } = useExternalUsageStore(
    useShallow((state) => ({
      usage: state.usage,
      loading: state.loading,
      error: state.error,
    })),
  );
  const externalOptions = useConsoleStore((state) => state.externalSession?.options);
  const activeModel = activeExternalModel(externalOptions);
  const activeGroup = usage ? selectActiveGroup(usage.groups, activeModel) : null;

  const panelRef = useRef<HTMLDivElement | null>(null);
  const onKeyDown = useDialogKeyboardNav(panelRef, true, 'button:not([disabled])');

  return (
    <div ref={panelRef} tabIndex={-1} onKeyDown={onKeyDown} className="space-y-2">
      <div className="flex items-center justify-between gap-2 border-b border-line/60 pb-1.5">
        <span className="text-xs font-bold text-gray-900">Antigravity 用量</span>
        <button
          type="button"
          onClick={() => refreshExternalUsage()}
          disabled={loading}
          className="ui-kbd shrink-0 disabled:cursor-not-allowed disabled:text-gray-300"
        >
          刷新
        </button>
      </div>

      {activeModel !== '' && (
        <div className="font-sans text-[10px] text-gray-400">当前模型 {activeModel}</div>
      )}

      {usage === null || usage.groups.length === 0 ? (
        <div className="font-sans text-[11px] text-gray-400">
          {loading ? '正在读取用量…' : EXTERNAL_USAGE_UNAVAILABLE}
        </div>
      ) : (
        <div className="space-y-2">
          {usage.groups.map((group) => {
            const isCurrent = activeGroup !== null && group.name === activeGroup.name;
            const buckets = sortBucketsForDisplay(group.buckets);
            return (
              <div
                key={group.name}
                className={`space-y-1 rounded border px-2 py-1.5 ${
                  isCurrent ? 'border-blue-300/80 bg-blue-50/30' : 'border-line/60'
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate font-sans text-[11px] font-medium text-gray-800">
                    {group.name}
                  </span>
                  {isCurrent && (
                    <span className="shrink-0 rounded bg-blue-100/80 px-1.5 py-0.25 font-sans text-[9px] text-blue-700">
                      当前
                    </span>
                  )}
                </div>
                {buckets.map((bucket) => {
                  const remaining = bucketRemainingPercent(bucket);
                  const win = bucketWindowLabel(bucket);
                  const critical = remaining !== null && remaining < 50;
                  return (
                    <div
                      key={bucket.id}
                      className="flex items-baseline justify-between gap-3 text-[11px]"
                    >
                      <span className="font-sans text-gray-400">
                        {win === '' ? bucket.name : `${win} 窗口`}
                      </span>
                      <span className={critical ? 'text-red-600' : 'text-gray-700'}>
                        {remaining === null ? '--' : `${remaining}% 剩余`}
                        {bucket.reset_at === null
                          ? ''
                          : ` · ${formatResetCountdown(bucket.reset_at, nowSeconds)} 后重置`}
                      </span>
                    </div>
                  );
                })}
              </div>
            );
          })}
          <div className="flex items-baseline justify-between gap-3 text-[11px]">
            <span className="font-sans text-gray-400">捕获时间</span>
            <span className="text-gray-700">
              {new Date(usage.captured_at * 1000).toLocaleTimeString()}
            </span>
          </div>
        </div>
      )}

      {error !== null && <div className="font-sans text-[11px] text-red-500">{error}</div>}

      <div className="flex justify-end">
        <button
          type="button"
          onClick={onClose}
          className="rounded border border-line px-2 py-0.5 text-[11px] text-gray-600"
        >
          关闭
        </button>
      </div>
    </div>
  );
};
