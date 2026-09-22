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
import {
  loadStoredUsagePrefs,
  saveStoredUsagePrefs,
} from '../stores/usagePrefs.ts';
import type {
  BreakdownDim,
  HeatDim,
  HeatMetric,
  StoredUsagePreferences,
} from '../stores/usagePrefs.ts';

export interface UsageDashboardDialogProps {
  onClose: () => void;
}

const WAN = 10000;

function fmtTokens(value: number): string {
  if (value >= WAN) return `${(value / WAN).toFixed(1)} 万`;
  return value.toLocaleString('en-US');
}

function fmtPct(value: number | null): string {
  return value === null ? '—' : `${value}%`;
}

function utcDateString(date: Date): string {
  return date.toISOString().slice(0, 10);
}

const WEEKDAY_LABELS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

/**
 * `2026-09-21` -> `2026年9月21日（周一）`.
 *
 * The heat map's tooltip has to name the day it points at: the grid only carries
 * weekday rows, so an ISO string would leave the reader doing the calendar maths
 * themselves (and the weekday is what ties the cell to its row).
 */
function fmtCalendarDate(iso: string): string {
  const date = new Date(`${iso}T00:00:00Z`);
  const weekday = WEEKDAY_LABELS[date.getUTCDay()];
  return `${date.getUTCFullYear()}年${date.getUTCMonth() + 1}月${date.getUTCDate()}日（${weekday}）`;
}

/** The heat map's series value for one day; `loc` has no source, so it stays flat. */
function heatMetricValue(day: HeatmapDay, metric: HeatMetric): number {
  if (metric === 'tokens') return day.tokens;
  if (metric === 'sessions') return day.sessions;
  return 0;
}

function breakdownFor(
  payload: UsageStatsPayload,
  dim: BreakdownDim,
): BreakdownItem[] | null {
  return payload.breakdowns[dim];
}

interface CalendarCell {
  date: string;
  dayData: HeatmapDay | null;
  isFuture: boolean;
}

/** One month tick, anchored to the week column it starts at (0-based). */
interface MonthTick {
  column: number;
  label: string;
}

/** The grid geometry both the ticks row and the cells row must share. */
const HEAT_CELL_PX = 11;
const HEAT_GAP_PX = 3.5;
/** The weekday labels column (22px) plus its gap (6px): where the grid starts. */
const HEAT_TICKS_OFFSET_PX = 28;
const HEAT_WEEKS = 52;
const HEAT_GRID_WIDTH_PX = HEAT_WEEKS * HEAT_CELL_PX + (HEAT_WEEKS - 1) * HEAT_GAP_PX;
/** The hourly matrix: 24 columns, one row per day, filling the card width. */
const HEAT_HOURS = 24;
const HEAT_HOUR_GAP_PX = 3;
/** The date label column left of the hourly rows (MM-DD). */
const HEAT_HOUR_LABEL_PX = 34;

