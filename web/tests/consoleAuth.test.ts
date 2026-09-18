/**
 * Offline authentication tests for the console store (phase-5 A1 / C1-C8).
 *
 * They run under the Node built-in test runner with fake browser globals
 * (`fetch`, `WebSocket`, `window.location`, `localStorage`, `sessionStorage`).
 * No host, no daemon and no real socket is involved; every assertion is about
 * what the console does *before* it is authenticated:
 *
 * - no runtime socket is ever constructed while the session probe or the
 *   pairing call has not succeeded;
 * - a failed probe/pairing is an explicit, visible error state with a reason;
 * - concurrent or repeated `initClient` calls take effect once;
 * - business RPCs are refused with a diagnosable reason;
 * - the initial state carries no demo project/session/workspace values.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

interface RecordedRequest {
  url: string;
  init: RequestInit | undefined;
}

const requests: RecordedRequest[] = [];
let respond: (url: string, init?: RequestInit) => Response = () => new Response(null, { status: 204 });

function installFetch(next: (url: string, init?: RequestInit) => Response): void {
  requests.length = 0;
  respond = next;
  (globalThis as any).fetch = (url: unknown, init?: RequestInit) => {
    requests.push({ url: String(url), init });
    return Promise.resolve(respond(String(url), init));
  };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function headersOf(init: RequestInit | undefined): Record<string, string> {
  return (init?.headers ?? {}) as Record<string, string>;
}

/** Every socket the console opened, in construction order. */
const sockets: Array<{ url: string }> = [];

class FakeWebSocket {
  static readonly OPEN = 1;
  readyState = 0;
  readonly url: string;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    sockets.push({ url });
    // No live daemon in these tests: fail the handshake on the next microtask
    // so the connect promise settles instead of hanging the test runner.
    queueMicrotask(() => {
      if (this.readyState === 0) {
        this.readyState = 3;
        this.onerror?.();
      }
    });
  }

  send(): void {
    // No frames are expected before authentication.
  }

  close(): void {
    this.readyState = 3;
  }
}

const storageWrites: string[] = [];
function fakeStorage(name: string): unknown {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    key: (index: number) => [...map.keys()][index] ?? null,
    getItem: (key: string) => map.get(key) ?? null,
    setItem: (key: string, value: string) => {
      storageWrites.push(`${name}:${key}=${value}`);
      map.set(key, value);
    },
    removeItem: (key: string) => {
      map.delete(key);
    },
    clear: () => {
      map.clear();
    },
  };
}

(globalThis as any).window = { location: { protocol: 'http:', host: '127.0.0.1:8080' } };
(globalThis as any).WebSocket = FakeWebSocket;
(globalThis as any).localStorage = fakeStorage('local');
(globalThis as any).sessionStorage = fakeStorage('session');

const { RUNTIME_RPC_NOT_READY, useConsoleStore } = await import('../src/stores/useConsoleStore.ts');
const { RUNTIME_STATUS_PATH } = await import('../src/client/runtimeStatus.ts');

/** The pristine state of the store, captured before any test mutates it. */
const initialState = { ...useConsoleStore.getState() };

function resetStore(): void {
  useConsoleStore.setState({
    client: null,
    pairingState: 'unpaired',
    pairingError: null,
    rpcBlockedReason: null,
    connectionState: 'disconnected',
    recoveryState: 'idle',
    recoveryDetail: null,
    currentSession: { project_id: '', thread_id: '' },
    sessionTitle: '',
    sessions: [],
    sessionsNextOffset: null,
    sessionsTotal: 0,
    workspacePath: '',
    gitBranch: '',
    gitDirty: false,
    messages: [],
    activeTurnId: null,
    runtimeStatus: 'idle',
    steerQueueCount: 0,
    pendingApproval: null,
    activeSubscriptionId: null,
    modelName: '',
    availableModels: [],
    mcpServers: [],
    mcpEnabled: false,
    canSetThinking: false,
    canToggleMcpGlobal: false,
  });
}

