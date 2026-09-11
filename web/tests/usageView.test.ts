/**
 * Offline tests for the `usage_updated` formatting helpers (no DOM, no socket).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  compactCount,
  EMPTY_USAGE,
  formatUsageMetrics,
  fullCount,
  parseUsagePayload,
  turnStatSegments,
  usageSegments,
  usageTooltip,
  type UsageView,
} from '../src/stores/usageView.ts';

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
    '输入 20,612 · 输出 752',
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
  assert.deepEqual(lines, ['输入 0 · 输出 5']);
});
