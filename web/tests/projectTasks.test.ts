/**
 * Offline tests for the pending project-task backlog (`runtime.project.task.*`).
 *
 * The real store runs against a stub runtime client with deferred promises: no
 * WebSocket, no daemon and no real network (the logout test uses a fake
 * `fetch`).  The properties pinned here are the ones the board depends on:
 *
 * - a list/create/update/delete/arrange call captures the client, the pairing,
 *   the active project and the project scope it was issued under; a response
 *   that lands after any of those moved on is dropped instead of overwriting the
 *   new project's array / loading / error (including an A -> B -> A round trip,
 *   and a client replaced for the same project);
 * - two refreshes of the same project are ordered by their own generation, so
 *   the newest load always wins;
 * - activating another project and logging out clear the backlog (logout before
 *   the request is issued);
 * - arranging never writes into a project the console has since left, and never
 *   falls back to the last session's project when no project is active.
 */
import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';

import { useConsoleStore } from '../src/stores/useConsoleStore.ts';
import type { ProjectTaskView } from '../src/stores/taskView.ts';
import type {
  CreateSessionResult,
  DeleteProjectTaskResult,
  ProjectTaskListResult,
  ProjectTaskResult,
  SessionListResult,
} from '../src/runtime-client/types.ts';

const realFetchSessions = useConsoleStore.getState().fetchSessions;
const PROJECT_A = 'proj-a';
const PROJECT_B = 'proj-b';

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** A task view carrying only the fields the store reads. */
function task(id: string, overrides: Partial<ProjectTaskView> = {}): ProjectTaskView {
  return {
    id,
    title: id,
    description: '',
    status: 'todo',
    priority: 'medium',
    labels: [],
    assigned_thread_id: null,
    created_at_ms: 0,
    updated_at_ms: 0,
    completed_at_ms: null,
    ...overrides,
  };
}

function listPage(projectId: string, tasks: ProjectTaskView[]): ProjectTaskListResult {
  return { project_id: projectId, tasks, total_count: tasks.length };
}

function taskResult(projectId: string, value: ProjectTaskView | null): ProjectTaskResult {
  return { project_id: projectId, task: value, status: 'ok' };
}

function deletedResult(projectId: string, taskId: string): DeleteProjectTaskResult {
  return { project_id: projectId, task_id: taskId, deleted: true };
}

interface Call {
  method: string;
  params: any;
}

const calls: Call[] = [];

// Per-method response plans: each call shifts the next entry so a test can hand
// back a deferred promise and settle it explicitly.  An empty plan falls back to
// a benign empty response.
let listPlan: Array<() => Promise<ProjectTaskListResult>> = [];
let createPlan: Array<() => Promise<ProjectTaskResult>> = [];
let updatePlan: Array<() => Promise<ProjectTaskResult>> = [];
let deletePlan: Array<() => Promise<DeleteProjectTaskResult>> = [];
let createSessionPlan: Array<() => Promise<CreateSessionResult>> = [];

/** Minimal stand-in for SynapseRuntimeClient (only what the store calls). */
const stubClient = {
  getState: () => 'connected',
  connect: () => Promise.resolve(undefined),
  disconnect: () => {},
  listProjectTasks: (params: any) => {
    calls.push({ method: 'runtime.project.task.list', params });
    const next = listPlan.shift();
    return next ? next() : Promise.resolve(listPage(params.project_id, []));
  },
  createProjectTask: (params: any) => {
    calls.push({ method: 'runtime.project.task.create', params });
    const next = createPlan.shift();
    return next ? next() : Promise.resolve(taskResult(params.project_id, null));
  },
  updateProjectTask: (params: any) => {
    calls.push({ method: 'runtime.project.task.update', params });
    const next = updatePlan.shift();
    return next ? next() : Promise.resolve(taskResult(params.project_id, null));
  },
  deleteProjectTask: (params: any) => {
    calls.push({ method: 'runtime.project.task.delete', params });
    const next = deletePlan.shift();
    return next ? next() : Promise.resolve(deletedResult(params.project_id, params.task_id));
  },
  createSession: (params: any) => {
    calls.push({ method: 'runtime.session.create', params });
    const next = createSessionPlan.shift();
    return next
      ? next()
      : Promise.resolve({
          command_id: 'cmd',
          session: { project_id: params.project_id, thread_id: 'allocated' },
          created: true,
          title: 'session',
        });
  },
};