/** The store intentionally logs refusals; the tests assert on state instead. */
async function muted<T>(fn: () => Promise<T>): Promise<T> {
  const warn = console.warn;
  const error = console.error;
  console.warn = () => {};
  console.error = () => {};
  try {
    const result = await fn();
    // Let fire-and-forget follow-up work (the connect attempt) settle so its
    // diagnostics are muted too.
    await new Promise((resolve) => setTimeout(resolve, 0));
    return result;
  } finally {
    console.warn = warn;
    console.error = error;
  }
}

test('C-06 initial state carries no demo project/session/workspace values', () => {
  assert.equal(initialState.currentSession.project_id, '');
  assert.equal(initialState.currentSession.thread_id, '');
  assert.notEqual(initialState.currentSession.project_id, 'default');
  assert.notEqual(initialState.currentSession.thread_id, '21');
  assert.equal(initialState.sessionTitle, '');
  assert.notEqual(initialState.sessionTitle, '21: Refactor WebSocket Transport');
  assert.equal(initialState.steerQueueCount, 0);
  assert.equal(initialState.workspacePath, '');
  assert.equal(initialState.gitBranch, '');
  assert.deepEqual(initialState.sessions, []);
  assert.equal(initialState.modelName, '');
  assert.deepEqual(initialState.availableModels, []);
  assert.deepEqual(initialState.thinkingLevels, []);
  assert.equal(initialState.metricsLabel, '');
  assert.equal(initialState.runtimeStatus, 'idle');
  assert.equal(initialState.mcpEnabled, false);
  assert.equal(initialState.client, null);
  assert.equal(initialState.rpcBlockedReason, null);
  assert.equal(initialState.pairingState, 'checking');
  assert.equal(initialState.pairingError, null);
});

test('C-05 a 401 session probe opens the pairing gate and never opens a socket', async () => {
  resetStore();
  sockets.length = 0;
  installFetch(() => jsonResponse({ error: 'unauthorized' }, 401));

  await muted(() => useConsoleStore.getState().initClient());

  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/session');
  const state = useConsoleStore.getState();
  assert.equal(state.pairingState, 'unpaired');
  assert.equal(state.connectionState, 'disconnected');
  assert.equal(state.client, null);
  assert.equal(sockets.length, 0);
  assert.equal(state.rpcBlockedReason, null);
});

test('C3 an unexpected session probe failure is a visible error state', async () => {
  for (const responder of [
    () => jsonResponse({ error: 'boom' }, 500),
    () => {
      throw new Error('connection refused');
    },
  ]) {
    resetStore();
    sockets.length = 0;
    installFetch(responder);

    await muted(() => useConsoleStore.getState().initClient());

    const state = useConsoleStore.getState();
    assert.equal(state.pairingState, 'error');
    assert.equal(state.connectionState, 'error');
    assert.ok((state.pairingError ?? '').length > 0);
    assert.equal(state.client, null);
    assert.equal(sockets.length, 0);
  }
});

test('C-04 concurrent and repeated initClient calls take effect exactly once', async () => {
  resetStore();
  sockets.length = 0;
  installFetch(() => jsonResponse({ project: { project_id: 'p1', workspace_path: '/w' } }));

  const first = useConsoleStore.getState().initClient();
  const second = useConsoleStore.getState().initClient();
  await muted(() => Promise.all([first, second]));

  const probes = () => requests.filter((call) => call.url === '/api/session');
  assert.equal(probes().length, 1, 'only one session probe may be issued');
  assert.equal(sockets.length, 1, 'only one runtime socket may be opened');
  assert.equal(useConsoleStore.getState().pairingState, 'paired');

  const before = requests.length;
  await muted(() => useConsoleStore.getState().initClient());
  assert.equal(requests.length, before, 'a repeated initClient must issue nothing new');
  assert.equal(probes().length, 1);
  assert.equal(sockets.length, 1);
});

