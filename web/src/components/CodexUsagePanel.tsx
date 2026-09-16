/**
 * The Codex usage panel: the windows, the reset-credit rows, the confirmation and
 * the outcome notice.
 *
 * It is the *content* of the strip's Codex entry: the box, the `dialog` role and
 * the portal belong to that entry's `FloatingPanel` (`bottomBar/codexUsageItem.tsx`),
 * so this file owns only what the panel paints and keeps its own keyboard
 * navigation.
 *
 * Two rules the markup has to make obvious:
 *
 *  - the redeem control never sends anything.  It raises the confirmation, which
 *    names the real cost ("真实消耗 1 次账户级 Codex 重置额度"), and only that
 *    confirmation's own button calls the one write;
 *  - a credit that is not `available`, has no id, has expired, or whose list is
 *    being refreshed is never offered: the button is disabled, so a double click
 *    and a slow network cannot turn one confirmation into two writes.
 */
import { useEffect, useRef } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useDialogKeyboardNav } from './keyboardNav.ts';
import {
  CODEX_USAGE_CONFIRM_TEXT,
  CODEX_USAGE_EMPTY_CREDITS,
  CODEX_USAGE_UNAVAILABLE,
  creditExpiryText,
  creditStatusLabel,
  creditTitle,
  formatResetCountdown,
  isCreditRedeemable,
  remainingPercent,
  windowLabel,
} from '../stores/codexUsageView.ts';
import {
  cancelResetCredit,
  closeCodexUsagePanel,
  confirmResetCredit,
  openCodexUsagePanel,
  refreshCodexUsage,
  requestResetCredit,
  useCodexUsageStore,
} from '../stores/codexUsage.ts';
import type { CodexResetCreditView } from '../runtime-client/codexUsage.ts';

/** One credit row: its own redeem control, and no request until it is confirmed. */
const CreditRow: React.FC<{
  credit: CodexResetCreditView;
  nowSeconds: number;
  /** Something is already happening: the rows are loading or a write is out. */
  busy: boolean;
  pending: boolean;
  /** A write for this credit is unresolved, so it is not offered again. */
  blocked: boolean;
}> = ({ credit, nowSeconds, busy, pending, blocked }) => {
  const redeemable = isCreditRedeemable(credit, nowSeconds);
  return (
    <div
      data-credit={credit.id}
      className="flex items-start justify-between gap-3 rounded border border-line/60 px-2 py-1.5"
    >
      <div className="min-w-0">
        <div className="truncate text-[11px] text-gray-800">{creditTitle(credit)}</div>
        <div className="font-sans text-[10px] text-gray-400">
          {creditStatusLabel(credit.status)} · {creditExpiryText(credit)}
        </div>
        {credit.description !== null && (
          <div className="font-sans text-[10px] text-gray-400">{credit.description}</div>
        )}
      </div>
      <button
        type="button"
        disabled={!redeemable || busy || pending || blocked}
        onClick={() => requestResetCredit(credit.id)}
        className="shrink-0 rounded border border-line px-2 py-0.5 text-[11px] text-gray-700 disabled:cursor-not-allowed disabled:text-gray-300"
      >
        {pending ? '待确认' : '兑换'}
      </button>
    </div>
  );
};

export interface CodexUsagePanelProps {
  /** The entry's own close action (the popover host's dismissal path). */
  onClose: () => void;
  /** The entry's local clock, so the countdown ticks without a request. */
  nowSeconds: number;
}

