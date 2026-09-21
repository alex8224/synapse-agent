/**
 * Read-only usage statistics client for the Web Console.
 *
 * The host exposes `GET /api/usage-stats`; this module is the *only* place a
 * response becomes a typed view object. It deliberately does **no** fallback:
 * a non-2xx response, a network failure, or a malformed payload all reject, so
 * the panel can show an explicit error instead of painting demo data. Empty
 * ranges are a real, valid payload with zero totals.
 *
 * Every field the panel reads is validated here (see `parseUsageStatsPayload`),
 * so a truncated or drifting payload fails loudly rather than rendering
 * `undefined`.
 */

export type UsageRangeKey = 'today' | '7d' | '30d' | 'all' | 'custom';

export interface UsageRangeInfo {
  key: string;
  start: string | null;
  end: string | null;
}

export interface UsageProjectCurrent {
  name: string;
  path: string;
  branch: string | null;
  dirty: boolean;
}

export interface UsageProjectInfo {
  selected: string;
  connected_count: number;
  current: UsageProjectCurrent;
}

export interface UsageKpi {
  total_tokens: number;
  provider_input_tokens: number;
  net_input_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  output_tokens: number;
  cache_hit_rate: number | null;
  saved_tokens: number;
  saved_pct: number | null;
  call_count: number;
  turn_count: number;
  active_duration_ms: number;
  /** No pricing source exists: always null. */
  estimated_cost: null;
  /** No changed-lines source exists: always null. */
  loc_added: null;
  loc_removed: null;
}

export interface ProjectMatrixItem {
  name: string;
  is_current: boolean;
  path: string;
  branch: string | null;
  dirty: boolean;
  tokens: number;
  share_pct: number;
  cost: null;
  sessions_count: number;
  turns_count: number;
  lines_added: null;
  lines_removed: null;
  efficiency: null;
}

export interface HeatmapDay {
  date: string;
  tokens: number;
  sessions: number;
}

/** Continuous, weekday-aligned activity series (see `truncated`). */
export interface UsageHeatmap {
  days: HeatmapDay[];
  /**
   * `true` when the heatmap was capped to the last N days of the window (a very
   * wide range). The aggregate KPIs still cover the whole window.
   */
  truncated: boolean;
}

export interface TrendItem {
  date: string;
  cache: number;
  input: number;
  output: number;
  raw: number;
}

export interface BreakdownItem {
  name: string;
  tokens: number;
  pct: number;
  color: string;
  offset: number;
  dash: number;
}

export interface TopToolItem {
  name: string;
  count: number;
  success_count: number;
  failure_count: number;
  success_rate: number | null;
  /** No tool-execution duration source exists: always null. */
  avg_ms: null;
}

export interface TopToolsInfo {
  supported: boolean;
  partial: boolean;
  source: string;
  scope_note: string;
  recorded_total: number;
  items: TopToolItem[];
}

export interface TopSessionItem {
  thread_id: string;
  title: string;
  model: string | null;
  turns: number;
  tokens: number;
  cache_rate: number | null;
}

export interface UsageStatsPayload {
  generated_at: string;
  range: UsageRangeInfo;
  project: UsageProjectInfo;
  available_models: string[];
  selected_model: string;
  kpi: UsageKpi;
  project_matrix: ProjectMatrixItem[];
  heatmap: UsageHeatmap;
  trend: {
    range_key: string;
    granularity: 'hour' | 'day';
    title: string;
    subtitle: string;
    items: TrendItem[];
  };
  breakdowns: {
    project: BreakdownItem[];
    model: BreakdownItem[];
    agent: BreakdownItem[] | null;
  };
  top_tools: TopToolsInfo;
  top_sessions: TopSessionItem[];
  notes: string[];
}

export class UsageStatsPayloadError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'UsageStatsPayloadError';
  }
}

export class UsageStatsRequestError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'UsageStatsRequestError';
    this.status = status;
  }
}

export interface UsageStatsQuery {
  project?: string;
  range?: UsageRangeKey;
  start?: string;
  end?: string;
  model?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function record(value: unknown, path: string): Record<string, unknown> {
  if (!isRecord(value)) throw new UsageStatsPayloadError(`${path} must be an object`);
  return value;
}

function num(value: unknown, path: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new UsageStatsPayloadError(`${path} must be a finite number`);
  }
  return value;
}

function nullableNum(value: unknown, path: string): number | null {
  if (value === null) return null;
  return num(value, path);
}

function str(value: unknown, path: string): string {
  if (typeof value !== 'string') throw new UsageStatsPayloadError(`${path} must be a string`);
  return value;
}

