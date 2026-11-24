/**
 * Unit tests for the external ACP (`agy`) usage decoder, view formatting, and
 * lifecycle controller.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  MalformedExternalUsagePayloadError,
  decodeExternalUsageView,
  type ExternalUsageView,
} from '../src/runtime-client/externalUsage.ts';
import {
  EXTERNAL_USAGE_UNAVAILABLE,
  activeExternalModel,
  formatExternalUsageLabel,
  lowestExternalRemainingPercent,
  selectActiveGroup,
} from '../src/stores/externalUsageView.ts';
import {
  ExternalUsageController,
  type ExternalUsageContext,
  type ExternalUsageState,
} from '../src/stores/externalUsageController.ts';

const NOW_S = 1_795_470_000;

function sampleView(overrides: Partial<ExternalUsageView> = {}): ExternalUsageView {
  return {
    command_id: 'cmd-1',
    session: { project_id: 'p1', thread_id: 't1' },
    agent_id: 'agy',
    supported: true,
    captured_at: NOW_S,
    groups: [
      {
        name: 'Gemini Models',
        description: 'Models within this group: Gemini 4 Neon, Gemini 3.8 Flash',
        buckets: [
          {
            id: 'gemini-weekly',
            name: 'Weekly Limit Remaining',
            window: 'weekly',
            window_minutes: 10080,
            remaining_percent: 92.51,
            reset_at: NOW_S + 6 * 86400 + 23 * 3600,
          },
          {
            id: 'gemini-5h',
            name: 'Five Hour Limit Remaining',
            window: '5h',
            window_minutes: 300,
            remaining_percent: 70.04,
            reset_at: NOW_S + 4 * 3600 + 10 * 60,
          },
        ],
      },
      {
        name: 'Claude and GPT models',
        description: 'Models within this group: Claude Opus 5.5, GPT-OSS 120B',
        buckets: [
          {
            id: '3p-weekly',
            name: 'Weekly Limit Remaining',
            window: 'weekly',
            window_minutes: 10080,
            remaining_percent: 2.13,
            reset_at: NOW_S + 3 * 86400,
          },
          {
            id: '3p-5h',
            name: 'Five Hour Limit Remaining',
            window: '5h',
            window_minutes: 300,
            remaining_percent: 100,
            reset_at: NOW_S + 5 * 3600,
          },
        ],
      },
    ],
    ...overrides,
  };
}

test('decodeExternalUsageView validates strict wire shape and rejects extra keys', () => {
  const decoded = decodeExternalUsageView(sampleView());
  assert.equal(decoded.agent_id, 'agy');
  assert.equal(decoded.supported, true);
  assert.equal(decoded.groups.length, 2);

  assert.throws(
    () => decodeExternalUsageView({ ...sampleView(), extra: 'leak' }),
    MalformedExternalUsagePayloadError,
  );
  assert.throws(
    () =>
      decodeExternalUsageView({
        ...sampleView(),
        groups: [
          {
            name: 'Gemini Models',
            description: null,
            buckets: [
              {
                id: 'b1',
                name: '5h',
                window: '5h',
                window_minutes: 300,
                remaining_percent: 150,
                reset_at: null,
              },
            ],
          },
        ],
      }),
    MalformedExternalUsagePayloadError,
  );
});

test('formatExternalUsageLabel selects matching model group and orders 5h before 7d', () => {
  const view = sampleView();
  assert.equal(
    formatExternalUsageLabel(view, 'Gemini 4 Neon (Medium)', NOW_S),
    '5h 70%/5h · 7d 93%/7d',
  );
  assert.equal(lowestExternalRemainingPercent(view, 'Gemini 4 Neon (Medium)'), 70);

  assert.equal(
    formatExternalUsageLabel(view, 'Claude Opus 5.5 (High)', NOW_S),
    '5h 100%/5h · 7d 2%/3d',
  );
  assert.equal(lowestExternalRemainingPercent(view, 'Claude Opus 5.5 (High)'), 2);

  assert.equal(selectActiveGroup(view.groups, 'GPT-OSS 120B')?.name, 'Claude and GPT models');
  assert.equal(formatExternalUsageLabel(null, '', NOW_S), EXTERNAL_USAGE_UNAVAILABLE);
});

test('activeExternalModel reads label from category=model option', () => {
  const label = activeExternalModel([
    {
      id: 'model',
      name: 'Model',
      category: 'model',
      current_value: 'gemini-4-neon-medium',
      values: [{ value: 'gemini-4-neon-medium', label: 'Gemini 4 Neon (Medium)' }],
      bool_value: null,
    },
  ]);
  assert.equal(label, 'Gemini 4 Neon (Medium)');
});

test('ExternalUsageController loads on external agy session and hides on native or unsupported', async () => {
  let calls = 0;
  let listener: (() => void) | null = null;
  let ctx: ExternalUsageContext = {
    client: {
      async getExternalUsage(session, _force) {
        calls += 1;
        return sampleView({ session });
      },
    },
    session: { project_id: 'p1', thread_id: 't1' },
    agentId: null,
    connected: true,
  };
  const states: ExternalUsageState[] = [];
  const controller = new ExternalUsageController({
    read: () => ctx,
    subscribe: (fn) => {
      listener = fn;
      return () => {
        listener = null;
      };
    },
    onState: (s) => states.push(s),
  });

  controller.start();
  assert.equal(controller.isAvailable(), false);
  assert.equal(calls, 0);

  // Switch to an agy external session
  ctx = { ...ctx, agentId: 'agy' };
  listener?.();
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(controller.isAvailable(), true);
  assert.equal(calls, 1);

  // Switch back to native session -> hidden immediately
  ctx = { ...ctx, agentId: null };
  listener?.();
  assert.equal(controller.isAvailable(), false);

  // Switch to unsupported external agent -> stays hidden
  ctx = {
    ...ctx,
    agentId: 'other',
    client: {
      async getExternalUsage(session) {
        calls += 1;
        return sampleView({ session, agent_id: 'other', supported: false, groups: [] });
      },
    },
  };
  listener?.();
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(controller.isAvailable(), false);
  assert.equal(calls, 2);

  controller.stop();
});