export const CodexUsagePanel: React.FC<CodexUsagePanelProps> = ({ onClose, nowSeconds }) => {
  const {
    usage,
    credits,
    usageLoading,
    creditsLoading,
    error,
    creditsError,
    notice,
    pending,
    consuming,
    model,
    unresolved,
  } = useCodexUsageStore(
    // Only the fields this panel paints: a reasoning delta must not re-render it.
    useShallow((state) => ({
      usage: state.usage,
      credits: state.credits,
      usageLoading: state.usageLoading,
      creditsLoading: state.creditsLoading,
      error: state.error,
      creditsError: state.creditsError,
      notice: state.notice,
      pending: state.pending,
      consuming: state.consuming,
      model: state.model,
      unresolved: state.unresolved,
    })),
  );
  const panelRef = useRef<HTMLDivElement | null>(null);
  // The trigger keeps the focus, so the panel has to take it: the arrows walk the
  // panel's own controls (the popover host owns Escape and the outside click).
  const onKeyDown = useDialogKeyboardNav(panelRef, true, 'button:not([disabled])');

  useEffect(() => {
    // Opening the panel is what asks for the credit rows; closing it drops a
    // half-raised confirmation instead of carrying it to the next open.
    openCodexUsagePanel();
    return () => closeCodexUsagePanel();
  }, []);

  return (
    <div ref={panelRef} tabIndex={-1} onKeyDown={onKeyDown} className="space-y-2">
      <div className="flex items-center justify-between gap-2 border-b border-line/60 pb-1.5">
        <span className="text-xs font-bold text-gray-900">Codex 用量与重置额度</span>
        <button
          type="button"
          onClick={() => refreshCodexUsage()}
          disabled={usageLoading || creditsLoading || consuming}
          className="ui-kbd shrink-0 disabled:cursor-not-allowed disabled:text-gray-300"
        >
          刷新
        </button>
      </div>

      {model !== '' && <div className="font-sans text-[10px] text-gray-400">模型 {model}</div>}

      {usage === null ? (
        <div className="font-sans text-[11px] text-gray-400">
          {usageLoading ? '正在读取用量…' : CODEX_USAGE_UNAVAILABLE}
        </div>
      ) : (
        <div className="space-y-1">
          {[usage.primary, usage.secondary].map((window, index) => {
            if (window === null) return null;
            const remaining = remainingPercent(window);
            const label = windowLabel(window.window_minutes);
            return (
              <div
                key={index === 0 ? 'primary' : 'secondary'}
                className="flex items-baseline justify-between gap-3 text-[11px]"
              >
                <span className="font-sans text-gray-400">
                  {label === '' ? (index === 0 ? '主窗口' : '次窗口') : `${label} 窗口`}
                </span>
                <span className="text-gray-700">
                  {remaining === null ? '--' : `${remaining}% 剩余`}
                  {window.reset_at === null
                    ? ''
                    : ` · ${formatResetCountdown(window.reset_at, nowSeconds)} 后重置`}
                </span>
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

      <div className="border-t border-line/60 pt-1.5">
        <div className="mb-1 flex items-baseline justify-between gap-2">
          <span className="font-sans text-[10px] text-gray-400">重置额度</span>
          <span className="text-[11px] text-gray-700">
            {credits === null
              ? creditsLoading
                ? '读取中…'
                : '--'
              : `${credits.available_count} 次可用`}
          </span>
        </div>
        {credits === null || credits.credits.length === 0 ? (
          <div className="font-sans text-[11px] text-gray-400">
            {creditsLoading ? '正在读取重置额度…' : credits === null
              ? '尚未获取重置额度' : CODEX_USAGE_EMPTY_CREDITS}
          </div>
        ) : (
          <div className="space-y-1">
            {credits.credits.map((credit) => (
              <CreditRow
                key={credit.id}
                credit={credit}
                nowSeconds={nowSeconds}
                // A credits read in flight is *not* an invitation to confirm: the
                // rows under the button are about to be replaced.
                busy={consuming || creditsLoading || unresolved !== null}
                pending={pending?.creditId === credit.id}
                blocked={unresolved?.creditId === credit.id}
              />
            ))}
          </div>
        )}
        {creditsError !== null && (
          <div className="font-sans text-[11px] text-red-500">{creditsError}</div>
        )}
      </div>

      {pending !== null && (
        <div
          role="alertdialog"
          aria-label="确认兑换重置额度"
          className="space-y-1.5 rounded border border-amber-300 bg-amber-50/70 px-2 py-1.5"
        >
          <div className="font-sans text-[11px] text-amber-800">{CODEX_USAGE_CONFIRM_TEXT}</div>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => cancelResetCredit()}
              className="rounded border border-line px-2 py-0.5 text-[11px] text-gray-600"
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => confirmResetCredit()}
              disabled={consuming || creditsLoading}
              className="rounded border border-amber-400 bg-amber-100 px-2 py-0.5 text-[11px] text-amber-900 disabled:cursor-not-allowed disabled:text-gray-300"
            >
              确认兑换
            </button>
          </div>
        </div>
      )}

      {consuming && <div className="font-sans text-[11px] text-gray-500">正在兑换，请勿重复点击…</div>}

      {unresolved !== null && !consuming && (
        <div className="font-sans text-[11px] text-amber-700">
          该额度有一次结果未知的兑换，核对前不会再次发起（刷新额度列表以核对）
        </div>
      )}

      {notice !== null && <div className="font-sans text-[11px] text-gray-600">{notice}</div>}

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
