/**
 * Workflow view helpers: strict decoding, progress counts and the resume verdict.
 *
 * Pure functions only, so this runs under `node --test` with no browser and no daemon.  The
 * assertions are about the two claims the console is allowed to make: how many calls a run
 * actually dispatched, and whether its own records permit continuing it.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  formatWorkflowDuration,
  formatWorkflowTime,
  formatWorkflowTokens,
  MalformedWorkflowPayloadError,
  parseWorkflowRun,
  parseWorkflowRunPage,
  workflowProgress,
  workflowResumeHint,
  workflowRoleLabel,
  workflowStatusLabel,
  workflowSummary,
} from '../src/runtime-client/workflows.ts';

function run(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    run_id: 'run-1',
    workflow_id: 'wf-1',
    project_id: 'p1',
    thread_id: '',
    status: 'running',
    active: true,
    resumable: true,
    resume_blockers: [],
    blocked_calls: [],
    resume_detail: 'run running has no unresolved calls and may continue',
    calls: [],
    input_tokens: 0,
    output_tokens: 0,
    error: null,
    result: null,
    created_at: '2026-01-01T00:00:00+00:00',
    updated_at: '2026-01-01T00:00:00+00:00',
    finished_at: null,
    ...overrides,
  };
}

function call(status: string, key = 'c1'): Record<string, unknown> {
  return {
    call_key: key,
    actor_key: 'reviewer:0',
    role: 'reviewer',
    status,
    attempts: 1,
    input_tokens: 10,
    output_tokens: 5,
    error: null,
  };
}

test('a well-formed run decodes', () => {
  const parsed = parseWorkflowRun(run({ calls: [call('completed')] }));
  assert.equal(parsed.run_id, 'run-1');
  assert.equal(parsed.calls.length, 1);
  assert.equal(parsed.calls[0]?.role, 'reviewer');
});

test('a malformed run is refused instead of reaching the UI half-shaped', () => {
  assert.throws(() => parseWorkflowRun({ ...run(), status: 7 }), MalformedWorkflowPayloadError);
  assert.throws(() => parseWorkflowRun({ ...run(), calls: 'nope' }), MalformedWorkflowPayloadError);
  assert.throws(
    () => parseWorkflowRun({ ...run(), resume_blockers: [1] }),
    MalformedWorkflowPayloadError,
  );
  assert.throws(() => parseWorkflowRun(null), MalformedWorkflowPayloadError);
  assert.throws(() => parseWorkflowRunPage({ runs: {}, total: 0 }), MalformedWorkflowPayloadError);
});

test('progress counts calls, never a percentage', () => {
  const parsed = parseWorkflowRun(
    run({
      calls: [
        call('completed', 'a'),
        call('completed', 'b'),
        call('running', 'c'),
        call('uncertain', 'd'),
        call('failed', 'e'),
      ],
    }),
  );
  const progress = workflowProgress(parsed);
  assert.deepEqual(progress, {
    completed: 2,
    running: 1,
    uncertain: 1,
    failed: 1,
    total: 5,
  });
  // No field of the summary is a fraction of a total the run cannot know.
  const summary = workflowSummary(parsed);
  assert.match(summary, /已完成 2 项/);
  assert.match(summary, /结果不确定 1 项/);
  assert.doesNotMatch(summary, /%/);
});

test('a blocked resume reports the reason instead of offering a continue', () => {
  const blocked = parseWorkflowRun(
    run({
      status: 'uncertain',
      resumable: false,
      resume_blockers: ['uncertain_call'],
      blocked_calls: ['c1'],
      resume_detail: 'call(s) with no established outcome: c1',
    }),
  );
  const hint = workflowResumeHint(blocked);
  assert.notEqual(hint, null);
  assert.match(String(hint), /no established outcome/);
  // A finished run is not offered a resume either.
  const finished = parseWorkflowRun(run({ status: 'completed', active: false, resumable: false }));
  assert.match(String(workflowResumeHint(finished)), /已完成/);
});

test('a continuable run offers no hint at all', () => {
  assert.equal(workflowResumeHint(parseWorkflowRun(run())), null);
});

test('status tokens get a label and unknown ones pass through', () => {
  assert.equal(workflowStatusLabel('uncertain'), '结果不确定');
  assert.equal(workflowStatusLabel('waiting_approval'), '等待审批');
  assert.equal(workflowStatusLabel('brand_new'), 'brand_new');
});

test('role tokens get a human-friendly label and unknown ones pass through', () => {
  assert.equal(workflowRoleLabel('reviewer'), '审阅者 (reviewer)');
  assert.equal(workflowRoleLabel('tester'), '测试者 (tester)');
  assert.equal(workflowRoleLabel('custom_worker'), 'custom_worker');
});

test('formatWorkflowTokens formats token quantities concisely', () => {
  assert.equal(formatWorkflowTokens(0), '0');
  assert.equal(formatWorkflowTokens(500), '500');
  assert.equal(formatWorkflowTokens(1500), '1.5k');
  assert.equal(formatWorkflowTokens(2000), '2k');
  assert.equal(formatWorkflowTokens(1500000), '1.5M');
});

test('formatWorkflowTime extracts concise HH:mm:ss', () => {
  assert.match(formatWorkflowTime('2026-04-18T14:30:15Z'), /^\d{2}:\d{2}:\d{2}$/);
  assert.equal(formatWorkflowTime('invalid-date'), 'invalid-date');
});

test('formatWorkflowDuration computes elapsed duration string', () => {
  const start = '2026-04-18T10:00:00Z';
  const end1 = '2026-04-18T10:00:25Z';
  const end2 = '2026-04-18T10:02:15Z';
  assert.equal(formatWorkflowDuration(start, end1), '25s');
  assert.equal(formatWorkflowDuration(start, end2), '2m 15s');
});
