/**
 * Offline tests for the console's session management surface
 * (`runtime.session.create` / `rename` / `delete` / `search`).
 *
 * No daemon, no host and no real socket: the core client runs over an injected
 * `SocketLike`, and the store runs against a stub client.  The properties pinned
 * here are the ones the UI contract depends on:
 *
 * - `create` sends NO thread id (the server allocates the real identity) and the
 *   console adopts the returned id/title instead of inventing one;
 * - `search` sends the trimmed query with bounded paging and is scoped to the
 *   active project; a stale page from an older query can never overwrite the
 *   newer result set (generation guard);
 * - a blank or over-long title is refused before any RPC is issued;
 * - `delete` reports the retained history explicitly and never claims the
 *   conversation was erased; a busy session is refused by the server (typed
 *   `conflict`) and the console neither removes the row nor cancels the turn;
 * - the switchable project list is enumerated over the shared runtime RPC
 *   (`runtime.project.list`), paged to a bounded end, and a stale page from a
 *   superseded enumeration can never overwrite the newer list.
 */
import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';

import {
  RpcCallError,
  SynapseRuntimeClient,
} from '../src/runtime-client/SynapseRuntimeClient.ts';
import type { SocketLike } from '../src/runtime-client/SynapseRuntimeClient.ts';
import type { SessionItem } from '../src/stores/historyMapper.ts';

const CAPS = { legacy_v1: true, raw_cursor: true, watch_resume: true, approval_resume: true };
const PROJECT = 'proj';
const SESSION = { project_id: PROJECT, thread_id: 'thr' };

interface SentFrame {
  id: number;
  method: string;
  params: any;
}

class FakeSocket implements SocketLike {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: any }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  sent: SentFrame[] = [];
  respond: (request: SentFrame) => unknown = () => ({});

  send(data: string): void {
    const parsed = JSON.parse(data) as SentFrame;
    this.sent.push(parsed);
    if (parsed.method === 'runtime.protocol.negotiate') {
      this.push({
        jsonrpc: '2.0',
        id: parsed.id,
        meta: { wire_version: '1' },
        result: { wire_version: '1', supported_versions: ['1'], capabilities: CAPS },
      });
      return;
    }
    this.push({
      jsonrpc: '2.0',
      id: parsed.id,
      meta: { wire_version: '1' },
      result: this.respond(parsed),
    });
  }

  close(): void {
    if (this.readyState === 3) return;
    this.readyState = 3;
    const cb = this.onclose;
    this.onclose = null;
    cb?.();
  }

  push(frame: unknown): void {
    if (this.onmessage) this.onmessage({ data: JSON.stringify(frame) });
  }

  serverOpen(): void {
    this.readyState = 1;
    this.onopen?.();
  }

  businessFrames(): SentFrame[] {
    return this.sent.filter((frame) => frame.method !== 'runtime.protocol.negotiate');
  }
}

const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

async function openClient() {
  const sockets: FakeSocket[] = [];
  const client = new SynapseRuntimeClient({
    url: 'ws://loopback',
    socketFactory: () => {
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket;
    },
    reconnect: { maxAttempts: 2, baseDelayMs: 1, maxDelayMs: 5 },
  });
  const promise = client.connect();
  await tick();
  const socket = sockets[0];
  socket.serverOpen();
  await promise;
  return { client, socket };
}

function item(threadId: string, title: string): SessionItem {
  return { thread_id: threadId, title, updated_at: '2026-09-12T00:00:00Z', time_label: '' };
}

// --- core client frames -----------------------------------------------------

test('createSession omits thread_id so the server allocates the real identity', async () => {
  const { client, socket } = await openClient();
  socket.respond = (request) => {
    assert.equal(request.method, 'runtime.session.create');
    return {
      command_id: request.params.command_id,
      session: { project_id: PROJECT, thread_id: 'server-allocated' },
      created: true,
      title: 'session server-allocated',
    };
  };

  const result = await client.createSession({ project_id: PROJECT });

  assert.deepEqual(result.session, { project_id: PROJECT, thread_id: 'server-allocated' });
  assert.equal(result.title, 'session server-allocated');
  const frame = socket.businessFrames()[0];
  assert.equal(frame.method, 'runtime.session.create');
  // Only the project: no thread_id (the server allocates one), no title (the
  // store derives the default), and no client-generated command id.
  assert.deepEqual(Object.keys(frame.params).sort(), ['project_id']);
  assert.equal('thread_id' in frame.params, false);
  assert.equal('title' in frame.params, false);
});

