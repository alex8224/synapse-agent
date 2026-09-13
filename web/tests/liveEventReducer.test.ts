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

test('each reasoning step of a turn gets its own thought fold', () => {
  let s = apply(baseState(), event('reasoning_delta', { text: 'step one' }));
  s = apply(s, event('reasoning_completed', { text: 'step one' }));
  // the tool call of the first step
  s = apply(s, event('tool_started', toolItem()));
  s = apply(s, event('reasoning_delta', { text: 'step two' }));
  s = apply(s, event('reasoning_completed', { text: 'step two' }));

  assert.deepEqual(
    s.messages.map((m) => m.type),
    ['thought', 'tool_group', 'thought'],
    'the second step must not be merged into the first fold',
  );
  const thoughts = s.messages.filter((m) => m.type === 'thought');
  // Neither completion may overwrite the other segment's text.
  assert.deepEqual(thoughts.map((m) => m.content), ['step one', 'step two']);
  assert.deepEqual(thoughts.map((m) => m.duration), ['0.0s', '0.0s']);
});

test('a step boundary does not require a completion event', () => {
  let s = apply(baseState(), event('reasoning_delta', { text: 'first' }));
  // The model goes straight to a tool call, so the segment ends with the row
  // that follows it rather than with `reasoning_completed`.
  s = apply(s, event('tool_started', toolItem()));
  s = apply(s, event('reasoning_delta', { text: 'second' }));

  const thoughts = s.messages.filter((m) => m.type === 'thought');
  assert.equal(thoughts.length, 2);
  assert.deepEqual(thoughts.map((m) => m.content), ['first', 'second']);
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

test('assistant text printed before a tool batch gets its own answer row', () => {
  let s = apply(baseState(), event('answer_delta', { text: 'let me check' }));
  s = apply(s, event('answer_completed', { text: 'let me check' }));
  s = apply(s, event('tool_batch_started', { parallel: false }));
  s = apply(s, event('tool_started', toolItem()));
  s = apply(s, event('answer_delta', { text: 'final' }));
  s = apply(s, event('answer_completed', { text: 'final' }));

  assert.deepEqual(
    s.messages.map((m) => m.type),
    ['assistant', 'tool_group', 'assistant'],
    'the final answer must not be folded into the row above the tool calls',
  );
  assert.deepEqual(
    s.messages.filter((m) => m.type === 'assistant').map((m) => m.content),
    ['let me check', 'final'],
  );
  assert.deepEqual(
    s.messages.filter((m) => m.type === 'assistant').map((m) => m.streaming),
    [false, false],
    'a completed segment must not swallow the next one',
  );
});

test('a delta after answer_completed opens the next answer segment', () => {
  let s = apply(baseState(), event('answer_delta', { text: 'first' }));
  s = apply(s, event('answer_completed', { text: 'first' }));
  s = apply(s, event('answer_delta', { text: 'second' }));

  assert.deepEqual(s.messages.map((m) => m.content), ['first', 'second']);
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

test('each tool batch of a turn gets its own group, in stream order', () => {
  let s = apply(baseState(), event('reasoning_delta', { text: 'step one' }));
  s = apply(s, event('tool_batch_started', { parallel: false }));
  s = apply(s, event('tool_started', toolItem({ item_id: 'g1-0', call_id: 'c1' })));
  s = apply(s, event('tool_batch_finished', { group_id: 'g1' }));
  s = apply(s, event('reasoning_delta', { text: 'step two' }));
  s = apply(s, event('tool_batch_started', { parallel: true }));
  s = apply(s, event('tool_started', toolItem({ item_id: 'g2-0', call_id: 'c2', name: 'grep' })));
  s = apply(s, event('tool_batch_finished', { group_id: 'g2' }));

  assert.deepEqual(
    s.messages.map((m) => m.type),
    ['thought', 'tool_group', 'thought', 'tool_group'],
    'the second batch must not be appended to the group the first batch opened',
  );
  assert.deepEqual(
    s.messages.filter((m) => m.type === 'tool_group').map((m) => m.tools?.map((t) => t.name)),
    [['read_file'], ['grep']],
  );
  assert.deepEqual(
    s.messages.filter((m) => m.type === 'tool_group').map((m) => m.parallel),
    [false, true],
  );
});

test('a batch that never reported finished is sealed by the next batch', () => {
  let s = apply(baseState(), event('tool_batch_started', { parallel: false }));
  s = apply(s, event('tool_started', toolItem({ item_id: 'g1-0' })));
  // No `tool_batch_finished` (truncated or older stream): the next batch must
  // still start its own group instead of appending to the first one.
  s = apply(s, event('tool_batch_started', { parallel: false }));
  s = apply(s, event('tool_started', toolItem({ item_id: 'g2-0', name: 'grep' })));

  const groups = s.messages.filter((m) => m.type === 'tool_group');
  assert.equal(groups.length, 2);
  assert.equal(groups[0].finished, true);
  assert.equal(groups[1].finished, false);
  assert.deepEqual(groups.map((m) => m.tools?.map((t) => t.name)), [['read_file'], ['grep']]);
});

test('a late update for a closed group patches that group instead of opening one', () => {
  let s = apply(baseState(), event('tool_batch_started', { parallel: false }));
  s = apply(s, event('tool_started', toolItem({ item_id: 'g1-0' })));
  s = apply(s, event('tool_batch_finished', { group_id: 'g1' }));
  s = apply(s, event('tool_updated', toolItem({ item_id: 'g1-0', status: 'completed', preview: 'ok' })));

  assert.equal(s.messages.length, 1, 'the update must not open a second group');
  assert.equal(s.messages[0].tools?.[0].status, 'completed');
  assert.equal(s.messages[0].tools?.[0].preview, 'ok');
});

test('legacy tool_result updates a known row and joins the batch still open', () => {
  let s = apply(baseState(), event('tool_batch_started', { parallel: false }));
  s = apply(s, event('tool_started', toolItem({ item_id: 'g1-0', call_id: 'c1' })));
  s = apply(s, event('tool_batch_finished', { group_id: 'g1' }));
  s = apply(s, event('tool_batch_started', { parallel: false }));
  // A result for the first batch's call still finds its own row ...
  s = apply(s, event('tool_result', { name: 'read_file', call_id: 'c1', status: 'completed' }));
  // ... while an unknown one joins the batch that is still open.
  s = apply(s, event('tool_result', { name: 'grep', call_id: 'c9', status: 'completed' }));

  const groups = s.messages.filter((m) => m.type === 'tool_group');
  assert.equal(groups.length, 2);
  assert.equal(groups[0].tools?.[0].status, 'completed');
  assert.deepEqual(groups[1].tools?.map((t) => t.name), ['grep']);
});

test('an item id reused by a later turn stays in that later turn', () => {
  let s = apply(
    baseState(),
    event('tool_started', toolItem({ item_id: 'g1-0', name: 'read_file' })),
  );
  // Every turn restarts the item ids, so this `g1-0` is a different call.
  s = apply(s, event('tool_started', toolItem({ item_id: 'g1-0', name: 'bash' }), 't2'));
  s = apply(
    s,
    event('tool_finished', { item_id: 'g1-0', status: 'completed', preview: 'out' }, 't2'),
  );

  assert.equal(s.messages.length, 2, 'the second turn must get its own row');
  assert.equal(s.messages[0].tools?.[0].name, 'read_file');
  assert.equal(s.messages[0].tools?.[0].preview, null, 'turn 1 must not take turn 2 results');
  assert.equal(s.messages[1].tools?.[0].name, 'bash');
  assert.equal(s.messages[1].tools?.[0].preview, 'out');
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
