/**
 * Behaviour tests for the window-capture surface: the strict decoders, the pure
 * presentation helpers, and the entry-local capture store.
 *
 * The runtime and the tool are faked: no window is captured and no process is
 * started.  What is pinned is the *console* half — that a malformed wire payload
 * never reaches the UI, that the state/error wording is stable, and that a
 * result is bound to the session + draft it was started in (filled when the
 * reader is still there, offered for confirmation when they moved on), with no
 * late async answer able to overwrite a newer task or fill the wrong draft.
 */
import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import {
  MalformedScreenshotError,
  DEFAULT_SCREENSHOT_SETTINGS,
  SCREENSHOT_MAX_FRAMES,
  clampCount,
  isTerminalScreenshotState,
  parseScreenshotCancel,
  parseScreenshotStart,
  parseScreenshotStatus,
  parseScreenshotToolStatus,
  sameScreenshotOrigin,
  screenshotErrorMessage,
  screenshotStateLabel,
  toWireSettings,
  type ScreenshotStatusView,
} from '../src/runtime-client/screenshot.ts';
import { useConsoleStore } from '../src/stores/useConsoleStore.ts';
import {
  SCREENSHOT_IMPORT_TIMEOUT_MESSAGE,
  SCREENSHOT_IMPORT_WAIT,
  useScreenshotStore,
} from '../src/stores/screenshotTask.ts';

// A capture that is still running schedules its next poll with a timer; clear it
// after every test so the runner's event loop can drain.
afterEach(() => {
  useScreenshotStore.getState().reset();
  // Tests shrink the bounded import wait; restore the production window.
  SCREENSHOT_IMPORT_WAIT.deadlineMs = 6_000;
  SCREENSHOT_IMPORT_WAIT.recheckMs = 400;
});

// --- strict decoders ---------------------------------------------------------

function idleStatus(available = true): ScreenshotStatusView {
  return {
    taskId: '',
    state: 'idle',
    requested: 0,
    captured: 0,
    attachments: [],
    available,
    unavailableReason: available ? '' : '未构建',
    errorCode: null,
    errorMessage: null,
  };
}

function wireStatus(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    session: { project_id: 'p', thread_id: 't' },
    task_id: 'task-1',
    state: 'running',
    requested: 3,
    captured: 1,
    attachments: [],
    available: true,
    unavailable_reason: '',
    error_code: null,
    error_message: null,
    ...overrides,
  };
}

test('the status decoder accepts a real payload and rejects unknown keys', () => {
  const view = parseScreenshotStatus(wireStatus());
  assert.equal(view.state, 'running');
  assert.equal(view.requested, 3);
  assert.equal(view.captured, 1);
  assert.deepEqual(view.attachments, []);
  assert.throws(() => parseScreenshotStatus(wireStatus({ extra: 1 })), MalformedScreenshotError);
  assert.throws(() => parseScreenshotStatus(wireStatus({ state: 'nonsense' })), MalformedScreenshotError);
  assert.throws(() => parseScreenshotStatus(wireStatus({ captured: -1 })), MalformedScreenshotError);
});

test('a frame attachment is decoded with its metadata', () => {
  const view = parseScreenshotStatus(
    wireStatus({
      state: 'completed',
      captured: 1,
      attachments: [
        { attachment_id: 'a'.repeat(32), name: '截图 1.png', mime: 'image/png', size: 1234, revision: 'r1' },
      ],
    }),
  );
  assert.equal(view.attachments.length, 1);
  assert.deepEqual(view.attachments[0], {
    attachmentId: 'a'.repeat(32),
    name: '截图 1.png',
    mime: 'image/png',
    size: 1234,
    revision: 'r1',
  });
});

test('the tool-status and start/cancel decoders are strict', () => {
  const tool = parseScreenshotToolStatus({
    available: true,
    platform: 'win32',
    reason: '',
    version: '1.0.0',
    busy: false,
    active_task_id: null,
  });
  assert.equal(tool.available, true);
  assert.equal(tool.version, '1.0.0');
  assert.throws(
    () => parseScreenshotToolStatus({ available: true, platform: 'win32', reason: '', version: null, busy: false }),
    MalformedScreenshotError,
  );

  const start = parseScreenshotStart({
    session: { project_id: 'p', thread_id: 't' },
    task_id: 'task-1',
    state: 'queued',
    requested: 1,
    settings: { count: 1 },
  });
  assert.equal(start.taskId, 'task-1');
  assert.equal(start.state, 'queued');

  const cancel = parseScreenshotCancel({
    session: { project_id: 'p', thread_id: 't' },
    task_id: 'task-1',
    state: 'cancelled',
    cancelled: true,
  });
  assert.equal(cancel.cancelled, true);
});

