/**
 * Offline tests for the goal status-bar helpers (no DOM, no socket).
 *
 * `runtime.session.goal` answers either the goal projection or `null`; the
 * formatters must therefore tolerate both shapes and every malformed payload
 * without throwing, and must render nothing for an absent goal.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  formatElapsed,
  goalLabel,
  goalTooltip,
  parseSessionGoal,
  type SessionGoalView,
} from '../src/stores/goalView.ts';

function goal(overrides: Partial<SessionGoalView> = {}): SessionGoalView {
  return {
    thread_id: 't',
    goal_id: 'g1',
    status: 'active',
    label: 'active',
    objective: 'ship the thing',
    token_budget: null,
    tokens_used: 0,
    time_used_seconds: 0,
    ...overrides,
  };
}

test('formatElapsed renders seconds, minutes and hours', () => {
  assert.equal(formatElapsed(0), '0s');
  assert.equal(formatElapsed(42), '42s');
  assert.equal(formatElapsed(59), '59s');
  assert.equal(formatElapsed(60), '1m00s');
  assert.equal(formatElapsed(192), '3m12s');
  assert.equal(formatElapsed(3599), '59m59s');
  assert.equal(formatElapsed(3600), '1h00m');
  assert.equal(formatElapsed(3900), '1h05m');
});

test('formatElapsed clamps negative and fractional input', () => {
  assert.equal(formatElapsed(-5), '0s');
  assert.equal(formatElapsed(1.9), '1s');
  assert.equal(formatElapsed(61.7), '1m01s');
});

test('goalLabel renders nothing without a goal', () => {
  assert.equal(goalLabel(null), '');
});

test('goalLabel shows budget usage while active', () => {
  assert.equal(goalLabel(goal({ token_budget: 1000, tokens_used: 250 })), 'goal·active 250/1.0k');
});

test('goalLabel falls back to elapsed time when there is no budget', () => {
  assert.equal(goalLabel(goal({ time_used_seconds: 42 })), 'goal·active 42s');
});

test('goalLabel shows the token count for a complete goal', () => {
  assert.equal(goalLabel(goal({ label: 'complete', tokens_used: 1234 })), 'goal·complete 1.2k');
});

test('goalLabel shows only the status for the remaining states', () => {
  assert.equal(goalLabel(goal({ label: 'paused' })), 'goal·paused');
  assert.equal(goalLabel(goal({ label: 'stalled' })), 'goal·stalled');
  assert.equal(goalLabel(goal({ label: 'limited by budget', token_budget: 5 })), 'goal·limited by budget');
});

test('goalTooltip carries the objective and the raw counters', () => {
  assert.equal(goalTooltip(null), '');
  assert.equal(
    goalTooltip(goal({ token_budget: 1000, tokens_used: 250, time_used_seconds: 42 })),
    'ship the thing\n预算 250 / 1.0k tokens · 42s',
  );
  assert.equal(
    goalTooltip(goal({ tokens_used: 250, time_used_seconds: 42 })),
    'ship the thing\n用量 250 tokens · 42s',
  );
});

test('parseSessionGoal copies only the whitelisted fields', () => {
  assert.deepEqual(
    parseSessionGoal({
      thread_id: 't',
      goal_id: 'g1',
      status: 'active',
      label: 'active',
      objective: 'ship it',
      token_budget: 1000,
      tokens_used: 250,
      time_used_seconds: 42,
      secret: 'must not be copied',
    }),
    goal({ objective: 'ship it', token_budget: 1000, tokens_used: 250, time_used_seconds: 42 }),
  );
});

test('parseSessionGoal reports a missing goal as null', () => {
  assert.equal(parseSessionGoal(null), null);
  assert.equal(parseSessionGoal(undefined), null);
  assert.equal(parseSessionGoal('nope'), null);
  assert.equal(parseSessionGoal(7), null);
});

test('parseSessionGoal tolerates malformed fields without throwing', () => {
  assert.deepEqual(parseSessionGoal({}), {
    thread_id: '',
    goal_id: '',
    status: '',
    label: '',
    objective: '',
    token_budget: null,
    tokens_used: 0,
    time_used_seconds: 0,
  });
  const malformed = parseSessionGoal({
    token_budget: 'lots',
    tokens_used: 'many',
    time_used_seconds: Number.NaN,
  });
  assert.notEqual(malformed, null);
  assert.equal(malformed?.token_budget, null);
  assert.equal(malformed?.tokens_used, 0);
  assert.equal(malformed?.time_used_seconds, 0);
  // A non-finite budget is an absent budget, not an infinite one.
  assert.equal(parseSessionGoal({ token_budget: Number.POSITIVE_INFINITY })?.token_budget, null);
  assert.equal(parseSessionGoal({ token_budget: 12.7 })?.token_budget, 12);
});
