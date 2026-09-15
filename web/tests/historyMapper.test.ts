/**
 * Focused tests for the RPC -> console mappers using only the Node built-in
 * test runner (no extra dependencies):
 *   node --test tests/
 *
 * They cover the wire contract of `runtime.session.list` /
 * `runtime.session.history` (request builders, projection mapping, and the
 * transcript mapping that must never pretend history is a full checkpoint).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  describeHistoryFailure,
  earlierHistoryParams,
  historyToolItem,
  isHistoryTooLarge,
  latestHistoryParams,
  mapHistoryEvents,
  readHistoryPage,
  timeLabelFromIso,
  toSessionItem,
  toSessionListView,
} from '../src/stores/historyMapper.ts';

const SESSION = { project_id: 'proj-a', thread_id: 'abc123' };

test('latestHistoryParams builds a newest-page history request (limit 20, no before_turn)', () => {
  assert.deepEqual(latestHistoryParams(SESSION), {
    session: SESSION,
    before_turn: null,
    limit: 20,
  });
});

test('earlierHistoryParams pages strictly before the oldest loaded turn', () => {
  assert.deepEqual(earlierHistoryParams(SESSION, 5), {
    session: SESSION,
    before_turn: 5,
    limit: 20,
  });
});

test('toSessionItem maps RPC metadata to the sidebar model with fallbacks', () => {
  const item = toSessionItem({
    thread_id: 't1',
    title: '',
    model: null,
    active_model: null,
    created_at: '2025-01-02T03:04:05',
    updated_at: '2025-06-07T08:09:10',
    summary: 's',
  });
  assert.equal(item.thread_id, 't1');
  // An unnamed row shows the console's own label, not the raw id: the server
  // keeps its placeholder until the first user message names the session.
  assert.equal(item.title, '新会话 t1');
  assert.equal(item.time_label, '06-07 08:09');
});

test('toSessionListView preserves pagination flags and maps every item', () => {
  const view = toSessionListView({
    items: [
      {
        thread_id: 't2',
        title: 'Second',
        model: 'm',
        active_model: null,
        created_at: '2025-01-02T03:04:05',
        updated_at: '2025-06-07T08:09:10',
        summary: null,
      },
    ],
    next_offset: 50,
    total: 77,
  });
  assert.equal(view.items.length, 1);
  assert.equal(view.items[0].title, 'Second');
  assert.equal(view.next_offset, 50);
  assert.equal(view.total, 77);
});

test('a projected tool row keeps its arguments so the row can show them', () => {
  const messages = mapHistoryEvents(
    [
      {
        kind: 'user',
        text: 'go',
        tool_calls: [],
        tool_results: [],
        turn_id: 'A',
        elapsed_s: 1,
        attachments: [],
      },
      {
        kind: 'tools',
        text: '',
        tool_calls: [
          {
            id: 'c1',
            name: 'execute',
            args: { intent: 'run checks', command: 'pytest -q', timeout_s: 30 },
          },
        ],
        tool_results: [{ id: 'c1', name: 'execute', status: 'ok', content: 'ok' }],
        attachments: [],
      },
    ] as HistoryEvent[],
    { startTurn: 1, pageTag: 'latest' },
  );

  const tool = messages.find((m) => m.type === 'tool_group')!.tools![0];
  // The name is what the call was; the intent is what the model said it was for.
  assert.equal(tool.name, 'execute');
  assert.equal(tool.label, 'run checks');
  assert.deepEqual(tool.args, { intent: 'run checks', command: 'pytest -q', timeout_s: 30 });
});

test('a tool call without arguments carries none', () => {
  const messages = mapHistoryEvents(
    [
      {
        kind: 'user',
        text: 'go',
        tool_calls: [],
        tool_results: [],
        attachments: [],
      },
      {
        kind: 'tools',
        text: '',
        tool_calls: [{ id: 'c1', name: 'execute', args: {} }],
        tool_results: [],
        attachments: [],
      },
    ] as HistoryEvent[],
    { startTurn: 1, pageTag: 'latest' },
  );

  assert.equal(messages.find((m) => m.type === 'tool_group')!.tools![0].args, undefined);
});

test('timeLabelFromIso formats MM-DD HH:MM and tolerates missing timestamps', () => {
  assert.equal(timeLabelFromIso('2025-06-07T08:09:10'), '06-07 08:09');
  assert.equal(timeLabelFromIso(null), '');
  assert.equal(timeLabelFromIso(undefined), '');
});

test('mapHistoryEvents renders user/answer/thought/tools and skips meta and empties', () => {
  const messages = mapHistoryEvents(
    [
      { kind: 'user', text: 'fix the build', tool_calls: [], tool_results: [] },
      { kind: 'thought', text: '', tool_calls: [], tool_results: [] },
      { kind: 'tools', text: '', tool_calls: [{ name: 'read', args: {} }], tool_results: [] },
      { kind: 'answer', text: 'done', tool_calls: [], tool_results: [] },
      { kind: 'meta', text: 'hidden meta', tool_calls: [], tool_results: [] },
      { kind: 'tools', text: '', tool_calls: [], tool_results: [] },
    ],
    { startTurn: 7, pageTag: 'latest' },
  );

  assert.equal(messages.length, 3);
  assert.deepEqual(messages.map((m) => m.type), ['user', 'tool_group', 'assistant']);
  assert.equal(messages[0].content, 'fix the build');
  assert.equal(messages[0].timestamp, 'Turn 7');
  assert.deepEqual(messages[1].tools, [historyToolItem('hist-x-history:7-2-0', 'read')]);
  assert.equal(messages[1].finished, true);
  assert.equal(messages[2].content, 'done');
  assert.equal(messages[2].timestamp, 'Turn 7'); // answer stays on the same turn
});

test('mapHistoryEvents falls back to tool_results names when calls are absent', () => {
  const messages = mapHistoryEvents(
    [
      { kind: 'user', text: 'update file', tool_calls: [], tool_results: [] },
      { kind: 'tools', text: '', tool_calls: [], tool_results: [{ name: 'edit', status: 'ok' }] },
    ],
    { startTurn: 1, pageTag: 'latest' },
  );
  assert.equal(messages.length, 2);
  assert.equal(messages[0].type, 'user');
  assert.equal(messages[1].type, 'tool_group');
  assert.equal(messages[1].tools?.[0].name, 'edit');
});

test('mapHistoryEvents increments Turn labels per user message', () => {
  const messages = mapHistoryEvents(
    [
      { kind: 'user', text: 'first', tool_calls: [], tool_results: [] },
      { kind: 'answer', text: 'a1', tool_calls: [], tool_results: [] },
      { kind: 'user', text: 'second', tool_calls: [], tool_results: [] },
      { kind: 'answer', text: 'a2', tool_calls: [], tool_results: [] },
    ],
    { startTurn: 3, pageTag: 'latest' },
  );
  assert.deepEqual(
    messages.map((m) => `${m.type}:${m.timestamp}`),
    ['user:Turn 3', 'assistant:Turn 3', 'user:Turn 4', 'assistant:Turn 4'],
  );
});

test('earlier pages use distinct ids so prepending never collides on React keys', () => {
  const newer = mapHistoryEvents(
    [{ kind: 'user', text: 'n', tool_calls: [], tool_results: [] }],
    { startTurn: 21, pageTag: 'latest' },
  );
  const older = mapHistoryEvents(
    [{ kind: 'user', text: 'o', tool_calls: [], tool_results: [] }],
    { startTurn: 1, pageTag: 'earlier-1' },
  );
  const ids = [...older, ...newer].map((m) => m.id);
  assert.equal(new Set(ids).size, ids.length);
  assert.equal(older[0].timestamp, 'Turn 1');
  assert.equal(newer[0].timestamp, 'Turn 21');
});

function tooLarge(): Error {
  return Object.assign(new Error('runtime service error'), {
    service_code: 'history_too_large',
  });
}

test('readHistoryPage shrinks the page while the runtime reports history_too_large', async () => {
  const seen: number[] = [];
  const result = await readHistoryPage(async (limit) => {
    seen.push(limit);
    if (limit > 2) throw tooLarge();
    return `page:${limit}`;
  });
  assert.deepEqual(seen, [20, 10, 5, 2]);
  assert.equal(result, 'page:2');
});

test('readHistoryPage rethrows the size rejection when even the smallest page fails', async () => {
  const seen: number[] = [];
  await assert.rejects(
    () =>
      readHistoryPage(async (limit) => {
        seen.push(limit);
        throw tooLarge();
      }),
    (err: unknown) => isHistoryTooLarge(err),
  );
  assert.deepEqual(seen, [20, 10, 5, 2, 1]);
});

test('readHistoryPage propagates a non-size failure immediately', async () => {
  const seen: number[] = [];
  await assert.rejects(
    () =>
      readHistoryPage(async (limit) => {
        seen.push(limit);
        throw Object.assign(new Error('missing session'), { service_code: 'not_found' });
      }),
    /missing session/,
  );
  assert.deepEqual(seen, [20], 'a non-size error must not trigger a smaller retry');
});

test('history request builders carry an explicit page size', () => {
  assert.equal(latestHistoryParams(SESSION).limit, 20);
  assert.equal(latestHistoryParams(SESSION, 3).limit, 3);
  assert.deepEqual(earlierHistoryParams(SESSION, 5, 2), {
    session: SESSION,
    before_turn: 5,
    limit: 2,
  });
});

test('history failure helpers recognise the size rejection and stay value-free', () => {
  assert.equal(isHistoryTooLarge(tooLarge()), true);
  assert.equal(isHistoryTooLarge(new Error('x')), false);
  assert.equal(isHistoryTooLarge(null), false);
  assert.equal(isHistoryTooLarge('history_too_large'), false);
  assert.match(describeHistoryFailure(tooLarge()), /history_too_large/);
  assert.equal(
    describeHistoryFailure(Object.assign(new Error('x'), { service_code: 'not_found' })),
    '历史加载失败（not_found）。',
  );
  assert.equal(describeHistoryFailure(new Error('plain failure')), '历史加载失败：plain failure');
});