// --- pure helpers ------------------------------------------------------------

test('settings are projected onto the wire with bounded members', () => {
  assert.deepEqual(toWireSettings(DEFAULT_SCREENSHOT_SETTINGS), {
    count: 1,
    interval_ms: 250,
    start_delay_ms: 0,
    max_edge: 0,
    ttl_seconds: 300,
    frame_timeout_ms: 5000,
    allow_reuse: true,
  });
  assert.equal(toWireSettings({ ...DEFAULT_SCREENSHOT_SETTINGS, count: 0, ttlSeconds: 0 }).count, 1);
  assert.equal(clampCount(10_000), 600);
});

test('terminal states and origin matching are exact', () => {
  assert.equal(isTerminalScreenshotState('completed'), true);
  assert.equal(isTerminalScreenshotState('running'), false);
  assert.equal(isTerminalScreenshotState('target_required'), true);
  const origin = { projectId: 'p', threadId: 't', generation: 2 };
  assert.equal(sameScreenshotOrigin(origin, { ...origin }), true);
  assert.equal(sameScreenshotOrigin(origin, { ...origin, generation: 3 }), false);
  assert.equal(sameScreenshotOrigin(origin, { ...origin, threadId: 't2' }), false);
});

test('refusals are worded, and every state has a label', () => {
  assert.match(screenshotErrorMessage('target_required', 'x'), /选择窗口/);
  assert.match(screenshotErrorMessage('target_closed', 'x'), /关闭/);
  assert.equal(screenshotErrorMessage('unknown_code', 'fallback'), 'fallback');
  for (const state of ['idle', 'queued', 'running', 'completed', 'cancelled', 'failed', 'target_required'] as const) {
    assert.ok(screenshotStateLabel(state).length > 0);
  }
});

// --- the capture store -------------------------------------------------------

interface Frame {
  attachment_id: string;
  name: string;
  mime: string;
  size: number;
  revision: string | null;
}

function frame(id: string, name = '截图 1.png'): Frame {
  return { attachment_id: id, name, mime: 'image/png', size: 64, revision: 'r1' };
}

/** One wire frame projected to the decoded status view. */
function frameView(f: Frame): {
  attachmentId: string;
  name: string;
  mime: string;
  size: number;
  revision: string | null;
} {
  return {
    attachmentId: f.attachment_id,
    name: f.name,
    mime: f.mime,
    size: f.size,
    revision: f.revision,
  };
}

/** One finalized composer row (an opaque id, no local pick). */
function readyRow(attachmentId: string): {
  localId: string;
  name: string;
  mime: string;
  size: number;
  status: 'ready';
  uploadedBytes: number;
  attachmentId: string;
  error: null;
} {
  return {
    localId: `row-${attachmentId}`,
    name: 'existing.png',
    mime: 'image/png',
    size: 64,
    status: 'ready',
    uploadedBytes: 64,
    attachmentId,
    error: null,
  };
}

class FakeClient {
  available = true;
  completed = false;
  frames: Frame[] = [];
  statusCalls: string[] = [];
  startCalls: unknown[] = [];
  cancelCalls: string[] = [];
  settingsCalls = 0;
  /**
   * A queue of scripted task snapshots, popped one per task read.
   *
   * Models an older daemon that flips `completed` before its frames are
   * finalized: the first read is empty (or partial), a later read has them all.
   */
  scripted: Array<{ state: 'running' | 'completed'; frames: Frame[]; requested?: number }> = [];
  /** When set, the capability probe (taskId '') waits on it. */
  probeGate: Promise<void> | null = null;
  /** When set, a task read (taskId !== '') waits on it. */
  statusGate: Promise<void> | null = null;
  /** When set, the start RPC waits on it (never resolves by default). */
  startGate: Promise<void> | null = null;