test('search/rename/delete send the exact frames the sidebar produces', async () => {
  const { client, socket } = await openClient();
  socket.respond = (request) => {
    switch (request.method) {
      case 'runtime.session.search':
        return { items: [], next_offset: null, total: 0 };
      case 'runtime.session.rename':
        return {
          command_id: request.params.command_id,
          session: SESSION,
          title: 'Renamed',
          renamed: true,
        };
      default:
        return {
          command_id: request.params.command_id,
          session: SESSION,
          deleted: true,
          retained_history: true,
        };
    }
  };

  const page = await client.searchSessions({ project_id: PROJECT, text: '  Renamed  ' });
  assert.deepEqual(page, { items: [], next_offset: null, total: 0 });
  const search = socket.businessFrames()[0];
  assert.equal(search.method, 'runtime.session.search');
  assert.deepEqual(search.params, {
    project_id: PROJECT,
    text: '  Renamed  ',
    limit: 50,
    offset: 0,
  });

  const renamed = await client.renameSession({ session: SESSION, title: 'Renamed' });
  assert.equal(renamed.title, 'Renamed');
  const rename = socket.businessFrames()[1];
  assert.equal(rename.method, 'runtime.session.rename');
  assert.equal(rename.params.session.thread_id, 'thr');
  assert.equal(rename.params.title, 'Renamed');

  const deleted = await client.deleteSession({ session: SESSION });
  assert.equal(deleted.deleted, true);
  assert.equal(deleted.retained_history, true);
  const remove = socket.businessFrames()[2];
  assert.equal(remove.method, 'runtime.session.delete');
  assert.deepEqual(Object.keys(remove.params).sort(), ['session']);
});

// --- store semantics --------------------------------------------------------

const stubCalls: Array<{ method: string; params: any }> = [];
let searchPlan: Array<() => Promise<unknown>> = [];
let projectPlan: Array<() => Promise<unknown>> = [];
let deleteImpl: (params: any) => Promise<unknown> = async () => ({
  command_id: 'cmd',
  session: SESSION,
  deleted: true,
  retained_history: true,
});
/** Decoded byte length of one base64 chunk (the stub's own offset math). */
function base64Bytes(text: string): number {
  const padding = text.endsWith('==') ? 2 : text.endsWith('=') ? 1 : 0;
  return (text.length / 4) * 3 - padding;
}

const stubAppend = async (params: any) => {
  const size = base64Bytes(params.data_base64);
  return {
    ref: params.ref,
    received_bytes: params.expected_offset + size,
    next_offset: params.expected_offset + size,
  };
};

let appendImpl: (params: any) => Promise<unknown> = stubAppend;
let finishImpl: (params: any) => Promise<unknown> = async (params) => ({
  attachmentId: params.ref.attachment_id,
  size: params.expected_size,
  mime: params.expected_mime,
  revision: 'rev-1',
});

const ATTACHMENT_ID = 'f'.repeat(32);