test('C-07 the authenticated socket URL is derived from window.location without credentials', async () => {
  resetStore();
  sockets.length = 0;
  installFetch(() => jsonResponse({ project: { project_id: 'p1', workspace_path: '/w', git_branch: 'main' } }));

  await muted(() => useConsoleStore.getState().initClient());

  assert.equal(sockets.length, 1);
  assert.equal(sockets[0].url, 'ws://127.0.0.1:8080/runtime-ws');
  assert.equal(sockets[0].url.includes('?'), false);
  assert.equal(sockets[0].url.includes('#'), false);
  assert.equal(/token|code|secret|authorization/i.test(sockets[0].url), false);

  const state = useConsoleStore.getState();
  assert.equal(state.currentSession.project_id, 'p1');
  assert.equal(state.workspacePath, '/w');
  assert.equal(state.gitBranch, 'main');
  // No placeholder thread/title is invented before the session list is read.
  assert.equal(state.currentSession.thread_id, '');
  assert.equal(state.sessionTitle, '');
});

test('C-02 a failed pairing shows the reason and opens no socket or RPC', async () => {
  for (const status of [401, 403, 413, 415, 429]) {
    resetStore();
    sockets.length = 0;
    installFetch(() => jsonResponse({ error: 'nope' }, status));

    const ok = await muted(() => useConsoleStore.getState().submitPairingCode('ABCDEFGH'));

    assert.equal(ok, false);
    const state = useConsoleStore.getState();
    assert.equal(state.pairingState, 'error');
    assert.equal(state.connectionState, 'error');
    assert.ok((state.pairingError ?? '').length > 0);
    assert.equal(state.client, null);
    assert.equal(sockets.length, 0, `HTTP ${status} must not open a runtime socket`);
    assert.deepEqual(requests.map((call) => call.url), ['/api/pair']);
    assert.equal(requests[0].init?.method, 'POST');
  }

  resetStore();
  sockets.length = 0;
  installFetch(() => {
    throw new Error('connection refused');
  });
  const ok = await muted(() => useConsoleStore.getState().submitPairingCode('ABCDEFGH'));
  assert.equal(ok, false);
  assert.equal(useConsoleStore.getState().pairingState, 'error');
  assert.equal(sockets.length, 0);
});

test('C-03 a malformed pairing code is refused locally without any request', async () => {
  resetStore();
  sockets.length = 0;
  installFetch(() => jsonResponse({ project: { project_id: 'p1', workspace_path: '/w' } }));

  const ok = await muted(() => useConsoleStore.getState().submitPairingCode('abc'));

  assert.equal(ok, false);
  assert.equal(requests.length, 0);
  assert.equal(sockets.length, 0);
  const state = useConsoleStore.getState();
  assert.equal(state.pairingState, 'unpaired');
  assert.ok((state.pairingError ?? '').length > 0);
});

test('C-01 a successful pairing authenticates the console and opens one socket', async () => {
  resetStore();
  sockets.length = 0;
  installFetch(() => jsonResponse({ project: { project_id: 'p1', workspace_path: '/w' } }));

  const ok = await muted(() => useConsoleStore.getState().submitPairingCode('abcd-efgh'));

  assert.equal(ok, true);
  // Pairing itself issues exactly one request.  This fake socket fails its
  // handshake, which is a connect failure: since C3 that (and only that) also
  // triggers the read-only diagnostics read of `GET /api/runtime-status`.
  const urls = requests.map((call) => call.url);
  assert.equal(urls.filter((url) => url === '/api/pair').length, 1);
  assert.deepEqual(
    [...new Set(urls)].filter((url) => url !== RUNTIME_STATUS_PATH),
    ['/api/pair'],
  );
  const diagnostics = requests.find((call) => call.url === RUNTIME_STATUS_PATH);
  assert.ok(diagnostics, 'the connect failure must trigger the read-only diagnostics read');
  assert.equal(diagnostics?.init?.method, 'GET');
  assert.equal(diagnostics?.init?.body, undefined);
  assert.equal(headersOf(requests[0].init)['Content-Type'], 'application/json');
  assert.equal(headersOf(requests[0].init)['X-Synapse-Console'], '1');
  assert.equal(requests[0].init?.credentials, 'same-origin');
  assert.deepEqual(JSON.parse(String(requests[0].init?.body)), { code: 'ABCDEFGH' });

  const state = useConsoleStore.getState();
  assert.equal(state.pairingState, 'paired');
  assert.equal(state.pairingError, null);
  assert.ok(state.client);
  assert.equal(sockets.length, 1);
});