  async getScreenshotStatus(_session: unknown, taskId: string): Promise<ScreenshotStatusView> {
    this.statusCalls.push(taskId);
    if (taskId === '') {
      if (this.probeGate !== null) await this.probeGate;
      return idleStatus(this.available);
    }
    if (this.statusGate !== null) await this.statusGate;
    if (this.scripted.length > 0) {
      const next = this.scripted.shift()!;
      return {
        ...idleStatus(),
        taskId,
        state: next.state,
        requested: next.requested ?? 3,
        captured: next.frames.length,
        attachments: next.frames.map(frameView),
      };
    }
    if (this.completed) {
      return {
        ...idleStatus(),
        taskId,
        state: 'completed',
        requested: 1,
        captured: 1,
        attachments: this.frames.map(frameView),
      };
    }
    return { ...idleStatus(), taskId, state: 'running', requested: 1, captured: 0 };
  }

  async startScreenshotCapture(): Promise<{ taskId: string; state: 'queued'; requested: number }> {
    this.startCalls.push(true);
    if (this.startGate !== null) await this.startGate;
    return { taskId: 'task-1', state: 'queued', requested: 1 };
  }

  async openScreenshotSettings(): Promise<{ available: boolean; platform: string; reason: string; version: string | null; busy: boolean; activeTaskId: string | null }> {
    this.settingsCalls += 1;
    return { available: true, platform: 'win32', reason: '', version: null, busy: false, activeTaskId: null };
  }

  async cancelScreenshotCapture(_session: unknown, taskId: string): Promise<{ taskId: string; state: 'cancelled'; cancelled: boolean }> {
    this.cancelCalls.push(taskId);
    return { taskId, state: 'cancelled', cancelled: true };
  }

  // The console store's submit path (used to bump the draft generation).
  async openSession(): Promise<{ view: null }> {
    return { view: null };
  }

  async submitTurn(): Promise<{ turn_id: null }> {
    return { turn_id: null };
  }
}

async function waitFor(predicate: () => boolean, timeoutMs = 3000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error('timed out waiting for the capture flow');
}

function install(client: FakeClient, threadId = 't'): void {
  useScreenshotStore.getState().reset();
  useConsoleStore.setState({
    client: client as never,
    pairingState: 'paired',
    connectionState: 'connected',
    currentSession: { project_id: 'p', thread_id: threadId },
    attachments: [],
    runtimeStatus: 'idle',
    activeTurnId: null,
    activeSubscriptionId: 'sub-existing',
    messages: [],
    sessionTitle: 'capture test',
  });
}

test('a finished capture is filled into the composer when the draft is unchanged', async () => {
  const client = new FakeClient();
  install(client);
  client.completed = true;
  client.frames = [frame('a'.repeat(32))];

  await useScreenshotStore.getState().start();
  assert.equal(client.startCalls.length, 1);
  await waitFor(() => useConsoleStore.getState().attachments.length === 1);

  const rows = useConsoleStore.getState().attachments;
  assert.equal(rows[0].status, 'ready');
  assert.equal(rows[0].attachmentId, 'a'.repeat(32));
  assert.equal(rows[0].source, undefined, 'a screenshot row carries no local pick');
  assert.equal(useScreenshotStore.getState().pending, null);
});

test('start binds the draft generation it was issued in, not one changed mid-await', async () => {
  const client = new FakeClient();
  install(client);
  client.completed = true;
  client.frames = [frame('c'.repeat(32))];
  let releaseProbe: () => void = () => {};
  client.probeGate = new Promise((resolve) => {
    releaseProbe = resolve;
  });

  const started = useScreenshotStore.getState().start();
  // A new draft leaves the composer while the start's capability probe is still
  // in flight: the capture belongs to the generation it was issued in.
  await useConsoleStore.getState().submitPrompt('新的草稿');
  releaseProbe();
  await started;

  await waitFor(() => useScreenshotStore.getState().pending !== null);
  assert.equal(
    useConsoleStore.getState().attachments.length,
    0,
    'the new draft never receives the old capture',
  );
  assert.equal(useScreenshotStore.getState().pending?.attachments.length, 1);
});

