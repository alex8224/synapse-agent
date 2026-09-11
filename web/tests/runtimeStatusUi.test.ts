/**
 * Offline tests for the C3 read-only runtime diagnostics surface.
 *
 * Scope (task C3-1..C3-4): the console must consume the host's
 * `GET /api/runtime-status` on the "relay unavailable / WS close 1011 /
 * connect failed" path, present `hint` / `endpoint` / `state_dir` readably, and
 * degrade silently (no request when unpaired, no request storm on repeated
 * failures, no new copy at all when the read is refused or fails).
 *
 * No host, no daemon, no real socket: `fetch` is a fake and every socket is an
 * injected `SocketLike`.
 */
import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';
import type { SocketLike } from '../src/client/SynapseRuntimeClient.ts';

interface RecordedRequest {
  url: string;
  init: RequestInit | undefined;
}

const requests: RecordedRequest[] = [];
let respond: (url: string, init?: RequestInit) => Response = () =>
  new Response(null, { status: 204 });

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

function tick(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

(globalThis as any).window = { location: { protocol: 'http:', host: '127.0.0.1:8080' } };
installFetch(() => jsonResponse({}));

const {
  RUNTIME_STATUS_PATH,
  RUNTIME_STATUS_FIELD_LIMIT,
  RuntimeStatusUnavailableError,
  parseRuntimeStatusPayload,
} = await import('../src/client/runtimeStatus.ts');
const { SynapseRuntimeClient, ConnectionLostError, describeSocketClose } = await import(
  '../src/client/SynapseRuntimeClient.ts'
);
const { RUNTIME_DIAGNOSTICS_IDLE, selectRuntimeDiagnosticsBanner } = await import(
  '../src/stores/runtimeDiagnosticsView.ts'
);
const { resetRuntimeDiagnostics, useConsoleStore } = await import(
  '../src/stores/useConsoleStore.ts'
);

const STATE_DIR = 'C:/Users/example/.synapse/runtime';
const HINT = `start synapse-runtime --state-dir ${STATE_DIR}`;
const PAYLOAD = {
  runtime: {
    endpoint: { host: '127.0.0.1', port: 8765 },
    state_dir: STATE_DIR,
    hint: HINT,
  },
};

function load(options?: { trigger?: string; detail?: string | null; force?: boolean }) {
  return useConsoleStore.getState().loadRuntimeDiagnostics(options);
}

function snapshot() {
  return useConsoleStore.getState().runtimeDiagnostics;
}

beforeEach(() => {
  resetRuntimeDiagnostics();
  installFetch(() => jsonResponse(PAYLOAD));
  useConsoleStore.setState({
    pairingState: 'paired',
    connectionState: 'disconnected',
    runtimeDiagnostics: RUNTIME_DIAGNOSTICS_IDLE,
  });
});

test('C3-1 a successful read presents endpoint, state dir and hint readably', async () => {
  await load({ trigger: 'relay_unavailable', detail: 'connection closed (code 1011)' });

  const state = snapshot();
  assert.equal(state.status, 'ready');
  assert.deepEqual(state.view, {
    endpoint: { host: '127.0.0.1', port: 8765 },
    state_dir: STATE_DIR,
    hint: HINT,
  });

  const model = selectRuntimeDiagnosticsBanner(state, { connected: false });
  assert.ok(model, 'a ready snapshot must render');
  const text = model.facts.join('\n');
  assert.ok(text.includes('127.0.0.1:8765'), 'endpoint must be shown');
  assert.ok(text.includes(STATE_DIR), 'state dir must be shown');
  assert.ok(text.includes(HINT), 'hint must be shown');
  assert.ok(text.includes('code 1011'), 'the close detail must be shown');
});

test('C3-3 the rendered copy states facts only (no verdict wording)', async () => {
  await load({ trigger: 'relay_unavailable' });
  const model = selectRuntimeDiagnosticsBanner(snapshot(), { connected: false });
  assert.ok(model);
  const rendered = [model.title, ...model.facts, model.note].join('\n');
  for (const word of ['验收', '通过', '安全', '已修复', 'verified']) {
    assert.equal(rendered.includes(word), false, `copy must not contain ${word}`);
  }
});

test('C3-2 a 401 answer degrades silently to the previous copy', async () => {
  installFetch(() => jsonResponse({ error: 'missing or invalid console session' }, 401));
  await load({ trigger: 'connect_failed' });

  const state = snapshot();
  assert.equal(state.status, 'unavailable');
  assert.equal(state.reason, 'unauthorized');
  assert.equal(state.view, null);
  assert.equal(selectRuntimeDiagnosticsBanner(state, { connected: false }), null);

  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, RUNTIME_STATUS_PATH);
  assert.equal(requests[0].init?.body, undefined, 'a read must never send a body');
  const headers = (requests[0].init?.headers ?? {}) as Record<string, string>;
  assert.deepEqual(
    Object.keys(headers).map((key) => key.toLowerCase()),
    ['accept'],
    'only an Accept header is sent (no credential header, no CSRF value)',
  );
});

