/**
 * Offline tests for the todo panel's parser (no DOM, no socket).
 *
 * The input is exactly what the runtime puts on a `write_todos` tool item's
 * `preview` (`runtime/timeline.py::format_todos_preview`), so the shapes pinned
 * here are the ones the daemon actually sends.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  extractTodos,
  latestTodos,
  parseTodoPreview,
  todoPanelLabel,
  todoPreviewFromArgs,
} from '../src/stores/todoView.ts';
import type { TranscriptMessage } from '../src/stores/historyMapper.ts';
import { mapHistoryEvents } from '../src/stores/historyMapper.ts';

const CHECKLIST = ['✓ read the docs', '● write the parser', '○ add tests', '— done 1 · doing 1 · todo 1'].join('\n');

test('a stored checklist parses into items with their kinds', () => {
  const view = parseTodoPreview(CHECKLIST);
  assert.ok(view);
  assert.deepEqual(
    view.items.map((item) => [item.kind, item.content]),
    [
      ['done', 'read the docs'],
      ['active', 'write the parser'],
      ['pending', 'add tests'],
    ],
  );
  assert.deepEqual([view.done, view.active, view.pending], [1, 1, 1]);
  assert.equal(view.omitted, 0);
});

test('the parser is tolerant of what it does not recognise', () => {
  assert.equal(parseTodoPreview(''), null);
  assert.equal(parseTodoPreview(null), null);
  assert.equal(parseTodoPreview('— done 0 · doing 0 · todo 0'), null);
  // The runtime's own cap marker becomes an "omitted" count, not an item.
  const capped = parseTodoPreview(['✓ a', '… +3 more', '— done 1 · doing 0 · todo 3'].join('\n'));
  assert.ok(capped);
  assert.equal(capped.items.length, 1);
  assert.equal(capped.omitted, 3);
  // Legacy ASCII rows still parse.
  const legacy = parseTodoPreview(['[x] old done', '[ ] old pending'].join('\n'));
  assert.deepEqual(
    legacy?.items.map((item) => item.kind),
    ['done', 'pending'],
  );
  // Unknown lines are skipped rather than guessed at.
  assert.equal(parseTodoPreview('just some prose'), null);
});

test('the newest todo call wins', () => {
  const group = (id: string, name: string, preview: string): TranscriptMessage => ({
    id,
    type: 'tool_group',
    timestamp: 'Turn 1',
    tools: [{ id: `${id}-t`, callId: null, name, label: name, path: null, status: 'completed', preview, error: false, sub: false }],
  });
  const messages: TranscriptMessage[] = [
    group('g1', 'write_todos', '✓ first\n— done 1 · doing 0 · todo 0'),
    group('g2', 'read_file', 'contents'),
    group('g3', 'write_todos', CHECKLIST),
  ];
  assert.equal(latestTodos(messages)?.items.length, 3);
  // No todo tool at all: the panel stays hidden.
  assert.equal(latestTodos([group('g4', 'read_file', 'x')]), null);
  assert.equal(latestTodos([]), null);
});

test('a write_todos call rebuilds the runtime checklist shape', () => {
  const args = {
    todos: [
      { content: 'read the docs', status: 'completed' },
      { content: 'write the parser', status: 'in_progress' },
      { content: 'add tests', status: 'pending' },
    ],
  };
  const preview = todoPreviewFromArgs('write_todos', args);
  assert.ok(preview);
  assert.equal(
    preview,
    [
      '✓ read the docs',
      '● write the parser',
      '○ add tests',
      '— done 1 · doing 1 · todo 1',
    ].join('\n'),
  );
  // Round-trip: the parser reads back exactly what the builder wrote.
  const view = parseTodoPreview(preview);
  assert.ok(view);
  assert.deepEqual(
    view.items.map((item) => [item.kind, item.content]),
    [
      ['done', 'read the docs'],
      ['active', 'write the parser'],
      ['pending', 'add tests'],
    ],
  );
});

test('only todo tools produce a checklist, and statuses are normalised', () => {
  assert.equal(todoPreviewFromArgs('read_file', { todos: [{ content: 'x' }] }), null);
  assert.equal(todoPreviewFromArgs('write_todos', {}), null);
  assert.equal(todoPreviewFromArgs('write_todos', { todos: [] }), null);
  // `text`/`title` fallbacks and a bare string entry both work.
  assert.deepEqual(
    extractTodos({ todos: [{ text: 'from text', status: 'DONE' }, 'plain string'] }),
    [
      { kind: 'done', content: 'from text' },
      { kind: 'pending', content: 'plain string' },
    ],
  );
  assert.ok(todoPreviewFromArgs('todos', { todos: ['a'] }));
  assert.ok(todoPreviewFromArgs('todo_write', { todos: ['a'] }));
  // The runtime's own cap marker appears once the list outruns the preview.
  const many = todoPreviewFromArgs('write_todos', {
    todos: Array.from({ length: 20 }, (_, i) => `item ${i}`),
  });
  assert.ok(many);
  assert.ok(many.includes('… +4 more'));
});

test('a projected history row carries the checklist, so the panel survives a reload', () => {
  // Shape of `HistoryEvent` from `runtime.session.history`: the tool calls keep
  // their raw args, which is what lets the panel rebuild a past turn's list.
  const events = [
    {
      kind: 'tools',
      text: '',
      tool_calls: [
        { id: 'c1', name: 'read_file', args: { file_path: 'a.ts' } },
        {
          id: 'c2',
          name: 'write_todos',
          args: {
            todos: [
              { content: 'first', status: 'completed' },
              { content: 'second', status: 'in_progress' },
            ],
          },
        },
      ],
      tool_results: [],
    },
  ];
  const messages = mapHistoryEvents(events as never, { startTurn: 0, tag: 'hist' });
  const group = messages.find((m) => m.type === 'tool_group');
  assert.ok(group, 'the tools event must map to a tool group');
  const todo = group.tools?.find((tool) => tool.name === 'write_todos');
  assert.ok(todo, 'the todo call must be present');
  assert.equal(todo.preview, ['✓ first', '● second', '— done 1 · doing 1 · todo 0'].join('\n'));
  // A non-todo call keeps no preview (the history row never had one).
  assert.equal(group.tools?.find((tool) => tool.name === 'read_file')?.preview, null);
  // And the panel finds it through the same entry point it uses at runtime.
  const view = latestTodos(messages);
  assert.deepEqual(
    view?.items.map((item) => [item.kind, item.content]),
    [
      ['done', 'first'],
      ['active', 'second'],
    ],
  );
});

test('the label mirrors the runtime summary', () => {
  const running = parseTodoPreview(CHECKLIST);
  assert.ok(running);
  assert.equal(todoPanelLabel(running), 'Todos 1/3 · in progress: write the parser');
  const finished = parseTodoPreview('✓ a\n✓ b\n— done 2 · doing 0 · todo 0');
  assert.ok(finished);
  assert.equal(todoPanelLabel(finished), 'Todos 2/2 · all done');
  const idle = parseTodoPreview('○ a\n○ b\n— done 0 · doing 0 · todo 2');
  assert.ok(idle);
  assert.equal(todoPanelLabel(idle), 'Todos 0/2');
});