function nullableStr(value: unknown, path: string): string | null {
  if (value === null) return null;
  return str(value, path);
}

function bool(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') throw new UsageStatsPayloadError(`${path} must be a boolean`);
  return value;
}

function literalNull(value: unknown, path: string): null {
  if (value !== null) throw new UsageStatsPayloadError(`${path} must be null`);
  return null;
}

function list<T>(value: unknown, path: string, item: (raw: unknown, at: string) => T): T[] {
  if (!Array.isArray(value)) throw new UsageStatsPayloadError(`${path} must be an array`);
  return value.map((raw, index) => item(raw, `${path}[${index}]`));
}

function parseBreakdown(raw: unknown, path: string): BreakdownItem {
  const value = record(raw, path);
  return {
    name: str(value.name, `${path}.name`),
    tokens: num(value.tokens, `${path}.tokens`),
    pct: num(value.pct, `${path}.pct`),
    color: str(value.color, `${path}.color`),
    offset: num(value.offset, `${path}.offset`),
    dash: num(value.dash, `${path}.dash`),
  };
}

/**
 * Validate a raw `GET /api/usage-stats` body field by field.
 *
 * Throws `UsageStatsPayloadError` on the first structural or type problem so a
 * drifting wire shape is caught at the boundary instead of reaching the panel.
 */
