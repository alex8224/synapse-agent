/**
 * Offline tests for the goal dialog's clear confirmation and the store's
 * goal-clear binding (no daemon, no host, no DOM).
 *
 * The dialog is pinned by a source guard: `clear` must be an explicit two-step
 * action whose confirmation is rendered inside the modal, never a blocking
 * browser `prompt`.  The store then runs against a stub client and pins the
 * remaining properties:
 *
 * - the clear is bound to the goal on display (`expected_goal_id`), so a goal
 *   replaced since the confirmation was armed is refused rather than cleared;
 * - clearing a goal cancels no turn: no cancel / pause frame is ever issued.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { beforeEach, test } from 'node:test';
import ts from 'typescript';

import type { SessionGoalView } from '../src/stores/goalView.ts';

const here = dirname(fileURLToPath(import.meta.url));
const dialogSource = readFileSync(join(here, '..', 'src', 'components', 'GoalDialog.tsx'), 'utf8');

/**
 * Code tokens of a source file joined without trivia, so a guard can assert on
 * real code while a doc comment mentioning `window.prompt` never trips it.
 */
function codeText(fileName: string, text: string): string {
  const kind = fileName.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const source = ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, false, kind);
  const tokens: string[] = [];
  const visit = (node: ts.Node): void => {
    if (ts.isJSDoc(node)) return;
    const children = node.getChildren(source);
    if (children.length === 0) {
      tokens.push(node.getText(source));
      return;
    }
    for (const child of children) visit(child);
  };
  visit(source);
  return tokens.join('');
}

const dialogCode = codeText('GoalDialog.tsx', dialogSource);

test('the clear confirmation is in-modal, never a blocking browser prompt', () => {
  assert.equal(dialogCode.includes('window.prompt'), false, 'the dialog must not prompt the browser');
  assert.equal(/prompt\(/.test(dialogCode), false, 'the dialog must not call any prompt()');
  // Two steps: the first click arms a pending-clear state, the second is a
  // distinct confirm handler.
  assert.ok(dialogSource.includes('confirmingClear'), 'the dialog needs a pending-clear state');
  assert.ok(dialogSource.includes('confirmClear'), 'the dialog needs a confirm-clear handler');
});

test('the first click only arms the confirmation, never clears', () => {
  const mutate = /const mutate = async[\s\S]*?\n  \};/.exec(dialogSource);
  assert.ok(mutate, 'the dialog must define its action dispatcher');
  assert.equal(mutate[0].includes('clearGoal('), false, 'the clear button must not clear directly');

  const confirm = /const confirmClear = async[\s\S]*?\n  \};/.exec(dialogSource);
  assert.ok(confirm, 'the dialog must define its clear confirmation handler');
  assert.ok(confirm[0].includes('clearGoal(confirmingClear)'), 'the confirm step clears the armed goal');
});

// --- store semantics --------------------------------------------------------

interface SentFrame {
  method: string;
  params: any;
}

const calls: SentFrame[] = [];

/** Minimal client surface the store's goal actions use. */
function stubClient() {
  return {
    clearSessionGoal: (params: any) => {
      calls.push({ method: 'runtime.session.goal.clear', params });
      return Promise.resolve({ goal: null, cancellation_requested: false });
    },
    pauseSessionGoal: (params: any) => {
      calls.push({ method: 'runtime.session.goal.pause', params });
      return Promise.resolve({ goal: null, cancellation_requested: true });
    },
    cancelTurn: (params: any) => {
      calls.push({ method: 'runtime.turn.cancel', params });
      return Promise.resolve({});
    },
  };
}

const { useConsoleStore } = await import('../src/stores/useConsoleStore.ts');

function goal(goalId: string): SessionGoalView {
  return {
    thread_id: 'thr',
    goal_id: goalId,
    status: 'active',
    label: 'active',
    objective: 'ship the thing',
    token_budget: null,
    tokens_used: 0,
    time_used_seconds: 0,
  };
}

beforeEach(() => {
  calls.length = 0;
  useConsoleStore.setState({
    pairingState: 'paired',
    client: stubClient() as any,
    activeProjectId: 'proj',
    currentSession: { project_id: 'proj', thread_id: 'thr' },
    goal: goal('g-1'),
    goalBusy: false,
    goalActionError: null,
    goalNotice: null,
  });
});

test('clearGoal forwards the displayed goal id as expected_goal_id', async () => {
  const cleared = await useConsoleStore.getState().clearGoal('g-1');
  assert.equal(cleared, true);
  assert.deepEqual(
    calls.map((call) => call.method),
    ['runtime.session.goal.clear'],
  );
  assert.equal(calls[0].params.expected_goal_id, 'g-1');
  assert.deepEqual(calls[0].params.session, { project_id: 'proj', thread_id: 'thr' });
});

test('clearGoal falls back to the current goal when no id is bound', async () => {
  const cleared = await useConsoleStore.getState().clearGoal();
  assert.equal(cleared, true);
  assert.equal(calls[0].params.expected_goal_id, 'g-1');
});

test('a goal replaced since the confirmation is refused, not silently cleared', async () => {
  // The dialog armed the confirmation for g-1, but the live goal is now g-2.
  useConsoleStore.setState({ goal: goal('g-2') });
  await useConsoleStore.getState().clearGoal('g-1');
  // The store forwards the displayed id, never the newer one, so the server
  // answers `conflict` instead of clearing a goal the user never confirmed.
  assert.equal(calls[0].params.expected_goal_id, 'g-1');
});

test('clearing a goal cancels no turn', async () => {
  await useConsoleStore.getState().clearGoal('g-1');
  assert.deepEqual(
    calls.map((call) => call.method),
    ['runtime.session.goal.clear'],
  );
});