/** Minimal client surface the store's session actions use. */
function stubClient() {
  return {
    getState: () => 'disconnected',
    searchSessions: (params: any) => {
      stubCalls.push({ method: 'runtime.session.search', params });
      const next = searchPlan.shift();
      return next ? next() : Promise.resolve({ items: [], next_offset: null, total: 0 });
    },
    listProjects: (params: any) => {
      stubCalls.push({ method: 'runtime.project.list', params });
      const next = projectPlan.shift();
      return next ? next() : Promise.resolve({ projects: [], next_offset: null, total: 0 });
    },
    renameSession: (params: any) => {
      stubCalls.push({ method: 'runtime.session.rename', params });
      return Promise.resolve({
        command_id: 'cmd',
        session: params.session,
        title: params.title,
        renamed: true,
      });
    },
    deleteSession: (params: any) => {
      stubCalls.push({ method: 'runtime.session.delete', params });
      return deleteImpl(params);
    },
    createSession: (params: any) => {
      stubCalls.push({ method: 'runtime.session.create', params });
      return Promise.resolve({
        command_id: 'cmd',
        session: { project_id: params.project_id, thread_id: 'allocated' },
        created: true,
        title: 'session allocated',
      });
    },
    unwatchEvents: () => Promise.resolve(undefined),
    openSession: (session: any) =>
      Promise.resolve({
        command_id: 'cmd',
        session,
        created: false,
        view: {
          project_id: session.project_id,
          thread_id: session.thread_id,
          status: 'idle',
          active_turn_id: null,
          latest_sequence: 0,
        },
      }),
    watchEvents: () => Promise.resolve({ subscription_id: 'sub-1', cursor: 0 }),
    readSessionHistory: () =>
      Promise.resolve({
        available: false,
        events: [],
        start_turn: 0,
        end_turn: 0,
        total_turns: 0,
        has_more: false,
      }),
    beginAttachment: (session: any, size: number, mime: string, displayName = '') => {
      stubCalls.push({
        method: 'runtime.attachments.begin',
        params: { session, size, mime, displayName },
      });
      return Promise.resolve({
        attachmentId: ATTACHMENT_ID,
        chunk_bytes: 262144,
        chunk_base64_chars: 349528,
        expires_at: '2026-01-01T00:00:00Z',
        next_offset: 0,
      });
    },
    appendAttachmentChunk: (
      session: any,
      attachmentId: string,
      expectedOffset: number,
      dataBase64: string,
    ) => {
      const params = {
        ref: { session, attachment_id: attachmentId },
        expected_offset: expectedOffset,
        data_base64: dataBase64,
      };
      stubCalls.push({ method: 'runtime.attachments.append', params });
      return appendImpl(params);
    },
    finishAttachment: (
      session: any,
      attachmentId: string,
      expectedSize: number,
      expectedMime: string,
    ) => {
      const params = {
        ref: { session, attachment_id: attachmentId },
        expected_size: expectedSize,
        expected_mime: expectedMime,
      };
      stubCalls.push({ method: 'runtime.attachments.finish', params });
      return finishImpl(params);
    },
    abortAttachment: (session: any, attachmentId: string) => {
      stubCalls.push({
        method: 'runtime.attachments.abort',
        params: { ref: { session, attachment_id: attachmentId } },
      });
      return Promise.resolve({
        ref: { session, attachment_id: attachmentId },
        removed: true,
      });
    },
    submitTurn: (params: any) => {
      stubCalls.push({ method: 'runtime.turn.submit', params });
      return Promise.resolve({ command_id: 'cmd', session: params.session, turn_id: 'turn-1' });
    },
  };
}

const { useConsoleStore } = await import('../src/stores/useConsoleStore.ts');

function resetStore() {
  stubCalls.length = 0;
  searchPlan = [];
  projectPlan = [];
  deleteImpl = async () => ({
    command_id: 'cmd',
    session: SESSION,
    deleted: true,
    retained_history: true,
  });
  appendImpl = stubAppend;
  finishImpl = async (params) => ({
    attachmentId: params.ref.attachment_id,
    size: params.expected_size,
    mime: params.expected_mime,
    revision: 'rev-1',
  });
  useConsoleStore.setState({
    pairingState: 'paired',
    client: stubClient() as any,
    activeProjectId: PROJECT,
    currentSession: { project_id: PROJECT, thread_id: 'thr' },
    sessions: [item('thr', 'First'), item('other', 'Second')],
    sessionsTotal: 2,
    projects: [],
    projectSessions: {},
    attachments: [],
    attachmentError: null,
    messages: [],
    runtimeStatus: 'idle',
    activeTurnId: null,
    sessionQuery: '',
    sessionSearch: {
      query: '',
      items: [],
      total: 0,
      nextOffset: null,
      loading: false,
      error: null,
      generation: 0,
    },
    sessionActionError: null,
    sessionNotice: null,
  });
}

beforeEach(resetStore);

test('a stale search page never overwrites a newer query', async () => {
  let releaseFirst: (value: unknown) => void = () => {};
  searchPlan = [
    () =>
      new Promise((resolve) => {
        releaseFirst = resolve;
      }),
    () => Promise.resolve({ items: [item('new', 'Newest')], next_offset: null, total: 1 }),
  ];

  useConsoleStore.getState().setSessionQuery('first');
  await tick();
  useConsoleStore.getState().setSessionQuery('second');
  await tick();
  // The first (slow) response lands after the second query already won.
  releaseFirst({ items: [item('old', 'Stale')], next_offset: null, total: 1 });
  await tick();
  await tick();

  const state = useConsoleStore.getState().sessionSearch;
  assert.equal(state.query, 'second');
  assert.deepEqual(state.items.map((entry) => entry.thread_id), ['new']);
  assert.deepEqual(
    stubCalls.map((call) => call.params.text),
    ['first', 'second'],
  );
});

test('clearing the search box issues no RPC and returns to the plain list', async () => {
  useConsoleStore.getState().setSessionQuery('anything');
  await tick();
  assert.equal(stubCalls.length, 1);

  useConsoleStore.getState().setSessionQuery('   ');
  await tick();

  const state = useConsoleStore.getState().sessionSearch;
  assert.equal(stubCalls.length, 1); // no request for the blank query
  assert.equal(state.query, '');
  assert.deepEqual(state.items, []);
});

