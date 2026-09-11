/**
 * Store-level read-only regression tests for the runtime config surface.
 *
 * The backend has no write path for the global MCP toggle or the thinking
 * level, so the console store must refuse those actions when the capability
 * flags are false — never pretending a save succeeded.  Uses only the Node
 * built-in test runner; the store is exercised without any WebSocket client.
 */
import { beforeEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { useConsoleStore } from '../src/stores/useConsoleStore.ts';

beforeEach(() => {
  useConsoleStore.setState({
    canSetThinking: false,
    canToggleMcpGlobal: false,
    thinkingLevel: 'high',
    thinkingLevels: ['low', 'medium', 'high', 'max'],
    mcpEnabled: true,
    mcpServers: [{ name: 'files', transport: 'stdio', enabled: true }],
  });
});

test('setThinkingLevel refuses to change state while read-only', async () => {
  await useConsoleStore.getState().setThinkingLevel('low');
  const state = useConsoleStore.getState();
  assert.equal(state.canSetThinking, false);
  assert.equal(state.thinkingLevel, 'high', 'read-only select must not mutate');
});

test('toggleMcpGlobal refuses to flip state while read-only', async () => {
  await useConsoleStore.getState().toggleMcpGlobal();
  const state = useConsoleStore.getState();
  assert.equal(state.canToggleMcpGlobal, false);
  assert.equal(state.mcpEnabled, true, 'read-only toggle must not mutate');
  assert.deepEqual(
    state.mcpServers.map((server) => server.enabled),
    [true],
    'server states must stay untouched',
  );
});

test('no client is fabricated and no RPC write is attempted while read-only', async () => {
  // These actions must be pure refusals: they must not require a client or
  // leave behind any observable "saved" state.
  await useConsoleStore.getState().toggleMcpGlobal();
  await useConsoleStore.getState().setThinkingLevel('max');
  const state = useConsoleStore.getState();
  assert.equal(state.thinkingLevel, 'high');
  assert.equal(state.mcpEnabled, true);
});