test('a result that lands after a session switch stays confirmable', async () => {
  const client = new FakeClient();
  install(client, 't');
  await useScreenshotStore.getState().start();
  // The reader moves on before the capture completes.
  useConsoleStore.setState({ currentSession: { project_id: 'p', thread_id: 't2' } });
  client.completed = true;
  client.frames = [frame('b'.repeat(32))];

  await waitFor(() => useScreenshotStore.getState().pending !== null);
  assert.equal(useConsoleStore.getState().attachments.length, 0, 'nothing is filled into the new draft');

  // Filling from another session is refused, with a switch-back hint.
  useScreenshotStore.getState().restorePending();
  assert.equal(useConsoleStore.getState().attachments.length, 0);
  assert.match(useScreenshotStore.getState().notice ?? '', /切回/);
  assert.ok(useScreenshotStore.getState().pending !== null, 'the result is kept');

  // Back in the originating session, the frames fill in.
  useConsoleStore.setState({ currentSession: { project_id: 'p', thread_id: 't' } });
  useScreenshotStore.getState().restorePending();
  assert.equal(useConsoleStore.getState().attachments.length, 1);
  assert.equal(useScreenshotStore.getState().pending, null);
});

test('a second start while one is queued does not spawn a duplicate', async () => {
  const client = new FakeClient();
  install(client);
  await useScreenshotStore.getState().start();
  await useScreenshotStore.getState().start();
  assert.equal(client.startCalls.length, 1);
  assert.match(useScreenshotStore.getState().notice ?? '', /正在进行/);
});

test('an unavailable tool refuses to start and names the reason', async () => {
  const client = new FakeClient();
  client.available = false;
  install(client);
  await assert.rejects(() => useScreenshotStore.getState().start(), /未构建|不可用/);
  assert.equal(client.startCalls.length, 0);
});

test('a start whose capability probe never resolves clears starting and shows a timeout', async () => {
  const client = new FakeClient();
  install(client);
  // The probe hangs forever: without an explicit deadline the banner would sit on
  // "正在启动截图…" indefinitely.
  client.probeGate = new Promise(() => {});
  await assert.rejects(() => useScreenshotStore.getState().start(30), /超时/);
  assert.equal(useScreenshotStore.getState().starting, false);
  assert.match(useScreenshotStore.getState().notice ?? '', /超时/);
  assert.equal(client.startCalls.length, 0);
});

test('a start RPC that never resolves clears starting and shows a timeout', async () => {
  const client = new FakeClient();
  install(client);
  client.startGate = new Promise(() => {});
  await assert.rejects(() => useScreenshotStore.getState().start(30), /超时/);
  assert.equal(useScreenshotStore.getState().starting, false);
  assert.match(useScreenshotStore.getState().notice ?? '', /超时/);
  // The request was actually issued; it is the response that never came.
  assert.equal(client.startCalls.length, 1);
});

test('a start that hangs does not block a later start once it has timed out', async () => {
  const client = new FakeClient();
  install(client);
  client.startGate = new Promise(() => {});
  await assert.rejects(() => useScreenshotStore.getState().start(20), /超时/);
  // The wedged request is abandoned; a fresh attempt is allowed and works.
  client.startGate = null;
  client.completed = true;
  client.frames = [frame('d'.repeat(32))];
  await useScreenshotStore.getState().start(2000);
  assert.equal(client.startCalls.length, 2);
  await waitFor(() => useConsoleStore.getState().attachments.length === 1);
});

test('opening settings asks the runtime, and cancel targets the running task', async () => {
  const client = new FakeClient();
  install(client);
  await useScreenshotStore.getState().openSettings();
  assert.equal(client.settingsCalls, 1);

  await useScreenshotStore.getState().start();
  await useScreenshotStore.getState().cancel();
  assert.deepEqual(client.cancelCalls, ['task-1']);
  assert.equal(useScreenshotStore.getState().status?.state, 'cancelled');
});

test('the composer budget caps how many frames are accepted, keeping the rest', async () => {
  const client = new FakeClient();
  install(client);
  client.completed = true;
  client.frames = Array.from({ length: SCREENSHOT_MAX_FRAMES + 2 }, (_v, i) =>
    frame(String(i).padStart(32, '0')),
  );
  await useScreenshotStore.getState().start();
  await waitFor(() => useConsoleStore.getState().attachments.length > 0);
  assert.equal(useConsoleStore.getState().attachments.length, SCREENSHOT_MAX_FRAMES);
  // The frames that did not fit are kept pending, not dropped.
  assert.equal(useScreenshotStore.getState().pending?.attachments.length, 2);
});