test('a blank or over-long title is refused before any rename RPC', async () => {
  assert.equal(await useConsoleStore.getState().renameSession('thr', '   '), false);
  assert.equal(await useConsoleStore.getState().renameSession('thr', 'x'.repeat(121)), false);
  assert.equal(stubCalls.length, 0);
  assert.match(useConsoleStore.getState().sessionActionError ?? '', /1-120/);
});

test('a successful rename updates every list the sidebar renders', async () => {
  const accepted = await useConsoleStore.getState().renameSession('thr', '  Manual title  ');
  assert.equal(accepted, true);
  assert.equal(stubCalls[0].params.title, 'Manual title');
  const state = useConsoleStore.getState();
  assert.equal(state.sessions.find((entry) => entry.thread_id === 'thr')?.title, 'Manual title');
  assert.equal(state.sessionTitle, 'Manual title');
});

test('delete states the retained history instead of claiming an erase', async () => {
  const deleted = await useConsoleStore.getState().deleteSession('other');
  assert.equal(deleted, true);
  const state = useConsoleStore.getState();
  assert.deepEqual(state.sessions.map((entry) => entry.thread_id), ['thr']);
  assert.match(state.sessionNotice ?? '', /对话历史（检查点与转录）仍保留/);
  assert.match(state.sessionNotice ?? '', /未被删除/);
  assert.equal(state.sessionActionError, null);
});

test('a busy session is refused by the server and nothing is cancelled', async () => {
  deleteImpl = async () => {
    throw new RpcCallError('conflict', -32000, 'conflict');
  };

  const deleted = await useConsoleStore.getState().deleteSession('thr');

  assert.equal(deleted, false);
  const state = useConsoleStore.getState();
  // The row survives: only the server decides, and it said no.
  assert.deepEqual(state.sessions.map((entry) => entry.thread_id), ['thr', 'other']);
  assert.match(state.sessionActionError ?? '', /正在运行中/);
  assert.match(state.sessionActionError ?? '', /不会自动取消/);
  assert.equal(state.sessionNotice, null);
  // No cancel / close frame was issued as a side effect of the refusal.
  assert.deepEqual(
    stubCalls.map((call) => call.method),
    ['runtime.session.delete'],
  );
});

// --- project enumeration ----------------------------------------------------

function project(projectId: string) {
  return {
    project_id: projectId,
    workspace_name: projectId,
    git_branch: null,
    workspace_path: `/w/${projectId}`,
  };
}

test('the project list is read over the runtime RPC, paged to the end', async () => {
  projectPlan = [
    () => Promise.resolve({ projects: [project('p1')], next_offset: 1, total: 2 }),
    () => Promise.resolve({ projects: [project('p2')], next_offset: null, total: 2 }),
  ];

  await useConsoleStore.getState().loadProjects();

  const frames = stubCalls.filter((call) => call.method === 'runtime.project.list');
  // Two pages, offsets 0 then the server's `next_offset`; the loop stops at null.
  assert.deepEqual(
    frames.map((call) => call.params.offset),
    [0, 1],
  );
  assert.deepEqual(
    useConsoleStore.getState().projects.map((entry) => entry.project_id),
    ['p1', 'p2'],
  );
});

test('a stale project page never overwrites a newer enumeration', async () => {
  let releaseFirst: (value: unknown) => void = () => {};
  projectPlan = [
    () =>
      new Promise((resolve) => {
        releaseFirst = resolve;
      }),
    () => Promise.resolve({ projects: [project('new')], next_offset: null, total: 1 }),
  ];

  const first = useConsoleStore.getState().loadProjects();
  await tick();
  const second = useConsoleStore.getState().loadProjects();
  await second;
  // The first (slow) enumeration lands after the second already won.
  releaseFirst({ projects: [project('old')], next_offset: null, total: 1 });
  await first;

  assert.deepEqual(
    useConsoleStore.getState().projects.map((entry) => entry.project_id),
    ['new'],
  );
});

test('a project list is not read before the console is paired', async () => {
  useConsoleStore.setState({ pairingState: 'unpaired', client: null });

  await useConsoleStore.getState().loadProjects();

  assert.deepEqual(stubCalls, []);
});

// --- image attachments ------------------------------------------------------

function fakeImage(name: string, bytes: Uint8Array, type = 'image/png') {
  return {
    name,
    type,
    size: bytes.length,
    arrayBuffer: async () => bytes.slice().buffer as ArrayBuffer,
  };
}