test('C3-2 a 403 answer (forbidden Host) also degrades silently', async () => {
  installFetch(() => jsonResponse({ error: 'forbidden host' }, 403));
  await load({ trigger: 'connect_failed' });
  assert.equal(snapshot().reason, 'unauthorized');
  assert.equal(selectRuntimeDiagnosticsBanner(snapshot(), { connected: false }), null);
});

test('C3-2 a network failure degrades silently and never rejects', async () => {
  installFetch(() => {
    throw new Error('network down');
  });
  await load({ trigger: 'relay_unavailable' });
  assert.equal(snapshot().status, 'unavailable');
  assert.equal(snapshot().reason, 'network');
  assert.equal(selectRuntimeDiagnosticsBanner(snapshot(), { connected: false }), null);
});

test('C3-2 an unreadable or malformed body degrades silently', async () => {
  installFetch(() => new Response('not json', { status: 200 }));
  await load({ trigger: 'relay_unavailable' });
  assert.equal(snapshot().reason, 'malformed');

  resetRuntimeDiagnostics();
  installFetch(() => jsonResponse({ runtime: { hint: HINT } }, 200));
  await load({ trigger: 'relay_unavailable' });
  assert.equal(snapshot().status, 'unavailable');
  assert.equal(snapshot().reason, 'malformed');
});

test('C3-2 an unpaired browser never requests the endpoint', async () => {
  for (const pairingState of ['unpaired', 'checking', 'error', 'pairing'] as const) {
    resetRuntimeDiagnostics();
    installFetch(() => jsonResponse(PAYLOAD));
    useConsoleStore.setState({ pairingState });
    await load({ trigger: 'relay_unavailable', force: true });
    assert.equal(requests.length, 0, `pairingState=${pairingState} must not read the endpoint`);
    assert.equal(snapshot().status, 'idle');
  }
});

test('C3-2 repeated failures issue exactly one request', async () => {
  installFetch(() => jsonResponse({ error: 'runtime service error' }, 500));
  await Promise.all([
    load({ trigger: 'connect_failed' }),
    load({ trigger: 'connect_failed' }),
    load({ trigger: 'relay_unavailable' }),
  ]);
  assert.equal(requests.length, 1, 'concurrent triggers must share one read');
  await load({ trigger: 'relay_unavailable' });
  assert.equal(requests.length, 1, 'a later failure must not re-read');
  assert.equal(snapshot().status, 'unavailable');
  assert.equal(snapshot().reason, 'http');

  // An explicit user refresh is the only path that reads again.
  installFetch(() => jsonResponse(PAYLOAD));
  await load({ trigger: 'manual', force: true });
  assert.equal(requests.length, 1, 'a forced refresh issues exactly one read');
  assert.equal(snapshot().status, 'ready');
});

test('C3-2 the request URL never carries response-derived content', async () => {
  installFetch(() =>
    jsonResponse({
      runtime: {
        endpoint: { host: '127.0.0.1', port: 8765 },
        state_dir: `${STATE_DIR}?redirect=//evil.example&probe=1`,
        hint: `${HINT} # fragment`,
        // Extra fields (whatever they are) must never reach console state.
        unexpected_extra: 'must-not-be-rendered',
      },
    }),
  );
  await load({ trigger: 'relay_unavailable' });

  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, RUNTIME_STATUS_PATH);
  assert.equal(requests[0].url.includes('?'), false, 'no query string is ever built');
  assert.equal(requests[0].url.includes('evil.example'), false);

  const state = snapshot();
  assert.equal(state.status, 'ready');
  assert.deepEqual(Object.keys(state.view ?? {}).sort(), ['endpoint', 'hint', 'state_dir']);
  assert.equal(JSON.stringify(state.view).includes('must-not-be-rendered'), false);
});

test('C3-1 the banner is withdrawn once the relay is connected again', async () => {
  await load({ trigger: 'relay_unavailable' });
  assert.ok(selectRuntimeDiagnosticsBanner(snapshot(), { connected: false }));
  assert.equal(selectRuntimeDiagnosticsBanner(snapshot(), { connected: true }), null);
});

test('C3-1 an unknown endpoint or missing hint is stated, not invented', async () => {
  installFetch(() => jsonResponse({ runtime: { endpoint: null, state_dir: STATE_DIR } }));
  await load({ trigger: 'relay_unavailable' });
  const model = selectRuntimeDiagnosticsBanner(snapshot(), { connected: false });
  assert.ok(model);
  assert.ok(model.facts.some((line) => line.startsWith('daemon endpoint: unknown')));
  assert.ok(model.facts.some((line) => line.startsWith('hint: (host reported no start hint)')));
});

test('C3-2 oversized fields are capped before rendering', async () => {
  installFetch(() =>
    jsonResponse({
      runtime: { state_dir: 'x'.repeat(RUNTIME_STATUS_FIELD_LIMIT + 50), hint: HINT },
    }),
  );
  await load({ trigger: 'relay_unavailable' });
  const state = snapshot();
  assert.equal(state.view?.state_dir.length, RUNTIME_STATUS_FIELD_LIMIT);
});