export function parseUsageStatsPayload(raw: unknown): UsageStatsPayload {
  const value = record(raw, 'payload');

  const range = record(value.range, 'range');
  const project = record(value.project, 'project');
  const current = record(project.current, 'project.current');
  const kpi = record(value.kpi, 'kpi');
  const heatmap = record(value.heatmap, 'heatmap');
  const trend = record(value.trend, 'trend');
  const breakdowns = record(value.breakdowns, 'breakdowns');
  const topTools = record(value.top_tools, 'top_tools');

  const granularity = str(trend.granularity, 'trend.granularity');
  if (granularity !== 'hour' && granularity !== 'day') {
    throw new UsageStatsPayloadError('trend.granularity must be "hour" or "day"');
  }

  return {
    generated_at: str(value.generated_at, 'generated_at'),
    available_models: Array.isArray(value.available_models)
      ? list(value.available_models, 'available_models', str)
      : [],
    selected_model: typeof value.selected_model === 'string' ? value.selected_model : 'all',
    range: {
      key: str(range.key, 'range.key'),
      start: nullableStr(range.start, 'range.start'),
      end: nullableStr(range.end, 'range.end'),
    },
    project: {
      selected: str(project.selected, 'project.selected'),
      connected_count: num(project.connected_count, 'project.connected_count'),
      current: {
        name: str(current.name, 'project.current.name'),
        path: str(current.path, 'project.current.path'),
        branch: nullableStr(current.branch, 'project.current.branch'),
        dirty: bool(current.dirty, 'project.current.dirty'),
      },
    },
    kpi: {
      total_tokens: num(kpi.total_tokens, 'kpi.total_tokens'),
      provider_input_tokens: num(kpi.provider_input_tokens, 'kpi.provider_input_tokens'),
      net_input_tokens: num(kpi.net_input_tokens, 'kpi.net_input_tokens'),
      cache_read_tokens: num(kpi.cache_read_tokens, 'kpi.cache_read_tokens'),
      cache_write_tokens: num(kpi.cache_write_tokens, 'kpi.cache_write_tokens'),
      output_tokens: num(kpi.output_tokens, 'kpi.output_tokens'),
      cache_hit_rate: nullableNum(kpi.cache_hit_rate, 'kpi.cache_hit_rate'),
      saved_tokens: num(kpi.saved_tokens, 'kpi.saved_tokens'),
      saved_pct: nullableNum(kpi.saved_pct, 'kpi.saved_pct'),
      call_count: num(kpi.call_count, 'kpi.call_count'),
      turn_count: num(kpi.turn_count, 'kpi.turn_count'),
      active_duration_ms: num(kpi.active_duration_ms, 'kpi.active_duration_ms'),
      estimated_cost: literalNull(kpi.estimated_cost, 'kpi.estimated_cost'),
      loc_added: literalNull(kpi.loc_added, 'kpi.loc_added'),
      loc_removed: literalNull(kpi.loc_removed, 'kpi.loc_removed'),
    },
    project_matrix: list(value.project_matrix, 'project_matrix', (raw, path) => {
      const item = record(raw, path);
      return {
        name: str(item.name, `${path}.name`),
        is_current: bool(item.is_current, `${path}.is_current`),
        path: str(item.path, `${path}.path`),
        branch: nullableStr(item.branch, `${path}.branch`),
        dirty: bool(item.dirty, `${path}.dirty`),
        tokens: num(item.tokens, `${path}.tokens`),
        share_pct: num(item.share_pct, `${path}.share_pct`),
        cost: literalNull(item.cost, `${path}.cost`),
        sessions_count: num(item.sessions_count, `${path}.sessions_count`),
        turns_count: num(item.turns_count, `${path}.turns_count`),
        lines_added: literalNull(item.lines_added, `${path}.lines_added`),
        lines_removed: literalNull(item.lines_removed, `${path}.lines_removed`),
        efficiency: literalNull(item.efficiency, `${path}.efficiency`),
      };
    }),
    heatmap: {
      truncated: bool(heatmap.truncated, 'heatmap.truncated'),
      days: list(heatmap.days, 'heatmap.days', (raw, path) => {
        const day = record(raw, path);
        return {
          date: str(day.date, `${path}.date`),
          tokens: num(day.tokens, `${path}.tokens`),
          sessions: num(day.sessions, `${path}.sessions`),
        };
      }),
    },
    trend: {
      range_key: str(trend.range_key, 'trend.range_key'),
      granularity,
      title: str(trend.title, 'trend.title'),
      subtitle: str(trend.subtitle, 'trend.subtitle'),
      items: list(trend.items, 'trend.items', (raw, path) => {
        const item = record(raw, path);
        return {
          date: str(item.date, `${path}.date`),
          cache: num(item.cache, `${path}.cache`),
          input: num(item.input, `${path}.input`),
          output: num(item.output, `${path}.output`),
          raw: num(item.raw, `${path}.raw`),
        };
      }),
    },
    breakdowns: {
      project: list(breakdowns.project, 'breakdowns.project', parseBreakdown),
      model: list(breakdowns.model, 'breakdowns.model', parseBreakdown),
      agent:
        breakdowns.agent === null
          ? null
          : list(breakdowns.agent, 'breakdowns.agent', parseBreakdown),
    },
    top_tools: {
      supported: bool(topTools.supported, 'top_tools.supported'),
      partial: bool(topTools.partial, 'top_tools.partial'),
      source: str(topTools.source, 'top_tools.source'),
      scope_note: str(topTools.scope_note, 'top_tools.scope_note'),
      recorded_total: num(topTools.recorded_total, 'top_tools.recorded_total'),
      items: list(topTools.items, 'top_tools.items', (raw, path) => {
        const item = record(raw, path);
        return {
          name: str(item.name, `${path}.name`),
          count: num(item.count, `${path}.count`),
          success_count: num(item.success_count, `${path}.success_count`),
          failure_count: num(item.failure_count, `${path}.failure_count`),
          success_rate: nullableNum(item.success_rate, `${path}.success_rate`),
          avg_ms: literalNull(item.avg_ms, `${path}.avg_ms`),
        };
      }),
    },
    top_sessions: list(value.top_sessions, 'top_sessions', (raw, path) => {
      const item = record(raw, path);
      return {
        thread_id: str(item.thread_id, `${path}.thread_id`),
        title: str(item.title, `${path}.title`),
        model: nullableStr(item.model, `${path}.model`),
        turns: num(item.turns, `${path}.turns`),
        tokens: num(item.tokens, `${path}.tokens`),
        cache_rate: nullableNum(item.cache_rate, `${path}.cache_rate`),
      };
    }),
    notes: list(value.notes, 'notes', (raw, path) => str(raw, path)),
  };
}

/**
 * Fetch usage statistics for the current workspace.
 *
 * Rejects on a non-2xx response (`UsageStatsRequestError`) or a malformed body
 * (`UsageStatsPayloadError`). An aborted request rejects with the abort reason
 * (`AbortError`), which callers use for stale-response protection.
 */
export async function fetchUsageStats(
  options?: UsageStatsQuery,
  signal?: AbortSignal,
): Promise<UsageStatsPayload> {
  const params = new URLSearchParams();
  if (options?.project) params.set('project', options.project);
  if (options?.range) params.set('range', options.range);
  if (options?.start) params.set('start', options.start);
  if (options?.end) params.set('end', options.end);
  if (options?.model && options.model !== 'all') params.set('model', options.model);

  const qs = params.toString();
  const url = `/api/usage-stats${qs ? `?${qs}` : ''}`;
  const response = await fetch(url, {
    method: 'GET',
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
    signal,
  });

  if (!response.ok) {
    throw new UsageStatsRequestError(response.status, `usage statistics request failed (${response.status})`);
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new UsageStatsPayloadError('usage statistics response was not valid JSON');
  }
  return parseUsageStatsPayload(body);
}
