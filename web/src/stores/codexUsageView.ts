/**
 * Pure presentation for the Codex usage / reset-credit surface.
 *
 * The TUI prints one line between the model and MCP chrome
 * (`synapse.ui.chrome.controller` -> `CodexUsageService.label`):
 *
 *     <window> <remaining>%/<reset countdown> · … · resets <n>
 *
 * with the *window* label hard-coded to `5h` for the primary window and `1d` for
 * the secondary one.  The console must not repeat that guess: the RPC carries the
 * window's real `window_minutes`, so a 7-day window is labelled `7d` and an
 * unknown length is left unlabelled instead of being called `1d`.
 *
 * Everything here is a pure function of the decoded view plus a caller-supplied
 * "now" in Unix seconds, so the countdown is a *local* redraw — it never needs a
 * request (and never asks the daemon for a fresh snapshot just to tick).
 *
 * Deliberately free of zustand / React / DOM so it runs under `node --test`.
 */
import type {
  CodexResetCreditView,
  CodexResetCreditsView,
  CodexUsageView,
  CodexUsageWindowView,
} from '../runtime-client/codexUsage.ts';

/** Copy for the states the panel has to name explicitly. */
export const CODEX_USAGE_UNAVAILABLE = 'codex n/a';
export const CODEX_USAGE_LOADING = 'codex …';
export const CODEX_USAGE_EMPTY_CREDITS = '当前没有可兑换的重置额度';
export const CODEX_USAGE_REDEEMED_NOTICE = '已兑换 1 次重置额度，用量与额度已刷新';
export const CODEX_USAGE_REFRESH_FAILED_NOTICE =
  '兑换已成功，但刷新失败；请手动刷新后再确认额度';
export const CODEX_USAGE_UNKNOWN_OUTCOME_NOTICE =
  '结果未知：请求可能已经消耗额度。请刷新核对，控制台不会自动重试';
export const CODEX_USAGE_ALREADY_REDEEMED_NOTICE = '该额度已被兑换过，已刷新额度列表';
export const CODEX_USAGE_NOTHING_TO_RESET_NOTICE = '当前没有需要重置的用量窗口';
export const CODEX_USAGE_NO_CREDIT_NOTICE = '该额度已不存在，已刷新额度列表';
export const CODEX_USAGE_OFFLINE_NOTICE = '与运行时连接已断开，未发送兑换请求';
export const CODEX_USAGE_READ_ERROR = '读取 Codex 用量失败';
export const CODEX_USAGE_CONSUME_ERROR = '兑换请求未完成';
export const CODEX_USAGE_CONFIRM_TEXT =
  '兑换会真实消耗 1 次账户级 Codex 重置额度（不可撤销），是否继续？';

/**
 * A bounded, credential-free error line.
 *
 * The raw error text is deliberately *not* surfaced: an RPC error can echo the
 * request it failed on, and this surface is next to OAuth state.  Only a fixed
 * sentence plus a protocol-shaped `service_code` (when the peer sent one) is
 * reported.
 */
export function codexUsageErrorText(error: unknown, base: string = CODEX_USAGE_READ_ERROR): string {
  if (error !== null && typeof error === 'object') {
    const candidate = error as { name?: unknown; service_code?: unknown };
    if (candidate.name === 'ConnectionLostError' || (error as { unknownOutcome?: unknown }).unknownOutcome === true) {
      return '与运行时连接已断开';
    }
    const code = candidate.service_code;
    if (typeof code === 'string' && /^[a-z0-9_.]{1,40}$/.test(code)) return `${base}（${code}）`;
  }
  return base;
}

/** `300` -> `5h`, `1440` -> `1d`, `10080` -> `7d`, `90` -> `1h30m`, `null` -> `''`. */
export function windowLabel(windowMinutes: number | null): string {
  if (windowMinutes === null || !Number.isFinite(windowMinutes) || windowMinutes <= 0) return '';
  const minutes = Math.trunc(windowMinutes);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.trunc(minutes / 60);
  const restMinutes = minutes % 60;
  const hourPart = `${hours}h`;
  const minutePart = restMinutes === 0 ? '' : `${restMinutes}m`;
  if (hours < 24) return `${hourPart}${minutePart}`;
  const days = Math.trunc(hours / 24);
  const restHours = hours % 24;
  return restHours === 0 ? `${days}d` : `${days}d${restHours}h`;
}

/** Percent of the window still available, rounded; `null` when the peer omitted it. */
export function remainingPercent(window: CodexUsageWindowView | null): number | null {
  if (window === null || window.used_percent === null) return null;
  return Math.round(Math.max(0, Math.min(100, 100 - window.used_percent)));
}

