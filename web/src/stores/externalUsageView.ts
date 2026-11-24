/**
 * Pure presentation helpers for the external ACP (e.g. `agy`) usage surface.
 *
 * Mirrors `codexUsageView.ts`: every helper is a pure function of the decoded
 * view plus a caller-supplied Unix-second clock, so the strip and panel share
 * Codex's exact formatting (`5h 70%/4h · 7d 93%/7d`) and tick locally without
 * issuing RPCs.
 *
 * Deliberately free of zustand / React / DOM so it runs under `node --test`.
 */
import type {
  ExternalConfigOptionView,
  ExternalUsageBucketView,
  ExternalUsageGroupView,
  ExternalUsageView,
} from '../runtime-client/contract.generated.ts';
import { formatResetCountdown, windowLabel } from './codexUsageView.ts';

export { formatResetCountdown, windowLabel };

export const EXTERNAL_USAGE_UNAVAILABLE = 'agy n/a';
export const EXTERNAL_USAGE_LOADING = 'agy …';
export const EXTERNAL_USAGE_READ_ERROR = '读取 Antigravity 用量失败';

/** Bounded, credential-free error line matching `codexUsageErrorText`. */
export function externalUsageErrorText(
  error: unknown,
  base: string = EXTERNAL_USAGE_READ_ERROR,
): string {
  if (error !== null && typeof error === 'object') {
    const candidate = error as { name?: unknown; service_code?: unknown };
    if (candidate.name === 'ConnectionLostError') {
      return '与运行时连接已断开';
    }
    const code = candidate.service_code;
    if (typeof code === 'string' && /^[a-z0-9_.]{1,40}$/.test(code)) return `${base}（${code}）`;
  }
  return base;
}

/** Extract the active model's human label (or value) from peer config options. */
export function activeExternalModel(
  options: readonly ExternalConfigOptionView[] | null | undefined,
): string {
  if (!options || options.length === 0) return '';
  const modelOpt =
    options.find((opt) => opt.category === 'model') ??
    options.find((opt) => opt.id.toLowerCase() === 'model');
  if (!modelOpt) return '';
  const matched = modelOpt.values.find((entry) => entry.value === modelOpt.current_value);
  return matched?.label || modelOpt.current_value || '';
}

/** Rounded remaining percent (`0..100`), or `null` when unknown. */
export function bucketRemainingPercent(bucket: ExternalUsageBucketView | null): number | null {
  if (bucket === null || bucket.remaining_percent === null) return null;
  return Math.round(Math.max(0, Math.min(100, bucket.remaining_percent)));
}

/** Short label for one bucket window (`5h`, `7d`, fallback to raw window). */
export function bucketWindowLabel(bucket: ExternalUsageBucketView): string {
  const fromMinutes = windowLabel(bucket.window_minutes);
  if (fromMinutes !== '') return fromMinutes;
  return bucket.window.trim();
}

/** Sort buckets shortest window first (`5h` before `7d`/`weekly`), preserving ties. */
export function sortBucketsForDisplay(
  buckets: readonly ExternalUsageBucketView[],
): ExternalUsageBucketView[] {
  return [...buckets].sort((left, right) => {
    const a = left.window_minutes ?? Number.MAX_SAFE_INTEGER;
    const b = right.window_minutes ?? Number.MAX_SAFE_INTEGER;
    return a - b;
  });
}

/**
 * Pick the quota group that corresponds to the session's active model.
 *
 * `agy` currently reports two groups: `"Gemini Models"` and
 * `"Claude and GPT models"`.  When the active model mentions Claude/GPT we
 * select the third-party group; when it mentions Gemini/Orchestrator we select
 * the Gemini group; otherwise we fall back to the first group.
 */
export function selectActiveGroup(
  groups: readonly ExternalUsageGroupView[],
  activeModel: string,
): ExternalUsageGroupView | null {
  if (groups.length === 0) return null;
  const needle = activeModel.trim().toLowerCase();
  if (needle !== '') {
    const isThirdParty = needle.includes('claude') || needle.includes('gpt');
    const isGemini = needle.includes('gemini') || needle.includes('orchestrator');
    if (isThirdParty) {
      const found = groups.find((group) => {
        const name = group.name.toLowerCase();
        return name.includes('claude') || name.includes('gpt');
      });
      if (found) return found;
    }
    if (isGemini) {
      const found = groups.find((group) => group.name.toLowerCase().includes('gemini'));
      if (found) return found;
    }
    const byDesc = groups.find((group) =>
      (group.description ?? '').toLowerCase().includes(needle),
    );
    if (byDesc) return byDesc;
  }
  return groups[0] ?? null;
}

/** One bucket as the strip prints it: `5h 70%/4h`. */
export function bucketLabelText(
  bucket: ExternalUsageBucketView,
  nowSeconds: number,
): string | null {
  const remaining = bucketRemainingPercent(bucket);
  if (remaining === null) return null;
  const label = bucketWindowLabel(bucket);
  const countdown = formatResetCountdown(bucket.reset_at, nowSeconds);
  return `${label === '' ? '' : `${label} `}${remaining}%/${countdown}`;
}

/** The bottom bar's single-line summary for the active model's quota group. */
export function formatExternalUsageLabel(
  view: ExternalUsageView | null,
  activeModel: string,
  nowSeconds: number,
): string {
  if (view === null || !view.supported) return EXTERNAL_USAGE_UNAVAILABLE;
  const group = selectActiveGroup(view.groups, activeModel);
  if (group === null) return EXTERNAL_USAGE_UNAVAILABLE;
  const parts: string[] = [];
  for (const bucket of sortBucketsForDisplay(group.buckets)) {
    const text = bucketLabelText(bucket, nowSeconds);
    if (text !== null) parts.push(text);
  }
  return parts.length === 0 ? EXTERNAL_USAGE_UNAVAILABLE : parts.join(' · ');
}

/** Lowest remaining percent across the active group's buckets (`null` when unknown). */
export function lowestExternalRemainingPercent(
  view: ExternalUsageView | null,
  activeModel: string,
): number | null {
  if (view === null || !view.supported) return null;
  const group = selectActiveGroup(view.groups, activeModel);
  if (group === null) return null;
  const values = group.buckets
    .map((bucket) => bucketRemainingPercent(bucket))
    .filter((value): value is number => value !== null);
  return values.length === 0 ? null : Math.min(...values);
}