function methodCalls(method: string): Call[] {
  return calls.filter((call) => call.method === method);
}

// The lifecycle actions the project switch reaches into are stubbed so a switch
// stays in-process (no socket, no session RPCs); the switch itself is the real
// `switchProject` -> `activateProject` path.
let fetchSessionsCalls = 0;
const fetchSessionsStub = async () => {
  fetchSessionsCalls += 1;
};
const createNewSessionStub = async () => {};

function resetStore(): void {
  calls.length = 0;
  listPlan = [];
  createPlan = [];
  updatePlan = [];
  deletePlan = [];
  createSessionPlan = [];
  fetchSessionsCalls = 0;
  useConsoleStore.setState({
    client: stubClient as never,
    pairingState: 'paired',
    pairingError: null,
    rpcBlockedReason: null,
    connectionState: 'connected',
    activeProjectId: PROJECT_A,
    projects: [
      { project_id: PROJECT_A, workspace_path: '/a', workspace_name: null, git_branch: 'main' },
      { project_id: PROJECT_B, workspace_path: '/b', workspace_name: null, git_branch: 'main' },
    ],
    currentSession: { project_id: PROJECT_A, thread_id: '' },
    sessions: [],
    sessionsTotal: 0,
    projectSessions: {},
    activeSubscriptionId: null,
    expandedProjectIds: [PROJECT_A],
    projectTasks: [],
    projectTasksLoading: false,
    projectTasksError: null,
    fetchSessions: fetchSessionsStub,
    createNewSession: createNewSessionStub,
  });
}

/** The store logs refusals/failures; the tests assert on state instead. */
async function muted<T>(fn: () => Promise<T>): Promise<T> {
  const warn = console.warn;
  const error = console.error;
  console.warn = () => {};
  console.error = () => {};
  try {
    return await fn();
  } finally {
    console.warn = warn;
    console.error = error;
  }
}

beforeEach(() => {
  resetStore();
});

test('loadProjectTasks publishes the active project backlog and clears loading', async () => {
  const pending = deferred<ProjectTaskListResult>();
  listPlan = [() => pending.promise];

  const load = useConsoleStore.getState().loadProjectTasks();
  assert.equal(useConsoleStore.getState().projectTasksLoading, true);

  pending.resolve(listPage(PROJECT_A, [task('a1'), task('a2')]));
  await load;

  const state = useConsoleStore.getState();
  assert.deepEqual(state.projectTasks.map((t) => t.id), ['a1', 'a2']);
  assert.equal(state.projectTasksLoading, false);
  assert.equal(state.projectTasksError, null);
  assert.deepEqual(calls[0].params, {
    project_id: PROJECT_A,
    status: undefined,
    label: undefined,
  });
});

test('createProjectTask prepends the returned row and sends the caller fields', async () => {
  useConsoleStore.setState({ projectTasks: [task('existing')] });
  createPlan = [() => Promise.resolve(taskResult(PROJECT_A, task('fresh')))];

  const ok = await useConsoleStore.getState().createProjectTask('fresh', '', 'high', ['x']);

  assert.equal(ok, true);
  assert.deepEqual(
    useConsoleStore.getState().projectTasks.map((t) => t.id),
    ['fresh', 'existing'],
  );
  assert.deepEqual(calls[0].params, {
    project_id: PROJECT_A,
    title: 'fresh',
    description: '',
    priority: 'high',
    labels: ['x'],
  });
});

