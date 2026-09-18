/**
 * Boundary tests for the extracted shared runtime core (`src/runtime-client/`).
 *
 * They run under the Node built-in test runner with no DOM, no browser bootstrap
 * and no real socket, and they pin the three properties the extraction has to
 * keep:
 *
 * 1. protocol closure: an injected fake `SocketLike` drives connect ->
 *    negotiate -> submit -> watch -> reconnect against the core alone;
 * 2. core purity: no browser or UI dependency (window / location / storage /
 *    React / zustand / host bootstrap import) is reachable from the core
 *    sources, and the host `WebSocket` is only ever read behind a guard;
 * 3. compatibility: every legacy path (`src/client/*`,
 *    `src/stores/recoveryDecider.ts`) is a pure re-export of the very same
 *    implementation, so existing callers and the offline tests keep working.
 */
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import {
  ConnectionLostError,
  RpcCallError,
  parseNegotiateResult,
  parseRuntimeEvent,
  SynapseRuntimeClient,
} from '../src/runtime-client/SynapseRuntimeClient.ts';
import type {
  RecoveryInfo,
  SocketLike,
} from '../src/runtime-client/SynapseRuntimeClient.ts';
import type { RuntimeEvent } from '../src/runtime-client/types.ts';
import { EVENT_VERSION } from '../src/runtime-client/contract.generated.ts';

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = join(here, '..');
const coreDir = join(webRoot, 'src', 'runtime-client');

const SESSION = { project_id: 'proj', thread_id: 'thr' };
const CAPABILITIES = {
  legacy_v1: true,
  raw_cursor: true,
  watch_resume: true,
  approval_resume: true,
};
const EVENT: RuntimeEvent = {
  sequence: 4,
  turn_sequence: 1,
  turn_id: 'turn-1',
  kind: 'answer_delta',
  payload: { text: 'hi' },
  version: 1,
};

interface SentFrame {
  id: number;
  method: string;
  params: any;
}

/**
 * Minimal fake transport: it answers the negotiate handshake and the three
 * business methods the closed loop needs, and it can deliver events or drop the
 * connection on demand.
 */
class FakeSocket implements SocketLike {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: any }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((ev?: { code?: number; reason?: string }) => void) | null = null;
  sent: SentFrame[] = [];

  send(data: string): void {
    const request = JSON.parse(data) as SentFrame;
    this.sent.push(request);
    this.push({
      jsonrpc: '2.0',
      id: request.id,
      meta: { wire_version: '1' },
      result: this.reply(request),
    });
  }

  reply(request: SentFrame): unknown {
    switch (request.method) {
      case 'runtime.protocol.negotiate':
        return { wire_version: '1', supported_versions: ['1'], capabilities: CAPABILITIES };
      case 'runtime.session.open':
        return {
          command_id: 'cmd-open',
          session: request.params.session,
          created: false,
          view: {
            project_id: SESSION.project_id,
            thread_id: SESSION.thread_id,
            status: 'idle',
            active_turn_id: null,
            latest_sequence: 0,
            usage: { input_tokens: 0, output_tokens: 0, cache_tokens: 0 },
            last_error: null,
            last_activity_at: '2024-01-01T00:00:00Z',
            active_model: null,
            model: null,
          },
        };
      case 'runtime.turn.submit':
        return {
          command_id: 'cmd-1',
          session: request.params.session,
          turn_id: 'turn-1',
          accepted: true,
        };
      case 'runtime.events.watch':
        return { subscription_id: 'sub-1', cursor: request.params.after };
      case 'runtime.events.unwatch':
        return { removed: true };
      case 'runtime.project.list':
        return {
          projects: [
            {
              project_id: 'proj',
              workspace_name: 'proj',
              git_branch: 'main',
              workspace_path: '/w/proj',
            },
          ],
          next_offset: null,
          total: 1,
        };
      default:
        return {};
    }
  }

  push(frame: unknown): void {
    const text = typeof frame === 'string' ? frame : JSON.stringify(frame);
    this.onmessage?.({ data: text });
  }

  /**
   * Deliver one live `runtime.event` notification at the given cursor.  The
   * daemon pushes the event's own session sequence as the cursor
   * (`cursor = stream.cursor.sequence`), so the helper keeps both consistent; a
   * deliberately inconsistent frame is pushed raw through `push`.
   */
  notify(cursor: number, event: RuntimeEvent = EVENT): void {
    this.push({
      jsonrpc: '2.0',
      method: 'runtime.event',
      params: { subscription_id: 'sub-1', cursor, event: { ...event, sequence: cursor } },
    });
  }

  serverOpen(): void {
    this.readyState = 1;
    this.onopen?.();
  }

  serverDrop(code = 1011, reason = 'runtime daemon unavailable'): void {
    this.readyState = 3;
    const cb = this.onclose;
    this.onclose = null;
    cb?.({ code, reason });
  }

  close(): void {
    if (this.readyState === 3) return;
    this.readyState = 3;
    const cb = this.onclose;
    this.onclose = null;
    cb?.();
  }
}

