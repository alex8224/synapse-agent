/**
 * Tests for taskView view model helpers and group logic.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  TASK_PRIORITIES,
  TASK_TITLE_MAX_CHARS,
  codePointLength,
  draftFromTask,
  emptyTaskDraft,
  groupTasksByStatus,
  formatTaskTime,
  isTaskTitleValid,
  parseTaskLabels,
  planTaskArrange,
  STATUS_LABELS,
  PRIORITY_LABELS,
  taskDraftChanges,
  type ProjectTaskView,
} from '../src/stores/taskView.ts';

/** A task view carrying only the fields the arrange policy reads. */
function arrangeTask(id: string, threadId: string | null = null): ProjectTaskView {
  return {
    id,
    title: id,
    description: '',
    status: 'todo',
    priority: 'medium',
    labels: [],
    assigned_thread_id: threadId,
    created_at_ms: 0,
    updated_at_ms: 0,
    completed_at_ms: null,
  };
}

test('groupTasksByStatus classifies tasks accurately into columns', () => {
  const sampleTasks: ProjectTaskView[] = [
    {
      id: 'task-1',
      title: 'Fix auth bug',
      description: 'Session token expires too soon',
      status: 'todo',
      priority: 'high',
      labels: ['auth', 'bug'],
      assigned_thread_id: null,
      created_at_ms: 1000,
      updated_at_ms: 1000,
      completed_at_ms: null,
    },
    {
      id: 'task-2',
      title: 'Implement Dark Mode',
      description: '',
      status: 'in_progress',
      priority: 'medium',
      labels: ['ui'],
      assigned_thread_id: 'thread-1',
      created_at_ms: 2000,
      updated_at_ms: 2500,
      completed_at_ms: null,
    },
    {
      id: 'task-3',
      title: 'Migrate DB',
      description: 'Blocked waiting on migration scripts',
      status: 'blocked',
      priority: 'critical',
      labels: ['db'],
      assigned_thread_id: null,
      created_at_ms: 3000,
      updated_at_ms: 3500,
      completed_at_ms: null,
    },
    {
      id: 'task-4',
      title: 'Initial setup',
      description: 'Setup monorepo structure',
      status: 'done',
      priority: 'low',
      labels: ['infra'],
      assigned_thread_id: null,
      created_at_ms: 500,
      updated_at_ms: 800,
      completed_at_ms: 800,
    },
  ];

  const grouped = groupTasksByStatus(sampleTasks);

  assert.equal(grouped.todo.length, 1);
  assert.equal(grouped.todo[0].id, 'task-1');

  assert.equal(grouped.in_progress.length, 1);
  assert.equal(grouped.in_progress[0].id, 'task-2');

  assert.equal(grouped.blocked.length, 1);
  assert.equal(grouped.blocked[0].id, 'task-3');

  assert.equal(grouped.done.length, 1);
  assert.equal(grouped.done[0].id, 'task-4');
});

test('formatTaskTime handles valid and empty timestamps', () => {
  assert.equal(formatTaskTime(0), '');
  const formatted = formatTaskTime(1700000000000);
  assert.ok(formatted.length > 0);
});

test('status and priority label maps have complete coverage', () => {
  assert.ok(STATUS_LABELS.todo);
  assert.ok(STATUS_LABELS.in_progress);
  assert.ok(STATUS_LABELS.blocked);
  assert.ok(STATUS_LABELS.done);

  assert.ok(PRIORITY_LABELS.low);
  assert.ok(PRIORITY_LABELS.medium);
  assert.ok(PRIORITY_LABELS.high);
  assert.ok(PRIORITY_LABELS.critical);
});

test('planTaskArrange decides how many sessions a selection takes', () => {
  // Nothing bound yet: one shared group that still needs a session.
  const free = [arrangeTask('a'), arrangeTask('b')];
  assert.deepEqual(planTaskArrange(free, 'shared'), [{ threadId: null, tasks: free }]);

  // Already sharing one session: reused, so pressing arrange twice is a no-op.
  const shared = [arrangeTask('a', 't1'), arrangeTask('b', 't1')];
  assert.deepEqual(planTaskArrange(shared, 'shared'), [{ threadId: 't1', tasks: shared }]);

  // Spread over two sessions: the whole selection is re-bound onto one fresh one.
  const scattered = [arrangeTask('a', 't1'), arrangeTask('b', 't2')];
  assert.deepEqual(planTaskArrange(scattered, 'shared'), [
    { threadId: null, tasks: scattered },
  ]);

  // Parallel: one group per task, keeping the session a task already has.
  const parallel = planTaskArrange([arrangeTask('a', 't1'), arrangeTask('b')], 'parallel');
  assert.deepEqual(
    parallel.map((group) => [group.threadId, group.tasks.map((task) => task.id)]),
    [
      ['t1', ['a']],
      [null, ['b']],
    ],
  );
});