test('a create failure is visible on the current project', async () => {
  createPlan = [() => Promise.reject(new Error('boom'))];

  const ok = await muted(() => useConsoleStore.getState().createProjectTask('x'));

  assert.equal(ok, false);
  assert.equal(useConsoleStore.getState().projectTasksError, 'boom');
  assert.deepEqual(useConsoleStore.getState().projectTasks, []);
});

test('updateProjectTask replaces the row and deleteProjectTask removes it', async () => {
  useConsoleStore.setState({ projectTasks: [task('t1'), task('t2')] });

  updatePlan = [() => Promise.resolve(taskResult(PROJECT_A, task('t1', { status: 'in_progress' })))];
  const updated = await useConsoleStore.getState().updateProjectTask('t1', { status: 'in_progress' });
  assert.equal(updated, true);
  assert.deepEqual(
    useConsoleStore.getState().projectTasks.map((t) => [t.id, t.status]),
    [['t1', 'in_progress'], ['t2', 'todo']],
  );

  deletePlan = [() => Promise.resolve(deletedResult(PROJECT_A, 't1'))];
  const removed = await useConsoleStore.getState().deleteProjectTask('t1');
  assert.equal(removed, true);
  assert.deepEqual(useConsoleStore.getState().projectTasks.map((t) => t.id), ['t2']);
});

test('a late list response for the previous project never overwrites the new project', async () => {
  const forA = deferred<ProjectTaskListResult>();
  const forB = deferred<ProjectTaskListResult>();
  listPlan = [() => forA.promise, () => forB.promise];

  const loadA = useConsoleStore.getState().loadProjectTasks();
  // Switch to B while A's list is still in flight.
  await muted(() => useConsoleStore.getState().switchProject(PROJECT_B));
  const loadB = useConsoleStore.getState().loadProjectTasks();

  forB.resolve(listPage(PROJECT_B, [task('b1')]));
  await loadB;
  // A's response lands *after* the switch and B's load: it must be dropped.
  forA.resolve(listPage(PROJECT_A, [task('a1')]));
  await loadA;

  const state = useConsoleStore.getState();
  assert.equal(state.activeProjectId, PROJECT_B);
  assert.deepEqual(state.projectTasks.map((t) => t.id), ['b1']);
  assert.equal(state.projectTasksLoading, false);
  assert.equal(state.projectTasksError, null);
});

test('the newest load wins when two refreshes of the same project overlap', async () => {
  const first = deferred<ProjectTaskListResult>();
  const second = deferred<ProjectTaskListResult>();
  listPlan = [() => first.promise, () => second.promise];

  const load1 = useConsoleStore.getState().loadProjectTasks();
  const load2 = useConsoleStore.getState().loadProjectTasks();

  // The newer refresh resolves first; the older one lands afterwards.
  second.resolve(listPage(PROJECT_A, [task('new')]));
  await load2;
  first.resolve(listPage(PROJECT_A, [task('old')]));
  await load1;

  assert.deepEqual(useConsoleStore.getState().projectTasks.map((t) => t.id), ['new']);
  assert.equal(useConsoleStore.getState().projectTasksLoading, false);
});

test('a stale list rejection leaves the new project loading and error untouched', async () => {
  const forA = deferred<ProjectTaskListResult>();
  listPlan = [() => forA.promise];
  const loadA = useConsoleStore.getState().loadProjectTasks();

  await muted(() => useConsoleStore.getState().switchProject(PROJECT_B));

  // B's own load is still in flight.
  const forB = deferred<ProjectTaskListResult>();
  listPlan = [() => forB.promise];
  const loadB = useConsoleStore.getState().loadProjectTasks();
  assert.equal(useConsoleStore.getState().projectTasksLoading, true);

  forA.reject(new Error('stale boom'));
  await muted(() => loadA);

  const mid = useConsoleStore.getState();
  assert.equal(mid.projectTasksLoading, true, "B's loading must survive the stale rejection");
  assert.equal(mid.projectTasksError, null, 'the stale error must not leak into B');

  forB.resolve(listPage(PROJECT_B, [task('b1')]));
  await loadB;
  assert.deepEqual(useConsoleStore.getState().projectTasks.map((t) => t.id), ['b1']);
});