class Factory {
  sockets: FakeSocket[] = [];
  make = (): FakeSocket => {
    const socket = new FakeSocket();
    this.sockets.push(socket);
    return socket;
  };
  get last(): FakeSocket {
    return this.sockets[this.sockets.length - 1];
  }
  get calls(): number {
    return this.sockets.length;
  }
}

const tick = (ms = 0) => new Promise<void>((resolve) => setTimeout(resolve, ms));

async function waitFor(predicate: () => boolean, label: string, timeoutMs = 2000): Promise<void> {
  const started = Date.now();
  while (!predicate()) {
    if (Date.now() - started > timeoutMs) throw new Error('timed out waiting for ' + label);
    await tick(2);
  }
}

/** Connect one client through the injected factory and let the host accept it. */
async function openClient(client: SynapseRuntimeClient, factory: Factory): Promise<FakeSocket> {
  const connecting = client.connect();
  const socket = factory.last;
  socket.serverOpen();
  await connecting;
  return socket;
}

test('the shared core closes connect -> negotiate -> submit -> watch on an injected socket', async () => {
  const factory = new Factory();
  const states: string[] = [];
  const events: RuntimeEvent[] = [];
  const client = new SynapseRuntimeClient({
    url: 'ws://core',
    socketFactory: factory.make,
    onStateChange: (state) => states.push(state),
    onEvent: (event) => events.push(event),
  });

  const socket = await openClient(client, factory);
  assert.deepEqual(states, ['connecting', 'connected']);
  assert.equal(client.getState(), 'connected');
  assert.equal(client.getGeneration(), 1);
  assert.equal(factory.calls, 1);

  const handshake = socket.sent[0];
  assert.equal(handshake.method, 'runtime.protocol.negotiate');
  assert.deepEqual(handshake.params.versions, ['1']);

  const opened = await client.openSession(SESSION);
  assert.equal(opened.session.thread_id, SESSION.thread_id);
  assert.equal(opened.view?.latest_sequence, 0);

  const receipt = await client.submitTurn({ session: SESSION, text: 'hello' });
  assert.equal(receipt.command_id, 'cmd-1');

  const watch = await client.watchEvents(SESSION);
  assert.equal(watch.subscription_id, 'sub-1');
  assert.equal(client.getWatchCursor(), 0);
  assert.equal(client.getWatchSession()?.thread_id, SESSION.thread_id);

  socket.notify(4);
  assert.equal(client.getWatchCursor(), 4);
  assert.equal(events.length, 1);
  assert.equal(events[0].turn_id, 'turn-1');

  await client.unwatchEvents();
  assert.equal(client.getWatchCursor(), null);
  assert.equal(client.getWatchSession(), null);

  client.disconnect();
  assert.equal(client.getState(), 'disconnected');
});

test('the console lists projects over the shared RPC, never a client-supplied scope', async () => {
  const factory = new Factory();
  const client = new SynapseRuntimeClient({ url: 'ws://core', socketFactory: factory.make });
  const socket = await openClient(client, factory);

  const page = await client.listProjects();
  assert.equal(page.total, 1);
  assert.equal(page.projects[0].project_id, 'proj');
  assert.equal(page.projects[0].workspace_path, '/w/proj');

  const frame = socket.sent[socket.sent.length - 1];
  assert.equal(frame.method, 'runtime.project.list');
  // The browser sends pagination only: the visible set is computed server-side
  // and cannot be widened (or narrowed) from here.
  assert.deepEqual(Object.keys(frame.params).sort(), ['limit', 'offset']);
  assert.equal(frame.params.offset, 0);

  client.disconnect();
});

