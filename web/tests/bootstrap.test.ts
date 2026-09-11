/**
 * Offline contract tests for the console authentication client
 * (`POST /api/pair`, `GET /api/session`, `POST /api/logout`).
 *
 * They run under the Node built-in test runner with a fake `fetch`: no host, no
 * daemon and no browser are involved.  They pin the request shapes the phase-5
 * host contract (A1/A3/A4) requires, the strict whitelist parsing of the project
 * payload, the local pre-validation of pairing codes, and the fact that the
 * runtime socket URL never carries a query string or a credential.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  BootstrapError,
  CONSOLE_CSRF_HEADER,
  ConsoleAuthRequiredError,
  deriveRuntimeSocketUrl,
  fetchConsoleSession,
  normalizePairingCode,
  pairConsole,
  parseConsoleSession,
  requestConsoleLogout,
  sanitizePairingCodeInput,
} from '../src/client/bootstrap.ts';
import type { FetchLike } from '../src/client/bootstrap.ts';

interface RecordedRequest {
  url: string;
  init: RequestInit | undefined;
}

function recorder(responder: (url: string, init?: RequestInit) => Response) {
  const calls: RecordedRequest[] = [];
  const fetchImpl: FetchLike = async (url, init) => {
    calls.push({ url: String(url), init });
    return responder(String(url), init);
  };
  return { calls, fetchImpl };
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

test('C-01 pairConsole sends the CSRF-shaped POST the host contract requires', async () => {
  const { calls, fetchImpl } = recorder(() =>
    jsonResponse({ project: { project_id: 'p1', workspace_path: '/w' } }),
  );
  const project = await pairConsole('abcd-efgh', fetchImpl);

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/pair');
  assert.equal(calls[0].init?.method, 'POST');
  assert.equal(calls[0].init?.credentials, 'same-origin');
  const headers = headersOf(calls[0].init);
  assert.equal(headers['Content-Type'], 'application/json');
  assert.equal(headers[CONSOLE_CSRF_HEADER], '1');
  // Separators are stripped and the code is upper-cased before it is sent.
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { code: 'ABCDEFGH' });
  assert.deepEqual(project, {
    project_id: 'p1',
    workspace_path: '/w',
    workspace_name: null,
    git_branch: null,
  });
});

test('C-10 pairConsole copies only the project whitelist and drops unknown fields', async () => {
  const { fetchImpl } = recorder(() =>
    jsonResponse({
      token: 'daemon-secret-that-must-not-reach-the-ui',
      env: { MODEL: 'secret' },
      project: {
        project_id: 'p1',
        workspace_path: '/w',
        workspace_name: 'synapse',
        git_branch: 'feature/agent-runtime-service',
        internal_path: '/should/be/dropped',
      },
    }),
  );
  const project = await pairConsole('ABCDEFGH', fetchImpl);
  assert.deepEqual(project, {
    project_id: 'p1',
    workspace_path: '/w',
    workspace_name: 'synapse',
    git_branch: 'feature/agent-runtime-service',
  });
  assert.ok(!('token' in project));
  assert.equal(JSON.stringify(project).includes('secret'), false);
});

test('C-10 a payload without project_id or workspace_path is rejected', async () => {
  await assert.rejects(
    pairConsole('ABCDEFGH', recorder(() => jsonResponse({ project: { workspace_path: '/w' } })).fetchImpl),
    (error: unknown) =>
      error instanceof BootstrapError && error.message.includes('project_id'),
  );
  await assert.rejects(
    pairConsole('ABCDEFGH', recorder(() => jsonResponse({ project: { project_id: 'p1' } })).fetchImpl),
    (error: unknown) =>
      error instanceof BootstrapError && error.message.includes('workspace_path'),
  );
  await assert.rejects(
    pairConsole('ABCDEFGH', recorder(() => jsonResponse({})).fetchImpl),
    (error: unknown) =>
      error instanceof BootstrapError && error.message.includes('project'),
  );
});

test('C-03 malformed pairing codes are rejected locally and never sent', async () => {
  // '' / wrong length / I / L / O / U / punctuation are all outside the host
  // alphabet; 'abcdefg1' is deliberately absent because it *is* a valid code.
  const bad = ['', 'ABC', 'ABCDEFGHI', 'ABCDEFGI', 'ABCDEFGL', 'OOOOOOOO', '1234567\u0021'];
  for (const code of bad) {
    const { calls, fetchImpl } = recorder(() => jsonResponse({ project: {} }));
    await assert.rejects(pairConsole(code, fetchImpl), (error: unknown) => error instanceof BootstrapError);
    assert.equal(calls.length, 0, `no request may be sent for ${JSON.stringify(code)}`);
  }
});

test('C-02 pairConsole surfaces the host status without echoing the code', async () => {
  for (const status of [401, 403, 413, 415, 429]) {
    const { fetchImpl } = recorder(() => jsonResponse({ error: 'nope' }, status));
    await assert.rejects(
      pairConsole('ABCDEFGH', fetchImpl),
      (error: unknown) => {
        assert.ok(error instanceof BootstrapError);
        assert.equal(error.status, status);
        assert.equal(error.message.includes('ABCDEFGH'), false);
        return true;
      },
    );
  }
});

test('C-02 pairConsole surfaces a network failure as a typed error', async () => {
  const fetchImpl: FetchLike = () => Promise.reject(new Error('connection refused'));
  await assert.rejects(
    pairConsole('ABCDEFGH', fetchImpl),
    (error: unknown) =>
      error instanceof BootstrapError &&
      error.status === undefined &&
      error.message.includes('connection refused'),
  );
});

test('C-04 fetchConsoleSession restores a session, or signals that pairing is required', async () => {
  const ok = await fetchConsoleSession(
    recorder(() =>
      jsonResponse({
        project: { project_id: 'p1', workspace_path: '/w' },
        expires_in: 43200,
      }),
    ).fetchImpl,
  );
  assert.deepEqual(ok, {
    project: { project_id: 'p1', workspace_path: '/w', workspace_name: null, git_branch: null },
    expires_in: 43200,
  });

  const { calls, fetchImpl } = recorder(() => jsonResponse({ error: 'unauthorized' }, 401));
  await assert.rejects(
    fetchConsoleSession(fetchImpl),
    (error: unknown) => error instanceof ConsoleAuthRequiredError && error.status === 401,
  );
  // The session probe is a read-only GET: no CSRF headers, no body.
  assert.equal(calls[0].url, '/api/session');
  assert.equal(calls[0].init?.method, undefined);
  assert.equal(calls[0].init?.body, undefined);
  assert.equal(calls[0].init?.credentials, 'same-origin');

  await assert.rejects(
    fetchConsoleSession(recorder(() => jsonResponse({ error: 'boom' }, 500)).fetchImpl),
    (error: unknown) => error instanceof BootstrapError && error.status === 500,
  );
});

test('C-10 parseConsoleSession keeps the whitelist and tolerates an absent expires_in', () => {
  const parsed = parseConsoleSession({
    project: { project_id: 'p1', workspace_path: '/w', token: 'nope' },
    expires_in: 'not-a-number',
  });
  assert.deepEqual(parsed, {
    project: { project_id: 'p1', workspace_path: '/w', workspace_name: null, git_branch: null },
    expires_in: null,
  });
});

test('C-07 deriveRuntimeSocketUrl is same-origin, unparameterized and credential-free', () => {
  const http = deriveRuntimeSocketUrl({ protocol: 'http:', host: '127.0.0.1:8080' });
  assert.equal(http, 'ws://127.0.0.1:8080/runtime-ws');
  const https = deriveRuntimeSocketUrl({ protocol: 'https:', host: 'localhost:443' });
  assert.equal(https, 'wss://localhost:443/runtime-ws');
  for (const url of [http, https]) {
    assert.equal(url.includes('?'), false);
    assert.equal(url.includes('#'), false);
    assert.equal(/token|code|secret|authorization/i.test(url), false);
  }
});

test('C-09 pairing code input is sanitized to the Crockford alphabet', () => {
  assert.equal(sanitizePairingCodeInput(' abcd-efgh '), 'ABCDEFGH');
  assert.equal(sanitizePairingCodeInput('ab cd ef gh'), 'ABCDEFGH');
  assert.equal(sanitizePairingCodeInput('ab!cd@ef'), 'ABCDEF');
  // I/L/O/U are not part of the host alphabet and can never be typed in.
  assert.equal(sanitizePairingCodeInput('IOUL1234'), '1234');
  assert.equal(sanitizePairingCodeInput('0123456789ABCDEF'), '01234567');
  assert.equal(normalizePairingCode('abcd efgh'), 'ABCDEFGH');
  assert.equal(normalizePairingCode('abcd-efg'), null);
  assert.equal(normalizePairingCode('ABCDEFGI'), null);
  assert.equal(normalizePairingCode(''), null);
});

test('C-08 requestConsoleLogout posts the CSRF chain and reports failures', async () => {
  const { calls, fetchImpl } = recorder(() => new Response(null, { status: 204 }));
  await requestConsoleLogout(fetchImpl);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/logout');
  assert.equal(calls[0].init?.method, 'POST');
  assert.equal(calls[0].init?.credentials, 'same-origin');
  const headers = headersOf(calls[0].init);
  assert.equal(headers['Content-Type'], 'application/json');
  assert.equal(headers[CONSOLE_CSRF_HEADER], '1');

  await assert.rejects(
    requestConsoleLogout(recorder(() => jsonResponse({ error: 'nope' }, 401)).fetchImpl),
    (error: unknown) => error instanceof BootstrapError && error.status === 401,
  );
});