test('every composer row counts against the capture budget', async () => {
  const client = new FakeClient();
  install(client);
  useConsoleStore.setState({
    attachments: Array.from({ length: SCREENSHOT_MAX_FRAMES }, (_v, i) => ({
      ...readyRow(String(i).padStart(32, '0')),
      status: 'uploading' as const,
      uploadedBytes: 0,
      attachmentId: null,
    })),
  });
  await useScreenshotStore.getState().start();
  assert.equal(client.startCalls.length, 0);
  assert.match(useScreenshotStore.getState().notice ?? '', /最多/);
});

test('starting a new capture while a result awaits confirmation is refused', async () => {
  const client = new FakeClient();
  install(client);
  client.completed = true;
  client.frames = Array.from({ length: SCREENSHOT_MAX_FRAMES + 1 }, (_v, i) =>
    frame(String(i).padStart(32, '0')),
  );
  await useScreenshotStore.getState().start();
  await waitFor(() => useScreenshotStore.getState().pending !== null);

  const before = client.startCalls.length;
  await useScreenshotStore.getState().start();
  assert.equal(client.startCalls.length, before, 'no new capture is queued');
  assert.match(useScreenshotStore.getState().notice ?? '', /未处理|先加入/);
  assert.ok(useScreenshotStore.getState().pending !== null, 'the pending result is not silently cleared');
});

test('restoring more frames than fit keeps the remainder pending', async () => {
  const client = new FakeClient();
  install(client);
  useConsoleStore.setState({
    attachments: [readyRow('e'.repeat(32)), readyRow('f'.repeat(32))],
  });
  client.completed = true;
  client.frames = Array.from({ length: 10 }, (_v, i) => frame(String(i).padStart(32, '0')));
  await useScreenshotStore.getState().start();
  // 2 existing rows + 10 frames, budget 8 -> 6 added, 4 kept pending.
  await waitFor(() => useScreenshotStore.getState().pending !== null);
  assert.equal(useConsoleStore.getState().attachments.length, SCREENSHOT_MAX_FRAMES);
  assert.equal(useScreenshotStore.getState().pending?.attachments.length, 4);

  // Free the composer, then the kept frames fill in.
  useConsoleStore.setState({ attachments: [] });
  useScreenshotStore.getState().restorePending();
  assert.equal(useConsoleStore.getState().attachments.length, 4);
  assert.equal(useScreenshotStore.getState().pending, null);
});

test('a poll that resolves after reset never resurrects the old task', async () => {
  const client = new FakeClient();
  install(client);
  await useScreenshotStore.getState().start();
  assert.equal(useScreenshotStore.getState().status?.state, 'queued');

  // Hold the next task read open, let the poll timer fire, then reset.
  let release: () => void = () => {};
  client.statusGate = new Promise((resolve) => {
    release = resolve;
  });
  await waitFor(() => client.statusCalls.includes('task-1'));
  useScreenshotStore.getState().reset();
  release();
  await new Promise((resolve) => setTimeout(resolve, 60));

  assert.equal(useScreenshotStore.getState().status, null);
  assert.equal(useScreenshotStore.getState().origin, null);
});

test('a tool probe for another session never overwrites the running task', async () => {
  const client = new FakeClient();
  install(client, 't');
  await useScreenshotStore.getState().start();
  assert.equal(useScreenshotStore.getState().origin?.threadId, 't');

  // The reader switches to another session; the mount effect refreshes there.
  useConsoleStore.setState({ currentSession: { project_id: 'p', thread_id: 't2' } });
  await useScreenshotStore.getState().refreshTool();

  assert.equal(useScreenshotStore.getState().origin?.threadId, 't', 'the origin task is untouched');
  assert.notEqual(useScreenshotStore.getState().status?.state, 'idle', 'the running snapshot survives');
});

test('refresh recovers an already completed task for confirmation without recapturing', async () => {
  const client = new FakeClient();
  install(client);
  client.completed = true;
  client.frames = [frame('a'.repeat(32)), frame('b'.repeat(32)), frame('c'.repeat(32))];
  const read = client.getScreenshotStatus.bind(client);
  client.getScreenshotStatus = (session, taskId) => read(session, taskId || 'task-1');

  await useScreenshotStore.getState().refreshTool();
  assert.equal(useScreenshotStore.getState().pending?.attachments.length, 3);
  assert.equal(useConsoleStore.getState().attachments.length, 0, 'reload does not prove draft identity');
  useScreenshotStore.getState().restorePending();
  assert.equal(useConsoleStore.getState().attachments.length, 3);
  await useScreenshotStore.getState().refreshTool();
  assert.equal(useConsoleStore.getState().attachments.length, 3, 'refresh must not duplicate frames');
  assert.equal(useScreenshotStore.getState().pending, null);
  assert.equal(client.startCalls.length, 0, 'recovery is a read, never a new capture');
});