test('an unexpected drop is recovered by the bounded budget and the watch resumes from the last cursor', async () => {
  const factory = new Factory();
  const recovery: RecoveryInfo[] = [];
  const client = new SynapseRuntimeClient({
    url: 'ws://core',
    socketFactory: factory.make,
    reconnect: { maxAttempts: 3, baseDelayMs: 1, maxDelayMs: 2 },
    onRecovery: (info) => recovery.push(info),
  });

  const first = await openClient(client, factory);
  await client.watchEvents(SESSION);
  first.notify(9);
  assert.equal(client.getWatchCursor(), 9);

  first.serverDrop();
  const scheduled = recovery[recovery.length - 1];
  assert.equal(scheduled.phase, 'reconnecting');
  assert.equal(scheduled.attempt, 1);
  assert.equal(client.getState(), 'connecting');

  // The budget opens a fresh socket on a timer; the host then accepts it.
  await waitFor(() => factory.calls === 2, 'the reconnect socket');
  factory.last.serverOpen();
  await waitFor(() => client.getState() === 'connected', 'reconnected');
  assert.equal(client.getGeneration(), 2);
  assert.equal(recovery[recovery.length - 1].phase, 'reconnected');

  const cursor = client.getWatchCursor();
  assert.equal(cursor, 9);
  const resumed = await client.watchEvents(SESSION, cursor ?? undefined);
  assert.equal(resumed.subscription_id, 'sub-1');
  assert.equal(factory.last.sent[factory.last.sent.length - 1].params.after, 9);

  client.disconnect();
  assert.equal(client.getState(), 'disconnected');
});

test('a host without a WebSocket rejects connect() with a typed error instead of throwing', async () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'WebSocket');
  try {
    delete (globalThis as any).WebSocket;
    const client = new SynapseRuntimeClient({ url: 'ws://core' });
    await assert.rejects(
      client.connect(),
      (err: unknown) =>
        err instanceof ConnectionLostError && err.unknownOutcome === false,
    );
    assert.equal(client.getState(), 'error');
  } finally {
    if (descriptor) Object.defineProperty(globalThis, 'WebSocket', descriptor);
  }
});

