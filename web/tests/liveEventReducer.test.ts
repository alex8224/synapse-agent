/**
 * Offline tests for the runtime-event reducer that drives the console
 * transcript.  No host, no daemon, no socket: every event is a plain JSON-RPC
 * notification payload, exactly as the relay forwards it.
 *
 * Coverage mirrors the TUI reference consumer
 * (`src/synapse/ui/turn/event_renderer.py`): activity phases, reasoning/answer
 * completion, the whole tool lifecycle, subagent stages, usage metrics, info
 * rows, approvals and turn terminals.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import type { RuntimeEvent } from '../src/client/types.ts';
import {
  reduceRuntimeEvent,
  toolItemFromPayload,
  type LiveReducibleState,
} from '../src/stores/liveEventReducer.ts';

const AT = new Date(0);
const now = () => AT;

function baseState(): LiveReducibleState {
  return {
    messages: [],
    activeTurnId: null,
    runtimeStatus: 'idle',
    steerQueueCount: 0,
    pendingApproval: null,
    activity: null,
    usage: null,
    metricsLabel: '',
  };
}

function event(kind: string, payload: unknown, turnId = 't1', sequence = 1): RuntimeEvent {
  return {
    sequence,
    turn_id: turnId,
    turn_sequence: sequence,
    kind,
    timestamp: '2025-01-01T00:00:00Z',
    payload: payload as Record<string, any>,
  };
}

function apply(state: LiveReducibleState, ev: RuntimeEvent): LiveReducibleState {
  return { ...state, ...reduceRuntimeEvent(state, ev, now) };
}

function toolItem(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    item_id: 'i1',
    call_id: 'c1',
    name: 'read_file',
    label: 'Read',
    category: 'filesystem',
    path: 'src/app.py',
    status: 'running',
    preview: null,
    error: false,
    sub: false,
    parent_id: null,
    ...overrides,
  };
}

test('activity_started marks the turn running and opens the activity line', () => {
  const s = apply(baseState(), event('activity_started', { phase: 'thinking', detail: 'model' }));
  assert.equal(s.runtimeStatus, 'running');
  assert.equal(s.activeTurnId, 't1');
  assert.deepEqual(s.activity, {
    phase: 'thinking',
    detail: 'model',
    startedAt: 0,
    active: true,
  });
});

test('activity_updated keeps the timer unless reset_timer is set', () => {
  let s = apply(baseState(), event('activity_started', { phase: 'thinking' }));
  const started = s.activity?.startedAt;
  s = apply(s, event('activity_updated', { phase: 'tools', detail: 'bash' }));
  assert.equal(s.activity?.phase, 'tools');
  assert.equal(s.activity?.startedAt, started);
  s = apply(s, event('activity_updated', { phase: 'thinking', reset_timer: true }));
  assert.equal(s.activity?.startedAt, 0);
});

test('activity_stopped clears the live flag without dropping the row', () => {
  let s = apply(baseState(), event('activity_started', { phase: 'thinking' }));
  s = apply(s, event('activity_stopped', { phase: 'idle' }));
  assert.equal(s.activity?.active, false);
});

test('reasoning deltas accumulate into one thought and complete with a duration', () => {
  let s = apply(baseState(), event('reasoning_delta', { text: 'a' }));
  s = apply(s, event('reasoning_delta', { text: 'b' }));
  assert.equal(s.messages.length, 1);
  assert.equal(s.messages[0].type, 'thought');
  assert.equal(s.messages[0].content, 'ab');
  assert.equal(s.messages[0].duration, 'streaming');

  s = apply(s, event('reasoning_completed', { text: 'ab' }));
  assert.equal(s.messages[0].content, 'ab');
  assert.equal(s.messages[0].duration, '0.0s');
});

test('answer_completed replaces the streamed answer with the authoritative text', () => {
  let s = apply(baseState(), event('answer_delta', { text: 'partial' }));
  s = apply(s, event('answer_completed', { text: 'the full answer' }));
  assert.equal(s.messages.length, 1);
  assert.equal(s.messages[0].type, 'assistant');
  assert.equal(s.messages[0].content, 'the full answer');
});

test('answer_completed creates the message when no delta was streamed', () => {
  const s = apply(baseState(), event('answer_completed', { text: 'only completion' }));
  assert.equal(s.messages[0].content, 'only completion');
});

test('tool_started upserts by item_id instead of duplicating rows', () => {
  let s = apply(baseState(), event('tool_started', toolItem()));
  s = apply(s, event('tool_started', toolItem({ status: 'running' })));
  assert.equal(s.messages.length, 1);
  assert.equal(s.messages[0].type, 'tool_group');
  assert.equal(s.messages[0].tools?.length, 1);
  assert.equal(s.messages[0].tools?.[0].name, 'read_file');
  assert.equal(s.messages[0].tools?.[0].path, 'src/app.py');
});

test('tool_updated merges the new status and preview into the existing row', () => {
  let s = apply(baseState(), event('tool_started', toolItem()));
  s = apply(s, event('tool_updated', toolItem({ status: 'completed', preview: 'ok' })));
  assert.equal(s.messages[0].tools?.length, 1);
  assert.equal(s.messages[0].tools?.[0].status, 'completed');
  assert.equal(s.messages[0].tools?.[0].preview, 'ok');
});

test('tool_finished patches status/preview/error by item_id', () => {
  let s = apply(baseState(), event('tool_started', toolItem()));
  s = apply(
    s,
    event('tool_finished', { item_id: 'i1', status: 'failed', preview: 'boom', error: true }),
  );
  assert.equal(s.messages[0].tools?.[0].status, 'failed');
  assert.equal(s.messages[0].tools?.[0].preview, 'boom');
  assert.equal(s.messages[0].tools?.[0].error, true);
});

test('tool_finished for an unknown item is a no-op', () => {
  const before = apply(baseState(), event('tool_started', toolItem()));
  const after = apply(before, event('tool_finished', { item_id: 'nope', status: 'completed' }));
  assert.deepEqual(after.messages, before.messages);
});

test('legacy tool_result correlates with an existing item by call_id', () => {
  let s = apply(baseState(), event('tool_started', toolItem()));
  s = apply(s, event('tool_result', { name: 'read_file', status: 'completed', call_id: 'c1' }));
  assert.equal(s.messages[0].tools?.length, 1);
  assert.equal(s.messages[0].tools?.[0].status, 'completed');
});

test('legacy tool_result without a matching item appends one', () => {
  const s = apply(baseState(), event('tool_result', { name: 'grep', status: 'completed' }));
  assert.equal(s.messages[0].tools?.length, 1);
  assert.equal(s.messages[0].tools?.[0].name, 'grep');
});

test('tool_batch_started marks the group and tool_batch_finished closes it', () => {
  let s = apply(baseState(), event('tool_batch_started', { calls: [], parallel: true }));
  assert.equal(s.messages[0].type, 'tool_group');
  assert.equal(s.messages[0].parallel, true);
  assert.equal(s.messages[0].finished, false);

  s = apply(s, event('tool_batch_finished', { group_id: 'g1' }));
  assert.equal(s.messages[0].finished, true);
});

test('tool_batch_finished never creates an empty group', () => {
  const s = apply(baseState(), event('tool_batch_finished', { group_id: 'g1' }));
  assert.deepEqual(s.messages, []);
});

test('subagent_status_changed survives a later tool_updated', () => {
  let s = apply(baseState(), event('tool_started', toolItem()));
  s = apply(s, event('subagent_status_changed', { parent_id: 'i1', status: 'reasoning' }));
  assert.equal(s.messages[0].tools?.[0].subagentStatus, 'reasoning');

  s = apply(s, event('tool_updated', toolItem({ status: 'running' })));
  assert.equal(s.messages[0].tools?.[0].subagentStatus, 'reasoning');
});

test('subagent_status_changed for an unknown parent is a no-op', () => {
  const before = apply(baseState(), event('tool_started', toolItem()));
  const after = apply(before, event('subagent_status_changed', { parent_id: 'zz', status: 'x' }));
  assert.deepEqual(after.messages, before.messages);
});

test('usage_updated fills the usage view and the TopBar metrics label', () => {
  const s = apply(
    baseState(),
    event('usage_updated', {
      turn_input: 1234,
      turn_output: 4321,
      turn_cache: 100,
      context_size: 45000,
      output_tokens_per_second: 32.5,
      model_calls: 3,
    }),
  );
  assert.equal(s.usage?.turnInput, 1234);
  assert.equal(s.usage?.modelCalls, 3);
  assert.equal(s.metricsLabel, 'up 1.2k down 4.3k - cache 100 - ctx 45.0k - 32.5 tok/s - 3 steps');
});

test('info accepts the plain-string payload the runtime actually sends', () => {
  const s = apply(baseState(), event('info', 'compacting context'));
  assert.equal(s.messages.length, 1);
  assert.equal(s.messages[0].type, 'info');
  assert.equal(s.messages[0].content, 'compacting context');
  assert.equal(s.messages[0].infoLevel, 'info');
});

test('approval_required normalizes actions with an index', () => {
  const s = apply(
    baseState(),
    event('approval_required', {
      actions: [
        { name: 'shell', args: { cmd: 'rm' }, description: 'danger', allowed_decisions: ['allow_once'] },
      ],
    }),
  );
  assert.deepEqual(s.pendingApproval, {
    turn_id: 't1',
    actions: [
      {
        index: 0,
        name: 'shell',
        args: { cmd: 'rm' },
        description: 'danger',
        allowed_decisions: ['allow_once'],
      },
    ],
  });
});

test('turn_waiting_approval keeps the turn in flight rather than idling it', () => {
  let s = apply(baseState(), event('activity_started', { phase: 'thinking' }));
  s = apply(s, event('turn_waiting_approval', {}));
  assert.equal(s.runtimeStatus, 'running');
});

test('terminal events reset the live state and surface a failure reason', () => {
  let s = apply(baseState(), event('activity_started', { phase: 'thinking' }));
  s = { ...s, steerQueueCount: 2 };
  s = apply(s, event('turn_failed', { status: 'failed', error: 'model exploded' }, 't1', 9));
  assert.equal(s.runtimeStatus, 'idle');
  assert.equal(s.steerQueueCount, 0);
  assert.equal(s.activity, null);
  assert.equal(s.pendingApproval, null);
  assert.equal(s.messages.at(-1)?.content, 'model exploded');
  assert.equal(s.messages.at(-1)?.infoLevel, 'warning');
});

test('unknown kinds and malformed payloads are ignored without throwing', () => {
  const s = baseState();
  // Only the turn bookkeeping is applied; nothing else is invented.
  assert.deepEqual(reduceRuntimeEvent(s, event('plan_updated', { plan_id: 'p' }), now), {
    activeTurnId: 't1',
  });
  assert.deepEqual(reduceRuntimeEvent(s, event('brand_new_kind', { x: 1 }), now), {
    activeTurnId: 't1',
  });
  assert.deepEqual(reduceRuntimeEvent(s, event('reasoning_delta', null), now), {
    activeTurnId: 't1',
  });
  assert.deepEqual(reduceRuntimeEvent(s, event('info', { nothing: true }), now), {
    activeTurnId: 't1',
  });
});

test('the reducer never mutates the input state', () => {
  const s = apply(baseState(), event('tool_started', toolItem()));
  const snapshot = structuredClone(s);
  reduceRuntimeEvent(s, event('tool_updated', toolItem({ status: 'completed' })), now);
  reduceRuntimeEvent(s, event('tool_batch_finished', { group_id: 'g' }), now);
  reduceRuntimeEvent(s, event('usage_updated', { turn_input: 5 }), now);
  assert.deepEqual(s, snapshot);
});

test('toolItemFromPayload tolerates a missing item_id by falling back to call_id', () => {
  const item = toolItemFromPayload({ call_id: 'c9', name: 'grep' });
  assert.equal(item.id, 'c9');
  assert.equal(item.callId, 'c9');
  assert.equal(item.status, 'running');
  assert.equal(item.label, 'grep');
});
