import {
  Dismiss20Regular,
  ArrowClockwise20Regular,
} from '@fluentui/react-icons';
import React, { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Portal } from './Portal.tsx';
import { useDialogKeyboardNav } from './keyboardNav.ts';
import { fetchUsageStats } from '../client/usageStats.ts';
import type {
  BreakdownItem,
  HeatmapDay,
  UsageRangeKey,
  UsageStatsPayload,
} from '../client/usageStats.ts';

export interface UsageDashboardDialogProps {
  onClose: () => void;
}

const WAN = 10000;
const USAGE_PREFS_STORAGE_KEY = 'synapse:usage-dashboard:prefs:v1';

interface StoredUsagePreferences {
  project?: string;
  range?: UsageRangeKey;
  customStart?: string;
  customEnd?: string;
  model?: string;
  heatMetric?: 'tokens' | 'sessions' | 'loc';
  breakdownDim?: 'project' | 'model' | 'agent';
}

function loadStoredUsagePrefs(): StoredUsagePreferences {
  if (typeof window === 'undefined' || !window.localStorage) return {};
  try {
    const raw = localStorage.getItem(USAGE_PREFS_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return typeof parsed === 'object' && parsed !== null ? parsed : {};
  } catch {
    return {};
  }
}

function saveStoredUsagePrefs(patch: Partial<StoredUsagePreferences>): void {
  if (typeof window === 'undefined' || !window.localStorage) return;
  try {
    const existing = loadStoredUsagePrefs();
    const updated = { ...existing, ...patch };
    localStorage.setItem(USAGE_PREFS_STORAGE_KEY, JSON.stringify(updated));
  } catch {
    // quota or security exception fallback
  }
}

function fmtTokens(value: number): string {
  if (value >= WAN) return `${(value / WAN).toFixed(1)} 万`;
  return value.toLocaleString('en-US');
}

function fmtInt(value: number): string {
  return value.toLocaleString('en-US');
}

function fmtPct(value: number | null): string {
  return value === null ? '—' : `${value}%`;
}

function utcDateString(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function daysAgoUtc(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days);
  return utcDateString(d);
}

/** Weekday index with Monday=0, matching the heatmap's row order. */
function mondayIndex(iso: string): number {
  const day = new Date(`${iso}T00:00:00Z`).getUTCDay();
  return (day + 6) % 7;
}

function breakdownFor(
  payload: UsageStatsPayload,
  dim: 'project' | 'model' | 'agent',
): BreakdownItem[] | null {
  return payload.breakdowns[dim];
}

interface CalendarCell {
  date: string;
  dayData: HeatmapDay | null;
  isFuture: boolean;
}

function build52WeekCalendar(
  heatDays: HeatmapDay[],
  rangeEndStr?: string | null,
): { cells: CalendarCell[]; months: string[] } {
  const dayMap = new Map<string, HeatmapDay>();
  for (const day of heatDays) {
    dayMap.set(day.date, day);
  }

  const todayStr = utcDateString(new Date());
  const refEndStr = rangeEndStr && rangeEndStr.length === 10 ? rangeEndStr : todayStr;
  const refDate = new Date(`${refEndStr}T00:00:00Z`);

  // Find Sunday of the reference week (ISO Monday=0 ... Sunday=6)
  const dayOfWeek = (refDate.getUTCDay() + 6) % 7;
  const daysToSunday = 6 - dayOfWeek;
  const endOfWeek = new Date(refDate);
  endOfWeek.setUTCDate(refDate.getUTCDate() + daysToSunday);

  // 52 周 = 52 * 7 = 364 天。起始日是 endOfWeek 往前推 363 天（对齐周一）
  const startOfCalendar = new Date(endOfWeek);
  startOfCalendar.setUTCDate(endOfWeek.getUTCDate() - 363);

  const cells: CalendarCell[] = [];
  for (let i = 0; i < 364; i += 1) {
    const cur = new Date(startOfCalendar);
    cur.setUTCDate(startOfCalendar.getUTCDate() + i);
    const dateStr = cur.toISOString().slice(0, 10);
    const isFuture = dateStr > todayStr;
    cells.push({
      date: dateStr,
      dayData: dayMap.get(dateStr) ?? null,
      isFuture,
    });
  }

  // 12 个月份刻度标签，顺次列出 52 周跨越的月份
  const startMonth = startOfCalendar.getUTCMonth();
  const months: string[] = [];
  for (let m = 0; m < 12; m += 1) {
    const monthNum = ((startMonth + m) % 12) + 1;
    months.push(`${monthNum}月`);
  }

  return { cells, months };
}

export const UsageDashboardDialog: React.FC<UsageDashboardDialogProps> = ({ onClose }) => {
  const titleId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const onKeyDown = useDialogKeyboardNav(dialogRef, true);

  const initialPrefs = useRef<StoredUsagePreferences>(loadStoredUsagePrefs()).current;

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  const [stats, setStats] = useState<UsageStatsPayload | null>(null);
  const [isInitialLoading, setIsInitialLoading] = useState(true);
  const [isFetching, setIsFetching] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [selectedProject, setSelectedProject] = useState<string>(() => {
    return initialPrefs.project && typeof initialPrefs.project === 'string'
      ? initialPrefs.project
      : 'all';
  });
  const [selectedModel, setSelectedModel] = useState<string>(() => {
    return initialPrefs.model && typeof initialPrefs.model === 'string'
      ? initialPrefs.model
      : 'all';
  });
  const [knownModels, setKnownModels] = useState<string[]>([]);
  // Remembered once a real payload arrives so the selector keeps its option even
  // if a later request fails (the option list must not depend on a live load).
  const [knownProjectName, setKnownProjectName] = useState<string | null>(null);
  const [knownProjectNames, setKnownProjectNames] = useState<string[]>([]);
  const [range, setRange] = useState<UsageRangeKey>(() => {
    const validRanges: UsageRangeKey[] = ['today', '7d', '30d', 'all', 'custom'];
    return initialPrefs.range && validRanges.includes(initialPrefs.range)
      ? initialPrefs.range
      : '30d';
  });
  // Draft inputs: never queried until "筛选" is pressed.
  const [draftStart, setDraftStart] = useState<string>(() => {
    return initialPrefs.customStart && typeof initialPrefs.customStart === 'string'
      ? initialPrefs.customStart
      : utcDateString(new Date());
  });
  const [draftEnd, setDraftEnd] = useState<string>(() => {
    return initialPrefs.customEnd && typeof initialPrefs.customEnd === 'string'
      ? initialPrefs.customEnd
      : utcDateString(new Date());
  });
  const [appliedStart, setAppliedStart] = useState<string>(() => {
    return initialPrefs.customStart && typeof initialPrefs.customStart === 'string'
      ? initialPrefs.customStart
      : '';
  });
  const [appliedEnd, setAppliedEnd] = useState<string>(() => {
    return initialPrefs.customEnd && typeof initialPrefs.customEnd === 'string'
      ? initialPrefs.customEnd
      : '';
  });
  // Bumped by "筛选"/"刷新" so an identical window still re-requests.
  const [nonce, setNonce] = useState(0);

  const [breakdownDim, setBreakdownDim] = useState<'project' | 'model' | 'agent'>(() => {
    return initialPrefs.breakdownDim === 'project' || initialPrefs.breakdownDim === 'agent'
      ? initialPrefs.breakdownDim
      : 'model';
  });
  const [heatMetric, setHeatMetric] = useState<'tokens' | 'sessions' | 'loc'>(() => {
    return initialPrefs.heatMetric === 'sessions'
      ? 'sessions'
      : initialPrefs.heatMetric === 'loc'
      ? 'loc'
      : 'tokens';
  });

  const [tooltip, setTooltip] = useState<{
    visible: boolean;
    x: number;
    y: number;
    title: string;
    rows: { label: string; val: string; color?: string }[];
  }>({ visible: false, x: 0, y: 0, title: '', rows: [] });

  // The single request trigger. Every filter change funnels through this effect,
  // which aborts the previous request so a slow reply for an old filter can
  // never paint over a new one.
  useEffect(() => {
    setIsFetching(true);
    setError(null);
    const controller = new AbortController();
    let active = true;
    const start = range === 'custom' ? appliedStart : '';
    const end = range === 'custom' ? appliedEnd : '';
    fetchUsageStats(
      { project: selectedProject, model: selectedModel, range, start, end },
      controller.signal,
    )
      .then((data) => {
        if (active) {
          setStats(data);
          setIsInitialLoading(false);
          setIsFetching(false);
          setError(null);
          setKnownProjectName(data.project.current.name);
          setKnownProjectNames((names) =>
            Array.from(new Set([...names, ...data.project_matrix.map((item) => item.name)])),
          );
          if (data.available_models && data.available_models.length > 0) {
            setKnownModels((prev) => Array.from(new Set([...prev, ...data.available_models])).sort());
          }
        }
      })
      .catch((err: unknown) => {
        if (!active) return;
        if (err instanceof Error && err.name === 'AbortError') return;
        setIsFetching(false);
        setIsInitialLoading(false);
        setError(err instanceof Error ? err.message : '使用统计加载失败');
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [selectedProject, selectedModel, range, appliedStart, appliedEnd, nonce]);

  const allDisplayModels = useMemo(() => {
    const s = new Set(knownModels);
    if (selectedModel && selectedModel !== 'all') s.add(selectedModel);
    return Array.from(s).sort();
  }, [knownModels, selectedModel]);

  const handleProjectChange = (project: string) => {
    if (project === selectedProject) return;
    setSelectedProject(project);
    saveStoredUsagePrefs({ project });
  };

  const handleModelChange = (model: string) => {
    if (model === selectedModel) return;
    setSelectedModel(model);
    saveStoredUsagePrefs({ model });
  };

  const handleRangeChange = (next: UsageRangeKey) => {
    if (next === range) return;
    setRange(next);
    saveStoredUsagePrefs({ range: next });
  };

  const handleApplyCustom = () => {
    if (!draftStart || !draftEnd) return;
    setAppliedStart(draftStart);
    setAppliedEnd(draftEnd);
    setRange('custom');
    saveStoredUsagePrefs({
      range: 'custom',
      customStart: draftStart,
      customEnd: draftEnd,
    });
    setNonce((n) => n + 1);
  };

  const handleHeatMetricChange = (metric: 'tokens' | 'sessions' | 'loc') => {
    if (metric === heatMetric) return;
    setHeatMetric(metric);
    saveStoredUsagePrefs({ heatMetric: metric });
  };

  const handleBreakdownDimChange = (dim: 'project' | 'model' | 'agent') => {
    if (dim === breakdownDim) return;
    setBreakdownDim(dim);
    saveStoredUsagePrefs({ breakdownDim: dim });
  };

  const handleRefresh = () => {
    setNonce((n) => n + 1);
  };

  const currentName = knownProjectName;
  const connectedCount = stats?.project.connected_count ?? 1;
  // A range with no model calls is only truly empty when it also has no tool
  // records and no sessions; otherwise the panel must still render (the tools
  // section is real data even when token accounting is zero).
  const isEmpty =
    stats !== null &&
    stats.kpi.call_count === 0 &&
    stats.kpi.total_tokens === 0 &&
    stats.top_tools.recorded_total === 0 &&
    stats.top_sessions.length === 0;

  const breakdownList = stats ? breakdownFor(stats, breakdownDim) : null;
  const C = 2 * Math.PI * 40;

  const trendItems = stats?.trend.items ?? [];
  const trendMax = Math.max(1, ...trendItems.map((i) => i.cache + i.input + i.output));
  const trendStep = trendItems.length > 1 ? 520 / (trendItems.length - 1) : 0;
  const labelEvery = trendItems.length > 12 ? Math.ceil(trendItems.length / 8) : 1;

  const heatDays: HeatmapDay[] = stats?.heatmap.days ?? [];
  const heatMax = Math.max(
    1,
    ...heatDays.map((d) => (heatMetric === 'tokens' ? d.tokens : heatMetric === 'sessions' ? d.sessions : 0)),
  );
  const { cells: calendarCells, months: calendarMonths } = useMemo(
    () => build52WeekCalendar(heatDays, stats?.range.end),
    [heatDays, stats?.range.end],
  );
  const heatColors = [
    'bg-[#ebedf0] dark:bg-[#262626]',
    'bg-[#0078d4]/[0.28]',
    'bg-[#0078d4]/[0.52]',
    'bg-[#0078d4]/[0.78]',
    'bg-[#0078d4]',
  ];
  const heatLevel = (day: HeatmapDay | null): number => {
    if (!day || heatMetric === 'loc') return 0;
    const value = heatMetric === 'tokens' ? day.tokens : day.sessions;
    if (value <= 0) return 0;
    return Math.min(4, Math.max(1, Math.ceil((value / heatMax) * 4)));
  };

  return (
    <Portal>
      <div
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 dark:bg-black/55 backdrop-blur-sm p-4 scrim-in"
        onClick={onClose}
      >
        <div
          role="dialog"
          aria-labelledby={titleId}
          ref={dialogRef}
          tabIndex={-1}
          onKeyDown={onKeyDown}
          className="no-scrollbar max-h-[92vh] w-full max-w-5xl overflow-y-auto rounded-card border border-line/80 material-flyout flyout-in p-6 font-sans shadow-flyout space-y-5 select-none"
          onClick={(e) => e.stopPropagation()}
        >
          {/* 顶栏操作与过滤区 */}
          <div className="flex items-start justify-between gap-4 border-b border-line/60 pb-3 flex-wrap">
            <div>
              <h1 id={titleId} className="text-base font-bold text-gray-900">
                使用统计与工程效能
              </h1>
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                支持多项目工作区横向对比、Token 消耗构成、Prompt 缓存收益与压缩节约追踪（UTC 闭区间）。
              </p>
            </div>

            <div className="flex items-center gap-2.5 flex-wrap">
              {/* 项目工作区选择器：仅当前项目 + 全部 */}
              <div className="flex items-center rounded-control border border-line bg-surface px-2 py-1 text-xs">
                <select
                  aria-label="切换统计项目范围"
                  value={selectedProject}
                  onChange={(e) => handleProjectChange(e.target.value)}
                  className="bg-surface font-mono text-xs font-semibold text-gray-900 outline-none cursor-pointer"
                >
                  <option value="all" className="bg-surface text-gray-900">
                    全部工作区（{connectedCount} 个）
                  </option>
                  {knownProjectNames.map((name) => (
                    <option key={name} value={name} className="bg-surface text-gray-900">
                      {name}{name === currentName ? ' [当前]' : ''}
                    </option>
                  ))}
                </select>
              </div>

              {/* 可用模型选择器 */}
              <div className="flex items-center rounded-control border border-line bg-surface px-2 py-1 text-xs">
                <select
                  aria-label="切换模型筛选范围"
                  value={selectedModel}
                  onChange={(e) => handleModelChange(e.target.value)}
                  className="bg-surface font-mono text-xs font-semibold text-gray-900 outline-none cursor-pointer max-w-[170px] truncate"
                >
                  <option value="all" className="bg-surface text-gray-900">
                    全部模型{allDisplayModels.length > 0 ? `（${allDisplayModels.length} 个）` : ''}
                  </option>
                  {allDisplayModels.map((modelName) => (
                    <option key={modelName} value={modelName} className="bg-surface text-gray-900">
                      {modelName}
                    </option>
                  ))}
                </select>
              </div>

              {/* 时间分段控制器 */}
              <div className="inline-flex rounded-control border border-line/60 bg-surface-sunken p-0.5">
                {(
                  [
                    ['today', '今天'],
                    ['7d', '近 7 天'],
                    ['30d', '近 30 天'],
                    ['all', '全部历史'],
                  ] as const
                ).map(([key, label]) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() => handleRangeChange(key)}
                    className={`rounded-control px-2.5 py-1 text-xs font-medium transition-all ${
                      range === key
                        ? 'bg-surface text-gray-900 font-bold shadow-card'
                        : 'text-gray-500 dark:text-gray-400 hover:text-gray-900'
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>

              {/* 自定义日期范围（草稿，点击筛选后才查询） */}
              <div className="flex items-center gap-1.5 rounded-control border border-line bg-surface px-2 py-0.5 text-xs">
                <input
                  type="date"
                  aria-label="统计起始日期"
                  value={draftStart}
                  onChange={(e) => setDraftStart(e.target.value)}
                  className="bg-transparent font-mono text-[11px] text-gray-900 outline-none cursor-pointer"
                />
                <span className="text-gray-400 text-[11px]">~</span>
                <input
                  type="date"
                  aria-label="统计截止日期"
                  value={draftEnd}
                  onChange={(e) => setDraftEnd(e.target.value)}
                  className="bg-transparent font-mono text-[11px] text-gray-900 outline-none cursor-pointer"
                />
                <button
                  type="button"
                  onClick={handleApplyCustom}
                  disabled={!draftStart || !draftEnd}
                  className={`rounded px-2 py-0.5 text-[11px] font-semibold transition-all ${
                    range === 'custom'
                      ? 'ui-primary text-on-accent'
                      : 'bg-blue-500/10 text-blue-700 dark:text-blue-400 hover:bg-blue-600 hover:text-on-accent'
                  }`}
                >
                  筛选
                </button>
              </div>

              {/* 刷新与关闭 */}
              <button
                type="button"
                onClick={handleRefresh}
                title="刷新数据"
                aria-label="刷新数据"
                className="ui-icon-button"
              >
                <ArrowClockwise20Regular className={isFetching ? 'animate-spin text-blue-600 dark:text-blue-400' : ''} />
              </button>
              <button
                type="button"
                onClick={onClose}
                title="关闭面板"
                aria-label="关闭面板"
                className="ui-icon-button"
              >
                <Dismiss20Regular />
              </button>
            </div>
          </div>

          {isInitialLoading && !stats && (
            <div className="space-y-5">
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                {Array.from({ length: 3 }).map((_, i) => (
                  <div key={i} className="rounded-card border border-line bg-surface p-3.5 space-y-2 shadow-card">
                    <div className="flex items-center justify-between">
                      <div className="h-3 w-20 rounded bg-surface-sunken animate-pulse" />
                      <div className="h-3 w-10 rounded bg-surface-sunken animate-pulse" />
                    </div>
                    <div className="h-7 w-28 rounded bg-surface-sunken animate-pulse" />
                    <div className="h-4 w-32 rounded-pill bg-surface-sunken animate-pulse" />
                  </div>
                ))}
              </div>
              <div className="rounded-card border border-line bg-surface p-4 space-y-3 shadow-card">
                <div className="flex items-center justify-between">
                  <div className="space-y-1">
                    <div className="h-4 w-44 rounded bg-surface-sunken animate-pulse" />
                    <div className="h-3 w-64 rounded bg-surface-sunken animate-pulse" />
                  </div>
                  <div className="h-3 w-28 rounded bg-surface-sunken animate-pulse" />
                </div>
                <div className="space-y-2 pt-2">
                  {Array.from({ length: 2 }).map((_, i) => (
                    <div key={i} className="flex items-center justify-between gap-4 py-2 border-b border-line/40">
                      <div className="h-4 w-36 rounded bg-surface-sunken animate-pulse" />
                      <div className="h-4 w-28 rounded bg-surface-sunken animate-pulse" />
                      <div className="h-4 w-24 rounded bg-surface-sunken animate-pulse" />
                      <div className="h-4 w-16 rounded bg-surface-sunken animate-pulse" />
                      <div className="h-4 w-20 rounded bg-surface-sunken animate-pulse" />
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {error && !stats && (
            <div className="rounded-card border border-red-500/30 bg-surface p-8 text-center space-y-2">
              <span className="font-semibold text-red-400">加载失败</span>
              <p className="font-mono text-xs text-gray-500 dark:text-gray-400">{error}</p>
              <div className="pt-2">
                <button
                  type="button"
                  onClick={handleRefresh}
                  className="ui-button ui-compact ui-primary text-xs font-semibold"
                >
                  重试
                </button>
              </div>
            </div>
          )}

          {isEmpty && !isFetching && !error && (
            <div className="flex flex-col items-center justify-center gap-1 py-16 text-sm text-gray-400">
              <span className="font-semibold text-gray-500">该筛选区间暂无用量数据</span>
              <span className="text-xs">
                当前范围 {stats?.range.start ?? '—'} ~ {stats?.range.end ?? '—'}（UTC）
              </span>
            </div>
          )}

          {stats && !error && (
            <div className={`space-y-5 transition-opacity duration-150 ${isFetching ? 'opacity-70' : 'opacity-100'}`}>
              {/* 第一层：核心 3 联 KPI 卡片 */}
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <div className="rounded-card border border-line bg-surface p-3.5 space-y-1 shadow-card hover:border-line-strong transition-all">
                  <div className="flex items-center justify-between text-xs text-gray-500 dark:text-gray-400">
                    <span>累计吞吐 Token</span>
                    <span className="font-mono text-[10px] text-gray-400 dark:text-gray-500">[TOKENS]</span>
                  </div>
                  <div className="text-2xl font-bold tracking-tight text-gray-900 font-mono">
                    {fmtTokens(stats.kpi.total_tokens)}
                  </div>
                  <div className="pt-0.5">
                    <span className="inline-flex items-center gap-1 rounded-pill bg-blue-500/10 text-blue-700 dark:text-blue-400 border border-blue-500/25 px-2 py-0.5 text-[11px] font-mono font-semibold">
                      输入 {fmtTokens(stats.kpi.provider_input_tokens)} + 输出 {fmtTokens(stats.kpi.output_tokens)}
                    </span>
                  </div>
                </div>

                <div className="rounded-card border border-line bg-surface p-3.5 space-y-1 shadow-card hover:border-line-strong transition-all">
                  <div className="flex items-center justify-between text-xs text-gray-500 dark:text-gray-400">
                    <span>Prompt 缓存命中率</span>
                    <span className="font-mono text-[10px] text-emerald-500">[CACHE]</span>
                  </div>
                  <div className="text-2xl font-bold tracking-tight text-emerald-600 dark:text-emerald-500 font-mono">
                    {fmtPct(stats.kpi.cache_hit_rate)}
                  </div>
                  <div className="pt-0.5">
                    <span className="inline-flex items-center gap-1 rounded-pill bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 border border-emerald-500/25 px-2 py-0.5 text-[11px] font-mono font-semibold">
                      缓存读取 {fmtTokens(stats.kpi.cache_read_tokens)}
                    </span>
                  </div>
                </div>

                <div className="rounded-card border border-line bg-surface p-3.5 space-y-1 shadow-card hover:border-line-strong transition-all">
                  <div className="flex items-center justify-between text-xs text-gray-500 dark:text-gray-400">
                    <span>压缩节约 Token</span>
                    <span className="font-mono text-[10px] text-purple-400">[SAVED]</span>
                  </div>
                  <div className="text-2xl font-bold tracking-tight text-purple-600 dark:text-purple-400 font-mono">
                    {fmtTokens(stats.kpi.saved_tokens)}
                  </div>
                  <div className="pt-0.5">
                    <span className="inline-flex items-center gap-1 rounded-pill bg-purple-500/10 text-purple-700 dark:text-purple-400 border border-purple-500/25 px-2 py-0.5 text-[11px] font-mono font-semibold">
                      裁剪率 {fmtPct(stats.kpi.saved_pct)}
                    </span>
                  </div>
                </div>
              </div>

              {/* 第二层：当前工作区用量与效能矩阵 */}
              <div className="rounded-card border border-line bg-surface p-4 space-y-3 shadow-card">
                <div className="flex items-center justify-between">
                  <div>
                    <h2 className="text-sm font-bold text-gray-900">多项目工作区用量与效能对比</h2>
                    <p className="text-xs text-gray-500 dark:text-gray-400">
                      跨 Workspace 观测 Token 消耗、预算配额、会话轮次与代码净产出转化比
                    </p>
                  </div>
                  <span className="font-mono text-xs text-gray-500 dark:text-gray-400">
                    共追踪 {stats.project_matrix.length} 个项目工作区
                  </span>
                </div>

                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead>
                      <tr className="border-b border-line text-gray-500 dark:text-gray-400 font-semibold text-xs">
                        <th className="pb-2 px-2">项目与工作区路径</th>
                        <th className="pb-2 px-2">当前 Git 状态</th>
                        <th className="pb-2 px-2">Token 消耗与全库占比</th>
                        <th className="pb-2 px-2">会话 / 轮次</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-line/40 font-mono">
                      {stats.project_matrix.map((p) => (
                        <tr key={p.name} className="hover:bg-surface-sunken/60 transition-colors">
                          <td className="py-2.5 px-2">
                            <div className="flex items-center gap-1.5 font-sans font-semibold text-gray-900">
                              <span>{p.name}</span>
                              {p.is_current && (
                                <span className="rounded-pill bg-blue-500/10 px-1.5 py-0.5 text-[9px] font-bold text-blue-700 dark:text-blue-400 border border-blue-500/25">
                                  当前工作区
                                </span>
                              )}
                            </div>
                            <div className="text-[10px] text-gray-500 dark:text-gray-400 truncate max-w-xs font-mono">
                              {p.path}
                            </div>
                          </td>
                          <td className="py-2.5 px-2">
                            <span className="flex items-center gap-1.5 text-[11px]">
                              <span
                                className={`inline-block h-2 w-2 rounded-full ${
                                  p.dirty ? 'bg-amber-500' : 'bg-emerald-500'
                                }`}
                              />
                              <span className="text-gray-700 dark:text-gray-300 font-mono text-xs">{p.branch ?? '—'}</span>
                            </span>
                          </td>
                          <td className="py-2.5 px-2">
                            <div className="flex items-center gap-2">
                              <div className="h-1.5 w-20 rounded-pill bg-surface-sunken overflow-hidden">
                                <div
                                  className="h-full rounded-pill bg-blue-600 dark:bg-blue-500"
                                  style={{ width: `${Math.min(100, p.share_pct)}%` }}
                                />
                              </div>
                              <span className="font-bold text-gray-900 font-mono">
                                {fmtTokens(p.tokens)} ({p.share_pct}%)
                              </span>
                            </div>
                          </td>
                          <td className="py-2.5 px-2 text-gray-700 dark:text-gray-300 font-mono">
                            {p.sessions_count} 会话 / {p.turns_count} 轮
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* 第三层：真实日期活动热力图 */}
              <div className="rounded-card border border-line bg-surface p-5 space-y-3 shadow-card">
                <div className="flex items-center justify-between">
                  <div>
                    <h2 className="text-sm font-bold text-gray-900">Token 活动与工程投入矩阵</h2>
                    <p className="text-xs text-gray-500 dark:text-gray-400">
                      按日记录 Agent 执行强度与活跃周期（过去 52 周）
                    </p>
                  </div>
                  <div className="inline-flex rounded-control border border-line/60 bg-surface-sunken p-0.5 text-xs">
                    <button
                      type="button"
                      onClick={() => handleHeatMetricChange('tokens')}
                      className={`rounded-control px-2.5 py-1 text-[11.5px] transition-all ${
                        heatMetric === 'tokens'
                          ? 'bg-surface font-semibold text-gray-900 shadow-sm'
                          : 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-200'
                      }`}
                    >
                      Token 消耗
                    </button>
                    <button
                      type="button"
                      onClick={() => handleHeatMetricChange('sessions')}
                      className={`rounded-control px-2.5 py-1 text-[11.5px] transition-all ${
                        heatMetric === 'sessions'
                          ? 'bg-surface font-semibold text-gray-900 shadow-sm'
                          : 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-200'
                      }`}
                    >
                      会话数
                    </button>
                    <button
                      type="button"
                      onClick={() => handleHeatMetricChange('loc')}
                      className={`rounded-control px-2.5 py-1 text-[11.5px] transition-all ${
                        heatMetric === 'loc'
                          ? 'bg-surface font-semibold text-gray-900 shadow-sm'
                          : 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-200'
                      }`}
                    >
                      代码变更行数
                    </button>
                  </div>
                </div>

                <div className="overflow-x-auto pb-1">
                  <div className="min-w-[780px]">
                    {/* 月份横向分布：左侧留出 28px（与星期指示等宽） */}
                    <div className="flex justify-between ml-[28px] mb-1.5 text-[11px] text-gray-400 dark:text-gray-500 font-mono select-none">
                      {calendarMonths.map((m, idx) => (
                        <span key={idx}>{m}</span>
                      ))}
                    </div>

                    {/* 主体：星期指示 + 52 周 x 7 天网格 */}
                    <div className="flex gap-1.5 items-start">
                      <div className="flex flex-col justify-between text-[10px] text-gray-400 dark:text-gray-500 pt-[2px] w-[22px] h-[98px] select-none shrink-0">
                        <span>周一</span>
                        <span>周三</span>
                        <span>周五</span>
                        <span>周日</span>
                      </div>

                      <div
                        className="grid grid-flow-col gap-[3.5px]"
                        style={{
                          gridTemplateRows: 'repeat(7, 11px)',
                          gridAutoColumns: '11px',
                        }}
                      >
                        {calendarCells.map((cell) => {
                          if (cell.isFuture) {
                            return (
                              <div
                                key={cell.date}
                                className="w-[11px] h-[11px] rounded-[2px] opacity-0 pointer-events-none"
                              />
                            );
                          }
                          const day = cell.dayData;
                          const level = heatLevel(day);
                          return (
                            <div
                              key={cell.date}
                              className={`w-[11px] h-[11px] rounded-[2px] cursor-pointer transition-transform hover:scale-125 hover:z-10 hover:outline hover:outline-[1.5px] hover:outline-gray-900 dark:hover:outline-gray-100 ${
                                heatColors[level]
                              }`}
                              onMouseEnter={(e) => {
                                const rect = e.currentTarget.getBoundingClientRect();
                                const rows = [
                                  { label: 'Token 消耗', val: fmtTokens(day ? day.tokens : 0) },
                                  { label: '会话数', val: `${day ? day.sessions : 0} 次` },
                                ];
                                if (heatMetric === 'loc') {
                                  rows.push({ label: '代码变更行数', val: '— (无数据来源)' });
                                }
                                setTooltip({
                                  visible: true,
                                  x: rect.left,
                                  y: rect.top - 70,
                                  title: cell.date,
                                  rows,
                                });
                              }}
                              onMouseLeave={() =>
                                setTooltip((t) => ({ ...t, visible: false }))
                              }
                            />
                          );
                        })}
                      </div>
                    </div>
                  </div>

                  {/* 较少 -> 重度使用 图例 */}
                  <div className="flex items-center justify-end gap-1.5 pt-3 text-[11px] text-gray-500 dark:text-gray-400 select-none">
                    <span>较少</span>
                    <div className="flex items-center gap-[3px]">
                      <div className="h-[11px] w-[11px] rounded-[2px] bg-[#ebedf0] dark:bg-[#262626]" />
                      <div className="h-[11px] w-[11px] rounded-[2px] bg-[#0078d4]/[0.28]" />
                      <div className="h-[11px] w-[11px] rounded-[2px] bg-[#0078d4]/[0.52]" />
                      <div className="h-[11px] w-[11px] rounded-[2px] bg-[#0078d4]/[0.78]" />
                      <div className="h-[11px] w-[11px] rounded-[2px] bg-[#0078d4]" />
                    </div>
                    <span>重度使用</span>
                  </div>
                </div>
              </div>

              {/* 第四层：核心双图表（分层堆叠趋势 + 环形剖析图） */}
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
                {/* 左侧堆叠柱状图 */}
                <div className="rounded-card border border-line/70 bg-surface p-4 space-y-3 lg:col-span-3">
                  <div className="flex items-center justify-between">
                    <div>
                      <h2 className="text-xs font-bold text-gray-900">{stats.trend.title}</h2>
                      <p className="text-xs text-gray-500 dark:text-gray-400">{stats.trend.subtitle}</p>
                    </div>
                    <div className="flex items-center gap-2 text-[10px] font-mono text-gray-600 dark:text-gray-400">
                      <span className="flex items-center gap-1">
                        <span className="h-2 w-2 rounded-sm bg-emerald-500" />
                        缓存命中
                      </span>
                      <span className="flex items-center gap-1">
                        <span className="h-2 w-2 rounded-sm bg-blue-500" />
                        非缓存读取输入
                      </span>
                      <span className="flex items-center gap-1">
                        <span className="h-2 w-2 rounded-sm bg-amber-500" />
                        输出
                      </span>
                    </div>
                  </div>

                  <div className="relative h-56 w-full">
                    <svg
                      className="h-full w-full overflow-visible"
                      viewBox="0 0 600 220"
                      preserveAspectRatio="xMidYMid meet"
                    >
                      {[0, 25, 50, 75, 100].map((pct) => (
                        <line
                          key={pct}
                          x1="30"
                          x2="590"
                          y1={180 - (pct / 100) * 160}
                          y2={180 - (pct / 100) * 160}
                          stroke="currentColor"
                          className="text-line/60"
                          strokeDasharray="3 3"
                        />
                      ))}
                      {trendItems.map((item, idx) => {
                        const x = 40 + idx * trendStep;
                        const hCache = (item.cache / trendMax) * 160;
                        const hInput = (item.input / trendMax) * 160;
                        const hOut = (item.output / trendMax) * 160;
                        return (
                          <g key={`${item.date}-${idx}`} className="cursor-pointer group">
                            <rect
                              x={x - 7}
                              y={180 - hCache}
                              width="14"
                              height={hCache}
                              fill="#10b981"
                              rx="1"
                              opacity="0.85"
                            />
                            <rect
                              x={x - 7}
                              y={180 - hCache - hInput}
                              width="14"
                              height={hInput}
                              fill="#0078d4"
                              rx="1"
                              opacity="0.9"
                            />
                            <rect
                              x={x - 7}
                              y={180 - hCache - hInput - hOut}
                              width="14"
                              height={hOut}
                              fill="#f59e0b"
                              rx="1"
                              opacity="0.9"
                            />
                            <circle
                              cx={x}
                              cy={180 - hCache - hInput - hOut}
                              r="3"
                              fill="currentColor"
                              className="text-surface"
                              stroke="#0078d4"
                              strokeWidth="2"
                              onMouseEnter={(e) => {
                                const rect = e.currentTarget.getBoundingClientRect();
                                setTooltip({
                                  visible: true,
                                  x: rect.left,
                                  y: rect.top - 120,
                                  title: `${item.date} 用量分解`,
                                  rows: [
                                    { label: '缓存读取', val: fmtTokens(item.cache), color: '#10b981' },
                                    {
                                      label: '非缓存读取输入',
                                      val: fmtTokens(item.input),
                                      color: '#0078d4',
                                    },
                                    { label: '输出', val: fmtTokens(item.output), color: '#f59e0b' },
                                    { label: '未压缩基准', val: fmtTokens(item.raw), color: '#9ca3af' },
                                  ],
                                });
                              }}
                              onMouseLeave={() =>
                                setTooltip((t) => ({ ...t, visible: false }))
                              }
                            />
                            {idx % labelEvery === 0 && (
                              <text
                                x={x}
                                y="205"
                                textAnchor="middle"
                                fill="currentColor"
                                className="text-[10px] font-mono fill-gray-500 dark:fill-gray-400"
                              >
                                {item.date}
                              </text>
                            )}
                          </g>
                        );
                      })}
                    </svg>
                  </div>
                </div>

                {/* 右侧多维环形剖析图 */}
                <div className="rounded-card border border-line/70 bg-surface p-4 space-y-3 lg:col-span-2">
                  <div className="flex items-center justify-between">
                    <div>
                      <h2 className="text-xs font-bold text-gray-900">结构剖析分布</h2>
                      <p className="text-xs text-gray-500 dark:text-gray-400">按真实 Token 用量分解</p>
                    </div>
                    <div className="inline-flex rounded-control border border-line/60 bg-surface-sunken p-0.5 text-xs">
                      {(
                        [
                          ['project', '按项目'],
                          ['model', '按模型'],
                          ['agent', '按角色'],
                        ] as const
                      ).map(([key, label]) => (
                        <button
                          key={key}
                          type="button"
                          onClick={() => handleBreakdownDimChange(key)}
                          className={`rounded-control px-1.5 py-0.5 text-[10px] ${
                            breakdownDim === key
                              ? 'bg-surface font-bold text-gray-900'
                              : 'text-gray-500 dark:text-gray-400 hover:text-gray-900'
                          }`}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                  </div>

                  {breakdownList === null || breakdownList.length === 0 ? (
                    <div className="flex h-28 items-center justify-center text-xs text-gray-500 dark:text-gray-400">
                      {breakdownDim === 'agent'
                        ? 'Agent/角色维度无数据来源'
                        : '当前区间无 Token 数据'}
                    </div>
                  ) : (
                    <div className="flex items-center gap-4 pt-2">
                      <div className="relative h-28 w-28 shrink-0">
                        <svg viewBox="0 0 100 100" className="h-full w-full -rotate-90">
                          {breakdownList.map((item) => {
                            const dashLen = (item.pct / 100) * C;
                            return (
                              <circle
                                key={item.name}
                                cx="50"
                                cy="50"
                                r="40"
                                fill="transparent"
                                stroke={item.color}
                                strokeWidth="14"
                                strokeDasharray={`${dashLen} ${C - dashLen}`}
                                strokeDashoffset={`-${(item.offset / 100) * C}`}
                              />
                            );
                          })}
                        </svg>
                        <div className="absolute inset-0 flex flex-col items-center justify-center text-center">
                          <span className="font-mono text-sm font-bold text-gray-900 leading-none">
                            {fmtTokens(stats.kpi.total_tokens)}
                          </span>
                          <span className="text-[9px] uppercase tracking-wider text-gray-500 dark:text-gray-400">
                            Tokens
                          </span>
                        </div>
                      </div>

                      <div className="flex-1 space-y-1.5 font-mono text-xs">
                        {breakdownList.map((item) => (
                          <div
                            key={item.name}
                            className="flex items-center justify-between text-[11px]"
                          >
                            <span className="flex items-center gap-1.5 truncate text-gray-700 dark:text-gray-300">
                              <span
                                className="h-2 w-2 rounded-full shrink-0"
                                style={{ background: item.color }}
                              />
                              <span className="truncate">{item.name}</span>
                            </span>
                            <span className="font-bold text-gray-900 shrink-0">
                              {item.pct}%
                            </span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              </div>

              {/* 第五层：深度可观测性（工具输出记录 + 高消耗会话排行） */}
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                {/* 工具输出记录（非全部调用） */}
                <div className="rounded-card border border-line/70 bg-surface p-4 space-y-2">
                  <div className="flex items-center justify-between border-b border-line/60 pb-2">
                    <h2 className="text-xs font-bold text-gray-900">工具输出压缩记录</h2>
                    <span className="font-mono text-xs text-gray-500 dark:text-gray-400">
                      记录数 {fmtInt(stats.top_tools.recorded_total)}（非全部调用）
                    </span>
                  </div>
                  {stats.top_tools.items.length === 0 ? (
                    <div className="py-6 text-center text-xs text-gray-500 dark:text-gray-400">
                      区间内无工具输出压缩记录
                    </div>
                  ) : (
                    <div className="divide-y divide-line/30">
                      {stats.top_tools.items.map((tool) => (
                        <div
                          key={tool.name}
                          className="flex items-center justify-between py-2 text-xs"
                        >
                          <div className="flex items-center gap-2 w-36 shrink-0">
                            <span className="rounded-control bg-blue-500/10 px-1.5 py-0.5 font-mono text-[11px] font-medium text-blue-700 dark:text-blue-400 border border-blue-500/25">
                              {tool.name}
                            </span>
                          </div>
                          <div className="mx-3 h-1.5 flex-1 rounded-full bg-surface-sunken overflow-hidden">
                            <div
                              className="h-full rounded-full bg-blue-600 dark:bg-blue-500"
                              style={{
                                width: `${
                                  stats.top_tools.recorded_total > 0
                                    ? (tool.count / stats.top_tools.recorded_total) * 100
                                    : 0
                                }%`,
                              }}
                            />
                          </div>
                          <div className="flex items-center gap-3 font-mono text-[11px] text-gray-500 dark:text-gray-400 shrink-0">
                            <span className="font-semibold text-gray-900">
                              {fmtInt(tool.count)} 条
                            </span>
                            <span>均耗 {tool.avg_ms === null ? '—' : `${tool.avg_ms}ms`}</span>
                            <span className="text-emerald-600 dark:text-emerald-400 font-semibold">{fmtPct(tool.success_rate)}</span>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                  <p className="pt-1 text-[10px] text-gray-500 dark:text-gray-400">{stats.top_tools.scope_note}</p>
                </div>

                {/* 高消耗会话排行 */}
                <div className="rounded-card border border-line/70 bg-surface p-4 space-y-2">
                  <div className="flex items-center justify-between border-b border-line/60 pb-2">
                    <h2 className="text-xs font-bold text-gray-900">用量溯源：高消耗会话排行</h2>
                    <span className="font-mono text-xs text-gray-500 dark:text-gray-400">按 Token 排序</span>
                  </div>
                  {stats.top_sessions.length === 0 ? (
                    <div className="py-6 text-center text-xs text-gray-500 dark:text-gray-400">
                      区间内无会话用量
                    </div>
                  ) : (
                    <div className="divide-y divide-line/30">
                      {stats.top_sessions.map((sess) => (
                        <div
                          key={sess.thread_id}
                          className="flex items-center justify-between py-2 text-xs"
                        >
                          <div className="max-w-[65%] space-y-0.5">
                            <div className="truncate font-medium text-gray-900">
                              {sess.title}
                            </div>
                            <div className="flex items-center gap-2 font-mono text-[10px] text-gray-500 dark:text-gray-400">
                              <span>{sess.thread_id}</span>
                              <span>{sess.model ?? '—'}</span>
                              <span>{sess.turns} 轮次</span>
                            </div>
                          </div>
                          <div className="text-right font-mono">
                            <div className="font-semibold text-gray-900">
                              {fmtTokens(sess.tokens)}
                            </div>
                            <div className="text-[10px] text-emerald-600 dark:text-emerald-400 font-semibold">
                              {fmtPct(sess.cache_rate)} 命中
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Floating Tooltip */}
      {tooltip.visible && (
        <div
          className="fixed pointer-events-none z-50 rounded-card border border-line/80 bg-surface p-2.5 shadow-flyout text-xs space-y-1 backdrop-blur-md"
          style={{ left: `${tooltip.x}px`, top: `${tooltip.y}px` }}
        >
          <div className="font-bold text-gray-900">{tooltip.title}</div>
          {tooltip.rows.map((r) => (
            <div
              key={r.label}
              className="flex items-center justify-between gap-3 font-mono text-xs text-gray-500 dark:text-gray-400"
            >
              <span style={{ color: r.color }}>{r.label}:</span>
              <span className="font-semibold text-gray-900">{r.val}</span>
            </div>
          ))}
        </div>
      )}
    </Portal>
  );
};
