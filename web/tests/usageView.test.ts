/**
 * Offline tests for the `usage_updated` formatting helpers (no DOM, no socket).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  cacheHitRate,
  compactCount,
  compactSegments,
  contextOccupancy,
  EMPTY_USAGE,
  formatSessionUsage,
  formatUsageMetrics,
  fullCount,
  parseSessionUsage,
  parseUsagePayload,
  sessionUsageSegments,
  turnStatSegments,
  usageSegments,
  usageTooltip,
  type UsageView,
} from '../src/stores/usageView.ts';

test('parseSessionUsage reads the session view totals and drops an unused session', () => {
  assert.deepEqual(
    parseSessionUsage({ input_tokens: 20327, output_tokens: 93, cache_tokens: 19840 }),
    { input: 20327, output: 93, cache: 19840 },
  );
  // All zeros is "this session has not run a turn yet", not a real total.
  assert.equal(parseSessionUsage({ input_tokens: 0, output_tokens: 0, cache_tokens: 0 }), null);
  assert.equal(parseSessionUsage(null), null);
  assert.equal(parseSessionUsage(undefined), null);
  assert.equal(parseSessionUsage('nope'), null);
});

test('the bar prints two unlabelled groups: totals, then context/window share', () => {
  assert.deepEqual(sessionUsageSegments(null), []);
  const segments = sessionUsageSegments(
    { input: 20327, output: 93, cache: 19840 },
    45000,
    200000,
  );
  assert.equal(segments.length, 2);
  // The TUI's order: input/cache/output, then context occupancy and how much of
  // the model window it takes.  Raw numbers, no label, no tooltip.
  assert.deepEqual(
    segments.map((segment) => [segment.label, segment.value]),
    [
      ['', '20.3k/19.8k/93/97.6%'],
      ['', '45.0k/23%'],
    ],
  );
  assert.equal(formatSessionUsage({ input: 20327, output: 93, cache: 19840 }), 'in 20,327 · cache 19,840 · out 93');
  assert.equal(formatSessionUsage(null), '-');
});

test('the context group degrades to whichever half is known', () => {
  // Context but no window: the count is printed, the share is not invented.
  assert.equal(sessionUsageSegments(null, 1234)[0].value, '1.2k');
  assert.equal(sessionUsageSegments(null, 1234, 100000)[0].value, '1.2k/1%');
  // Neither: nothing to print, so the placeholder keeps its job.
  assert.deepEqual(sessionUsageSegments(null, 0), []);
});

test('the cache hit rate is appended to the totals, and omitted when unknown', () => {
  assert.equal(cacheHitRate(null), null);
  assert.equal(cacheHitRate({ input: 0, output: 5, cache: 0 }), null);
  assert.equal(cacheHitRate({ input: 20327, output: 93, cache: 19840 }), '97.6%');
  // No input to divide by: three numbers, no trailing placeholder.
  assert.equal(sessionUsageSegments({ input: 0, output: 5, cache: 0 })[0].value, '0/0/5');
});

test('the context occupancy falls back to the last call input', () => {
  // The runtime never sends `context_size`, so the last call's prompt is the
  // occupancy — the same quantity the TUI labels its context with.
  assert.equal(contextOccupancy(null), null);
  assert.equal(contextOccupancy({ ...EMPTY_USAGE, lastInput: 20978 }), 20978);
  // An explicit metric wins when a runtime does send one.
  assert.equal(contextOccupancy({ ...EMPTY_USAGE, contextSize: 45000, lastInput: 20978 }), 45000);
  assert.equal(contextOccupancy(EMPTY_USAGE), null);
});

test('the narrow strip prints the emphasized metrics first, and nothing is lost', () => {
  const segments = [
    { key: 'steps', label: '', value: '3 步', emphasis: false },
    { key: 'rate', label: '', value: '42.0 tok/s', emphasis: true },
    { key: 'context', label: '', value: '45.0k/23%', emphasis: true },
    { key: 'session', label: '', value: '20.3k/19.8k/93', emphasis: false },
  ] as const;
  // Emphasized first, then the rest in their own order, capped at the limit.
  assert.deepEqual(
    compactSegments([...segments]).map((segment) => segment.key),
    ['rate', 'context'],
  );
  assert.deepEqual(
    compactSegments([...segments], 3).map((segment) => segment.key),
    ['rate', 'context', 'steps'],
  );
  // Nothing is dropped when the list already fits: the compact form never
  // reorders a short list.
  assert.deepEqual(
    compactSegments([...segments].slice(0, 2)).map((segment) => segment.key),
    ['steps', 'rate'],
  );
  assert.deepEqual(compactSegments([]), []);
});

test('the usage tooltip leads with the session totals when they exist', () => {
  const usage: UsageView = { ...EMPTY_USAGE, turnInput: 10, turnOutput: 2 };
  assert.equal(usageTooltip(usage), '本轮 输入 10 · 输出 2');
  const withSession = usageTooltip(usage, { input: 20327, output: 93, cache: 19840 });
  assert.equal(
    withSession.split('\n')[0],
    '本会话累计 in 20,327 / cache 19,840 / out 93',
  );
  // The session line survives with no turn telemetry at all (idle bar).
  assert.equal(usageTooltip(null, { input: 5, output: 1, cache: 0 }), '本会话累计 in 5 / cache 0 / out 1');
});

test('compactCount scales token counts', () => {
  assert.equal(compactCount(0), '0');
  assert.equal(compactCount(999), '999');
  assert.equal(compactCount(1234), '1.2k');
  assert.equal(compactCount(45000), '45.0k');
  assert.equal(compactCount(1234567), '1.2M');
});

test('parseUsagePayload reads the snake_case wire fields and defaults the rest', () => {
  const usage = parseUsagePayload({
    turn_input: 10,
    turn_output: 20,
    turn_cache: 5,
    last_input: 1,
    last_output: 2,
    last_cache: 3,
    output_tokens_per_second: 12.5,
    ttft_s: 0.4,
    rate_basis: 'first_token',
    rate_estimated: true,
    context_size: 8000,
    model_calls: 4,
  });
  assert.deepEqual(usage, {
    turnInput: 10,
    turnOutput: 20,
    turnCache: 5,
    lastInput: 1,
    lastOutput: 2,
    lastCache: 3,
    outputTokensPerSecond: 12.5,
    ttftS: 0.4,
    rateBasis: 'first_token',
    rateEstimated: true,
    contextSize: 8000,
    modelCalls: 4,
  });
});

test('parseUsagePayload ignores malformed values instead of throwing', () => {
  const usage = parseUsagePayload({
    turn_input: 'many',
    output_tokens_per_second: Number.NaN,
    context_size: null,
    rate_basis: 7,
  });
  assert.equal(usage.turnInput, 0);
  assert.equal(usage.outputTokensPerSecond, null);
  assert.equal(usage.contextSize, null);
  assert.equal(usage.rateBasis, 'end_to_end');
  assert.equal(usage.rateEstimated, false);
});

test('formatUsageMetrics renders nothing for a missing usage', () => {
  assert.equal(formatUsageMetrics(null), '');
});

test('formatUsageMetrics renders nothing when every metric is zero', () => {
  const usage: UsageView = {
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
  assert.equal(formatUsageMetrics(usage), '');
});

test('formatUsageMetrics joins only the metrics that are present', () => {
  const usage: UsageView = {
    turnInput: 1234,
    turnOutput: 4321,
    turnCache: 0,
    lastInput: 0,
    lastOutput: 0,
    lastCache: 0,
    outputTokensPerSecond: 32.5,
    ttftS: null,
    rateBasis: 'end_to_end',
    rateEstimated: false,
    contextSize: 45000,
    modelCalls: 3,
  };
  assert.equal(
    formatUsageMetrics(usage),
    'up 1.2k down 4.3k - ctx 45.0k - 32.5 tok/s - 3 steps',
  );
});

test('formatUsageMetrics marks an estimated rate with a trailing tilde', () => {
  const usage: UsageView = {
    turnInput: 0,
    turnOutput: 10,
    turnCache: 0,
    lastInput: 0,
    lastOutput: 0,
    lastCache: 0,
    outputTokensPerSecond: 20,
    ttftS: null,
    rateBasis: 'end_to_end',
    rateEstimated: true,
    contextSize: null,
    modelCalls: 0,
  };
  assert.equal(formatUsageMetrics(usage), 'up 0 down 10 - 20.0 tok/s~');
});

test('fullCount groups thousands deterministically', () => {
  assert.equal(fullCount(0), '0');
  assert.equal(fullCount(752), '752');
  assert.equal(fullCount(20612), '20,612');
  assert.equal(fullCount(1234567), '1,234,567');
});

test('usageSegments returns nothing for a missing usage', () => {
  assert.deepEqual(usageSegments(null), []);
  assert.equal(usageTooltip(null), '');
});

test('usageSegments keeps only the cumulative metrics, in reading order', () => {
  const segments = usageSegments({
    ...EMPTY_USAGE,
    turnInput: 20612,
    turnOutput: 752,
    contextSize: 45000,
    outputTokensPerSecond: 327.25,
    modelCalls: 2,
  });
  assert.deepEqual(
    segments.map((segment) => segment.key),
    ['tokens', 'context'],
  );
  assert.equal(segments[0].value, '↑20.6k ↓752');
  assert.equal(segments[0].emphasis, false, 'context carries the emphasis when present');
  assert.equal(segments[1].label, '上下文');
  assert.equal(segments[1].value, '45.0k');
  assert.equal(segments[1].emphasis, true, 'context is the emphasized metric');
});

test('usageSegments emphasizes the token pair when no context metric exists', () => {
  const segments = usageSegments({
    ...EMPTY_USAGE,
    turnOutput: 10,
  });
  assert.deepEqual(
    segments.map((segment) => segment.key),
    ['tokens'],
  );
  assert.equal(segments[0].value, '↑0 ↓10');
  assert.equal(segments[0].emphasis, true, 'tokens carry the emphasis without a context metric');
});

test('turnStatSegments carries the this-turn telemetry for the footer centre', () => {
  const segments = turnStatSegments({
    ...EMPTY_USAGE,
    outputTokensPerSecond: 354.75,
    ttftS: 0.92,
    modelCalls: 3,
  });
  assert.deepEqual(
    segments.map((segment) => segment.key),
    ['rate', 'steps', 'ttft'],
  );
  assert.equal(segments[0].value, '354.8 tok/s');
  assert.equal(segments[0].emphasis, true);
  assert.equal(segments[1].value, '3 步');
  assert.equal(segments[2].label, '首字');
  assert.equal(segments[2].value, '0.92s');
});

test('turnStatSegments marks an estimated rate and stays empty without telemetry', () => {
  assert.deepEqual(turnStatSegments(null), []);
  assert.deepEqual(turnStatSegments({ ...EMPTY_USAGE, turnOutput: 1 }), []);
  const segments = turnStatSegments({
    ...EMPTY_USAGE,
    outputTokensPerSecond: 20,
    rateEstimated: true,
  });
  assert.deepEqual(
    segments.map((segment) => segment.key),
    ['rate'],
  );
  assert.equal(segments[0].value, '20.0 tok/s~');
});

test('usageTooltip carries the compressed numbers including the cache share', () => {
  const lines = usageTooltip({
    ...EMPTY_USAGE,
    turnInput: 20612,
    turnOutput: 752,
    turnCache: 19712,
    contextSize: 45000,
    outputTokensPerSecond: 327.25,
    rateEstimated: true,
    ttftS: 0.42,
    lastInput: 10,
    lastOutput: 2,
    modelCalls: 2,
  }).split('\n');
  assert.deepEqual(lines, [
    '本轮 输入 20,612 · 输出 752',
    '缓存 19,712（占输入 95.6%）',
    '上下文 45,000',
    '速率 327.3 tok/s（估算）',
    '首字 0.42s',
    '上次调用 in 10 / out 2',
    '2 步',
  ]);
});

test('usageTooltip omits a cache line when nothing was cached', () => {
  const lines = usageTooltip({ ...EMPTY_USAGE, turnOutput: 5 }).split('\n');
  assert.deepEqual(lines, ['本轮 输入 0 · 输出 5']);
});
