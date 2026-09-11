/**
 * Store-level tests for the session-scoped reasoning-level write.
 *
 * The runtime exposes `runtime.session.thinking.set`, so the console must
 * perform a real write: an optimistic flip, a rollback plus a visible reason on
 * failure, a refusal while the capability flag is false, and no application of a
 * result that belongs to a session the user already switched away from.
 *
 * Uses only the Node built-in test runner; the store is exercised with a
 * hand-written client stub (no WebSocket).
 */
import { beforeEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { useConsoleStore } from '../src/stores/useConsoleStore.ts';

const SESSION = { project_id: 'p', thread_id: 't' };

const VIEW = {
  current_model: 'm',
  available_models: ['m'],
  thinking_level: 'low',
  thinking_levels: ['low', 'high'],
  mcp_servers: [],
  mcp_enabled: false,
  can_set_thinking: true,
  can_toggle_mcp_global: false,
};

function fakeClient(overrides: Record<string, unknown> = {}): unknown {
  return {
    getState: () => 'connected',
    connect: async () => undefined,
    openSession: async () => ({ created: false, view: null }),
    setThinkingLevel: async () => ({
      command_id: 'c1',
      session: SESSION,
      level: 'low',
      view: VIEW,
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
    canSetThinking: true,
    thinkingLevel: 'high',
    thinkingLevels: ['low', 'high'],
    thinkingLevelError: null,
    availableModels: [],
    modelName: 'keep-me',
  });
});

test('a successful write publishes the refreshed config view', async () => {
  const ok = await useConsoleStore.getState().setThinkingLevel('low');
  assert.equal(ok, true);
  const state = useConsoleStore.getState();
  assert.equal(state.thinkingLevel, 'low');
  assert.equal(state.thinkingLevelError, null);
  assert.deepEqual(state.availableModels, ['m']);
  assert.equal(state.modelName, 'keep-me', 'a config refresh must not reset the model');
});

test('a failed write rolls back and publishes a visible reason', async () => {
  useConsoleStore.setState({
    client: fakeClient({
      setThinkingLevel: async () => {
        throw new Error('thinking level not allowed');
      },
    }) as never,
  });
  const ok = await useConsoleStore.getState().setThinkingLevel('low');
  assert.equal(ok, false);
  const state = useConsoleStore.getState();
  assert.equal(state.thinkingLevel, 'high', 'the optimistic flip must be rolled back');
  assert.equal(state.thinkingLevelError, 'thinking level not allowed');
});

test('a read-only session refuses without issuing any RPC', async () => {
  let calls = 0;
  useConsoleStore.setState({
    canSetThinking: false,
    client: fakeClient({
      setThinkingLevel: async () => {
        calls += 1;
        return { command_id: 'c1', session: SESSION, level: 'low', view: VIEW };
      },
    }) as never,
  });
  const ok = await useConsoleStore.getState().setThinkingLevel('low');
  assert.equal(ok, false);
  assert.equal(calls, 0);
  assert.equal(useConsoleStore.getState().thinkingLevel, 'high');
});

test('a result for a session the user switched away from is discarded', async () => {
  useConsoleStore.setState({
    client: fakeClient({
      setThinkingLevel: async () => {
        // The user switches sessions while the write is in flight.
        useConsoleStore.setState({ currentSession: { project_id: 'p', thread_id: 'other' } });
        return { command_id: 'c1', session: SESSION, level: 'low', view: VIEW };
      },
    }) as never,
  });
  const ok = await useConsoleStore.getState().setThinkingLevel('low');
  assert.equal(ok, false);
  const state = useConsoleStore.getState();
  assert.deepEqual(state.availableModels, [], 'the stale view must not be applied');
  assert.equal(state.thinkingLevelError, null);
});
