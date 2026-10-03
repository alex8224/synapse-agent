/**
 * Store-level MCP runtime state: the console attaches like the TUI and reports
 * what the daemon actually loaded instead of the configured flag alone.
 *
 * The store is exercised with a stub runtime client: no WebSocket, no daemon.
 */
import { beforeEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { useConsoleStore } from '../src/stores/useConsoleStore.ts';
import type { ReloadMcpParams, ReloadMcpResult } from '../src/client/types.ts';

interface Call {
  params: Record<string, unknown>;
}

const calls: Call[] = [];
let behaviour: () => Promise<ReloadMcpResult> = async () => {
  throw new Error('no behaviour configured');
};

function result(overrides: Partial<ReloadMcpResult> = {}): ReloadMcpResult {
  return {
    command_id: 'c1',
    session: { project_id: 'p', thread_id: 't' },
    server: null,
    enabled: null,
    attached: true,
    active_servers: ['search'],
    tool_count: 2,
    warnings: [],
    tool_names: ['search__query', 'search__fetch'],
    servers: [
      {
        name: 'search',
        enabled: true,
        attached: true,
        include_tools: ['query'],
        discovered: ['query', 'fetch'],
        loaded: ['search__query', 'search__fetch'],
      },
      {
        name: 'off',
        enabled: false,
        attached: false,
        include_tools: [],
        discovered: [],
        loaded: [],
      },
    ],
    ...overrides,
  };
}

/** Minimal stand-in for SynapseRuntimeClient (only what the store calls). */
const stubClient = {
  getState: () => 'connected',
  reloadMcp: async (params: ReloadMcpParams) => {
    calls.push({ params: params as unknown as Record<string, unknown> });
    return behaviour();
  },
};

beforeEach(() => {
  calls.length = 0;
  behaviour = async () => result();
  useConsoleStore.setState({
    client: stubClient as never,
    pairingState: 'paired',
    currentSession: { project_id: 'p', thread_id: 't' },
    mcpEnabled: true,
    mcpServers: [
      { name: 'search', transport: 'streamable_http', enabled: true },
      { name: 'off', transport: 'stdio', enabled: false },
    ],
    mcpRuntime: {},
    mcpWarnings: [],
    mcpConnecting: false,
    mcpRuntimeKnown: false,
    mcpStatus: '1 on',
  });
});

test('the attach-all refresh names no server and records the real state', async () => {
  await useConsoleStore.getState().refreshMcpRuntime();
  assert.equal(calls.length, 1);
  assert.deepEqual(Object.keys(calls[0].params), ['session']);
  const state = useConsoleStore.getState();
  assert.equal(state.mcpConnecting, false);
  assert.equal(state.mcpRuntimeKnown, true);
  assert.equal(state.mcpRuntime.search.attached, true);
  assert.deepEqual(state.mcpRuntime.search.loaded, ['search__query', 'search__fetch']);
  assert.equal(state.mcpStatus, '1 on');
  assert.equal(state.mcpServers[0].attached, true);
});

test('the footer reports 启动中 while the attach RPC is still in flight', async () => {
  let release: (value: ReloadMcpResult) => void = () => {};
  behaviour = () =>
    new Promise<ReloadMcpResult>((resolve) => {
      release = resolve;
    });
  const pending = useConsoleStore.getState().refreshMcpRuntime();
  // Mid-flight: the previous label must not pretend the attach is not happening.
  assert.equal(useConsoleStore.getState().mcpConnecting, true);
  assert.equal(useConsoleStore.getState().mcpStatus, '启动中');
  release(result());
  await pending;
  assert.equal(useConsoleStore.getState().mcpStatus, '1 on');
});

test('a failed attach degrades to a warning instead of a false "attached"', async () => {
  behaviour = async () => {
    throw new Error('boom');
  };
  await useConsoleStore.getState().refreshMcpRuntime();
  const state = useConsoleStore.getState();
  assert.equal(state.mcpConnecting, false);
  assert.equal(state.mcpRuntimeKnown, false);
  assert.deepEqual(state.mcpWarnings, ['boom']);
  // The configured flag still says "on": the label must not pretend it runs.
  assert.equal(state.mcpStatus, '1 on');
  assert.equal(state.mcpServers[0].attached, undefined);
});

test('saving a tool whitelist sends include_tools and refreshes the state', async () => {
  await useConsoleStore.getState().saveMcpTools('search', ['query']);
  assert.deepEqual(calls[0].params, {
    session: { project_id: 'p', thread_id: 't' },
    server: 'search',
    include_tools: ['query'],
  });
  assert.equal(useConsoleStore.getState().mcpRuntimeKnown, true);
});

test('toggling a server persists the flag and merges the reported state', async () => {
  behaviour = async () =>
    result({
      server: 'search',
      enabled: false,
      attached: false,
      active_servers: [],
      tool_count: 0,
      tool_names: [],
      servers: [
        {
          name: 'search',
          enabled: false,
          attached: false,
          include_tools: [],
          discovered: [],
          loaded: [],
        },
      ],
    });
  await useConsoleStore.getState().toggleMcpServer('search');
  assert.deepEqual(calls[0].params, {
    session: { project_id: 'p', thread_id: 't' },
    server: 'search',
    enabled: false,
  });
  const state = useConsoleStore.getState();
  assert.equal(state.mcpServers[0].enabled, false);
  assert.equal(state.mcpRuntime.search.attached, false);
  assert.equal(state.mcpStatus, '0 on');
});

test('createNewSession resets MCP runtime state and triggers reloadMcp for the new session', async () => {
  // Seed state with an existing session having attached MCP
  useConsoleStore.setState({
    currentSession: { project_id: 'p', thread_id: 'old-thread' },
    mcpRuntime: {
      search: {
        attached: true,
        loaded: ['search__query'],
        discovered: ['query'],
        include_tools: ['query'],
      },
    },
    mcpRuntimeKnown: true,
    mcpServers: [{ name: 'search', transport: 'streamable_http', enabled: true, attached: true }],
  });

  const fullStub = {
    ...stubClient,
    createSession: async ({ project_id }: { project_id: string }) => ({
      command_id: 'cmd-1',
      session: { project_id, thread_id: 'new-thread-1' },
      title: 'New Session Title',
      created: true,
    }),
    openSession: async (session: unknown) => ({
      session,
      view: { latest_sequence: 0 },
    }),
    watchEvents: async () => ({ subscription_id: 'sub-new-1' }),
    getRuntimeConfig: async () => ({
      mcp_enabled: true,
      mcp_servers: [{ name: 'search', transport: 'streamable_http', enabled: true }],
      available_models: [],
      thinking_levels: [],
    }),
  };
  useConsoleStore.setState({ client: fullStub as never });

  await useConsoleStore.getState().createNewSession();

  await new Promise((resolve) => setTimeout(resolve, 20));

  const reloadCall = calls.find((c) => (c.params.session as { thread_id: string })?.thread_id === 'new-thread-1');
  assert.ok(reloadCall, 'createNewSession must trigger reloadMcp for the brand-new session');
  assert.deepEqual(reloadCall.params, {
    session: { project_id: 'p', thread_id: 'new-thread-1' },
  });
  const state = useConsoleStore.getState();
  assert.equal(state.currentSession.thread_id, 'new-thread-1');
  assert.equal(state.mcpRuntimeKnown, true);
  assert.equal(state.mcpRuntime.search.attached, true);
});