test('the core sources carry no browser bootstrap or UI dependency', () => {
  const coreFiles: string[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name.endsWith('.ts')) coreFiles.push(full);
    }
  };
  walk(coreDir);
  assert.ok(coreFiles.length >= 5, 'the core must actually contain the moved modules');
  assert.ok(
    coreFiles.some((file) => file.endsWith('SynapseRuntimeClient.ts')),
    'the protocol client must live in the core',
  );

  const forbidden: Array<{ name: string; pattern: RegExp }> = [
    { name: 'browser window global', pattern: /\bwindow\s*\./ },
    { name: 'browser document global', pattern: /\bdocument\s*\./ },
    { name: 'browser location global', pattern: /\blocation\s*\./ },
    { name: 'browser navigator global', pattern: /\bnavigator\s*\./ },
    { name: 'browser storage', pattern: /\b(localStorage|sessionStorage)\b/ },
    { name: 'bundler env flag', pattern: /import\.meta\.env/ },
    { name: 'unguarded WebSocket constructor', pattern: /new\s+WebSocket\s*\(/ },
    { name: 'React import', pattern: /from\s+['"]react/ },
    { name: 'zustand import', pattern: /from\s+['"]zustand/ },
    { name: 'host process global', pattern: /\bprocess\s*\./ },
    { name: 'python host module', pattern: /aiohttp/ },
    { name: 'core -> view/host import', pattern: /from\s+['"]\.\.\// },
  ];
  for (const file of coreFiles) {
    const text = readFileSync(file, 'utf8');
    for (const rule of forbidden) {
      assert.equal(
        rule.pattern.test(text),
        false,
        file.replace(webRoot, '') + ' must not contain a ' + rule.name,
      );
    }
  }
});

/**
 * The legacy module must expose exactly the core module's runtime exports, with
 * identical bindings: a partial alias (or a second implementation) fails here.
 */
async function assertPureAlias(coreSpecifier: string, legacySpecifier: string): Promise<void> {
  const core = (await import(coreSpecifier)) as Record<string, unknown>;
  const legacy = (await import(legacySpecifier)) as Record<string, unknown>;
  const names = Object.keys(core).sort();
  assert.ok(names.length > 0, coreSpecifier + ' must export runtime values');
  assert.deepEqual(
    Object.keys(legacy).sort(),
    names,
    legacySpecifier + ' must re-export every core export and nothing else',
  );
  for (const name of names) {
    assert.equal(legacy[name], core[name], legacySpecifier + ' must re-export ' + name);
  }
}

test('the legacy paths re-export the shared core implementation', async () => {
  const core = await import('../src/runtime-client/SynapseRuntimeClient.ts');
  const legacy = await import('../src/client/SynapseRuntimeClient.ts');
  assert.equal(legacy.SynapseRuntimeClient, core.SynapseRuntimeClient);
  assert.equal(legacy.ConnectionLostError, core.ConnectionLostError);
  assert.equal(legacy.RpcCallError, core.RpcCallError);

  const aliases: Array<[string, string]> = [
    ['../src/runtime-client/SynapseRuntimeClient.ts', '../src/client/SynapseRuntimeClient.ts'],
    ['../src/runtime-client/types.ts', '../src/client/types.ts'],
    ['../src/runtime-client/artifacts.ts', '../src/client/artifacts.ts'],
    ['../src/runtime-client/artifactsDiff.ts', '../src/client/artifactsDiff.ts'],
    ['../src/runtime-client/recoverability.ts', '../src/client/recoverability.ts'],
    ['../src/runtime-client/recoveryDecider.ts', '../src/stores/recoveryDecider.ts'],
  ];
  for (const [coreSpecifier, legacySpecifier] of aliases) {
    await assertPureAlias(coreSpecifier, legacySpecifier);
  }

  // The legacy files stay aliases: no second implementation may reappear there.
  const legacyFiles: Array<[string, string]> = [
    ['client', 'SynapseRuntimeClient.ts'],
    ['client', 'types.ts'],
    ['client', 'artifacts.ts'],
    ['client', 'artifactsDiff.ts'],
    ['client', 'recoverability.ts'],
    ['stores', 'recoveryDecider.ts'],
  ];
  for (const [dir, name] of legacyFiles) {
    const text = readFileSync(join(webRoot, 'src', dir, name), 'utf8');
    const reexports = text.match(/^export \* from '\.\.\/runtime-client\/[^']+';$/gm) ?? [];
    assert.equal(reexports.length, 1, dir + '/' + name + ' must be a single re-export');
    assert.equal(/\bclass\s+\w+/.test(text), false, dir + '/' + name + ' must not hold an implementation');
  }
});

test('a wrong-version frame fences its subscription so the next legal event cannot jump the gap', async () => {
  const factory = new Factory();
  const events: RuntimeEvent[] = [];
  const notices: Array<{ type: string; service_code?: string; subscription_id?: string }> = [];
  const client = new SynapseRuntimeClient({
    url: 'ws://core',
    socketFactory: factory.make,
    onEvent: (event) => events.push(event),
    onSubscriptionNotice: (notice) => notices.push(notice),
  });
  const socket = await openClient(client, factory);
  await client.watchEvents(SESSION);

  socket.notify(4);
  assert.equal(client.getWatchCursor(), 4);
  assert.equal(events.length, 1);

  // v1 is the only envelope this client speaks: a future version is not
  // "compatible additive", it is unconsumable and must not be counted.
  assert.equal(parseRuntimeEvent({ ...EVENT, version: EVENT_VERSION + 1 }), null);
  socket.notify(6, { ...EVENT, version: EVENT_VERSION + 1 });
  assert.equal(events.length, 1, 'a wrong-version event must not reach the view');
  assert.equal(client.getWatchCursor(), 4, 'a rejected frame must not advance the cursor');
  assert.deepEqual(
    notices.map((notice) => [notice.type, notice.service_code, notice.subscription_id]),
    [['error', 'unsupported_event_version', 'sub-1']],
  );

  // The narrowest repro of the gap: the very next *legal* v1 event (sequence 7)
  // must not be delivered either.  Its subscription already reported a frame
  // that was never replayed, so counting 7 would let the next reconnect resume
  // after 6 and skip it forever.
  socket.notify(7);
  assert.equal(events.length, 1, 'a fenced subscription may not deliver past the gap');
  assert.equal(client.getWatchCursor(), 4, 'a fenced subscription may not advance the cursor');
  assert.equal(notices.length, 1, 'the failure is reported exactly once per subscription');

  // A frame that names no subscription at all must not slip through the fence.
  socket.push({
    jsonrpc: '2.0',
    method: 'runtime.event',
    params: { cursor: 7, event: { ...EVENT, sequence: 7 } },
  });
  assert.equal(events.length, 1, 'an unattributed frame may not bypass the fence');
  assert.equal(client.getWatchCursor(), 4);

  // Only an explicit re-watch clears the fence, and it resumes from the last
  // good cursor so sequence 6 is replayed instead of skipped.
  const resumed = await client.watchEvents(SESSION, client.getWatchCursor() ?? 0);
  assert.equal(resumed.subscription_id, 'sub-1');
  assert.equal(socket.sent[socket.sent.length - 1].params.after, 4);
  socket.notify(7);
  assert.equal(events.length, 2, 'a fresh watch delivers again');
  assert.equal(client.getWatchCursor(), 7);
  client.disconnect();
});

test('an event missing a required v1 field fences the watch without advancing the cursor', async () => {
  const factory = new Factory();
  const events: RuntimeEvent[] = [];
  const notices: Array<{ type: string; service_code?: string }> = [];
  const client = new SynapseRuntimeClient({
    url: 'ws://core',
    socketFactory: factory.make,
    onEvent: (event) => events.push(event),
    onSubscriptionNotice: (notice) => notices.push(notice),
  });
  const socket = await openClient(client, factory);
  await client.watchEvents(SESSION);

  // The watch cursor starts at 0 (the negotiated resume point).
  assert.equal(client.getWatchCursor(), 0);

  const { turn_id: _turnId, ...withoutTurnId } = EVENT;
  socket.notify(7, withoutTurnId as unknown as RuntimeEvent);
  assert.equal(events.length, 0, 'no incomplete frame may reach the view');
  assert.equal(client.getWatchCursor(), 0, 'no incomplete frame may advance the cursor');
  assert.deepEqual(
    notices.map((notice) => [notice.type, notice.service_code]),
    [['error', 'malformed_runtime_event']],
  );

  // The fence holds for every later frame of that subscription, malformed or
  // not: one unconsumable frame is enough to stop the cursor from crossing it.
  const { version: _version, ...withoutVersion } = EVENT;
  const { payload: _payload, ...withoutPayload } = EVENT;
  socket.notify(8, withoutVersion as unknown as RuntimeEvent);
  socket.notify(9, withoutPayload as unknown as RuntimeEvent);
  socket.notify(10);
  assert.equal(events.length, 0, 'a fenced subscription stays silent');
  assert.equal(client.getWatchCursor(), 0, 'a fenced subscription never advances the cursor');
  assert.equal(notices.length, 1, 'the failure is reported exactly once per subscription');
  client.disconnect();
});

test('a cursor that is missing or sits below the event sequence fences the watch', async () => {
  const factory = new Factory();
  const events: RuntimeEvent[] = [];
  const notices: Array<{ type: string; service_code?: string }> = [];
  const client = new SynapseRuntimeClient({
    url: 'ws://core',
    socketFactory: factory.make,
    onEvent: (event) => events.push(event),
    onSubscriptionNotice: (notice) => notices.push(notice),
  });
  const socket = await openClient(client, factory);
  await client.watchEvents(SESSION);
  const codes = () => notices.map((notice) => [notice.type, notice.service_code]);

  // No cursor at all: there is nothing to resume from, so the frame is refused
  // instead of being delivered with an unknown position.
  socket.push({
    jsonrpc: '2.0',
    method: 'runtime.event',
    params: { subscription_id: 'sub-1', event: { ...EVENT, sequence: 5 } },
  });
  assert.equal(events.length, 0);
  assert.equal(client.getWatchCursor(), 0);
  assert.deepEqual(codes(), [['error', 'invalid_event_cursor']]);

  // A cursor *below* the event's own sequence is impossible: that sequence was
  // never scanned, so the frame cannot be resumed from.
  await client.watchEvents(SESSION, 0);
  socket.push({
    jsonrpc: '2.0',
    method: 'runtime.event',
    params: { subscription_id: 'sub-1', cursor: 4, event: { ...EVENT, sequence: 5 } },
  });
  assert.equal(events.length, 0);
  assert.equal(client.getWatchCursor(), 0);
  assert.deepEqual(codes(), [
    ['error', 'invalid_event_cursor'],
    ['error', 'event_cursor_mismatch'],
  ]);

  // A fractional (or negative) cursor is not a session sequence either.
  await client.watchEvents(SESSION, 0);
  socket.push({
    jsonrpc: '2.0',
    method: 'runtime.event',
    params: { subscription_id: 'sub-1', cursor: 2.5, event: { ...EVENT, sequence: 2 } },
  });
  assert.equal(events.length, 0);
  assert.deepEqual(codes(), [
    ['error', 'invalid_event_cursor'],
    ['error', 'event_cursor_mismatch'],
    ['error', 'invalid_event_cursor'],
  ]);

  // A legal v1 event of an unknown kind, with a consistent cursor, still lands.
  await client.watchEvents(SESSION, 0);
  socket.notify(3, { ...EVENT, kind: 'future_kind', payload: { future: true } });
  assert.equal(events.length, 1, 'an unknown kind is additive and still delivered');
  assert.equal(events[0].kind, 'future_kind');
  assert.equal(client.getWatchCursor(), 3);
  client.disconnect();
});

test('a filtered event whose cursor runs ahead of its sequence is delivered, and a rewind is fenced', async () => {
  const factory = new Factory();
  const events: RuntimeEvent[] = [];
  const notices: Array<{ type: string; service_code?: string }> = [];
  const client = new SynapseRuntimeClient({
    url: 'ws://core',
    socketFactory: factory.make,
    onEvent: (event) => events.push(event),
    onSubscriptionNotice: (notice) => notices.push(notice),
  });
  const socket = await openClient(client, factory);
  await client.watchEvents(SESSION);

  // The wire cursor is the stream's raw scanned progress, not the delivered
  // event's own sequence: the filtered-out sequences scanned between two matches
  // push the cursor past the event it is attached to, so `cursor > sequence` is
  // legal, must be delivered, and must become the resume point.
  socket.push({
    jsonrpc: '2.0',
    method: 'runtime.event',
    params: { subscription_id: 'sub-1', cursor: 6, event: { ...EVENT, sequence: 4 } },
  });
  assert.deepEqual(
    events.map((event) => event.sequence),
    [4],
    'a cursor ahead of the event sequence is a filtered scan, not a mismatch',
  );
  assert.equal(client.getWatchCursor(), 6);
  assert.deepEqual(notices, []);

  // A later frame that repeats the delivered position is a non-monotonic replay:
  // delivering it again would resume from a sequence already consumed.
  socket.push({
    jsonrpc: '2.0',
    method: 'runtime.event',
    params: { subscription_id: 'sub-1', cursor: 6, event: { ...EVENT, sequence: 4 } },
  });
  assert.equal(events.length, 1, 'a repeated cursor must not be delivered twice');
  assert.equal(client.getWatchCursor(), 6, 'a repeated cursor must not move the resume point');
  assert.deepEqual(
    notices.map((notice) => [notice.type, notice.service_code]),
    [['error', 'event_cursor_mismatch']],
  );

  // A rewind below the last delivered cursor is refused the same way.
  await client.watchEvents(SESSION, client.getWatchCursor() ?? 0);
  socket.push({
    jsonrpc: '2.0',
    method: 'runtime.event',
    params: { subscription_id: 'sub-1', cursor: 5, event: { ...EVENT, sequence: 4 } },
  });
  assert.equal(events.length, 1, 'a rewound cursor must not be delivered');
  assert.equal(client.getWatchCursor(), 6);
  assert.deepEqual(
    notices.map((notice) => [notice.type, notice.service_code]),
    [
      ['error', 'event_cursor_mismatch'],
      ['error', 'event_cursor_mismatch'],
    ],
  );

  // The fresh watch resumes from the kept cursor and the next forward frame lands.
  await client.watchEvents(SESSION, client.getWatchCursor() ?? 0);
  socket.notify(7);
  assert.equal(client.getWatchCursor(), 7);
  assert.equal(events.length, 2);
  client.disconnect();
});

test('a consumer callback that throws does not commit the cursor and fences the watch', async () => {
  const factory = new Factory();
  const notices: Array<{ type: string; service_code?: string }> = [];
  const delivered: number[] = [];
  let throwOnDelivery = false;
  const client = new SynapseRuntimeClient({
    url: 'ws://core',
    socketFactory: factory.make,
    onEvent: (event) => {
      if (throwOnDelivery) throw new Error('view exploded on ' + JSON.stringify(event.payload));
      delivered.push(event.sequence);
    },
    onSubscriptionNotice: (notice) => notices.push(notice),
  });
  const socket = await openClient(client, factory);
  await client.watchEvents(SESSION);

  socket.notify(4);
  assert.deepEqual(delivered, [4]);
  assert.equal(client.getWatchCursor(), 4);

  throwOnDelivery = true;
  socket.notify(5);
  assert.deepEqual(delivered, [4], 'a throwing delivery is not counted as delivered');
  assert.equal(client.getWatchCursor(), 4, 'the cursor must not move before the consumer took it');
  assert.deepEqual(
    notices.map((notice) => [notice.type, notice.service_code]),
    [['error', 'event_delivery_failed']],
  );
  assert.equal(JSON.stringify(notices).includes('hi'), false, 'the notice must not echo the payload');

  // The fence holds until a new watch resumes from the last good cursor.
  throwOnDelivery = false;
  socket.notify(6);
  assert.equal(client.getWatchCursor(), 4, 'the fenced watch stays silent');
  await client.watchEvents(SESSION, client.getWatchCursor() ?? 0);
  socket.notify(6);
  assert.equal(client.getWatchCursor(), 6, 'the replay of the gap advances the cursor again');
  client.disconnect();
});

test('negotiate requires the selected version in both the request and the peer list, plus the v1 flags', () => {
  const ok = {
    wire_version: '1',
    supported_versions: ['1'],
    capabilities: { ...CAPABILITIES },
  };
  // The exact v1 result is returned untouched.
  assert.equal(parseNegotiateResult(ok), ok);
  // A peer that advertises more versions or more flags is additive growth.
  assert.doesNotThrow(() =>
    parseNegotiateResult({
      wire_version: '1',
      supported_versions: ['1', '2'],
      capabilities: { ...CAPABILITIES, future_flag: true },
    }),
  );
  // A selection this client never offered cannot be spoken.
  assert.throws(
    () => parseNegotiateResult({ ...ok, wire_version: '2' }),
    (err: any) => err instanceof RpcCallError && err.service_code === 'unsupported_wire_version',
    'a version outside the request must fail the handshake',
  );
  // A selection the peer does not itself list cannot be spoken either.
  assert.throws(
    () => parseNegotiateResult({ ...ok, supported_versions: ['2'] }),
    (err: any) => err instanceof RpcCallError && err.service_code === 'unsupported_wire_version',
    'a selection missing from the peer list must fail the handshake',
  );
  // Every v1 flag this client depends on must be advertised as exactly true.
  for (const required of ['legacy_v1', 'raw_cursor', 'watch_resume', 'approval_resume']) {
    const missing = { ...CAPABILITIES } as Record<string, boolean>;
    delete missing[required];
    assert.throws(
      () => parseNegotiateResult({ ...ok, capabilities: missing }),
      (err: any) => err instanceof RpcCallError && err.service_code === 'unsupported_wire_version',
      `a missing ${required} flag must fail the handshake`,
    );
    assert.throws(
      () => parseNegotiateResult({ ...ok, capabilities: { ...CAPABILITIES, [required]: false } }),
      (err: any) => err instanceof RpcCallError && err.service_code === 'unsupported_wire_version',
      `a false ${required} flag must fail the handshake`,
    );
  }
  // A non-boolean flag value is a malformed result, not a version mismatch.
  assert.throws(
    () => parseNegotiateResult({ ...ok, capabilities: { ...CAPABILITIES, legacy_v1: 'yes' } }),
    (err: any) => err instanceof RpcCallError && err.service_code === 'malformed_negotiate_result',
  );
});