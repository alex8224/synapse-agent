/**
 * Store-level tests for the project-scoped reasoning default.
 *
 * The runtime exposes `runtime.project.thinking.set`, so the settings dialog must
 * perform a real write: no optimistic flip (the dialog reports the *stored*
 * default), a visible reason on failure, a refusal while the capability flag is
 * false, and no application of a result that belongs to a project the user
 * already switched away from. The current session's level must never change.
 *
 * Uses only the Node built-in test runner; the store is exercised with a
 * hand-written client stub (no WebSocket).
 */
import { beforeEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { useConsoleStore } from '../src/stores/useConsoleStore.ts';

const SESSION = { project_id: 'p', thread_id: 't' };

function fakeClient(overrides: Record<string, unknown> = {}): unknown {
  return {
    getState: () => 'connected',
    connect: async () => undefined,
    setProjectThinkingLevel: async () => ({
      command_id: 'c1',
      project_id: 'p',
      level: 'low',
    }),
    ...overrides,
  };
}

beforeEach(() => {
  useConsoleStore.setState({
    pairingState: 'paired',
    connectionState: 'connected',
    client: fakeClient() as never,
    currentSession: SESSION,
    canSetProjectThinking: true,
    projectThinkingLevel: 'high',
    projectThinkingError: null,
    thinkingLevel: 'max',
    thinkingLevelError: null,
  });
});

test('a successful write stores the persisted project default', async () => {
  const ok = await useConsoleStore.getState().setProjectThinkingLevel('low');
  assert.equal(ok, true);
  const state = useConsoleStore.getState();
  assert.equal(state.projectThinkingLevel, 'low');
  assert.equal(state.projectThinkingError, null);
  // The session's own level is untouched: a project default applies to sessions
  // opened afterwards.
  assert.equal(state.thinkingLevel, 'max');
});

test('a failed write keeps the previous default and publishes a visible reason', async () => {
  useConsoleStore.setState({
    client: fakeClient({
      setProjectThinkingLevel: async () => {
        throw new Error('invalid thinking level');
      },
    }) as never,
  });
  const ok = await useConsoleStore.getState().setProjectThinkingLevel('low');
  assert.equal(ok, false);
  const state = useConsoleStore.getState();
  assert.equal(state.projectThinkingLevel, 'high', 'no optimistic flip to roll back');
  assert.equal(state.projectThinkingError, 'invalid thinking level');
});

test('a read-only peer refuses without issuing any RPC', async () => {
  let calls = 0;
  useConsoleStore.setState({
    canSetProjectThinking: false,
    client: fakeClient({
      setProjectThinkingLevel: async () => {
        calls += 1;
        return { command_id: 'c1', project_id: 'p', level: 'low' };
      },
    }) as never,
  });
  const ok = await useConsoleStore.getState().setProjectThinkingLevel('low');
  assert.equal(ok, false);
  assert.equal(calls, 0);
  assert.equal(useConsoleStore.getState().projectThinkingLevel, 'high');
});

test('a result for a project the user switched away from is discarded', async () => {
  let switched = false;
  useConsoleStore.setState({
    client: fakeClient({
      setProjectThinkingLevel: async () => {
        switched = true;
        useConsoleStore.setState({ currentSession: { project_id: 'other', thread_id: 't2' } });
        return { command_id: 'c1', project_id: 'p', level: 'low' };
      },
    }) as never,
  });
  const ok = await useConsoleStore.getState().setProjectThinkingLevel('low');
  assert.equal(switched, true);
  assert.equal(ok, false);
  assert.equal(useConsoleStore.getState().projectThinkingLevel, 'high');
});