/** The lowest remaining percent across both windows (`null` when unknown). */
export function lowestRemainingPercent(view: CodexUsageView | null): number | null {
  if (view === null) return null;
  const values = [remainingPercent(view.primary), remainingPercent(view.secondary)].filter(
    (value): value is number => value !== null,
  );
  return values.length === 0 ? null : Math.min(...values);
}

/**
 * Countdown to `reset_at`, mirroring the TUI's `format_reset_remaining`
 * (`59s` -> `1m`, `90m` -> `2h`, `25h` -> `2d`): the next unit up is rounded up,
 * so a countdown never reads `0s` while the reset is still in the future.
 */
export function formatResetCountdown(resetAt: number | null, nowSeconds: number): string {
  if (resetAt === null || !Number.isFinite(resetAt)) return '--';
  const seconds = Math.max(0, Math.trunc(resetAt - nowSeconds));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.ceil(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.ceil(hours / 24)}d`;
}

/** One window as the strip prints it: `5h 82%/3h`. */
export function windowLabelText(
  window: CodexUsageWindowView | null,
  nowSeconds: number,
): string | null {
  const remaining = remainingPercent(window);
  if (window === null || remaining === null) return null;
  const label = windowLabel(window.window_minutes);
  const countdown = formatResetCountdown(window.reset_at, nowSeconds);
  return `${label === '' ? '' : `${label} `}${remaining}%/${countdown}`;
}

/**
 * The strip's own line.  `resets <n>` comes from the *loaded* credit rows when
 * they describe the same snapshot the usage view does (they are the fresher
 * number) and from the usage summary otherwise.
 */
export function formatUsageLabel(
  view: CodexUsageView | null,
  nowSeconds: number,
  credits: CodexResetCreditsView | null = null,
): string {
  if (view === null) return CODEX_USAGE_UNAVAILABLE;
  const parts: string[] = [];
  for (const window of [view.primary, view.secondary]) {
    const text = windowLabelText(window, nowSeconds);
    if (text !== null) parts.push(text);
  }
  const resetCount =
    (credits !== null && describesSameSnapshot(view, credits) ? credits.available_count : null) ??
    view.available_reset_count;
  if (resetCount !== null && resetCount > 0) parts.push(`resets ${resetCount}`);
  return parts.length === 0 ? CODEX_USAGE_UNAVAILABLE : parts.join(' · ');
}

/**
 * Whether a credits view may speak for a usage view.
 *
 * The rows are the fresher count only *while they describe the same snapshot*: a
 * list loaded for another model or another session — the entry can outlive the
 * profile it was loaded for, because the daemon may switch the effective model
 * without the console's own context changing — must never override the newer usage
 * summary.  Otherwise a stale count would be painted next to a fresh one forever,
 * since the credit rows are only re-read when the panel is opened or refreshed.
 */
function describesSameSnapshot(view: CodexUsageView, credits: CodexResetCreditsView): boolean {
  return (
    credits.model === view.model &&
    credits.session.project_id === view.session.project_id &&
    credits.session.thread_id === view.session.thread_id
  );
}

/**
 * Whether one credit may be redeemed right now.
 *
 * The peer's `available` status, a non-empty id and an unexpired row are all
 * required: the confirmation dialog must never be raised for a credit the
 * backend would refuse.
 */
export function isCreditRedeemable(credit: CodexResetCreditView, nowSeconds: number): boolean {
  if (credit.id === '') return false;
  if (credit.status.trim().toLowerCase() !== 'available') return false;
  if (credit.expires_at !== null && credit.expires_at <= nowSeconds) return false;
  return true;
}

/** Human label for one credit's status verb. */
export function creditStatusLabel(status: string): string {
  switch (status.trim().toLowerCase()) {
    case 'available':
      return '可兑换';
    case 'redeeming':
      return '兑换中';
    case 'redeemed':
      return '已兑换';
    case 'expired':
      return '已过期';
    default:
      return '不可用';
  }
}

/** One credit's expiry as the panel prints it, or `--` when it never expires. */
export function creditExpiryText(credit: CodexResetCreditView): string {
  if (credit.expires_at === null) return '长期有效';
  return new Date(credit.expires_at * 1000).toLocaleString();
}

/** The credit's title, falling back to its type. */
export function creditTitle(credit: CodexResetCreditView): string {
  if (credit.title !== null && credit.title !== '') return credit.title;
  return credit.reset_type === '' ? '重置额度' : credit.reset_type;
}