test('a reconnect refresh settles an owned task without losing its completed result', async () => {
  const client = new FakeClient();
  install(client);
  await useScreenshotStore.getState().start();
  client.completed = true;
  client.frames = [frame('a'.repeat(32))];
  const read = client.getScreenshotStatus.bind(client);
  client.getScreenshotStatus = (session, taskId) => read(session, taskId || 'task-1');

  await useScreenshotStore.getState().refreshTool();
  assert.equal(useConsoleStore.getState().attachments.length, 1);
  await useScreenshotStore.getState().refreshTool();
  assert.equal(useConsoleStore.getState().attachments.length, 1);
  assert.equal(useScreenshotStore.getState().pending, null);
});

test('recovered running tasks keep an unknown draft and require confirmation on completion', async () => {
  const client = new FakeClient();
  install(client);
  const read = client.getScreenshotStatus.bind(client);
  client.getScreenshotStatus = (session, taskId) => read(session, taskId || 'task-1');
  await useScreenshotStore.getState().refreshTool();
  client.completed = true;
  client.frames = [frame('a'.repeat(32))];
  await waitFor(() => useScreenshotStore.getState().pending !== null);
  assert.equal(useConsoleStore.getState().attachments.length, 0);
  assert.equal(client.startCalls.length, 0);
});

test('discarded recovered results do not reappear on refresh', async () => {
  const client = new FakeClient();
  install(client);
  client.completed = true;
  client.frames = [frame('a'.repeat(32))];
  const read = client.getScreenshotStatus.bind(client);
  client.getScreenshotStatus = (session, taskId) => read(session, taskId || 'task-1');
  await useScreenshotStore.getState().refreshTool();
  useScreenshotStore.getState().discardPending();
  await useScreenshotStore.getState().refreshTool();
  assert.equal(useScreenshotStore.getState().pending, null);
  assert.equal(useConsoleStore.getState().attachments.length, 0);
});

test('a completed result without images gives a visible error instead of silently stopping', async () => {
  SCREENSHOT_IMPORT_WAIT.deadlineMs = 400;
  SCREENSHOT_IMPORT_WAIT.recheckMs = 40;
  const client = new FakeClient();
  install(client);
  client.completed = true;
  await useScreenshotStore.getState().start();
  // The console re-reads for a bounded window before it settles on an error.
  await waitFor(() => useScreenshotStore.getState().status?.state === 'completed');
  assert.equal(useScreenshotStore.getState().importing, true, 'the wait shows importing first');
  await waitFor(() => useScreenshotStore.getState().notice !== null, 4000);
  assert.match(useScreenshotStore.getState().notice ?? '', /附件|图片/);
});

test('refresh cannot resurrect a running snapshot once its result has settled', async () => {
  const client = new FakeClient();
  install(client);
  await useScreenshotStore.getState().start();
  client.completed = true;
  client.frames = [frame('a'.repeat(32))];
  await waitFor(() => useConsoleStore.getState().attachments.length === 1);
  client.completed = false;
  const read = client.getScreenshotStatus.bind(client);
  client.getScreenshotStatus = (session, taskId) => read(session, taskId || 'task-1');
  await useScreenshotStore.getState().refreshTool();
  assert.equal(useScreenshotStore.getState().status?.state, 'completed');
  assert.equal(useConsoleStore.getState().attachments.length, 1);
});

// --- a daemon that reports `completed` before its frames are finalized ---------

test('an empty completed snapshot is re-read until its frames arrive, with no notice', async () => {
  const client = new FakeClient();
  install(client);
  client.completed = true;
  client.frames = [frame('a'.repeat(32)), frame('b'.repeat(32)), frame('c'.repeat(32))];
  // An older daemon flips `completed` while its attachments are still empty.
  client.scripted = [{ state: 'completed', frames: [], requested: 3 }];

  await useScreenshotStore.getState().start();
  await waitFor(() => useConsoleStore.getState().attachments.length === 3);

  assert.equal(
    useScreenshotStore.getState().notice,
    null,
    'a recoverable empty snapshot must not paint an error',
  );
  assert.equal(useScreenshotStore.getState().importing, false);
  assert.equal(useScreenshotStore.getState().pending, null);
  // The daemon was read again after the premature completion (the auto re-read).
  assert.ok(
    client.statusCalls.filter((id) => id === 'task-1').length >= 2,
    'the premature completion must be re-read',
  );
});