test('C-05 business RPCs are refused with a diagnosable reason before authentication', async () => {
  resetStore();
  sockets.length = 0;
  installFetch(() => jsonResponse({}, 200));

  await muted(async () => {
    const store = useConsoleStore;
    await store.getState().submitPrompt('hello');
    await store.getState().createNewSession();
    await store.getState().fetchSessions();
    await store.getState().cancelActiveTurn();
    await store.getState().loadEarlierHistory();
    await store.getState().switchSession('thread-1', 'title');
    await store.getState().loadSessionHistory({ project_id: 'p1', thread_id: 'thread-1' });
    await store.getState().fetchRuntimeConfig();
    await store.getState().setModel('openai:gpt-4o');
    await store.getState().toggleMcpServer('server');
    await store.getState().resolveApproval('allow_once');
  });

  const state = useConsoleStore.getState();
  assert.equal(state.rpcBlockedReason, RUNTIME_RPC_NOT_READY);
  assert.equal(requests.length, 0, 'no HTTP request may be issued before authentication');
  assert.equal(sockets.length, 0, 'no runtime socket may be opened before authentication');
  assert.deepEqual(state.messages, [], 'no local transcript mutation either');
  assert.equal(state.currentSession.project_id, '');
  assert.equal(state.currentSession.thread_id, '');
  assert.equal(state.modelName, '');
  assert.equal(state.sessions.length, 0);
});

test('C-08 logout invalidates the session, clears state and returns to the pairing gate', async () => {
  resetStore();
  sockets.length = 0;
  storageWrites.length = 0;
  installFetch((url) =>
    url === '/api/session'
      ? jsonResponse({ project: { project_id: 'p1', workspace_path: '/w', git_branch: 'main' } })
      : new Response(null, { status: 204 }),
  );

  await muted(() => useConsoleStore.getState().initClient());
  assert.equal(useConsoleStore.getState().pairingState, 'paired');
  useConsoleStore.setState({
    sessions: [{ thread_id: 'thread-1', title: 'T', updated_at: '', time_label: 'now' }],
    sessionTitle: 'T',
    currentSession: { project_id: 'p1', thread_id: 'thread-1' },
  });

  await muted(() => useConsoleStore.getState().logoutConsole());

  const logout = requests.find((call) => call.url === '/api/logout');
  assert.ok(logout, 'logout must invalidate the session server-side');
  assert.equal(logout.init?.method, 'POST');
  assert.equal(headersOf(logout.init)['Content-Type'], 'application/json');
  assert.equal(headersOf(logout.init)['X-Synapse-Console'], '1');
  assert.equal(logout.init?.credentials, 'same-origin');

  const state = useConsoleStore.getState();
  assert.equal(state.client, null);
  assert.equal(state.pairingState, 'unpaired');
  assert.equal(state.connectionState, 'disconnected');
  assert.equal(state.currentSession.project_id, '');
  assert.equal(state.currentSession.thread_id, '');
  assert.equal(state.sessionTitle, '');
  assert.deepEqual(state.sessions, []);
  assert.equal(state.workspacePath, '');
  assert.equal(state.gitBranch, '');
  assert.deepEqual(state.messages, []);
  assert.equal(storageWrites.length, 0, 'no console credential may be persisted');
});