test('A -> B -> A: the first visit\'s late list response is fenced out', async () => {
  const firstA = deferred<ProjectTaskListResult>();
  listPlan = [() => firstA.promise];
  const loadFirstA = useConsoleStore.getState().loadProjectTasks();

  await muted(() => useConsoleStore.getState().switchProject(PROJECT_B));
  await muted(() => useConsoleStore.getState().switchProject(PROJECT_A));

  const secondA = deferred<ProjectTaskListResult>();
  listPlan = [() => secondA.promise];
  const loadSecondA = useConsoleStore.getState().loadProjectTasks();

  secondA.resolve(listPage(PROJECT_A, [task('fresh')]));
  await loadSecondA;
  // The response from the *first* visit to A lands after the round trip: the
  // project id matches, but the scope does not, so it must be dropped.
  firstA.resolve(listPage(PROJECT_A, [task('stale')]));
  await loadFirstA;

  assert.deepEqual(useConsoleStore.getState().projectTasks.map((t) => t.id), ['fresh']);
});

test('create / update / delete responses that land after a switch never touch the new project', async () => {
  useConsoleStore.setState({ projectTasks: [task('a1')] });
  const createP = deferred<ProjectTaskResult>();
  const updateP = deferred<ProjectTaskResult>();
  const deleteP = deferred<DeleteProjectTaskResult>();
  createPlan = [() => createP.promise];
  updatePlan = [() => updateP.promise];
  deletePlan = [() => deleteP.promise];

  const createPromise = useConsoleStore.getState().createProjectTask('late');
  const updatePromise = useConsoleStore.getState().updateProjectTask('a1', { status: 'done' });
  const deletePromise = useConsoleStore.getState().deleteProjectTask('a1');

  await muted(() => useConsoleStore.getState().switchProject(PROJECT_B));
  assert.deepEqual(useConsoleStore.getState().projectTasks, [], 'B starts from its own empty backlog');

  createP.resolve(taskResult(PROJECT_A, task('late')));
  updateP.resolve(taskResult(PROJECT_A, task('a1', { status: 'done' })));
  deleteP.resolve(deletedResult(PROJECT_A, 'a1'));

  assert.equal(await createPromise, false);
  assert.equal(await updatePromise, false);
  assert.equal(await deletePromise, false);

  const state = useConsoleStore.getState();
  assert.deepEqual(state.projectTasks, [], 'no stale mutation may repopulate the new project');
  assert.equal(state.projectTasksError, null);
  assert.equal(state.projectTasksLoading, false);
});