test('parser rejects a body without a usable state dir and drops an invalid endpoint', () => {
  for (const payload of [null, 42, {}, { runtime: null }, { runtime: {} }, { runtime: { state_dir: '' } }]) {
    assert.throws(
      () => parseRuntimeStatusPayload(payload),
      (err: unknown) => err instanceof RuntimeStatusUnavailableError && err.reason === 'malformed',
    );
  }
  const view = parseRuntimeStatusPayload({
    runtime: { state_dir: STATE_DIR, endpoint: { host: '127.0.0.1', port: '8765' } },
  });
  assert.equal(view.endpoint, null, 'a non-numeric port is not a usable endpoint');
});

test('C3-1 a 1011 close surfaces the frozen reason to the state callback', async () => {
  class ClosingSocket implements SocketLike {
    readyState = 0;
    onopen: (() => void) | null = null;
    onmessage: ((ev: { data: any }) => void) | null = null;
    onerror: (() => void) | null = null;
    onclose: ((ev?: { code?: number; reason?: string }) => void) | null = null;
    sent: string[] = [];

    send(data: string): void {
      // The handshake never answers: the close arrives while negotiating.
      this.sent.push(data);
    }

    close(): void {
      this.readyState = 3;
    }

    serverOpen(): void {
      this.readyState = 1;
      this.onopen?.();
    }

    serverClose(code: number, reason: string): void {
      this.readyState = 3;
      const cb = this.onclose;
      this.onclose = null;
      cb?.({ code, reason });
    }
  }

  const socket = new ClosingSocket();
  const seen: Array<{ state: string; reason?: string }> = [];
  const client = new SynapseRuntimeClient({
    url: 'ws://127.0.0.1:8080/runtime-ws',
    socketFactory: () => socket,
    onStateChange: (state, reason) => seen.push({ state, reason }),
  });
  const failure = client.connect().catch((err: unknown) => err);
  await tick();
  socket.serverOpen();
  await tick();
  socket.serverClose(1011, 'runtime daemon unavailable');

  const err = await failure;
  assert.ok(err instanceof ConnectionLostError);
  assert.equal(seen[seen.length - 1].state, 'error');
  assert.equal(
    seen[seen.length - 1].reason,
    'connection closed (code 1011: runtime daemon unavailable)',
    'the frozen host close reason must reach the store trigger',
  );
});

test('close descriptions stay bounded and never invent a code or reason', () => {
  assert.equal(describeSocketClose(), 'connection closed');
  assert.equal(describeSocketClose({ code: 1006 }), 'connection closed (code 1006)');
  assert.equal(describeSocketClose({ reason: 'gone' }), 'connection closed (gone)');
  const long = describeSocketClose({ code: 1011, reason: 'y'.repeat(400) });
  assert.ok(long.length < 200, 'a close reason must be capped before it is rendered');
});

test('C3-1 the store reads the endpoint when the relay closes 1011', async () => {
  // The host upgrades the socket and then closes it with the frozen
  // `1011 runtime daemon unavailable` frame (daemon not reachable).  The store
  // must turn that into one read of the diagnostics endpoint.
  class RelayDownSocket {
    static readonly OPEN = 1;
    readyState = 0;
    onopen: (() => void) | null = null;
    onmessage: ((event: { data: unknown }) => void) | null = null;
    onerror: (() => void) | null = null;
    onclose: ((event?: { code?: number; reason?: string }) => void) | null = null;

    constructor() {
      queueMicrotask(() => {
        this.readyState = 1;
        this.onopen?.();
        setTimeout(() => {
          this.readyState = 3;
          this.onclose?.({ code: 1011, reason: 'runtime daemon unavailable' });
        }, 0);
      });
    }

    send(): void {
      // The handshake is never answered: the close arrives while negotiating.
    }

    close(): void {
      this.readyState = 3;
    }
  }

  (globalThis as any).WebSocket = RelayDownSocket;
  resetRuntimeDiagnostics();
  useConsoleStore.setState({
    pairingState: 'unpaired',
    client: null,
    connectionState: 'disconnected',
    runtimeDiagnostics: RUNTIME_DIAGNOSTICS_IDLE,
  });
  installFetch((url) =>
    url === RUNTIME_STATUS_PATH
      ? jsonResponse(PAYLOAD)
      : jsonResponse({ project: { project_id: 'p1', workspace_path: '/w' } }),
  );

  await useConsoleStore.getState().submitPairingCode('ABCDEFGH');
  await tick();
  await tick();

  const state = snapshot();
  assert.equal(state.status, 'ready');
  assert.equal(state.trigger, 'connect_failed');
  assert.ok(
    (state.detail ?? '').includes('code 1011'),
    `the close code must reach the store (got ${state.detail})`,
  );
  assert.ok((state.detail ?? '').includes('runtime daemon unavailable'));
  assert.deepEqual(
    requests.map((call) => call.url).filter((url) => url !== '/api/pair'),
    [RUNTIME_STATUS_PATH],
    'exactly one diagnostics read must be issued',
  );
  const model = selectRuntimeDiagnosticsBanner(state, { connected: false });
  assert.ok(model);
  assert.ok(model.facts.some((line) => line.includes('code 1011')));
});