function build52WeekCalendar(
  heatDays: HeatmapDay[],
  rangeEndStr?: string | null,
): { cells: CalendarCell[]; monthTicks: MonthTick[] } {
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

  // Month ticks are anchored to the column they label, never spread evenly: a
  // month boundary lands inside a week, so an evenly spaced row drifts away from
  // the cells it names (the axis would end on August while the last column is
  // September).  Each column is named by its Thursday -- the day a week is
  // "mostly" in -- so the two September days that open the window cannot claim a
  // tick of their own, and the row lists exactly the months the grid covers.
  const monthTicks: MonthTick[] = [];
  let lastMonth = -1;
  for (let column = 0; column < HEAT_WEEKS; column += 1) {
    const thursday = cells[column * 7 + 3];
    if (!thursday) continue;
    const month = new Date(`${thursday.date}T00:00:00Z`).getUTCMonth();
    if (month === lastMonth) continue;
    lastMonth = month;
    monthTicks.push({ column, label: `${month + 1}月` });
  }

  return { cells, monthTicks };
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

  const [breakdownDim, setBreakdownDim] = useState<BreakdownDim>(() => {
    return initialPrefs.breakdownDim === 'project' || initialPrefs.breakdownDim === 'agent'
      ? initialPrefs.breakdownDim
      : 'model';
  });
  const [heatMetric, setHeatMetric] = useState<HeatMetric>(() => {
    return initialPrefs.heatMetric === 'sessions'
      ? 'sessions'
      : initialPrefs.heatMetric === 'loc'
      ? 'loc'
      : 'tokens';
  });
  const [heatDim, setHeatDim] = useState<HeatDim>(() =>
    initialPrefs.heatDim === 'day' ? 'day' : 'week',
  );

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

  const handleHeatMetricChange = (metric: HeatMetric) => {
    if (metric === heatMetric) return;
    setHeatMetric(metric);
    saveStoredUsagePrefs({ heatMetric: metric });
  };

  const handleHeatDimChange = (dim: HeatDim) => {
    if (dim === heatDim) return;
    setHeatDim(dim);
    saveStoredUsagePrefs({ heatDim: dim });
  };

  const handleBreakdownDimChange = (dim: BreakdownDim) => {
    if (dim === breakdownDim) return;
    setBreakdownDim(dim);
    saveStoredUsagePrefs({ breakdownDim: dim });
  };

  const handleRefresh = () => {
    setNonce((n) => n + 1);
  };

  const currentName = knownProjectName;
  const connectedCount = stats?.project.connected_count ?? 1;
  // The panel only paints token/session telemetry, so a range with no calls and
  // no sessions is empty even when the payload still carries tool-output refs
  // (that section is no longer rendered).
  const isEmpty =
    stats !== null &&
    stats.kpi.call_count === 0 &&
    stats.kpi.total_tokens === 0 &&
    stats.top_sessions.length === 0;

  const breakdownList = stats ? breakdownFor(stats, breakdownDim) : null;
  const C = 2 * Math.PI * 40;

  const trendItems = useMemo(() => stats?.trend.items ?? [], [stats]);
  const trendW = 600;
  const trendH = 220;
  const padL = 40;
  const padR = 20;
  const padT = 20;
  const padB = 30;
  const plotW = trendW - padL - padR;
  const plotH = trendH - padT - padB;

  const trendMax = Math.max(
    1,
    ...trendItems.map((i) => Math.max(i.cache + i.input + i.output, i.raw)),
  );
  const trendGridTicks = [0, 0.25, 0.5, 0.75, 1].map((pct) => ({
    val: Math.round(trendMax * pct),
    y: padT + plotH - pct * plotH,
  }));

  const trendPaths = useMemo(() => {
    if (trendItems.length === 0) return null;
    const pCache: { x: number; y: number; yBase: number }[] = [];
    const pInput: { x: number; y: number; yBase: number }[] = [];
    const pOut: { x: number; y: number; yBase: number }[] = [];
    const pRaw: { x: number; y: number }[] = [];

    const getX = (idx: number) =>
      padL + (trendItems.length > 1 ? (idx / (trendItems.length - 1)) * plotW : plotW / 2);
    const getY = (val: number) => padT + plotH - (val / trendMax) * plotH;
    const y0 = padT + plotH;

    trendItems.forEach((d, i) => {
      const x = getX(i);
      const yCache = getY(d.cache);
      const yInput = getY(d.cache + d.input);
      const yOut = getY(d.cache + d.input + d.output);
      const yRaw = getY(d.raw);

      pCache.push({ x, y: yCache, yBase: y0 });
      pInput.push({ x, y: yInput, yBase: yCache });
      pOut.push({ x, y: yOut, yBase: yInput });
      pRaw.push({ x, y: yRaw });
    });

    const drawArea = (points: { x: number; y: number; yBase: number }[]) => {
      if (points.length === 0) return '';
      let d = `M ${points[0].x} ${points[0].yBase}`;
      points.forEach((p) => (d += ` L ${p.x} ${p.y}`));
      for (let i = points.length - 1; i >= 0; i -= 1) {
        d += ` L ${points[i].x} ${points[i].yBase}`;
      }
      return d + ' Z';
    };

    let dRaw = `M ${pRaw[0].x} ${pRaw[0].y}`;
    pRaw.forEach((p, i) => {
      if (i > 0) dRaw += ` L ${p.x} ${p.y}`;
    });

    return {
      dCache: drawArea(pCache),
      dInput: drawArea(pInput),
      dOut: drawArea(pOut),
      dRaw,
      pRaw,
      pOut,
    };
  }, [trendItems, trendMax, plotW, plotH]);

  // Both series are read straight out of the payload; memoising them keeps the two
  // derivations below from re-running on every unrelated re-render.
  const heatDays = useMemo<HeatmapDay[]>(() => stats?.heatmap.days ?? [], [stats]);
  // Quartile thresholds over the days that actually have activity.  Scaling by the
  // window's single busiest day would flatten everything else into the lowest step
  // as soon as one outlier day exists, which is what made the grid read as "all
  // empty".  Quartiles keep the four steps spread over the real distribution.
  const heatThresholds = useMemo(() => {
    const active = heatDays
      .map((day) => heatMetricValue(day, heatMetric))
      .filter((value) => value > 0)
      .sort((a, b) => a - b);
    if (active.length === 0) return null;
    const at = (q: number) => active[Math.min(active.length - 1, Math.floor(q * active.length))];
    return { q1: at(0.25), q2: at(0.5), q3: at(0.75), max: active[active.length - 1] };
  }, [heatDays, heatMetric]);
  const { cells: calendarCells, monthTicks } = useMemo(
    () => build52WeekCalendar(heatDays, stats?.range.end),
    [heatDays, stats?.range.end],
  );
  // Five steps of the brand ramp, taken from the theme's blue role palette: the
  // empty cell is the sunken surface and the four activity steps are the same
  // brand hue at rising weight, so a theme re-paints the whole scale.  Every step
  // keeps a hairline frame (`border` is set on the cell, the colour here) so the
  // calendar still reads as a grid when a day has no activity.
  const heatColors = [
    'bg-sunken border-line/50',
    'bg-blue-500/35 border-transparent',
    'bg-blue-500/60 border-transparent',
    'bg-blue-500/85 border-transparent',
    'bg-blue-500 border-transparent',
  ];
  const heatLevel = (day: HeatmapDay | null): number => {
    if (!day || heatThresholds === null) return 0;
    const value = heatMetricValue(day, heatMetric);
    if (value <= 0) return 0;
    const { q1, q2, q3, max } = heatThresholds;
    // Every active day carries the same load: the whole window is at its own top
    // step, so painting it in the lowest one would understate a uniform workload.
    if (q3 === q1) return 4;
    if (value <= q1) return 1;
    if (value <= q2) return 2;
    // `max` must always reach the top step, even when the quartile lands on it.
    if (value < max && value <= q3) return 3;
    return 4;
  };

  // The hourly matrix re-uses the same ramp, but its own quartiles: an hour is
  // never comparable to a whole day, and the panel would otherwise paint every
  // hour of a busy day in the same two steps.
  const hourlyRows = useMemo(
    () => stats?.heatmap.hourly.rows ?? [],
    [stats],
  );
  const hourlyThresholds = useMemo(() => {
    const values = hourlyRows
      .flatMap((row) => (heatMetric === 'tokens' ? row.tokens : row.sessions))
      .filter((value) => value > 0)
      .sort((a, b) => a - b);
    if (values.length === 0) return null;
    const at = (q: number) => values[Math.min(values.length - 1, Math.floor(q * values.length))];
    return { q1: at(0.25), q2: at(0.5), q3: at(0.75), max: values[values.length - 1] };
  }, [hourlyRows, heatMetric]);
  const hourLevel = (value: number): number => {
    if (heatMetric === 'loc' || hourlyThresholds === null || value <= 0) return 0;
    const { q1, q2, q3, max } = hourlyThresholds;
    if (q3 === q1) return 4;
    if (value <= q1) return 1;
    if (value <= q2) return 2;
    if (value < max && value <= q3) return 3;
    return 4;
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
              <div className="inline-flex rounded-control border border-line/60 bg-sunken p-0.5">
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
                  <div key={i} className="rounded-card border border-line bg-surface p-4 flex flex-col justify-between shadow-card min-h-[108px]">
                    <div>
                      <div className="h-3 w-24 rounded bg-sunken animate-pulse" />
                      <div className="mt-2 h-7 w-28 rounded bg-sunken animate-pulse" />
                    </div>
                    <div className="mt-2.5 h-4 w-32 rounded-pill bg-sunken animate-pulse" />
                  </div>
                ))}
              </div>
              <div className="rounded-card border border-line bg-surface p-4 space-y-3 shadow-card">
                <div className="flex items-center justify-between">
                  <div className="space-y-1">
                    <div className="h-4 w-44 rounded bg-sunken animate-pulse" />
                    <div className="h-3 w-64 rounded bg-sunken animate-pulse" />
                  </div>
                  <div className="h-3 w-28 rounded bg-sunken animate-pulse" />
                </div>
                <div className="space-y-2 pt-2">
                  {Array.from({ length: 2 }).map((_, i) => (
                    <div key={i} className="flex items-center justify-between gap-4 py-2 border-b border-line/40">
                      <div className="h-4 w-36 rounded bg-sunken animate-pulse" />
                      <div className="h-4 w-28 rounded bg-sunken animate-pulse" />
                      <div className="h-4 w-24 rounded bg-sunken animate-pulse" />
                      <div className="h-4 w-16 rounded bg-sunken animate-pulse" />
                      <div className="h-4 w-20 rounded bg-sunken animate-pulse" />
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
                <div className="rounded-card border border-line bg-surface p-4 flex flex-col justify-between shadow-card hover:border-line-strong transition-all min-h-[108px]">
                  <div>
                    <div className="text-xs font-medium text-gray-500 dark:text-gray-400">
                      累计吞吐 Token
                    </div>
                    <div className="mt-1.5 text-2xl font-bold tracking-tight text-gray-900 dark:text-gray-100 font-mono">
                      {fmtTokens(stats.kpi.total_tokens)}
                    </div>
                  </div>
                  <div className="mt-2.5">
                    <span className="inline-flex items-center gap-1 rounded-pill bg-blue-500/10 text-blue-700 dark:text-blue-400 border border-blue-500/25 px-2 py-0.5 text-[11px] font-mono font-semibold">
                      输入 {fmtTokens(stats.kpi.provider_input_tokens)} + 输出 {fmtTokens(stats.kpi.output_tokens)}
                    </span>
                  </div>
                </div>

                <div className="rounded-card border border-line bg-surface p-4 flex flex-col justify-between shadow-card hover:border-line-strong transition-all min-h-[108px]">
                  <div>
                    <div className="text-xs font-medium text-gray-500 dark:text-gray-400">
                      Prompt 缓存命中率
                    </div>
                    <div className="mt-1.5 text-2xl font-bold tracking-tight text-emerald-600 dark:text-emerald-500 font-mono">
                      {fmtPct(stats.kpi.cache_hit_rate)}
                    </div>
                  </div>
                  <div className="mt-2.5">
                    <span className="inline-flex items-center gap-1 rounded-pill bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 border border-emerald-500/25 px-2 py-0.5 text-[11px] font-mono font-semibold">
                      缓存读取 {fmtTokens(stats.kpi.cache_read_tokens)}
                    </span>
                  </div>
                </div>

                <div className="rounded-card border border-line bg-surface p-4 flex flex-col justify-between shadow-card hover:border-line-strong transition-all min-h-[108px]">
                  <div>
                    <div className="text-xs font-medium text-gray-500 dark:text-gray-400">
                      压缩节约 Token
                    </div>
                    <div className="mt-1.5 text-2xl font-bold tracking-tight text-purple-600 dark:text-purple-400 font-mono">
                      {fmtTokens(stats.kpi.saved_tokens)}
                    </div>
                  </div>
                  <div className="mt-2.5">
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
                        <tr key={p.name} className="hover:bg-sunken/60 transition-colors">
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
                              <div className="h-1.5 w-20 rounded-pill bg-sunken overflow-hidden">
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
                      {heatDim === 'week'
                        ? '按日记录 Agent 执行强度与活跃周期（过去 52 周，UTC）'
                        : `按小时记录 Agent 执行强度（最近最多 14 个活跃日，本地时区 ${stats.heatmap.hourly.timezone}，每日 24 小时）`}
                      {heatDim === 'day' && stats.heatmap.hourly.truncated
                        ? '；更早的活跃日未显示'
                        : ''}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                  <div className="inline-flex rounded-control border border-line/60 bg-sunken p-0.5 text-xs">
                    <button
                      type="button"
                      onClick={() => handleHeatDimChange('week')}
                      className={`rounded-control px-2.5 py-1 text-[11.5px] transition-all ${
                        heatDim === 'week'
                          ? 'bg-surface font-semibold text-gray-900 shadow-card'
                          : 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-200'
                      }`}
                    >
                      按周
                    </button>
                    <button
                      type="button"
                      onClick={() => handleHeatDimChange('day')}
                      className={`rounded-control px-2.5 py-1 text-[11.5px] transition-all ${
                        heatDim === 'day'
                          ? 'bg-surface font-semibold text-gray-900 shadow-card'
                          : 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-200'
                      }`}
                    >
                      按天
                    </button>
                  </div>
                  <div className="inline-flex rounded-control border border-line/60 bg-sunken p-0.5 text-xs">
                    <button
                      type="button"
                      onClick={() => handleHeatMetricChange('tokens')}
                      className={`rounded-control px-2.5 py-1 text-[11.5px] transition-all ${
                        heatMetric === 'tokens'
                          ? 'bg-surface font-semibold text-gray-900 shadow-card'
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
                          ? 'bg-surface font-semibold text-gray-900 shadow-card'
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
                          ? 'bg-surface font-semibold text-gray-900 shadow-card'
                          : 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-200'
                      }`}
                    >
                      代码变更行数
                    </button>
                  </div>
                  </div>
                </div>

                {heatDim === 'week' ? (
                  <div className="overflow-x-auto pb-1">
                    <div
                      className="min-w-max"
                      style={{ width: `${HEAT_TICKS_OFFSET_PX + HEAT_GRID_WIDTH_PX}px` }}
                    >
                      {/* 月份刻度：与下方 52 列网格共用同一套列宽/间距，每个刻度锚定在它所标注的那一列 */}
                      <div
                        className="grid mb-1.5 text-[11px] text-gray-400 dark:text-gray-500 font-mono select-none"
                        style={{
                          gridTemplateColumns: `repeat(${HEAT_WEEKS}, ${HEAT_CELL_PX}px)`,
                          columnGap: `${HEAT_GAP_PX}px`,
                          width: `${HEAT_GRID_WIDTH_PX}px`,
                          marginLeft: `${HEAT_TICKS_OFFSET_PX}px`,
                        }}
                      >
                        {monthTicks.map((tick) => (
                          <span
                            key={tick.column}
                            style={{ gridColumnStart: tick.column + 1 }}
                            className="whitespace-nowrap"
                          >
                            {tick.label}
                          </span>
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
                          className="grid grid-flow-col"
                          style={{
                            gridTemplateRows: `repeat(7, ${HEAT_CELL_PX}px)`,
                            gridAutoColumns: `${HEAT_CELL_PX}px`,
                            gap: `${HEAT_GAP_PX}px`,
                          }}
                        >
                          {calendarCells.map((cell) => {
                            if (cell.isFuture) {
                              return (
                                <div
                                  key={cell.date}
                                  className="rounded-[2px] opacity-0 pointer-events-none"
                                />
                              );
                            }
                            const day = cell.dayData;
                            const level = heatLevel(day);
                            return (
                              <div
                                key={cell.date}
                                className={`rounded-[2px] border cursor-pointer transition-transform hover:scale-[1.35] hover:z-20 hover:outline hover:outline-[1.5px] hover:outline-gray-900 dark:hover:outline-white ${
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
                                    title: fmtCalendarDate(cell.date),
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
                        {heatColors.map((swatch) => (
                          <div
                            key={swatch}
                            className={`rounded-[2px] border ${swatch}`}
                            style={{ width: `${HEAT_CELL_PX}px`, height: `${HEAT_CELL_PX}px` }}
                          />
                        ))}
                      </div>
                      <span>重度使用</span>
                    </div>
                  </div>
                ) : (
                  <div className="space-y-1.5">
                    {hourlyRows.length === 0 ? (
                      <div className="flex h-24 items-center justify-center text-xs text-gray-400">
                        当前区间无小时级活动数据
                      </div>
                    ) : (
                      <>
                        {/* 小时刻度：与下方 24 列共用同一套列宽，整点每 3 小时标一次 */}
                        <div
                          className="grid text-[10px] text-gray-400 dark:text-gray-500 font-mono select-none"
                          style={{
                            gridTemplateColumns: `repeat(${HEAT_HOURS}, minmax(0, 1fr))`,
                            columnGap: `${HEAT_HOUR_GAP_PX}px`,
                            marginLeft: `${HEAT_HOUR_LABEL_PX + 6}px`,
                          }}
                        >
                          {Array.from({ length: HEAT_HOURS }).map((_, hour) => (
                            <span key={hour} className="text-center">
                              {hour % 3 === 0 ? hour : ''}
                            </span>
                          ))}
                        </div>
                        {hourlyRows.map((row) => (
                          <div key={row.date} className="flex items-center gap-1.5">
                            <span
                              className="shrink-0 text-[10px] text-gray-400 dark:text-gray-500 font-mono select-none"
                              style={{ width: `${HEAT_HOUR_LABEL_PX}px` }}
                            >
                              {row.date.slice(5)}
                            </span>
                            <div
                              className="grid flex-1"
                              style={{
                                gridTemplateColumns: `repeat(${HEAT_HOURS}, minmax(0, 1fr))`,
                                columnGap: `${HEAT_HOUR_GAP_PX}px`,
                              }}
                            >
                              {row.tokens.map((tokens, hour) => {
                                const sessions = row.sessions[hour];
                                const level = hourLevel(heatMetric === 'tokens' ? tokens : sessions);
                                return (
                                  <div
                                    key={hour}
                                    className={`h-4 rounded-[3px] border cursor-pointer transition-transform hover:scale-110 hover:z-20 hover:outline hover:outline-[1.5px] hover:outline-gray-900 dark:hover:outline-white ${heatColors[level]}`}
                                    onMouseEnter={(e) => {
                                      const rect = e.currentTarget.getBoundingClientRect();
                                      const rows = [
                                        { label: 'Token 消耗', val: fmtTokens(tokens) },
                                        { label: '会话数', val: `${sessions} 次` },
                                      ];
                                      if (heatMetric === 'loc') {
                                        rows.push({ label: '代码变更行数', val: '— (无数据来源)' });
                                      }
                                      setTooltip({
                                        visible: true,
                                        x: rect.left,
                                        y: rect.top - 76,
                                        title: `${fmtCalendarDate(row.date)} ${String(hour).padStart(2, '0')}:00–${String(
                                          (hour + 1) % 24,
                                        ).padStart(2, '0')}:00（${stats.heatmap.hourly.timezone}）`,
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
                        ))}
                        <div className="flex items-center justify-end gap-1.5 pt-2 text-[11px] text-gray-500 dark:text-gray-400 select-none">
                          <span>较少</span>
                          <div className="flex items-center gap-[3px]">
                            {heatColors.map((swatch) => (
                              <div
                                key={swatch}
                                className={`rounded-[2px] border ${swatch}`}
                                style={{ width: `${HEAT_CELL_PX}px`, height: `${HEAT_CELL_PX}px` }}
                              />
                            ))}
                          </div>
                          <span>重度使用</span>
                        </div>
                      </>
                  )}
                </div>
                )}
              </div>

              {/* 第四层：核心双图表（分层堆叠趋势 + 环形剖析图，严格对齐原型） */}
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1.8fr_1.2fr]">
                {/* 左侧：每日 Token 消耗与优化分层堆叠面积图 */}
                <div className="rounded-card border border-line/70 bg-surface p-4 space-y-3 shadow-card">
                  <div className="flex items-center justify-between">
                    <div>
                      <h2 className="text-sm font-bold text-gray-900">
                        {stats.trend.title || "每日 Token 消耗与节约构成"}
                      </h2>
                      <p className="text-xs text-gray-500 dark:text-gray-400">
                        {stats.trend.subtitle || "分层解析：Prompt 命中 / 全新输入 / 推理思考 / 节约对比"}
                      </p>
                    </div>
                    <div className="flex items-center gap-3 text-[11px] font-mono text-gray-600 dark:text-gray-400 select-none flex-wrap justify-end">
                      <span className="flex items-center gap-1">
                        <span className="h-2 w-2 rounded-[2px] bg-emerald-500" />
                        缓存命中
                      </span>
                      <span className="flex items-center gap-1">
                        <span className="h-2 w-2 rounded-[2px] bg-blue-600" />
                        未命中输入
                      </span>
                      <span className="flex items-center gap-1">
                        <span className="h-2 w-2 rounded-[2px] bg-purple-500" />
                        思考推理
                      </span>
                      <span className="flex items-center gap-1">
                        <span className="h-2 w-2 rounded-[2px] bg-amber-500" />
                        正文输出
                      </span>
                      <span className="flex items-center gap-1">
                        <span className="w-3 h-0.5 border-t border-dashed border-gray-400 dark:border-gray-500" />
                        未压缩基线
                      </span>
                    </div>
                  </div>

                  <div className="relative h-60 w-full">
                    {trendPaths === null ? (
                      <div className="flex h-full items-center justify-center text-xs text-gray-400">
                        当前区间无趋势数据
                      </div>
                    ) : (
                      <svg
                        className="h-full w-full overflow-visible"
                        viewBox="0 0 600 220"
                        preserveAspectRatio="none"
                      >
                        {/* 背景水平网格虚线与 Y 轴刻度 */}
                        {trendGridTicks.map((tick, idx) => (
                          <g key={idx}>
                            <line
                              x1={padL}
                              x2={trendW - padR}
                              y1={tick.y}
                              y2={tick.y}
                              stroke="currentColor"
                              className="text-line/60"
                              strokeDasharray="3 3"
                            />
                            <text
                              x={padL - 8}
                              y={tick.y + 3}
                              textAnchor="end"
                              fontSize="10"
                              fill="currentColor"
                              className="text-gray-400 dark:text-gray-500 font-mono"
                            >
                              {tick.val >= 10000 ? `${Math.round(tick.val / 10000)}w` : tick.val}
                            </text>
                          </g>
                        ))}

                        {/* 从底至顶堆叠面积层（颜色取自主题角色，见上方图例） */}
                        <path
                          d={trendPaths.dCache}
                          className="text-emerald-500"
                          fill="currentColor"
                          fillOpacity="0.75"
                        />
                        <path
                          d={trendPaths.dInput}
                          className="text-blue-600"
                          fill="currentColor"
                          fillOpacity="0.85"
                        />
                        <path
                          d={trendPaths.dOut}
                          className="text-amber-500"
                          fill="currentColor"
                          fillOpacity="0.9"
                        />

                        {/* 顶层：未压缩应耗基线虚线折线 */}
                        <path
                          d={trendPaths.dRaw}
                          fill="none"
                          stroke="currentColor"
                          className="text-gray-400 dark:text-gray-500"
                          strokeWidth="1.8"
                          strokeDasharray="4 4"
                        />

                        {/* 数据点圆圈与交互热区 */}
                        {trendItems.map((d, i) => {
                          const x = padL + (trendItems.length > 1 ? (i / (trendItems.length - 1)) * plotW : plotW / 2);
                          const yPoint = trendPaths.pRaw[i]?.y ?? 0;
                          return (
                            <g key={i} className="cursor-pointer group">
                              {/* X 轴日期 */}
                              <text
                                x={x}
                                y={trendH - 8}
                                textAnchor="middle"
                                fontSize="10.5"
                                fill="currentColor"
                                className="text-gray-400 dark:text-gray-500 font-mono select-none"
                              >
                                {d.date}
                              </text>
                              {/* 数据点：琥珀色圆环 + 卡片底色的实心内芯 */}
                              <g className="text-amber-500 transition-transform group-hover:scale-150">
                                <circle cx={x} cy={yPoint} r="3.5" fill="currentColor" />
                                <circle
                                  cx={x}
                                  cy={yPoint}
                                  r="1.6"
                                  className="text-surface"
                                  fill="currentColor"
                                />
                              </g>
                              {/* 透明感应交互纵条 */}
                              <rect
                                x={x - 14}
                                y={padT}
                                width="28"
                                height={plotH}
                                fill="transparent"
                                onMouseEnter={(e) => {
                                  const rect = e.currentTarget.getBoundingClientRect();
                                  setTooltip({
                                    visible: true,
                                    x: rect.left,
                                    y: rect.top - 120,
                                    title: `${d.date} 用量构成`,
                                    rows: [
                                      {
                                        label: "缓存命中",
                                        val: fmtTokens(d.cache),
                                        color: "rgb(var(--emerald-500))",
                                      },
                                      {
                                        label: "未命中输入",
                                        val: fmtTokens(d.input),
                                        color: "rgb(var(--blue-600))",
                                      },
                                      {
                                        label: "正文输出",
                                        val: fmtTokens(d.output),
                                        color: "rgb(var(--amber-500))",
                                      },
                                      {
                                        label: "未压缩基线",
                                        val: fmtTokens(d.raw),
                                        color: "rgb(var(--gray-400))",
                                      },
                                    ],
                                  });
                                }}
                                onMouseLeave={() =>
                                  setTooltip((t) => ({ ...t, visible: false }))
                                }
                              />
                            </g>
                          );
                        })}
                      </svg>
                    )}
                  </div>
                </div>

                {/* 右侧：多维环形剖析图 */}
                <div className="rounded-card border border-line/70 bg-surface p-4 space-y-3 shadow-card">
                  <div className="flex items-center justify-between">
                    <div>
                      <h2 className="text-sm font-bold text-gray-900">
                        {breakdownDim === "project"
                          ? "项目工作区用量分布"
                          : breakdownDim === "model"
                          ? "模型用量分布"
                          : "Subagent 角色分布"}
                      </h2>
                      <p className="text-xs text-gray-500 dark:text-gray-400">用量与计费占比分解</p>
                    </div>
                    <div className="inline-flex rounded-control border border-line/60 bg-sunken p-0.5 text-xs">
                      {(
                        [
                          ["project", "按项目"],
                          ["model", "按模型"],
                          ["agent", "按 Subagent"],
                        ] as const
                      ).map(([key, label]) => (
                        <button
                          key={key}
                          type="button"
                          onClick={() => handleBreakdownDimChange(key)}
                          className={`rounded-control px-2.5 py-1 text-[11.5px] transition-all ${
                            breakdownDim === key
                              ? "bg-surface font-semibold text-gray-900 shadow-card"
                              : "text-gray-500 dark:text-gray-400 hover:text-gray-900"
                          }`}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                  </div>

                  {breakdownList === null || breakdownList.length === 0 ? (
                    <div className="flex h-48 items-center justify-center text-xs text-gray-400">
                      {breakdownDim === "agent"
                        ? "Subagent 角色无独立遥测来源"
                        : "当前区间无 Token 数据"}
                    </div>
                  ) : (
                    <div className="flex items-center gap-5 pt-4">
                      {/* Donut 环形图 */}
                      <div className="relative h-[140px] w-[140px] shrink-0">
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
                                strokeWidth="16"
                                strokeDasharray={`${dashLen} ${C - dashLen}`}
                                strokeDashoffset={`-${(item.offset / 100) * C}`}
                                className="transition-all hover:stroke-[20px]"
                              />
                            );
                          })}
                        </svg>
                        <div className="absolute inset-0 flex flex-col items-center justify-center text-center pointer-events-none">
                          <span className="font-mono text-base font-bold text-gray-900 leading-tight">
                            {fmtTokens(stats.kpi.total_tokens)}
                          </span>
                          <span className="text-[10px] uppercase tracking-wider text-gray-400 font-mono">
                            Tokens
                          </span>
                        </div>
                      </div>

                      {/* 右侧列表项 */}
                      <div className="flex-1 flex flex-col gap-2">
                        {breakdownList.map((item) => (
                          <div
                            key={item.name}
                            className="flex items-center justify-between text-xs px-2 py-1.5 rounded-control hover:bg-sunken transition-colors"
                          >
                            <div className="flex items-center gap-2 truncate">
                              <span
                                className="h-2 w-2 rounded-full shrink-0"
                                style={{ backgroundColor: item.color }}
                              />
                              <span className="text-gray-700 dark:text-gray-200 font-medium truncate">
                                {item.name}
                              </span>
                            </div>
                            <div className="flex items-center gap-3 font-mono text-[11.5px] shrink-0">
                              <span className="text-gray-500 dark:text-gray-400">
                                {fmtTokens(item.tokens)}
                              </span>
                              <span className="text-gray-900 font-bold w-9 text-right">
                                {item.pct}%
                              </span>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              </div>

              {/* 第五层：深度可观测性（工具输出记录 + 高消耗会话排行） */}
              <div className="rounded-card border border-line/70 bg-surface p-4 space-y-2 shadow-card">
                <div className="flex items-center justify-between border-b border-line/60 pb-2">
                  <div>
                    <h2 className="text-sm font-bold text-gray-900">用量溯源：高消耗会话排行</h2>
                    <p className="text-xs text-gray-500 dark:text-gray-400">
                      排查上下文臃肿与长链调用（按 Token 排序）
                    </p>
                  </div>
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
                        className="flex items-center justify-between py-2.5 text-xs hover:bg-sunken/60 px-1 rounded-control transition-colors"
                      >
                        <div className="max-w-[75%] space-y-1">
                          <div className="truncate font-medium text-gray-900 text-[12.5px]">
                            {sess.title}
                          </div>
                          <div className="flex items-center gap-2 font-mono text-[10.5px] text-gray-500 dark:text-gray-400">
                            <span className="rounded-control bg-sunken px-1.5 py-0.5 border border-line/50">{sess.thread_id}</span>
                            <span>{sess.model ?? '—'}</span>
                            <span>{sess.turns} 轮次</span>
                          </div>
                        </div>
                        <div className="text-right font-mono shrink-0">
                          <div className="font-bold text-gray-900 text-sm">
                            {fmtTokens(sess.tokens)}
                          </div>
                          <div className="text-[11px] text-emerald-600 dark:text-emerald-400 font-semibold">
                            {fmtPct(sess.cache_rate)} 命中
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
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