test('logout clears the backlog before the request and fences a late response', async () => {
  const requests: string[] = [];
  const originalFetch = globalThis.fetch;
  (globalThis as any).fetch = (url: unknown) => {
    requests.push(String(url));
    return Promise.resolve(new Response(null, { status: 204 }));
  };
  try {
    useConsoleStore.setState({ projectTasks: [task('a1')] });
    const pending = deferred<ProjectTaskListResult>();
    listPlan = [() => pending.promise];
    const load = useConsoleStore.getState().loadProjectTasks();
    assert.equal(useConsoleStore.getState().projectTasksLoading, true);

    const logout = useConsoleStore.getState().logoutConsole();
    // The backlog is isolated *before* the logout round-trip is awaited.
    assert.deepEqual(useConsoleStore.getState().projectTasks, []);
    assert.equal(useConsoleStore.getState().projectTasksLoading, false);

    await muted(() => logout);
    assert.ok(requests.includes('/api/logout'));

    pending.resolve(listPage(PROJECT_A, [task('a1')]));
    await load;

    const state = useConsoleStore.getState();
    assert.deepEqual(state.projectTasks, [], 'a late list must not repopulate after logout');
    assert.equal(state.projectTasksLoading, false);
    assert.equal(state.projectTasksError, null);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('a late response from a replaced client is dropped even on the same project', async () => {
  const pending = deferred<ProjectTaskListResult>();
  listPlan = [() => pending.promise];
  const load = useConsoleStore.getState().loadProjectTasks();
  assert.equal(useConsoleStore.getState().projectTasksLoading, true);

  // A fresh pairing installs a new client and wipes the backlog for the same
  // project id; the old client's in-flight response must not come back.
  const replacement = { getState: () => 'connected' };
  useConsoleStore.setState({ client: replacement as never, projectTasks: [], projectTasksLoading: false });

  pending.resolve(listPage(PROJECT_A, [task('a1')]));
  await load;

  const state = useConsoleStore.getState();
  assert.deepEqual(state.projectTasks, []);
  assert.equal(state.projectTasksLoading, false);
});

test('arrangeTasks never falls back to the last session project when no project is active', async () => {
  useConsoleStore.setState({
    activeProjectId: '',
    currentSession: { project_id: PROJECT_A, thread_id: 'thr' },
  });

  const result = await useConsoleStore.getState().arrangeTasks([task('a1')], 'parallel');

  assert.deepEqual(result, { bound: 0, sessions: [] });
  assert.equal(calls.length, 0, 'no RPC may be issued without an active project');
});

test('arrangeTasks stops creating sessions when the project switches mid-run', async () => {
  const created = deferred<CreateSessionResult>();
  createSessionPlan = [() => created.promise];

  const arrange = useConsoleStore.getState().arrangeTasks([task('a1')], 'parallel');
  assert.equal(methodCalls('runtime.session.create').length, 1);

  await muted(() => useConsoleStore.getState().switchProject(PROJECT_B));
  const afterSwitch = fetchSessionsCalls;

  created.resolve({
    command_id: 'cmd',
    session: { project_id: PROJECT_A, thread_id: 'new-thr' },
    created: true,
    title: 'session',
  });
  const result = await arrange;

  assert.deepEqual(result, { bound: 0, sessions: [] });
  assert.equal(
    methodCalls('runtime.project.task.update').length,
    0,
    'no task may be sent to the project the console left',
  );
  assert.equal(fetchSessionsCalls, afterSwitch, "arrange must not refresh the new project's sidebar");
});

test('arrangeTasks stops updating tasks when the project switches mid-update', async () => {
  const first = deferred<ProjectTaskResult>();
  updatePlan = [() => first.promise];
  const tasks = [
    task('a1', { assigned_thread_id: 't1' }),
    task('a2', { assigned_thread_id: 't2' }),
  ];

  const arrange = useConsoleStore.getState().arrangeTasks(tasks, 'parallel');
  assert.equal(methodCalls('runtime.project.task.update').length, 1);

  await muted(() => useConsoleStore.getState().switchProject(PROJECT_B));
  first.resolve(taskResult(PROJECT_A, task('a1', { status: 'in_progress', assigned_thread_id: 't1' })));
  const result = await arrange;

  assert.deepEqual(result, { bound: 0, sessions: [] });
  assert.equal(
    methodCalls('runtime.project.task.update').length,
    1,
    'the second task must not be sent to the project the console left',
  );
  assert.deepEqual(useConsoleStore.getState().projectTasks, []);
});

test('late mutation errors cannot replace the next project error', async () => {
  const createP = deferred<ProjectTaskResult>();
  const updateP = deferred<ProjectTaskResult>();
  const deleteP = deferred<DeleteProjectTaskResult>();
  createPlan = [() => createP.promise];
  updatePlan = [() => updateP.promise];
  deletePlan = [() => deleteP.promise];
  const pending = [
    useConsoleStore.getState().createProjectTask('a1'),
    useConsoleStore.getState().updateProjectTask('a1', { status: 'done' }),
    useConsoleStore.getState().deleteProjectTask('a1'),
  ];

  await useConsoleStore.getState().switchProject(PROJECT_B);
  useConsoleStore.setState({ projectTasksError: 'B error', projectTasksLoading: true });
  createP.reject(new Error('A create error'));
  updateP.reject(new Error('A update error'));
  deleteP.reject(new Error('A delete error'));

  assert.deepEqual(await Promise.all(pending), [false, false, false]);
  assert.equal(useConsoleStore.getState().projectTasksError, 'B error');
  assert.equal(useConsoleStore.getState().projectTasksLoading, true);
});

test('an arrange sidebar refresh cannot overwrite the next project or report stale success', async () => {
  const sidebar = deferred<SessionListResult>();
  const readingSidebar = deferred<void>();
  const client = {
    ...stubClient,
    listSessions: (params: { project_id: string }) => {
      if (params.project_id === PROJECT_A) {
        readingSidebar.resolve();
        return sidebar.promise;
      }
      return Promise.resolve({ items: [], total: 0, next_offset: null });
    },
  };
  useConsoleStore.setState({ client: client as never, fetchSessions: realFetchSessions });
  updatePlan = [() => Promise.resolve(taskResult(PROJECT_A, task('a1')))];

  const arrange = useConsoleStore.getState().arrangeTasks([task('a1')], 'parallel');
  await readingSidebar.promise;
  await useConsoleStore.getState().switchProject(PROJECT_B);
  sidebar.resolve({
    items: [{
      thread_id: 'a-session', title: 'A session', model: null, active_model: null,
      summary: null, created_at: '2026-07-24T00:00:00Z', updated_at: '2026-07-24T00:00:00Z',
    }],
    total: 1,
    next_offset: null,
  });

  assert.deepEqual(await arrange, { bound: 0, sessions: [] });
  assert.equal(useConsoleStore.getState().activeProjectId, PROJECT_B);
  assert.deepEqual(useConsoleStore.getState().sessions, []);
  assert.equal(useConsoleStore.getState().sessionsTotal, 0);
});

test('arrange stops after A -> B -> A even if the task ids still match', async () => {
  const first = deferred<ProjectTaskResult>();
  updatePlan = [() => first.promise];
  const selected = [task('a1', { assigned_thread_id: 't' }), task('a2', { assigned_thread_id: 't' })];
  const arrange = useConsoleStore.getState().arrangeTasks(selected, 'shared');

  await useConsoleStore.getState().switchProject(PROJECT_B);
  await useConsoleStore.getState().switchProject(PROJECT_A);
  useConsoleStore.setState({ projectTasks: [task('a1', { title: 'fresh A' })] });
  first.resolve(taskResult(PROJECT_A, task('a1', { status: 'in_progress' })));

  assert.deepEqual(await arrange, { bound: 0, sessions: [] });
  assert.equal(methodCalls('runtime.project.task.update').length, 1);
  assert.equal(useConsoleStore.getState().projectTasks[0].title, 'fresh A');
});

test('losing pairing rejects a pending task response even before the client is replaced', async () => {
  const response = deferred<ProjectTaskResult>();
  createPlan = [() => response.promise];
  const pending = useConsoleStore.getState().createProjectTask('old');

  useConsoleStore.setState({ pairingState: 'unpaired' });
  response.resolve(taskResult(PROJECT_A, task('old')));

  assert.equal(await pending, false);
  assert.deepEqual(useConsoleStore.getState().projectTasks, []);
});

test('a disconnected sidebar refresh does not send the old project read after switching', async () => {
  const connected = deferred<void>();
  let isConnected = false;
  const projectReads: string[] = [];
  const client = {
    ...stubClient,
    getState: () => isConnected ? 'connected' : 'disconnected',
    connect: () => connected.promise,
    listSessions: (params: { project_id: string }) => {
      projectReads.push(params.project_id);
      return Promise.resolve({ items: [], total: 0, next_offset: null });
    },
  };
  useConsoleStore.setState({ client: client as never, fetchSessions: realFetchSessions });
  const oldRefresh = useConsoleStore.getState().fetchSessions();
  isConnected = true;
  await useConsoleStore.getState().switchProject(PROJECT_B);
  connected.resolve();
  await oldRefresh;

  assert.deepEqual(projectReads, [PROJECT_B]);
  assert.equal(useConsoleStore.getState().activeProjectId, PROJECT_B);
});

test('editing sends cleared fields and keeps the server status, binding and completion time', async () => {
  const original = task('a1', {
    title: 'Original', description: 'Original description', priority: 'low', labels: ['old'],
    status: 'done', assigned_thread_id: 'session-1', completed_at_ms: 1234,
  });
  const saved = { ...original, title: 'Edited', description: '', priority: 'critical', labels: [] };
  useConsoleStore.setState({ projectTasks: [original], projectTasksError: 'old failure' });
  updatePlan = [() => Promise.resolve(taskResult(PROJECT_A, saved))];

  assert.equal(await useConsoleStore.getState().updateProjectTask('a1', {
    title: 'Edited', description: '', priority: 'critical', labels: [],
  }), true);
  const params = methodCalls('runtime.project.task.update')[0].params;
  assert.equal(params.description, '');
  assert.deepEqual(params.labels, []);
  assert.equal(params.status, undefined);
  assert.equal(params.assigned_thread_id, undefined);
  assert.equal(params.clear_assigned_thread, undefined);
  assert.deepEqual(useConsoleStore.getState().projectTasks, [saved]);
  assert.equal(useConsoleStore.getState().projectTasksError, null);
});

test('not_found and thrown updates preserve the row and expose an error, retry clears it', async () => {
  const original = task('a1');
  useConsoleStore.setState({ projectTasks: [original] });
  updatePlan = [() => Promise.resolve({ project_id: PROJECT_A, task: null, status: 'not_found' })];

  assert.equal(await useConsoleStore.getState().updateProjectTask('a1', { title: 'Edited' }), false);
  assert.match(useConsoleStore.getState().projectTasksError ?? '', /不存在/);
  assert.deepEqual(useConsoleStore.getState().projectTasks, [original]);

  updatePlan = [() => Promise.reject(new Error('Update failed'))];
  assert.equal(await muted(() => useConsoleStore.getState().updateProjectTask('a1', { status: 'done' })), false);
  assert.equal(useConsoleStore.getState().projectTasksError, 'Update failed');
  assert.equal(useConsoleStore.getState().projectTasks[0].status, 'todo');

  const response = deferred<ProjectTaskResult>();
  updatePlan = [() => response.promise];
  const retry = useConsoleStore.getState().updateProjectTask('a1', { status: 'blocked' });
  assert.equal(useConsoleStore.getState().projectTasksError, null, 'retry clears the old error');
  response.resolve(taskResult(PROJECT_A, task('a1', { status: 'blocked' })));
  assert.equal(await retry, true);
  assert.equal(useConsoleStore.getState().projectTasksError, null);
  assert.equal(useConsoleStore.getState().projectTasks[0].status, 'blocked');
});

test('four-state updates always use the returned entity, including reopened completion time', async () => {
  useConsoleStore.setState({ projectTasks: [task('a1')] });
  for (const status of ['in_progress', 'blocked', 'done', 'todo', 'done', 'blocked']) {
    const saved = task('a1', {
      status, assigned_thread_id: 'session-1', completed_at_ms: status === 'done' ? 2000 : null,
    });
    updatePlan = [() => Promise.resolve(taskResult(PROJECT_A, saved))];
    assert.equal(await useConsoleStore.getState().updateProjectTask('a1', { status }), true);
    assert.deepEqual(useConsoleStore.getState().projectTasks, [saved]);
    assert.equal(methodCalls('runtime.project.task.update').at(-1)?.params.status, status);
    assert.equal(useConsoleStore.getState().projectTasksError, null);
  }
});
