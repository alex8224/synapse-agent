/**
 * Pure formatting for the `usage_updated` runtime event.
 *
 * The runtime reports token/rate metrics on every usage update; the console
 * renders them as one compact TopBar label while keeping the raw numbers in the
 * store.  Deliberately dependency-free so it is exercised directly by the Node
 * built-in test runner (`node --test`), like `historyMapper` / `recoveryDecider`.
 */

export interface UsageView {
  turnInput: number;
  turnOutput: number;
  turnCache: number;
  lastInput: number;
  lastOutput: number;
  lastCache: number;
  outputTokensPerSecond: number | null;
  ttftS: number | null;
  rateBasis: string;
  rateEstimated: boolean;
  contextSize: number | null;
  modelCalls: number;
}

export const EMPTY_USAGE: UsageView = {
  turnInput: 0,
  turnOutput: 0,
  turnCache: 0,
  lastInput: 0,
  lastOutput: 0,
  lastCache: 0,
  outputTokensPerSecond: null,
  ttftS: null,
  rateBasis: 'end_to_end',
  rateEstimated: false,
  contextSize: null,
  modelCalls: 0,
};

function int(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.trunc(value) : 0;
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** Normalize one `UsagePayload` JSON object into the console view model. */
export function parseUsagePayload(payload: Record<string, unknown>): UsageView {
  const context = num(payload.context_size);
  return {
    turnInput: int(payload.turn_input),
    turnOutput: int(payload.turn_output),
    turnCache: int(payload.turn_cache),
    lastInput: int(payload.last_input),
    lastOutput: int(payload.last_output),
    lastCache: int(payload.last_cache),
    outputTokensPerSecond: num(payload.output_tokens_per_second),
    ttftS: num(payload.ttft_s),
    rateBasis: typeof payload.rate_basis === 'string' ? payload.rate_basis : 'end_to_end',
    rateEstimated: payload.rate_estimated === true,
    contextSize: context === null ? null : Math.trunc(context),
    modelCalls: int(payload.model_calls),
  };
}

/** `1234` -> `1.2k`, `1234567` -> `1.2M` (token counts are always >= 0). */
export function compactCount(value: number): string {
  const n = Math.max(0, Math.trunc(value));
  if (n < 1000) return String(n);
  if (n < 1000000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1000000).toFixed(1)}M`;
}

/**
 * Session-cumulative token totals.
 *
 * This is the `usage` object `runtime.session.open` returns on the session view
 * (the runtime accumulates it across turns), *not* the last turn's numbers.
 */
export interface SessionUsage {
  input: number;
  output: number;
  cache: number;
}

/**
 * Parse the session view's `usage` object, or `null` when nothing was used.
 *
 * A session that has not run a turn yet reports all zeros; that is "no usage
 * yet", so the bar keeps its placeholder instead of printing `0/0/0`.
 */
export function parseSessionUsage(raw: unknown): SessionUsage | null {
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const record = raw as Record<string, unknown>;
  const input = int(record['input_tokens']);
  const output = int(record['output_tokens']);
  const cache = int(record['cache_tokens']);
  if (input === 0 && output === 0 && cache === 0) return null;
  return { input, output, cache };
}

/**
 * The two usage groups of the status bar, as raw numbers and nothing else:
 *
 * 1. the session's token totals, in the TUI's `input/cache/output` order;
 * 2. the current context occupancy, followed by its share of the model window.
 *
 * No label and no tooltip: the bar prints what the runtime reported rather than
 * naming it.  `runtime.session.open` already carries the totals, so this
 * requests nothing extra.
 */
export function sessionUsageSegments(
  session: SessionUsage | null,
  contextSize: number | null = null,
  contextWindow: number | null = null,
): UsageSegment[] {
  const segments: UsageSegment[] = [];
  if (session !== null) {
    // input/cache/output, then the hit share those two imply.  The share is left
    // out (never printed as a placeholder) when there is no input to divide by.
    const totals = [
      compactCount(session.input),
      compactCount(session.cache),
      compactCount(session.output),
      cacheHitRate(session),
    ].filter((part): part is string => part !== null);
    segments.push({
      key: 'session',
      label: '',
      value: totals.join('/'),
      emphasis: false,
    });
  }
  const context = contextSize !== null && contextSize > 0 ? contextSize : null;
  // How much of the model's window the context occupies.  Without a window the
  // share is simply unknown, so only the count is printed.
  const share =
    context !== null && contextWindow !== null && contextWindow > 0
      ? `${Math.round((context / contextWindow) * 100)}%`
      : null;
  const contextPart = [context === null ? '' : compactCount(context), share ?? ''].filter(
    (part) => part !== '',
  );
  if (contextPart.length > 0) {
    segments.push({
      key: 'context',
      label: '',
      value: contextPart.join('/'),
      emphasis: true,
    });
  }
  return segments;
}

/**
 * Share of the session's input tokens that were served from cache.
 *
 * `null` when the session has no input to divide by, so callers can omit it
 * instead of printing a placeholder.  The status bar appends it to the token
 * totals it derives from; the settings panel renders it as its own labelled row.
 */
export function cacheHitRate(session: SessionUsage | null): string | null {
  if (session === null || session.input <= 0) return null;
  return `${((session.cache / session.input) * 100).toFixed(1)}%`;
}

/**
 * Tokens currently occupying the context, or `null` when nothing was sent yet.
 *
 * Prefers the explicit `context_size` metric — which the runtime does not send
 * today — and otherwise falls back to the last model call's input, the same
 * quantity the TUI uses for its occupancy label: a call's prompt *is* the context.
 */
export function contextOccupancy(usage: UsageView | null): number | null {
  if (usage === null) return null;
  if (usage.contextSize !== null && usage.contextSize > 0) return usage.contextSize;
  return usage.lastInput > 0 ? usage.lastInput : null;
}

/** Full-precision session totals for the settings panel. */
export function formatSessionUsage(session: SessionUsage | null): string {
  if (session === null) return '-';
  return `in ${fullCount(session.input)} · cache ${fullCount(session.cache)} · out ${fullCount(session.output)}`;
}

/**
 * Compact metrics label for the TopBar, e.g.
 * `up 12.3k down 4.5k - ctx 45.0k - 32.1 tok/s - 3 steps`.
 *
 * Returns `''` when there is nothing real to show, so the header renders no
 * placeholder (same rule as the pre-existing `metricsLabel !== ''` guard).
 */
export function formatUsageMetrics(usage: UsageView | null): string {
  if (usage === null) return '';
  const parts: string[] = [];
  if (usage.turnInput > 0 || usage.turnOutput > 0) {
    parts.push(`up ${compactCount(usage.turnInput)} down ${compactCount(usage.turnOutput)}`);
  }
  if (usage.turnCache > 0) {
    parts.push(`cache ${compactCount(usage.turnCache)}`);
  }
  if (usage.contextSize !== null && usage.contextSize > 0) {
    parts.push(`ctx ${compactCount(usage.contextSize)}`);
  }
  if (usage.outputTokensPerSecond !== null && usage.outputTokensPerSecond > 0) {
    parts.push(`${usage.outputTokensPerSecond.toFixed(1)} tok/s${usage.rateEstimated ? '~' : ''}`);
  }
  if (usage.modelCalls > 0) {
    parts.push(`${usage.modelCalls} steps`);
  }
  return parts.join(' - ');
}

/** One compact metric in the TopBar usage bar. */
export interface UsageSegment {
  key: 'tokens' | 'context' | 'rate' | 'ttft' | 'steps' | 'session';
  /** Short Chinese label rendered before the value (empty when the unit speaks). */
  label: string;
  /** Compact value for the bar. */
  value: string;
  /** True for the metric the header emphasizes (context occupancy). */
  emphasis: boolean;
}

/** `20612` -> `20,612`; deterministic grouping (no locale/ICU dependency). */
export function fullCount(value: number): string {
  return Math.max(0, Math.trunc(value))
    .toString()
    .replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

/**
 * Structured segments for the TopBar usage bar.
 *
 * Replaces the single `up X down Y - ...` log line: each metric becomes its own
 * segment so the header can group, separate and emphasize them, and so the
 * compact bar can omit the numbers that only matter on hover.
 */
export function usageSegments(usage: UsageView | null): UsageSegment[] {
  if (usage === null) return [];
  const contextSize = usage.contextSize !== null && usage.contextSize > 0 ? usage.contextSize : null;
  const hasContext = contextSize !== null;
  const segments: UsageSegment[] = [];

  if (usage.turnInput > 0 || usage.turnOutput > 0) {
    segments.push({
      key: 'tokens',
      label: '',
      value: `↑${compactCount(usage.turnInput)} ↓${compactCount(usage.turnOutput)}`,
      // The runtime does not currently report `context_size` at all, so the
      // token pair is the context-occupancy proxy and carries the emphasis
      // whenever no explicit context metric is available.
      emphasis: !hasContext,
    });
  }

  if (contextSize !== null) {
    segments.push({
      key: 'context',
      label: '上下文',
      value: compactCount(contextSize),
      emphasis: true,
    });
  }

  return segments;
}

/**
 * This-turn latency/throughput segments for the status bar centre.
 *
 * Split from `usageSegments` on purpose: the header reports the *cumulative*
 * token/context picture, the footer reports the *current turn's* telemetry
 * (matching the TUI, whose bottom bar owns `turn_stats`).  Showing the same
 * `tok/s` in both places would just be noise.
 */
export function turnStatSegments(usage: UsageView | null): UsageSegment[] {
  if (usage === null) return [];
  const segments: UsageSegment[] = [];

  if (usage.outputTokensPerSecond !== null && usage.outputTokensPerSecond > 0) {
    segments.push({
      key: 'rate',
      label: '',
      value: `${usage.outputTokensPerSecond.toFixed(1)} tok/s${usage.rateEstimated ? '~' : ''}`,
      emphasis: true,
    });
  }

  if (usage.modelCalls > 0) {
    segments.push({
      key: 'steps',
      label: '',
      value: `${usage.modelCalls} 步`,
      emphasis: false,
    });
  }

  if (usage.ttftS !== null) {
    segments.push({
      key: 'ttft',
      label: '首字',
      value: `${usage.ttftS.toFixed(2)}s`,
      emphasis: false,
    });
  }

  return segments;
}

/**
 * The segments a *narrow* strip prints, capped at `limit`.
 *
 * The phone band cannot afford the full telemetry row, so the strip prints the
 * numbers worth a glance: the emphasized ones first (this turn's rate, the
 * context occupancy), then the rest in their original order.  Nothing is lost —
 * the strip entry's own overlay prints the complete list, which is what keeps
 * "simplified" from meaning "hidden".
 */
export function compactSegments(segments: UsageSegment[], limit = 2): UsageSegment[] {
  if (segments.length <= limit) return segments;
  const emphasized = segments.filter((segment) => segment.emphasis);
  const rest = segments.filter((segment) => !segment.emphasis);
  return [...emphasized, ...rest].slice(0, limit);
}

/**
 * Full-precision hover breakdown for the usage bar.
 *
 * Carries every number the compact bar compresses, including the cache share
 * the bar deliberately omits.  Newline-separated: it is used as a native
 * `title`, which renders the line breaks.
 */
export function usageTooltip(usage: UsageView | null, session: SessionUsage | null = null): string {
  const lines: string[] = [];
  if (session !== null) {
    lines.push(
      `本会话累计 in ${fullCount(session.input)} / cache ${fullCount(session.cache)} / out ${fullCount(session.output)}`,
    );
  }
  if (usage === null) return lines.join('\n');
  lines.push(`本轮 输入 ${fullCount(usage.turnInput)} · 输出 ${fullCount(usage.turnOutput)}`);
  if (usage.turnCache > 0) {
    const share =
      usage.turnInput > 0 ? `（占输入 ${((usage.turnCache / usage.turnInput) * 100).toFixed(1)}%）` : '';
    lines.push(`缓存 ${fullCount(usage.turnCache)}${share}`);
  }
  if (usage.contextSize !== null && usage.contextSize > 0) {
    lines.push(`上下文 ${fullCount(usage.contextSize)}`);
  }
  if (usage.outputTokensPerSecond !== null && usage.outputTokensPerSecond > 0) {
    lines.push(
      `速率 ${usage.outputTokensPerSecond.toFixed(1)} tok/s${usage.rateEstimated ? '（估算）' : ''}`,
    );
  }
  if (usage.ttftS !== null) {
    lines.push(`首字 ${usage.ttftS.toFixed(2)}s`);
  }
  if (usage.lastInput > 0 || usage.lastOutput > 0) {
    lines.push(`上次调用 in ${fullCount(usage.lastInput)} / out ${fullCount(usage.lastOutput)}`);
  }
  if (usage.modelCalls > 0) {
    lines.push(`${usage.modelCalls} 步`);
  }
  return lines.join('\n');
}