test('a premature completion keeps importing feedback instead of stopping', async () => {
  const client = new FakeClient();
  install(client);
  client.completed = true;
  client.frames = [frame('a'.repeat(32))];
  client.scripted = [
    { state: 'completed', frames: [], requested: 3 },
    { state: 'completed', frames: [], requested: 3 },
  ];
  await useScreenshotStore.getState().start();
  // While the frames are still missing the banner shows importing, not an error.
  await waitFor(() => useScreenshotStore.getState().importing === true);
  assert.equal(useScreenshotStore.getState().notice, null);
  assert.equal(useScreenshotStore.getState().status?.state, 'completed');
  // The frames land later without any manual refresh.
  await waitFor(() => useConsoleStore.getState().attachments.length === 1);
  assert.equal(useScreenshotStore.getState().importing, false);
});

test('a partial premature completion keeps waiting for the rest of the frames', async () => {
  const client = new FakeClient();
  install(client);
  client.completed = true;
  client.frames = [frame('a'.repeat(32)), frame('b'.repeat(32)), frame('c'.repeat(32))];
  client.scripted = [
    { state: 'completed', frames: [frame('a'.repeat(32))], requested: 3 },
  ];
  await useScreenshotStore.getState().start();
  await waitFor(() => useConsoleStore.getState().attachments.length === 3);
  assert.equal(useScreenshotStore.getState().notice, null);
});

test('an empty completion that never fills times out with a manual-refresh hint', async () => {
  SCREENSHOT_IMPORT_WAIT.deadlineMs = 500;
  SCREENSHOT_IMPORT_WAIT.recheckMs = 40;
  const client = new FakeClient();
  install(client);
  client.completed = true;
  client.frames = [];
  await useScreenshotStore.getState().start();
  await waitFor(() => useScreenshotStore.getState().notice !== null, 4000);
  assert.equal(useScreenshotStore.getState().notice, SCREENSHOT_IMPORT_TIMEOUT_MESSAGE);
  assert.match(useScreenshotStore.getState().notice ?? '', /刷新/);
  assert.equal(useScreenshotStore.getState().importing, false);
});

test('refresh clears a stale empty-completion notice and never duplicates frames', async () => {
  SCREENSHOT_IMPORT_WAIT.deadlineMs = 200;
  SCREENSHOT_IMPORT_WAIT.recheckMs = 40;
  const client = new FakeClient();
  install(client);
  client.completed = true;
  // The daemon is empty long enough to time out ...
  client.frames = [];
  await useScreenshotStore.getState().start();
  await waitFor(() => useScreenshotStore.getState().notice !== null, 4000);
  assert.equal(useConsoleStore.getState().attachments.length, 0);

  // ... then a manual refresh recovers the frames and drops the old error.
  client.frames = [frame('a'.repeat(32)), frame('b'.repeat(32)), frame('c'.repeat(32))];
  await useScreenshotStore.getState().refreshTool();
  assert.equal(useConsoleStore.getState().attachments.length, 3);
  assert.equal(useScreenshotStore.getState().notice, null);

  // A second refresh must neither duplicate the frames nor bring the notice back.
  await useScreenshotStore.getState().refreshTool();
  assert.equal(useConsoleStore.getState().attachments.length, 3);
  assert.equal(useScreenshotStore.getState().notice, null);
});

test('a premature completion that lands after a session switch is kept pending', async () => {
  const client = new FakeClient();
  install(client, 't');
  client.completed = true;
  client.frames = [frame('a'.repeat(32))];
  client.scripted = [{ state: 'completed', frames: [], requested: 3 }];
  await useScreenshotStore.getState().start();
  // The reader moves on while the frames are still missing.
  useConsoleStore.setState({ currentSession: { project_id: 'p', thread_id: 't2' } });
  await waitFor(() => useScreenshotStore.getState().pending !== null);
  assert.equal(useConsoleStore.getState().attachments.length, 0, 'no cross-draft fill');
});
