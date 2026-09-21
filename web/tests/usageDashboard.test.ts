/**
 * Contract tests for the usage-dashboard client.
 *
 * The panel shows real telemetry or nothing: `fetchUsageStats` never falls back
 * to demo data, so these tests pin the three honest failure modes (a non-2xx
 * response, an aborted request, a malformed body) plus the strict payload
 * validator that keeps `undefined` out of the UI. The "filters" tests assert the
 * exact query each selection produces, which is the behaviour the panel's single
 * request effect relies on.
 *
 * No host, no DOM: `fetch` is a fake.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  fetchUsageStats,
  parseUsageStatsPayload,
  UsageStatsPayloadError,
  UsageStatsRequestError,
} from '../src/client/usageStats.ts';

const recorded: string[] = [];

function installFetch(handler: (url: string, init?: RequestInit) => Response): void {
  (globalThis as any).fetch = (url: unknown, init?: RequestInit) => {
    recorded.push(String(url));
    return Promise.resolve(handler(String(url), init));
  };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const EMPTY_PAYLOAD = {
  generated_at: '2026-09-21T00:00:00+00:00',
  available_models: [],
  selected_model: 'all',
  range: { key: '7d', start: '2026-09-15', end: '2026-09-21' },
  project: {
    selected: 'all',
    connected_count: 1,
    current: { name: 'synapse', path: '/w/synapse', branch: 'main', dirty: false },
  },
  kpi: {
    total_tokens: 0,
    provider_input_tokens: 0,
    net_input_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    output_tokens: 0,
    cache_hit_rate: null,
    saved_tokens: 0,
    saved_pct: null,
    call_count: 0,
    turn_count: 0,
    active_duration_ms: 0,
    estimated_cost: null,
    loc_added: null,
    loc_removed: null,
  },
  project_matrix: [
    {
      name: 'synapse',
      is_current: true,
      path: '/w/synapse',
      branch: 'main',
      dirty: false,
      tokens: 0,
      share_pct: 0,
      cost: null,
      sessions_count: 0,
      turns_count: 0,
      lines_added: null,
      lines_removed: null,
      efficiency: null,
    },
  ],
  heatmap: {
    days: [],
    truncated: false,
    hourly: { rows: [], truncated: false, timezone: 'UTC+08:00' },
  },
  trend: { range_key: '7d', granularity: 'day', title: 't', subtitle: 's', items: [] },
  breakdowns: { project: [], model: [], agent: null },
  top_tools: {
    supported: true,
    partial: true,
    source: 'tool_output_refs',
    scope_note: 'note',
    recorded_total: 0,
    items: [],
  },
  top_sessions: [],
  notes: ['net input口径'],
};

const POPULATED_PAYLOAD = {
  ...EMPTY_PAYLOAD,
  range: { key: 'custom', start: '2026-09-01', end: '2026-09-21' },
  kpi: {
    ...EMPTY_PAYLOAD.kpi,
    total_tokens: 2000,
    provider_input_tokens: 1200,
    net_input_tokens: 400,
    cache_read_tokens: 700,
    cache_write_tokens: 100,
    output_tokens: 800,
    cache_hit_rate: 58.3,
    saved_tokens: 250,
    saved_pct: 40.0,
    call_count: 3,
    turn_count: 2,
    active_duration_ms: 5400,
  },
  heatmap: {
    truncated: false,
    days: [
      { date: '2026-09-15', tokens: 600, sessions: 1 },
      { date: '2026-09-16', tokens: 600, sessions: 2 },
    ],
    hourly: {
      truncated: false,
      timezone: 'UTC+08:00',
      rows: [
        {
          date: '2026-09-15',
          tokens: [...Array(24)].map((_, hour) => (hour === 1 ? 600 : 0)),
          sessions: [...Array(24)].map((_, hour) => (hour === 1 ? 1 : 0)),
        },
      ],
    },
  },
  trend: {
    range_key: 'custom',
    granularity: 'day',
    title: 't',
    subtitle: 's',
    items: [{ date: '09-15', cache: 700, input: 500, output: 800, raw: 1500 }],
  },
  breakdowns: {
    project: [{ name: 'synapse', tokens: 1200, pct: 100, color: '#0078d4', offset: 0, dash: 100 }],
    model: [{ name: 'gpt', tokens: 1200, pct: 100, color: '#0078d4', offset: 0, dash: 100 }],
    agent: null,
  },
  top_tools: {
    ...EMPTY_PAYLOAD.top_tools,
    recorded_total: 4,
    items: [
      {
        name: 'read_file',
        count: 4,
        success_count: 3,
        failure_count: 1,
        success_rate: 75,
        avg_ms: null,
      },
    ],
  },
  top_sessions: [
    { thread_id: 'abcdef123456', title: '会话', model: 'gpt', turns: 2, tokens: 1200, cache_rate: 58.3 },
  ],
};

test('a valid payload round-trips through the strict parser', () => {
  assert.deepEqual(parseUsageStatsPayload(POPULATED_PAYLOAD), POPULATED_PAYLOAD);
});

test('an empty payload is valid and reports real zeros, not demo data', () => {
  const parsed = parseUsageStatsPayload(EMPTY_PAYLOAD);
  assert.equal(parsed.kpi.total_tokens, 0);
  assert.equal(parsed.kpi.call_count, 0);
  assert.equal(parsed.kpi.cache_hit_rate, null);
  assert.equal(parsed.kpi.estimated_cost, null);
  assert.deepEqual(parsed.heatmap.days, []);
  assert.equal(parsed.heatmap.truncated, false);
  assert.deepEqual(parsed.heatmap.hourly.rows, []);
  assert.equal(parsed.heatmap.hourly.truncated, false);
  assert.equal(parsed.heatmap.hourly.timezone, 'UTC+08:00');
  assert.equal(parsed.breakdowns.agent, null);
});

test('a heatmap truncated flag and provider input are carried through the parser', () => {
  const wide = {
    ...POPULATED_PAYLOAD,
    kpi: { ...POPULATED_PAYLOAD.kpi, provider_input_tokens: 1200 },
    heatmap: { ...POPULATED_PAYLOAD.heatmap, truncated: true },
  };
  const parsed = parseUsageStatsPayload(wide);
  assert.equal(parsed.kpi.provider_input_tokens, 1200);
  assert.equal(parsed.heatmap.truncated, true);
});

test('unknown cost / loc / tool duration must be null, never a number', () => {
  const fabricated = {
    ...EMPTY_PAYLOAD,
    kpi: { ...EMPTY_PAYLOAD.kpi, estimated_cost: 1.42 },
  };
  assert.throws(() => parseUsageStatsPayload(fabricated), UsageStatsPayloadError);
  const fabricatedLoc = { ...EMPTY_PAYLOAD, kpi: { ...EMPTY_PAYLOAD.kpi, loc_added: 8755 } };
  assert.throws(() => parseUsageStatsPayload(fabricatedLoc), UsageStatsPayloadError);
  const fabricatedMs = {
    ...EMPTY_PAYLOAD,
    top_tools: {
      ...EMPTY_PAYLOAD.top_tools,
      items: [
        { name: 'x', count: 1, success_count: 1, failure_count: 0, success_rate: 100, avg_ms: 18 },
      ],
    },
  };
  assert.throws(() => parseUsageStatsPayload(fabricatedMs), UsageStatsPayloadError);
});

test('a truncated or drifting payload is rejected field by field', () => {
  const cases: unknown[] = [
    {},
    { ...EMPTY_PAYLOAD, kpi: {} },
    { ...EMPTY_PAYLOAD, trend: undefined },
    { ...EMPTY_PAYLOAD, project_matrix: 'nope' },
    { ...EMPTY_PAYLOAD, heatmap: { days: [{ date: '2026-09-15' }] } },
    { ...EMPTY_PAYLOAD, trend: { ...EMPTY_PAYLOAD.trend, granularity: 'week' } },
    { ...EMPTY_PAYLOAD, breakdowns: { project: [], model: [], agent: [{ name: 'a' }] } },
    { ...EMPTY_PAYLOAD, kpi: { ...EMPTY_PAYLOAD.kpi, total_tokens: 'lots' } },
    { ...EMPTY_PAYLOAD, kpi: { ...EMPTY_PAYLOAD.kpi, provider_input_tokens: 'lots' } },
    { ...EMPTY_PAYLOAD, heatmap: { days: [] } },
    { ...EMPTY_PAYLOAD, heatmap: { days: [], truncated: 'yes' } },
    { ...EMPTY_PAYLOAD, heatmap: { days: [], truncated: false } },
    {
      ...EMPTY_PAYLOAD,
      heatmap: {
        days: [],
        truncated: false,
        hourly: { truncated: false, rows: [], timezone: 8 },
      },
    },
    {
      ...EMPTY_PAYLOAD,
      heatmap: {
        days: [],
        truncated: false,
        hourly: {
          truncated: false,
          timezone: 'UTC+08:00',
          rows: [{ date: '2026-09-15', tokens: [1, 2, 3], sessions: [] }],
        },
      },
    },
  ];
  for (const payload of cases) {
    assert.throws(() => parseUsageStatsPayload(payload), UsageStatsPayloadError, JSON.stringify(payload));
  }
});

test('the request carries exactly the declared filters', async () => {
  installFetch(() => jsonResponse(EMPTY_PAYLOAD));
  recorded.length = 0;
  await fetchUsageStats({ project: 'synapse', range: 'custom', start: '2026-01-01', end: '2026-01-31' });
  assert.equal(
    recorded[0],
    '/api/usage-stats?project=synapse&range=custom&start=2026-01-01&end=2026-01-31',
  );
});

test('omitted filters are never sent as empty parameters', async () => {
  installFetch(() => jsonResponse(EMPTY_PAYLOAD));
  recorded.length = 0;
  await fetchUsageStats();
  assert.equal(recorded[0], '/api/usage-stats');
  await fetchUsageStats({ range: '7d', project: 'all' });
  assert.equal(recorded[1], '/api/usage-stats?project=all&range=7d');
  await fetchUsageStats({ model: 'all' });
  assert.equal(recorded[2], '/api/usage-stats');
  await fetchUsageStats({ model: 'deepseek-v4-flash' });
  assert.equal(recorded[3], '/api/usage-stats?model=deepseek-v4-flash');
});

test('each range selection maps to its own request', async () => {
  installFetch(() => jsonResponse(EMPTY_PAYLOAD));
  recorded.length = 0;
  for (const range of ['today', '7d', '30d', 'all'] as const) {
    await fetchUsageStats({ range });
  }
  assert.deepEqual(recorded, [
    '/api/usage-stats?range=today',
    '/api/usage-stats?range=7d',
    '/api/usage-stats?range=30d',
    '/api/usage-stats?range=all',
  ]);
});

test('a non-2xx response rejects with its status instead of a fallback payload', async () => {
  installFetch(() => new Response('bad request', { status: 400 }));
  await assert.rejects(
    () => fetchUsageStats({ range: 'custom', start: 'x', end: 'y' }),
    (err: unknown) => err instanceof UsageStatsRequestError && err.status === 400,
  );
});

test('a malformed body rejects instead of reaching the panel half-shaped', async () => {
  installFetch(() => jsonResponse({ kpi: { total_tokens: 1 } }));
  await assert.rejects(() => fetchUsageStats(), UsageStatsPayloadError);
});

test('a non-JSON body rejects as a payload error', async () => {
  installFetch(() => new Response('<html>', { status: 200 }));
  await assert.rejects(() => fetchUsageStats(), UsageStatsPayloadError);
});

test('an aborted request rejects with AbortError so a stale reply is dropped', async () => {
  const controller = new AbortController();
  (globalThis as any).fetch = (_url: unknown, init?: RequestInit) =>
    new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => {
        reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
      });
    });
  const pending = fetchUsageStats({ range: '7d' }, controller.signal);
  controller.abort();
  await assert.rejects(pending, (err: Error) => err.name === 'AbortError');
});