test('an attachment-only submit sends attachment_refs and clears the composer', async () => {
  await useConsoleStore.getState().addAttachments([fakeImage('shot.png', new Uint8Array([1, 2, 3]))]);
  const row = useConsoleStore.getState().attachments[0];
  assert.equal(row.status, 'ready');
  assert.equal(row.attachmentId, ATTACHMENT_ID);

  await useConsoleStore.getState().submitPrompt('');

  const submit = stubCalls.find((call) => call.method === 'runtime.turn.submit');
  assert.ok(submit);
  assert.equal(submit.params.text, '');
  assert.deepEqual(submit.params.attachment_refs, [ATTACHMENT_ID]);
  // The composer is emptied, but the finalized ref is never deleted server-side.
  assert.deepEqual(useConsoleStore.getState().attachments, []);
  assert.equal(
    stubCalls.filter((call) => call.method === 'runtime.attachments.abort').length,
    0,
  );
  // The live user message renders the uploaded metadata with empty text.
  const user = useConsoleStore.getState().messages.find((message) => message.type === 'user');
  assert.ok(user);
  assert.equal(user.content, '');
  assert.equal(user.attachments?.[0].attachmentId, ATTACHMENT_ID);
});

test('a submit is refused while an attachment chunk is still uploading', async () => {
  let release: () => void = () => {};
  appendImpl = (params) =>
    new Promise((resolve) => {
      release = () => resolve({ ref: params.ref, received_bytes: 1, next_offset: 1 });
    });
  const upload = useConsoleStore.getState().addAttachments([fakeImage('slow.png', new Uint8Array([7]))]);
  await tick();
  assert.equal(useConsoleStore.getState().attachments[0].status, 'uploading');

  await useConsoleStore.getState().submitPrompt('hello');

  assert.equal(
    stubCalls.some((call) => call.method === 'runtime.turn.submit'),
    false,
  );
  assert.match(useConsoleStore.getState().attachmentError ?? '', /上传/);
  assert.equal(useConsoleStore.getState().messages.length, 0);

  release();
  await upload;
});

test('a failed upload reports a visible reason and never becomes prompt text', async () => {
  appendImpl = async () => {
    throw new Error('append rejected');
  };

  await useConsoleStore.getState().addAttachments([fakeImage('broken.png', new Uint8Array([1]))]);

  const state = useConsoleStore.getState();
  assert.equal(state.attachments[0].status, 'failed');
  assert.match(state.attachments[0].error ?? '', /append rejected/);
  assert.match(state.attachmentError ?? '', /broken\.png/);
  // The filename was never turned into a prompt and nothing was submitted.
  assert.equal(state.messages.length, 0);
  assert.equal(
    stubCalls.some((call) => call.method === 'runtime.turn.submit'),
    false,
  );
  // The partial upload was aborted best-effort.
  assert.equal(
    stubCalls.filter((call) => call.method === 'runtime.attachments.abort').length,
    1,
  );

  await useConsoleStore.getState().submitPrompt('');
  assert.equal(
    stubCalls.some((call) => call.method === 'runtime.turn.submit'),
    false,
  );
});

test('switching session cancels the in-flight upload and aborts its partial bytes', async () => {
  let release: () => void = () => {};
  appendImpl = (params) =>
    new Promise((resolve) => {
      release = () =>
        resolve({ ref: params.ref, received_bytes: 262144, next_offset: 262144 });
    });
  const payload = new Uint8Array(262144 + 10);
  const upload = useConsoleStore.getState().addAttachments([fakeImage('big.png', payload)]);
  await tick();
  await tick();
  assert.equal(useConsoleStore.getState().attachments[0].status, 'uploading');
  assert.equal(
    stubCalls.filter((call) => call.method === 'runtime.attachments.append').length,
    1,
  );

  await useConsoleStore.getState().switchSession('other');
  // The composer is cleared at once: it was bound to the session being left.
  assert.deepEqual(useConsoleStore.getState().attachments, []);

  release();
  await upload;

  const aborts = stubCalls.filter((call) => call.method === 'runtime.attachments.abort');
  assert.equal(aborts.length, 1);
  assert.equal(aborts[0].params.ref.attachment_id, ATTACHMENT_ID);
});

test('switching session never aborts an already finalized attachment', async () => {
  await useConsoleStore.getState().addAttachments([fakeImage('done.png', new Uint8Array([1, 2, 3]))]);
  assert.equal(useConsoleStore.getState().attachments[0].status, 'ready');

  await useConsoleStore.getState().switchSession('other');

  assert.equal(
    stubCalls.filter((call) => call.method === 'runtime.attachments.abort').length,
    0,
  );
  assert.deepEqual(useConsoleStore.getState().attachments, []);
});
