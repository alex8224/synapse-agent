/**
 * Source guard for the sidebar's per-row fork action.
 *
 * Every session row carries the same write actions, and the two older ones
 * (`renameSession` / `deleteSession`) address the *active* project: they take a
 * bare thread id and pair it with `currentSession.project_id` inside the store.
 * That is right for a row of the active project and wrong for any other one, and
 * the sidebar renders the rows of every project.  The fork action must not repeat
 * that mistake: it hands over the row's own `{project_id, thread_id}`, which is
 * what lets the store fork the session the user actually pointed at and then move
 * the console to the project that session belongs to.
 *
 * This file pins that wiring statically, next to the behavioural tests in
 * `sessionManagement.test.ts` (which drive the real store against a stub client);
 * it also pins the two guards that keep one click from starting two forks.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const read = (relative: string) => readFileSync(join(here, '..', 'src', relative), 'utf8');

const sidebar = read('components/SideBar.tsx');
const store = read('stores/useConsoleStore.ts');

test("the row fork action hands over the row's own project and thread", () => {
  assert.ok(
    sidebar.includes('forkSessionFrom({ project_id: projectId, thread_id: threadId })'),
    'the fork must be sent with the identity the sidebar already holds for the row',
  );
  assert.ok(
    /void forkRow\(project\.project_id, sess\.thread_id\)/.test(sidebar),
    "the row's own project id must be the one passed, never the active project's",
  );
  assert.equal(
    /forkRow\(currentSession|forkSession\(/.test(sidebar),
    false,
    'the row must not fork whichever session happens to be open',
  );
});

test('a fork in flight cannot be started twice from the same row', () => {
  assert.ok(
    sidebar.includes('if (forkingThreadId !== null) return;'),
    'the handler must refuse a second fork while one is on the wire',
  );
  assert.ok(
    sidebar.includes('disabled={forkingThreadId === sess.thread_id}'),
    "the row's own trigger must be disabled while its fork runs",
  );
  assert.ok(
    sidebar.includes('setForkingThreadId(null)'),
    'and it must become actionable again once the fork returns',
  );
});

test('the store opens the source before forking it, and attaches in the child project', () => {
  // The runtime only forks a session it already holds an agent for, and the
  // sidebar lists sessions nobody has opened: without the open first, "fork this
  // row" would be refused instead of copying anything.
  const openAt = store.indexOf('if (!sessionIsOpen(source))');
  const forkAt = store.indexOf('.forkSession({');
  assert.ok(openAt > -1, 'the store must ask whether the source is open');
  assert.ok(forkAt > openAt, 'and open it before the fork, not after it');
  // The child belongs to the source's project, so the console is pointed at that
  // project before attaching: header, tree and transcript must agree.
  assert.ok(
    store.includes('activateProject(child.project_id)'),
    "a child of another project must move the console to that project",
  );
});