/** A fully-populated task view for the draft helpers. */
function fullTask(overrides: Partial<ProjectTaskView> = {}): ProjectTaskView {
  return {
    id: 't1',
    title: 'Fix auth bug',
    description: 'Session token expires too soon',
    status: 'todo',
    priority: 'high',
    labels: ['auth', 'bug'],
    assigned_thread_id: 'thread-1',
    created_at_ms: 1,
    updated_at_ms: 2,
    completed_at_ms: null,
    ...overrides,
  };
}

test('emptyTaskDraft is the blank create draft', () => {
  assert.deepEqual(emptyTaskDraft(), {
    title: '',
    description: '',
    priority: 'medium',
    labels: '',
  });
});

test('draftFromTask prefills from the saved row and joins labels', () => {
  assert.deepEqual(draftFromTask(fullTask()), {
    title: 'Fix auth bug',
    description: 'Session token expires too soon',
    priority: 'high',
    labels: 'auth, bug',
  });
  // An unknown priority degrades to the default instead of leaking a bad value.
  assert.equal(draftFromTask(fullTask({ priority: 'bogus' })).priority, 'medium');
});

test('parseTaskLabels splits both commas and whitespace, trims, lowercases and de-dupes', () => {
  assert.deepEqual(parseTaskLabels('UI, Auth，  auth\nBug\tui'), ['ui', 'auth', 'bug']);
  assert.deepEqual(parseTaskLabels('   '), []);
  assert.deepEqual(parseTaskLabels(''), []);
});

test('isTaskTitleValid rejects blank titles and mirrors the backend cap by codepoint', () => {
  assert.equal(isTaskTitleValid(''), false);
  assert.equal(isTaskTitleValid('   '), false);
  assert.equal(isTaskTitleValid('  ok  '), true);
  // 200 astral codepoints are 400 UTF-16 units: a `length`-based cap would wrongly
  // reject a title the Python backend accepts.
  const astral = '😀'.repeat(TASK_TITLE_MAX_CHARS);
  assert.equal(codePointLength(astral), TASK_TITLE_MAX_CHARS);
  assert.equal(astral.length, TASK_TITLE_MAX_CHARS * 2);
  assert.equal(isTaskTitleValid(astral), true);
  assert.equal(isTaskTitleValid('a'.repeat(TASK_TITLE_MAX_CHARS + 1)), false);
});

test('taskDraftChanges sends only the touched fields, never status or the session', () => {
  const task = fullTask();

  // Untouched draft -> no changes at all (the form closes with no RPC).
  assert.deepEqual(taskDraftChanges(task, draftFromTask(task)), {});
  // Clearing the description and the labels is a real change, sent as '' / [].
  assert.deepEqual(
    taskDraftChanges(task, { title: task.title, description: '', priority: task.priority, labels: '' }),
    { description: '', labels: [] },
  );

  // Every field is compared against the saved row, and status is never included.
  const changed = taskDraftChanges(task, {
    title: '  New title  ',
    description: 'a longer description',
    priority: 'critical',
    labels: 'Auth, NEW',
  });
  assert.deepEqual(changed, {
    title: 'New title',
    description: 'a longer description',
    priority: 'critical',
    labels: ['auth', 'new'],
  });
  assert.equal('status' in changed, false);
  assert.equal('assigned_thread_id' in changed, false);
});

test('TASK_PRIORITIES covers the four backend priorities', () => {
  assert.deepEqual([...TASK_PRIORITIES], ['low', 'medium', 'high', 'critical']);
});

test('unchanged prefilled tags containing delimiters are not rewritten by another field edit', () => {
  const opening = fullTask({ labels: ['needs review', 'owner,team'] });
  const draft = { ...draftFromTask(opening), title: 'Only edit the title' };
  assert.deepEqual(taskDraftChanges(opening, draft), { title: 'Only edit the title' });
});

test('an opening snapshot yields only user edits, not stale fields from a refreshed row', () => {
  const opening = fullTask();
  const draft = { ...draftFromTask(opening), title: 'My title edit' };
  const refreshed = {
    ...opening, description: 'Updated by another client', priority: 'critical',
    labels: ['new-tag'], status: 'blocked', assigned_thread_id: 'new-session',
  };
  const changes = taskDraftChanges(opening, draft);
  assert.deepEqual(changes, { title: 'My title edit' });
  const afterSave = { ...refreshed, ...changes };
  assert.equal(afterSave.description, refreshed.description);
  assert.equal(afterSave.status, 'blocked');
  assert.equal(afterSave.assigned_thread_id, 'new-session');
});
